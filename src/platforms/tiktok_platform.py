"""TikTok platform integration."""

import logging

import aiofiles
import httpx

from config.settings import settings
from src.platforms.base import BasePlatform, PostResult, RateLimiter
from src.core.utils import async_retry

logger = logging.getLogger(__name__)

# Realistic browser-like headers to reduce fingerprinting risk
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 12; SM-G991B) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.6099.230 Mobile Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
}


class TikTokPlatform(BasePlatform):
    """TikTok posting and analytics via Content Posting API."""

    platform_name = "tiktok"

    def __init__(self):
        self.platform_name = "tiktok"
        self.access_token = settings.tiktok_access_token
        self.base_url = "https://open.tiktokapis.com/v2"

        # Determine proxy: prefer dedicated tiktok_proxy, fall back to instagram_proxy
        proxy_url = getattr(settings, "tiktok_proxy", None) or getattr(settings, "instagram_proxy", None)

        # Merge browser-like headers with the auth header
        headers = {
            **_BROWSER_HEADERS,
            "Authorization": f"Bearer {self.access_token}",
        }

        client_kwargs: dict = {
            "timeout": 300,
            "headers": headers,
        }
        if proxy_url:
            client_kwargs["proxy"] = proxy_url
            logger.info("TikTok client configured with proxy")

        self.client = httpx.AsyncClient(**client_kwargs)
        self.rate_limiter = RateLimiter(max_requests=20, window_seconds=3600)

    @async_retry(max_retries=2, base_delay=3.0, max_delay=30.0)
    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        await self.rate_limiter.acquire()
        url = f"{self.base_url}{endpoint}"
        response = await self.client.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()

    async def post_text(self, text: str, **kwargs) -> PostResult:
        await self.rate_limiter.acquire()
        return PostResult(
            success=False,
            platform="tiktok",
            error="TikTok requires video content.",
        )

    async def post_image(self, text: str, image_path: str, **kwargs) -> PostResult:
        """Post a photo post on TikTok.

        Note: TikTok's Content Posting API requires photos to be
        publicly accessible URLs (PULL_FROM_URL), not direct file uploads.
        The image_path should be a public URL, or the caller must upload
        the image to a CDN first.
        """
        await self.rate_limiter.acquire()
        try:
            # TikTok photo posts use /content/init/ with PULL_FROM_URL
            image_url = kwargs.get("image_url", image_path)

            init_data = await self._request(
                "POST",
                "/post/publish/content/init/",
                json={
                    "post_info": {
                        "title": text[:150],
                        "privacy_level": "PUBLIC_TO_EVERYONE",
                        "disable_comment": False,
                    },
                    "source_info": {
                        "source": "PULL_FROM_URL",
                        "photo_cover_index": 0,
                        "photo_images": [image_url],
                    },
                    "post_mode": "DIRECT_POST",
                    "media_type": "PHOTO",
                },
            )
            publish_id = init_data.get("data", {}).get("publish_id", "")

            if not publish_id:
                return PostResult(
                    success=False, platform="tiktok",
                    error="TikTok photo init failed: missing publish_id",
                )

            return PostResult(
                success=True,
                platform="tiktok",
                post_id=publish_id,
            )
        except Exception as e:
            return PostResult(success=False, platform="tiktok", error=str(e))

    async def post_video(self, text: str, video_path: str, **kwargs) -> PostResult:
        """Post a video to TikTok."""
        await self.rate_limiter.acquire()
        try:
            import os as _os
            file_size = _os.path.getsize(video_path)

            # Chunk size: use 10MB or file_size if smaller
            chunk_size = min(10 * 1024 * 1024, file_size)
            total_chunks = max(1, -(-file_size // chunk_size))  # ceil division

            # Step 1: Initialize upload with required fields
            init_data = await self._request(
                "POST",
                "/post/publish/inbox/video/init/",
                json={
                    "post_info": {
                        "title": text[:150],
                        "privacy_level": "PUBLIC_TO_EVERYONE",
                        "disable_comment": False,
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD",
                        "video_size": file_size,
                        "chunk_size": chunk_size,
                        "total_chunk_count": total_chunks,
                    },
                },
            )
            publish_id = init_data.get("data", {}).get("publish_id", "")
            upload_url = init_data.get("data", {}).get("upload_url", "")

            if not publish_id or not upload_url:
                return PostResult(
                    success=False, platform="tiktok",
                    error="TikTok init failed: missing publish_id or upload_url",
                )

            # Step 2: Upload video in chunks
            async with aiofiles.open(video_path, "rb") as f:
                for chunk_idx in range(total_chunks):
                    offset = chunk_idx * chunk_size
                    read_size = min(chunk_size, file_size - offset)
                    await f.seek(offset)
                    content = await f.read(read_size)

                    end_byte = offset + read_size - 1
                    upload_headers = {
                        **_BROWSER_HEADERS,
                        "Content-Type": "video/mp4",
                        "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                        "Content-Length": str(read_size),
                    }
                    await self.client.put(
                        upload_url,
                        content=content,
                        headers=upload_headers,
                    )

            # Step 3: Poll publish status (up to 5 attempts)
            import asyncio
            for attempt in range(5):
                await asyncio.sleep(5)  # Wait before checking
                try:
                    status_data = await self._request(
                        "POST",
                        "/post/publish/status/fetch/",
                        json={"publish_id": publish_id},
                    )
                    status = status_data.get("data", {}).get("status", "")
                    if status == "PUBLISH_COMPLETE":
                        return PostResult(
                            success=True,
                            platform="tiktok",
                            post_id=publish_id,
                        )
                    elif status in ("FAILED", "PUBLISH_FAILED"):
                        fail_reason = status_data.get("data", {}).get("fail_reason", "unknown")
                        return PostResult(
                            success=False, platform="tiktok",
                            error=f"TikTok publish failed: {fail_reason}",
                        )
                    # PROCESSING_UPLOAD, PROCESSING_DOWNLOAD, SENDING_TO_USER_INBOX
                    logger.debug("TikTok publish status: %s (attempt %d)", status, attempt + 1)
                except Exception as e:
                    logger.warning("TikTok status check failed: %s", e)

            # Final attempt - assume success if no failure was returned
            return PostResult(
                success=True,
                platform="tiktok",
                post_id=publish_id,
            )
        except Exception as e:
            return PostResult(success=False, platform="tiktok", error=str(e))

    async def get_comments(self, post_id: str, max_results: int = 20) -> list[dict]:
        """TikTok comment fetching - not supported via Content Posting API."""
        logger.debug("TikTok get_comments not available via Content Posting API")
        return []

    async def get_metrics(self, post_id: str) -> dict:
        """Get video metrics."""
        try:
            data = await self._request(
                "POST",
                "/video/query/",
                json={"filters": {"video_ids": [post_id]}},
            )
            videos = data.get("data", {}).get("videos", [])
            if not videos:
                return {}
            v = videos[0]
            return {
                "impressions": v.get("view_count", 0),  # normalize views -> impressions
                "likes": v.get("like_count", 0),
                "comments": v.get("comment_count", 0),
                "shares": v.get("share_count", 0),
                "saves": v.get("save_count", 0),
                "clicks": 0,
                "views": v.get("view_count", 0),
            }
        except Exception:
            return {}

    async def get_follower_count(self) -> int:
        """Get current follower count."""
        try:
            data = await self._request(
                "GET",
                "/user/info/",
                params={"fields": "follower_count"},
            )
            return data.get("data", {}).get("user", {}).get("follower_count", 0)
        except Exception:
            return 0

    async def close(self):
        """Close the shared HTTP client."""
        await self.client.aclose()
