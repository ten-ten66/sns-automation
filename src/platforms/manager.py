"""Platform manager for coordinating multi-platform posting."""

import asyncio
import logging

from config.settings import settings
from src.platforms.base import BasePlatform, PostResult
from src.platforms.x_platform import XPlatform
from src.platforms.youtube_platform import YouTubePlatform

# Instagram + TikTok are disabled in production (IG banned 2026-04-08,
# TikTok has no Basic publish path). Their platform classes pull in heavy
# transitive deps (instagrapi → Pillow / moviepy / etc.), so we lazy-import
# them inside register_all() only when credentials are actually present.

logger = logging.getLogger(__name__)

# Platforms that require media (cannot accept text-only posts)
MEDIA_REQUIRED_PLATFORMS = {"instagram", "tiktok", "youtube"}

# Platforms that support text-only posts
TEXT_SUPPORTED_PLATFORMS = {"x"}


class PlatformManager:
    """Manage and coordinate posting across multiple platforms."""

    def __init__(self):
        self._platforms: dict[str, BasePlatform] = {}

    def register(self, platform: BasePlatform):
        self._platforms[platform.platform_name] = platform

    def register_all(self):
        """Register available platforms (only if credentials are configured)."""
        if settings.x_api_key:
            self.register(XPlatform())
        if settings.instagram_username:
            from src.platforms.instagram_platform import InstagramPlatform
            self.register(InstagramPlatform())
        if settings.tiktok_access_token:
            from src.platforms.tiktok_platform import TikTokPlatform
            self.register(TikTokPlatform())
        if settings.youtube_api_key:
            self.register(YouTubePlatform())
        logger.info(f"Registered platforms: {list(self._platforms.keys())}")

    @staticmethod
    def _normalize_platform_name(name: str) -> str:
        """Normalize platform name to internal key.

        Claude may return varied casing/labels like 'Instagram',
        'Twitter/X', 'X(Twitter)', 'YouTube Shorts'.  Map them all
        to the canonical lowercase keys used as dict keys.
        """
        lower = name.strip().lower()
        # Direct match
        if lower in ("x", "instagram", "tiktok", "youtube"):
            return lower
        # Common aliases
        aliases = {
            "twitter": "x",
            "twitter/x": "x",
            "x(twitter)": "x",
            "x (twitter)": "x",
            "x（twitter）": "x",  # full-width parentheses
            "ig": "instagram",
            "insta": "instagram",
            "instagram reels": "instagram",
            "youtube shorts": "youtube",
            "yt": "youtube",
            "pinterest": "",  # not supported, return empty
        }
        return aliases.get(lower, lower)

    def get(self, name: str) -> BasePlatform | None:
        normalized = self._normalize_platform_name(name)
        return self._platforms.get(normalized) if normalized else None

    async def post_to_all(
        self,
        text: str,
        platforms: list[str] | None = None,
        image_path: str | None = None,
        video_path: str | None = None,
        **kwargs,
    ) -> dict[str, PostResult]:
        """Post content to multiple platforms in parallel."""
        target_platforms = platforms or list(self._platforms.keys())
        results = {}
        tasks = {}

        has_media = bool(image_path or video_path)

        for name in target_platforms:
            platform = self._platforms.get(name)
            if not platform:
                results[name] = PostResult(success=False, platform=name, error="Platform not registered")
                continue

            # Smart routing: skip media-required platforms when no media is provided
            if not has_media and name in MEDIA_REQUIRED_PLATFORMS:
                results[name] = PostResult(
                    success=False,
                    platform=name,
                    error=f"{name} requires media (image or video). Text-only posts are not supported. "
                          f"Generate an image first using /api/content/generate and the suggested_media_prompt.",
                )
                logger.warning(f"Skipping {name}: text-only post not supported. Provide image_path or video_path.")
                continue

            if video_path:
                tasks[name] = platform.post_video(text, video_path, **kwargs)
            elif image_path:
                tasks[name] = platform.post_image(text, image_path, **kwargs)
            else:
                tasks[name] = platform.post_text(text, **kwargs)

        if tasks:
            gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for name, result in zip(tasks.keys(), gathered):
                if isinstance(result, Exception):
                    results[name] = PostResult(success=False, platform=name, error=str(result))
                    logger.error(f"Post to {name} failed: {result}")
                else:
                    results[name] = result

        return results

    async def get_all_follower_counts(self) -> dict[str, int]:
        """Get follower counts across all platforms in parallel."""
        if not self._platforms:
            return {}
        names = list(self._platforms.keys())
        tasks = [p.get_follower_count() for p in self._platforms.values()]
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        counts = {}
        for name, result in zip(names, gathered):
            counts[name] = result if isinstance(result, int) else 0
        return counts

    async def get_all_metrics(self, post_ids: dict[str, str]) -> dict[str, dict]:
        """Get metrics for posts across platforms in parallel."""
        tasks = {}
        for platform_name, post_id in post_ids.items():
            platform = self._platforms.get(platform_name)
            if platform:
                tasks[platform_name] = platform.get_metrics(post_id)

        if not tasks:
            return {}

        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        metrics = {}
        for name, result in zip(tasks.keys(), gathered):
            metrics[name] = result if isinstance(result, dict) else {}
        return metrics
