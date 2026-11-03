"""X (Twitter) platform integration."""

import asyncio
import logging

import requests
from requests_oauthlib import OAuth1
import tweepy

from config.settings import settings
from src.platforms.base import BasePlatform, PostResult, RateLimiter
from src.core.utils import async_retry

logger = logging.getLogger(__name__)

# Safety latch for search_recent_tweets 401 errors.
#
# Root cause identified 2026-04-12: tweepy defaults to Bearer (app-only)
# auth for GET endpoints, which returns 401 on the Basic plan. The fix is
# `user_auth=True` on every search_recent_tweets call — that switches to
# OAuth 1.0a, which WORKS on Basic. All call sites now have it.
#
# This latch is kept as a safety net: if a 401 somehow still happens (e.g.
# token revoked, plan downgraded), it latches off search for the rest of
# the process lifetime to prevent log spam. Clears at restart.
_X_SEARCH_DISABLED: bool = False


def _disable_x_search(reason: str) -> None:
    """Latch the search-disabled flag and log the truth exactly once."""
    global _X_SEARCH_DISABLED
    if not _X_SEARCH_DISABLED:
        _X_SEARCH_DISABLED = True
        logger.warning(
            "X recent-search endpoint disabled for the rest of this process: %s. "
            "This is the documented Basic-plan limitation — get_comments / "
            "search_viral / run_like_cycle will return empty until restart.",
            reason,
        )


def is_x_search_disabled() -> bool:
    return _X_SEARCH_DISABLED


def reset_x_search_disabled() -> None:
    """Test-only helper to clear the latch between test cases."""
    global _X_SEARCH_DISABLED
    _X_SEARCH_DISABLED = False


def _is_search_unauthorized(exc: BaseException) -> bool:
    """True iff *exc* is the tweepy 401 we get from Basic-plan search calls."""
    if isinstance(exc, tweepy.Unauthorized):
        return True
    # Some tweepy versions wrap into a generic TweepyException
    msg = str(exc)
    return "401" in msg and "Unauthorized" in msg


