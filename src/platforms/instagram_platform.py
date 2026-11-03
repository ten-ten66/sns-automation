"""Instagram platform integration using instagrapi."""

import asyncio
import logging
import os
from pathlib import Path

import random

from instagrapi import Client as InstaClient

from config.settings import settings
from src.platforms.base import BasePlatform, PostResult, RateLimiter
from src.core.utils import lognormal_delay as _lognormal_delay

logger = logging.getLogger(__name__)

SESSION_PATH = "data/instagram_session.json"

# Realistic device fingerprint to reduce detection risk
DEVICE_SETTINGS = {
    "app_version": "269.0.0.18.75",
    "android_version": 31,
    "android_release": "12",
    "dpi": "480dpi",
    "resolution": "1080x2400",
    "manufacturer": "Samsung",
    "device": "SM-G991B",
    "model": "samsung",
    "cpu": "exynos2100",
}


class InstagramPlatform(BasePlatform):
    """Instagram posting and analytics."""

    platform_name = "instagram"

    def __init__(self):
        self.client = InstaClient()
        self._logged_in = False
        self._login_lock = asyncio.Lock()
        self.rate_limiter = RateLimiter(max_requests=30, window_seconds=3600)

        # Set realistic device fingerprint before anything else
        self.client.set_device(DEVICE_SETTINGS)

        # Configure proxy if set
        if getattr(settings, "instagram_proxy", None):
            self.client.set_proxy(settings.instagram_proxy)

        # Try to restore a previous session to avoid unnecessary logins
        if os.path.exists(SESSION_PATH):
            try:
                self.client.load_settings(SESSION_PATH)
                logger.info("Loaded Instagram session from %s", SESSION_PATH)
            except Exception as exc:
                logger.warning("Failed to load Instagram session file: %s", exc)

    async def _ensure_login(self):
        async with self._login_lock:
            if not self._logged_in and settings.instagram_username:
                try:
                    await asyncio.to_thread(
                        self.client.login, settings.instagram_username, settings.instagram_password
                    )
                except Exception as exc:
                    logger.error("Instagram login failed: %s: %s", type(exc).__name__, exc)
                    raise
                self._logged_in = True
                # Persist session so next startup can skip login
                try:
                    await asyncio.to_thread(self.client.dump_settings, SESSION_PATH)
                    logger.info("Saved Instagram session to %s", SESSION_PATH)
                except Exception as exc:
                    logger.warning("Failed to save Instagram session: %s", exc)

    async def _force_relogin(self):
        """Force re-login after session expiry."""
        async with self._login_lock:
            self._logged_in = False
            # Delete stale session file to prevent login loops
            import os
            try:
                if os.path.exists(SESSION_PATH):
                    os.remove(SESSION_PATH)
                    logger.info("Removed stale session file: %s", SESSION_PATH)
            except OSError as e:
                logger.warning("Failed to remove stale session: %s", e)
            self.client = InstaClient()  # Fresh client
            self.client.set_device(DEVICE_SETTINGS)
            if getattr(settings, "instagram_proxy", None):
                self.client.set_proxy(settings.instagram_proxy)
        await self._ensure_login()

    async def _with_relogin(self, action_callable, action_name: str = "action"):
        """Execute *action_callable* with automatic recovery from session/rate errors.

        Handles login expiry, challenge/2FA blocks, and rate limiting with
        appropriate backoff strategies.
        """
        for attempt in range(2):
            try:
                await self._ensure_login()
                return await action_callable()
            except Exception as e:
                error_str = str(e).lower()
                error_type = type(e).__name__.lower()

                is_login_error = (
                    "login_required" in error_str
                    or "loginrequired" in error_type
                    or "clientloginrequired" in error_type
                )
                is_challenge_error = (
                    "challenge" in error_type
                    or "twofactor" in error_type
                    or "captcha" in error_type
                )
                is_rate_limit = (
                    "ratelimit" in error_type
                    or "pleasewait" in error_type
                    or "throttl" in error_str
                    or "please wait" in error_str
                )

                if attempt == 0 and is_login_error:
                    logger.warning("Instagram session expired during %s, attempting re-login...", action_name)
                    try:
                        await self._force_relogin()
                        await asyncio.sleep(random.uniform(5, 15))
                        continue
                    except Exception as relogin_err:
                        logger.error("Instagram re-login failed: %s", relogin_err)

                if attempt == 0 and is_rate_limit:
                    wait = random.uniform(120, 300)
                    logger.warning("Instagram rate limited during %s, waiting %.0fs...", action_name, wait)
                    await asyncio.sleep(wait)
                    continue

                if is_challenge_error:
                    logger.error(
                        "Instagram %s blocked by challenge (%s). Manual intervention may be required.",
                        action_name, type(e).__name__,
                    )

                logger.error("Instagram %s failed: %s: %s", action_name, type(e).__name__, e)
                return PostResult(success=False, platform="instagram", error=f"{type(e).__name__}: {e}")
        return PostResult(success=False, platform="instagram", error="Max retries exceeded")

    async def post_text(self, text: str, **kwargs) -> PostResult:
        """Instagram doesn't support text-only posts; post as story text."""
        await self.rate_limiter.acquire()
        await asyncio.sleep(_lognormal_delay(10, 45))
        return PostResult(
            success=False,
            platform="instagram",
            error="Instagram requires media. Use post_image or post_video.",
        )

    async def post_image(self, text: str, image_path: str, **kwargs) -> PostResult:
        """Post an image to Instagram feed."""
        await self.rate_limiter.acquire()
        await asyncio.sleep(_lognormal_delay(10, 45))

        hashtags = kwargs.get("hashtags", [])
        caption = text
        if hashtags:
            caption += "\n\n" + " ".join(f"#{tag}" for tag in hashtags)

        async def _do_post():
            media = await asyncio.to_thread(
                self.client.photo_upload, Path(image_path), caption=caption,
            )
            return PostResult(
                success=True, platform="instagram",
                post_id=str(media.pk),
                url=f"https://www.instagram.com/p/{media.code}/",
            )

        return await self._with_relogin(_do_post, "post_image")

    async def post_video(self, text: str, video_path: str, **kwargs) -> PostResult:
        """Post a video (Reel) to Instagram."""
        await self.rate_limiter.acquire()
        await asyncio.sleep(_lognormal_delay(10, 45))

        hashtags = kwargs.get("hashtags", [])
        caption = text
        if hashtags:
            caption += "\n\n" + " ".join(f"#{tag}" for tag in hashtags)
        is_reel = kwargs.get("as_reel", True)

        async def _do_post():
            if is_reel:
                media = await asyncio.to_thread(
                    self.client.clip_upload, Path(video_path), caption,
                )
            else:
                media = await asyncio.to_thread(
                    self.client.video_upload, Path(video_path), caption,
                )
            return PostResult(
                success=True, platform="instagram",
                post_id=str(media.pk),
                url=f"https://www.instagram.com/p/{media.code}/",
            )

        return await self._with_relogin(_do_post, "post_video")

    async def post_carousel(self, text: str, image_paths: list[str], **kwargs) -> PostResult:
        """Post a carousel (multiple images) to Instagram."""
        await self.rate_limiter.acquire()
        await asyncio.sleep(_lognormal_delay(15, 60))

        hashtags = kwargs.get("hashtags", [])
        caption = text
        if hashtags:
            caption += "\n\n" + " ".join(f"#{tag}" for tag in hashtags)

        async def _do_post():
            media = await asyncio.to_thread(
                self.client.album_upload,
                [Path(p) for p in image_paths],
                caption,
            )
            return PostResult(
                success=True, platform="instagram",
                post_id=str(media.pk),
                url=f"https://www.instagram.com/p/{media.code}/",
            )

        return await self._with_relogin(_do_post, "post_carousel")

    async def post_story(self, media_path: str, **kwargs) -> PostResult:
        """Post a story."""
        await self.rate_limiter.acquire()
        await asyncio.sleep(_lognormal_delay(3, 15))

        async def _do_post():
            path = Path(media_path)
            if path.suffix.lower() in (".mp4", ".mov"):
                media = await asyncio.to_thread(self.client.video_upload_to_story, path)
            else:
                media = await asyncio.to_thread(self.client.photo_upload_to_story, path)
            return PostResult(
                success=True, platform="instagram",
                post_id=str(media.pk),
            )

        return await self._with_relogin(_do_post, "post_story")

    async def get_comments(self, post_id: str, max_results: int = 20) -> list[dict]:
        """Fetch comments on an Instagram post."""
        await self.rate_limiter.acquire()
        try:
            await self._ensure_login()
            media_id = await asyncio.to_thread(
                lambda: self.client.media_pk_from_code(post_id) if not post_id.isdigit() else int(post_id)
            )
            raw_comments = await asyncio.to_thread(
                self.client.media_comments, media_id, amount=max_results
            )
            return [
                {
                    "id": str(c.pk),
                    "text": c.text,
                    "username": c.user.username if c.user else "unknown",
                    "created_at": str(c.created_at_utc) if c.created_at_utc else "",
                }
                for c in raw_comments
            ]
        except Exception as e:
            logger.warning("Failed to fetch Instagram comments for %s: %s", post_id, e)
            return []

    async def post_reply(self, post_id: str, comment_id: str, text: str, **kwargs) -> PostResult:
        """Reply to an Instagram comment."""
        await self.rate_limiter.acquire()

        async def _do_reply():
            media_id = await asyncio.to_thread(
                lambda: self.client.media_pk_from_code(post_id) if not post_id.isdigit() else int(post_id)
            )
            result = await asyncio.to_thread(
                self.client.media_comment, media_id, text, replied_to_comment_pk=int(comment_id)
            )
            return PostResult(success=True, platform="instagram", post_id=str(result.pk) if result else "")

        return await self._with_relogin(_do_reply, "post_reply")

    async def get_metrics(self, post_id: str) -> dict:
        """Get post insights."""
        try:
            await self._ensure_login()
            insights = await asyncio.to_thread(self.client.insights_media, int(post_id))
            return {
                "impressions": insights.get("impressions", 0),
                "reach": insights.get("reach", 0),
                "likes": insights.get("likes", 0),
                "comments": insights.get("comments", 0),
                "saves": insights.get("saved", 0),
                "shares": insights.get("shares", 0),
            }
        except Exception:
            return {}

    async def get_follower_count(self) -> int:
        """Get current follower count."""
        try:
            await self._ensure_login()
            info = await asyncio.to_thread(self.client.account_info)
            return info.follower_count
        except Exception:
            return 0
