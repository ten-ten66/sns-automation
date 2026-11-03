"""YouTube platform integration for Shorts and regular videos."""

import asyncio
import logging
import os
import re

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from config.settings import settings
from src.platforms.base import BasePlatform, PostResult, RateLimiter

logger = logging.getLogger(__name__)

# Per-chunk HTTP timeout for resumable uploads. next_chunk() otherwise hangs
# indefinitely on stalled network, which burned 20+ posts on 2026-04-20.
_CHUNK_TIMEOUT_SEC = 60
# Max resumable-chunk retries before giving up on a single upload.
_MAX_CHUNK_RETRIES = 3


class YouTubePlatform(BasePlatform):
    """YouTube posting and analytics."""

    platform_name = "youtube"

    def __init__(self):
        self.youtube = None
        self._init_client()
        self.rate_limiter = RateLimiter(max_requests=50, window_seconds=86400)

    def _init_client(self):
        """Initialize YouTube API client."""
        if settings.youtube_api_key:
            self.youtube = build("youtube", "v3", developerKey=settings.youtube_api_key)

    def _get_authenticated_service(self):
        """Get authenticated YouTube service for uploads (requires OAuth)."""
        from google.oauth2.credentials import Credentials

        creds_path = settings.youtube_credentials_path
        if creds_path and os.path.exists(creds_path):
            creds = Credentials.from_authorized_user_file(creds_path)
            http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_CHUNK_TIMEOUT_SEC))
            return build("youtube", "v3", http=http)
        raise RuntimeError("YouTube OAuth credentials not configured")

    async def post_text(self, text: str, **kwargs) -> PostResult:
        await self.rate_limiter.acquire()
        return PostResult(
            success=False,
            platform="youtube",
            error="YouTube requires video content.",
        )

    async def post_image(self, text: str, image_path: str, **kwargs) -> PostResult:
        await self.rate_limiter.acquire()
        return PostResult(
            success=False,
            platform="youtube",
            error="YouTube requires video content. Consider creating a slideshow video.",
        )

    async def post_video(self, text: str, video_path: str, **kwargs) -> PostResult:
        """Upload a video to YouTube."""
        await self.rate_limiter.acquire()
        try:
            result = await asyncio.to_thread(
                self._sync_upload_video, text, video_path, **kwargs
            )
            return result
        except Exception as e:
            return PostResult(success=False, platform="youtube", error=str(e))

    def _sync_upload_video(self, text: str, video_path: str, **kwargs) -> PostResult:
        """Synchronous video upload (runs in thread)."""
        service = self._get_authenticated_service()

        # Build SEO-optimized title: extract first sentence (up to ~60 chars)
        # for the title, use the full caption for description. Previously we
        # truncated to 100 chars mid-word which produced ugly titles like
        # "Okay so I've been OBSESSED with this lately and I genuinely cann".
        title = kwargs.get("title") or self._derive_title(text)
        description = kwargs.get("description") or self._build_description(text, kwargs.get("hashtags", []))
        tags = kwargs.get("tags", [])
        category_id = kwargs.get("category_id", "22")  # 22 = People & Blogs
        is_short = kwargs.get("is_short", False)

        if is_short and "#Shorts" not in title:
            # Append #Shorts only if title budget allows (YouTube title max 100)
            if len(title) + len(" #Shorts") <= 100:
                title = f"{title} #Shorts"

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": tags,
                "categoryId": category_id,
                "defaultLanguage": settings.content_language,
            },
            "status": {
                "privacyStatus": kwargs.get("privacy", "public"),
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            video_path,
            mimetype="video/mp4",
            resumable=True,
        )

        request = service.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        response = None
        retries = 0
        while response is None:
            try:
                _, response = request.next_chunk()
                # Counter tracks CONSECUTIVE failures, not lifetime failures.
                # Without this reset, a long video with one transient error per
                # chunk accumulates past _MAX_CHUNK_RETRIES even though each
                # chunk only failed once.
                retries = 0
            except (httplib2.HttpLib2Error, TimeoutError, OSError) as e:
                retries += 1
                if retries > _MAX_CHUNK_RETRIES:
                    raise RuntimeError(
                        f"YouTube upload stalled after {retries} retries: {e}"
                    ) from e
                logger.warning(
                    "YouTube chunk upload retry %d/%d: %s",
                    retries, _MAX_CHUNK_RETRIES, e,
                )
            except HttpError as e:
                # 5xx errors are retryable per Google docs; 4xx is fatal.
                if 500 <= e.resp.status < 600 and retries < _MAX_CHUNK_RETRIES:
                    retries += 1
                    logger.warning(
                        "YouTube chunk HTTP %d retry %d/%d",
                        e.resp.status, retries, _MAX_CHUNK_RETRIES,
                    )
                else:
                    raise

        video_id = response["id"]
        return PostResult(
            success=True,
            platform="youtube",
            post_id=video_id,
            url=f"https://www.youtube.com/watch?v={video_id}",
        )

    @staticmethod
    def _derive_title(text: str, max_len: int = 85) -> str:
        """Pick a clean, SEO-friendly title from the caption. Prefers the
        first sentence, falls back to a word-boundary truncation. Leaves
        ~15 chars of room for a `#Shorts` suffix if needed.
        """
        if not text:
            return "Daily beauty moment"
        # Remove leading filler phrases that tank CTR on YouTube
        stripped = text.lstrip()
        for filler in ("Okay so ", "okay so ", "So I've ", "so i've ",
                       "Okay but ", "okay but ", "POV: ", "pov: "):
            if stripped.startswith(filler):
                stripped = stripped[len(filler):].lstrip()
                break
        # First sentence ends at . ! ? — or at a newline
        m = re.search(r"[.!?—\n]", stripped)
        if m and m.start() <= max_len:
            return stripped[: m.start()].strip().capitalize()
        # Word-boundary truncation
        if len(stripped) <= max_len:
            return stripped.strip().capitalize()
        cut = stripped[:max_len].rsplit(" ", 1)[0]
        return (cut.strip() + "…").capitalize()

    @staticmethod
    def _build_description(text: str, hashtags: list[str] | None = None) -> str:
        """Description: caption + CTA + hashtags as a block at the bottom.
        YouTube indexes the first 150 chars heavily, so the caption stays
        at top; hashtags only appear at the end (max 15 per YouTube policy).
        """
        parts = [text.strip()]
        cta = "\n\n✨ Follow for more daily beauty drops ✨"
        parts.append(cta)
        if hashtags:
            tag_line = " ".join(
                f"#{t.lstrip('#')}" for t in hashtags[:15]
            )
            parts.append("\n" + tag_line)
        return "\n".join(parts)

    async def post_short(self, text: str, video_path: str, **kwargs) -> PostResult:
        """Upload a YouTube Short."""
        return await self.post_video(text, video_path, is_short=True, **kwargs)

    async def get_comments(self, post_id: str, max_results: int = 20) -> list[dict]:
        """Fetch comments on a YouTube video."""
        await self.rate_limiter.acquire()
        if not self.youtube:
            return []
        try:
            response = await asyncio.to_thread(
                lambda: self.youtube.commentThreads().list(
                    part="snippet",
                    videoId=post_id,
                    maxResults=max_results,
                    order="time",
                ).execute()
            )
            comments = []
            for item in response.get("items", []):
                snippet = item["snippet"]["topLevelComment"]["snippet"]
                comments.append({
                    "id": item["snippet"]["topLevelComment"]["id"],
                    "text": snippet.get("textDisplay", ""),
                    "username": snippet.get("authorDisplayName", "unknown"),
                    "created_at": snippet.get("publishedAt", ""),
                })
            return comments
        except Exception as e:
            logger.warning("Failed to fetch YouTube comments for %s: %s", post_id, e)
            return []

    async def post_reply(self, post_id: str, comment_id: str, text: str, **kwargs) -> PostResult:
        """Reply to a YouTube comment (requires OAuth, not just API key)."""
        await self.rate_limiter.acquire()
        try:
            def _do_reply():
                service = self._get_authenticated_service()
                body = {
                    "snippet": {
                        "parentId": comment_id,
                        "textOriginal": text,
                    }
                }
                return service.comments().insert(part="snippet", body=body).execute()

            response = await asyncio.to_thread(_do_reply)
            reply_id = response.get("id", "")
            return PostResult(success=True, platform="youtube", post_id=reply_id)
        except RuntimeError as e:
            # OAuth credentials not configured
            logger.warning("YouTube post_reply requires OAuth: %s", e)
            return PostResult(success=False, platform="youtube", error=str(e))
        except Exception as e:
            return PostResult(success=False, platform="youtube", error=str(e))

    async def get_metrics(self, post_id: str) -> dict:
        """Get video analytics."""
        try:
            if not self.youtube:
                return {}
            service = await asyncio.to_thread(self._get_authenticated_service)
            response = await asyncio.to_thread(
                lambda: service.videos().list(
                    part="statistics", id=post_id
                ).execute()
            )

            items = response.get("items", [])
            if not items:
                return {}

            stats = items[0].get("statistics", {})
            return {
                "impressions": int(stats.get("viewCount", 0)),  # normalize
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
                "shares": 0,
                "saves": int(stats.get("favoriteCount", 0)),
                "clicks": 0,
                "views": int(stats.get("viewCount", 0)),
            }
        except Exception:
            return {}

    async def get_follower_count(self) -> int:
        """Get channel subscriber count."""
        try:
            if not self.youtube:
                return 0
            service = await asyncio.to_thread(self._get_authenticated_service)
            response = await asyncio.to_thread(
                lambda: service.channels().list(
                    part="statistics", mine=True
                ).execute()
            )
            items = response.get("items", [])
            if not items:
                return 0
            return int(items[0].get("statistics", {}).get("subscriberCount", 0))
        except Exception:
            return 0