class XPlatform(BasePlatform):
    """X (Twitter) posting and analytics."""

    platform_name = "x"

    def __init__(self):
        self._oauth = OAuth1(
            settings.x_api_key,
            settings.x_api_secret,
            settings.x_access_token,
            settings.x_access_token_secret,
        )
        # v1.1 API for media upload
        self.auth = tweepy.OAuth1UserHandler(
            settings.x_api_key,
            settings.x_api_secret,
            settings.x_access_token,
            settings.x_access_token_secret,
        )
        self.api = tweepy.API(self.auth)
        # v2 client for read operations (get_me, search, metrics)
        self.client = tweepy.Client(
            bearer_token=settings.x_bearer_token,
            consumer_key=settings.x_api_key,
            consumer_secret=settings.x_api_secret,
            access_token=settings.x_access_token,
            access_token_secret=settings.x_access_token_secret,
        )
        self.rate_limiter = RateLimiter(max_requests=95, window_seconds=86400)
        self._my_user_id: int | None = None  # Lazily fetched via _get_my_id()

    async def _get_my_id(self) -> int | None:
        """Return the authenticated user's numeric ID (cached)."""
        if self._my_user_id is not None:
            return self._my_user_id
        try:
            me = await asyncio.to_thread(self.client.get_me, user_auth=True)
            if me and me.data:
                self._my_user_id = me.data.id
                return self._my_user_id
        except Exception as e:
            logger.warning("get_me() failed: %s", e)
        return None

    def _post_tweet(self, text: str, media_ids: list[str] | None = None,
                    reply_to: str | None = None, quote_tweet_id: str | None = None) -> dict:
        """Post a tweet via v2 API using requests_oauthlib (sync)."""
        payload: dict = {"text": text}
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        if reply_to:
            payload["reply"] = {"in_reply_to_tweet_id": reply_to}
        if quote_tweet_id:
            payload["quote_tweet_id"] = quote_tweet_id

        resp = requests.post(
            "https://api.x.com/2/tweets",
            json=payload,
            auth=self._oauth,
        )
        resp.raise_for_status()
        return resp.json()

    # X image upload limit: 5 MB for PNG/JPEG/WEBP.
    # AI-generated images routinely land at 6-8 MB (PNG, high-res).
    _MAX_IMAGE_BYTES = 4_800_000  # 4.8 MB, with headroom

    @staticmethod
    def _compress_image(file_path: str, max_bytes: int = _MAX_IMAGE_BYTES) -> str:
        """Return *file_path* unchanged if it's small enough, else create a
        compressed JPEG copy and return that path instead.

        Strategy:
          1. Skip if already under limit.
          2. Convert to JPEG at decreasing quality (95 → 85 → 75 → 60).
          3. If still over, scale down by 25% steps and re-compress at q75.
        """
        import os as _os
        if _os.path.getsize(file_path) <= max_bytes:
            return file_path

        from PIL import Image
        import io

        img = Image.open(file_path)

        # JPEG doesn't support alpha — flatten onto white
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        compressed_path = file_path.rsplit(".", 1)[0] + "_compressed.jpg"

        for quality in (95, 85, 75, 60):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= max_bytes:
                with open(compressed_path, "wb") as f:
                    f.write(buf.getvalue())
                logger.info(
                    "Compressed image %s → %s (q%d, %.1f MB → %.1f MB)",
                    file_path, compressed_path, quality,
                    _os.path.getsize(file_path) / 1e6, buf.tell() / 1e6,
                )
                return compressed_path

        # Still too large — progressively shrink
        for _ in range(10):
            w, h = img.size
            img = img.resize((w * 3 // 4, h * 3 // 4), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75, optimize=True)
            if buf.tell() <= max_bytes:
                with open(compressed_path, "wb") as f:
                    f.write(buf.getvalue())
                logger.info(
                    "Compressed+resized image → %s (%dx%d, %.1f MB)",
                    compressed_path, img.size[0], img.size[1], buf.tell() / 1e6,
                )
                return compressed_path

        logger.warning("Could not compress %s below %.1f MB", file_path, max_bytes / 1e6)
        return file_path  # best-effort: try uploading the original

    def _upload_media(self, file_path: str) -> str:
        """Upload media via v1.1 API and return media_id_string.

        Automatically compresses oversized images before upload.
        Deletes the compressed temp file after upload (success or failure).
        """
        import os as _os
        upload_path = self._compress_image(file_path)
        compressed = upload_path != file_path
        try:
            with open(upload_path, "rb") as f:
                resp = requests.post(
                    "https://upload.twitter.com/1.1/media/upload.json",
                    auth=self._oauth,
                    files={"media": f},
                )
            resp.raise_for_status()
            return resp.json()["media_id_string"]
        finally:
            if compressed:
                try:
                    _os.unlink(upload_path)
                except OSError:
                    pass

    @async_retry(max_retries=2, base_delay=3.0, max_delay=30.0)
    async def post_text(self, text: str, **kwargs) -> PostResult:
        """Post a text tweet."""
        await self.rate_limiter.acquire()
        reply_to = kwargs.get("reply_to")
        quote_tweet_id = kwargs.get("quote_tweet_id")

        try:
            data = await asyncio.to_thread(
                self._post_tweet, text,
                reply_to=reply_to, quote_tweet_id=quote_tweet_id,
            )
            tweet_id = data["data"]["id"]
            return PostResult(
                success=True, platform="x", post_id=tweet_id,
                url=f"https://x.com/i/status/{tweet_id}",
            )
        except Exception as e:
            logger.error(f"X post_text failed: {type(e).__name__}: {e}")
            return PostResult(success=False, platform="x", error=str(e))

    async def post_image(self, text: str, image_path: str, **kwargs) -> PostResult:
        """Post a tweet with an image."""
        await self.rate_limiter.acquire()
        try:
            media_id = await asyncio.to_thread(self._upload_media, image_path)
            data = await asyncio.to_thread(self._post_tweet, text, media_ids=[media_id])
            tweet_id = data["data"]["id"]
            return PostResult(
                success=True, platform="x", post_id=tweet_id,
                url=f"https://x.com/i/status/{tweet_id}",
            )
        except Exception as e:
            logger.error(f"X post_image failed: {type(e).__name__}: {e}")
            return PostResult(success=False, platform="x", error=str(e))

    async def post_video(self, text: str, video_path: str, **kwargs) -> PostResult:
        """Post a tweet with a video."""
        await self.rate_limiter.acquire()
        try:
            # Video requires chunked upload via tweepy v1.1
            media = await asyncio.to_thread(
                self.api.media_upload,
                video_path,
                media_category="tweet_video",
                chunked=True,
            )
            data = await asyncio.to_thread(
                self._post_tweet, text, media_ids=[media.media_id_string],
            )
            tweet_id = data["data"]["id"]
            return PostResult(
                success=True, platform="x", post_id=tweet_id,
                url=f"https://x.com/i/status/{tweet_id}",
            )
        except Exception as e:
            logger.error(f"X post_video failed: {type(e).__name__}: {e}")
            return PostResult(success=False, platform="x", error=str(e))

    async def post_thread(self, texts: list[str]) -> list[PostResult]:
        """Post a thread (multiple connected tweets)."""
        results = []
        previous_id = None
        for text in texts:
            result = await self.post_text(text, reply_to=previous_id)
            results.append(result)
            if result.success:
                previous_id = result.post_id
            else:
                break
        return results

    async def get_metrics(self, post_id: str) -> dict:
        """Get tweet metrics."""
        try:
            tweet = await asyncio.to_thread(
                self.client.get_tweet,
                post_id,
                tweet_fields=["public_metrics", "organic_metrics"],
            )
            if not tweet.data:
                return {}
            metrics = getattr(tweet.data, 'public_metrics', None) or {}
            if isinstance(tweet.data, dict):
                metrics = tweet.data.get("public_metrics", {})
            return {
                "impressions": metrics.get("impression_count", 0),
                "likes": metrics.get("like_count", 0),
                "shares": metrics.get("retweet_count", 0),
                "comments": metrics.get("reply_count", 0),
                "clicks": 0,
                "quotes": metrics.get("quote_count", 0),
                "bookmarks": metrics.get("bookmark_count", 0),
            }
        except Exception:
            return {}

    async def get_follower_count(self) -> int:
        """Get current follower count."""
        try:
            me = await asyncio.to_thread(
                self.client.get_me, user_fields=["public_metrics"],
                user_auth=True,  # Basic plan requires OAuth1, not Bearer
            )
            metrics = getattr(me.data, 'public_metrics', {}) or {}
            return metrics.get("followers_count", 0)
        except Exception:
            return 0

    async def get_comments(self, post_id: str, max_results: int = 20) -> list[dict]:
        """Fetch replies to a tweet.

        Strategy:
          1. Try search_recent_tweets (conversation_id) — best, but 401 on Basic.
          2. Fallback: get_users_mentions — works on Basic plan, then filter
             by referenced_tweets to find replies to ``post_id``.
        """
        # --- Strategy 1: search (Pro+ plans) ---
        if not _X_SEARCH_DISABLED:
            await self.rate_limiter.acquire()
            try:
                my_id = await self._get_my_id()
                response = await asyncio.to_thread(
                    self.client.search_recent_tweets,
                    query=f"conversation_id:{post_id}",
                    max_results=min(max_results, 100),
                    tweet_fields=["author_id", "created_at", "text"],
                    user_fields=["username"],
                    expansions=["author_id"],
                    user_auth=True,
                )
                comments = []
                users = {u.id: u.username for u in (response.includes.get("users", []) if response.includes else [])}
                for tweet in (response.data or []):
                    # Skip our own tweets — never reply to ourselves
                    if my_id and tweet.author_id == my_id:
                        continue
                    comments.append({
                        "id": str(tweet.id),
                        "text": tweet.text,
                        "username": users.get(tweet.author_id, "unknown"),
                        "created_at": str(tweet.created_at) if tweet.created_at else "",
                    })
                return comments
            except Exception as e:
                if _is_search_unauthorized(e):
                    _disable_x_search(f"get_comments({post_id}) → 401")
                else:
                    logger.warning("Failed to fetch comments for tweet %s: %s", post_id, e)
                    return []

        # --- Strategy 2: mentions fallback (Basic plan) ---
        return await self._get_comments_via_mentions(post_id, max_results)

    async def _get_comments_via_mentions(self, post_id: str, max_results: int = 20) -> list[dict]:
        """Fetch replies to a specific tweet via the mentions timeline.

        ``get_users_mentions`` is available on the Basic plan (unlike search).
        We fetch recent mentions and filter to those that are direct replies
        to ``post_id`` using the ``referenced_tweets`` expansion.
        """
        await self.rate_limiter.acquire()
        try:
            my_id = await self._get_my_id()
            if not my_id:
                logger.warning("get_me() returned no data — cannot fetch mentions")
                return []

            response = await asyncio.to_thread(
                self.client.get_users_mentions,
                id=my_id,
                max_results=min(max_results, 100),
                tweet_fields=["author_id", "created_at", "text",
                              "referenced_tweets", "in_reply_to_user_id"],
                user_fields=["username"],
                expansions=["author_id", "referenced_tweets.id"],
                user_auth=True,
            )
            if not response or not response.data:
                return []

            users = {}
            if response.includes:
                users = {u.id: u.username for u in response.includes.get("users", [])}

            comments = []
            for tweet in response.data:
                # Only keep tweets that are direct replies to our target post
                refs = tweet.referenced_tweets or []
                is_reply_to_target = any(
                    ref.type == "replied_to" and str(ref.id) == str(post_id)
                    for ref in refs
                )
                if not is_reply_to_target:
                    continue
                # Skip our own replies (don't reply to ourselves)
                if tweet.author_id == my_id:
                    continue
                comments.append({
                    "id": str(tweet.id),
                    "text": tweet.text,
                    "username": users.get(tweet.author_id, "unknown"),
                    "created_at": str(tweet.created_at) if tweet.created_at else "",
                })
            logger.info(
                "Mentions fallback: found %d replies to tweet %s (from %d mentions)",
                len(comments), post_id, len(response.data),
            )
            return comments
        except Exception as e:
            logger.warning("Failed to fetch mentions for tweet %s: %s", post_id, e)
            return []

    async def get_recent_mentions(self, max_results: int = 20) -> list[dict]:
        """Fetch recent mentions of the authenticated user (Basic-plan compatible).

        Unlike ``_get_comments_via_mentions``, this returns ALL recent mentions
        (not filtered to a specific post_id) so the growth engine can engage
        with any follower who tagged the account.
        """
        await self.rate_limiter.acquire()
        try:
            my_id = await self._get_my_id()
            if not my_id:
                logger.warning("get_me() returned no data — cannot fetch mentions")
                return []

            response = await asyncio.to_thread(
                self.client.get_users_mentions,
                id=my_id,
                max_results=min(max_results, 100),
                tweet_fields=["author_id", "created_at", "text", "in_reply_to_user_id"],
                user_fields=["username"],
                expansions=["author_id"],
                user_auth=True,
            )
            if not response or not response.data:
                return []

            users = {}
            if response.includes:
                users = {u.id: u.username for u in response.includes.get("users", [])}

            mentions = []
            for tweet in response.data:
                # Skip our own tweets
                if tweet.author_id == my_id:
                    continue
                mentions.append({
                    "id": str(tweet.id),
                    "text": tweet.text,
                    "author_id": str(tweet.author_id),
                    "username": users.get(tweet.author_id, "unknown"),
                    "created_at": str(tweet.created_at) if tweet.created_at else "",
                })
            return mentions
        except Exception as e:
            logger.warning("get_recent_mentions failed: %s", e)
            return []

    async def update_profile(self, description: str | None = None, **kwargs) -> None:
        """Update the X profile bio."""
        await self.rate_limiter.acquire()
        await asyncio.to_thread(
            self.api.update_profile, description=description
        )

    async def post_reply(self, post_id: str, comment_id: str, text: str, **kwargs) -> PostResult:
        """Reply to a tweet."""
        await self.rate_limiter.acquire()
        try:
            data = await asyncio.to_thread(
                self._post_tweet, text, reply_to=comment_id,
            )
            tweet_id = data["data"]["id"]
            return PostResult(success=True, platform="x", post_id=tweet_id)
        except Exception as e:
            return PostResult(success=False, platform="x", error=str(e))

    async def search_viral(self, query: str, min_likes: int = 1000) -> list[dict]:
        """Search for viral tweets matching a query.

        Returns ``[]`` immediately when the recent-search endpoint has been
        latched off (Basic-plan 401). Note: min_faves: requires Pro plan
        even when search itself is allowed, so we filter client-side.
        """
        if _X_SEARCH_DISABLED:
            return []
        try:
            my_id = await self._get_my_id()
            tweets = await asyncio.to_thread(
                self.client.search_recent_tweets,
                query=f"{query} lang:en -is:retweet",
                max_results=50,
                tweet_fields=["author_id", "public_metrics", "created_at", "entities"],
                user_auth=True,
            )
            results = []
            for tweet in tweets.data or []:
                # Never reply to our own tweets
                if my_id and tweet.author_id == my_id:
                    continue
                m = getattr(tweet, 'public_metrics', {}) or {}
                like_count = m.get("like_count", 0)
                # Client-side filter for min_likes (Basic plan fallback)
                if like_count < min_likes:
                    continue
                results.append({
                    "id": tweet.id,
                    "text": tweet.text,
                    "likes": like_count,
                    "retweets": m.get("retweet_count", 0),
                    "replies": m.get("reply_count", 0),
                    "created_at": tweet.created_at,
                })
            # Sort by likes descending
            results.sort(key=lambda x: x["likes"], reverse=True)
            return results
        except Exception as e:
            if _is_search_unauthorized(e):
                _disable_x_search(f"search_viral('{query[:40]}') → 401")
            else:
                logger.warning("search_viral failed for '%s': %s", query, e)
            return []
