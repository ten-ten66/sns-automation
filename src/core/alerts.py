"""Alert notification service for Slack/Discord webhooks."""

import logging
import time
from datetime import datetime
from pathlib import Path

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


class AlertService:
    """Send alerts to Slack/Discord webhooks."""

    def __init__(self):
        self.webhook_url = settings.alert_webhook_url
        self.enabled = settings.alert_enabled and bool(self.webhook_url)
        self._recent_alerts: list[tuple[str, float]] = []  # dedup within 5 min

    async def _send(self, message: str, level: str = "info"):
        """Send a webhook message."""
        if not self.enabled:
            return

        # Dedup: skip if same message sent in last 5 min
        key = f"{level}:{message}"
        now = time.monotonic()
        # Clean expired entries
        self._recent_alerts = [(k, t) for k, t in self._recent_alerts if now - t < 300]
        # Check for recent duplicate
        if any(k == key for k, _ in self._recent_alerts):
            return
        self._recent_alerts.append((key, now))

        # Detect webhook type by URL
        emoji = {"error": "\U0001f6a8", "warning": "\u26a0\ufe0f", "info": "\u2139\ufe0f", "success": "\u2705"}.get(level, "\U0001f4cc")
        from zoneinfo import ZoneInfo
        from config.settings import settings as _settings
        timestamp = datetime.now(ZoneInfo(_settings.default_timezone)).strftime("%Y-%m-%d %H:%M:%S")

        if "discord" in self.webhook_url:
            payload = {
                "content": f"{emoji} **[{level.upper()}]** {timestamp}\n{message}"
            }
        else:
            # Slack format
            payload = {
                "text": f"{emoji} *[{level.upper()}]* {timestamp}\n{message}"
            }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.webhook_url, json=payload)
                resp.raise_for_status()
        except Exception as e:
            logger.warning(f"Failed to send alert: {e}")

    async def notify_error(self, error_message: str):
        """Send error alert."""
        if settings.alert_on_error:
            await self._send(error_message, "error")

    async def notify_budget_exceeded(self, daily_cost: float, limit: float):
        """Send budget exceeded alert."""
        if settings.alert_on_budget:
            await self._send(
                f"API\u4e88\u7b97\u8d85\u904e: ${daily_cost:.2f} / ${limit:.2f} (\u65e5\u6b21\u4e0a\u9650)",
                "warning"
            )

    async def notify_rate_limit(self, platform: str):
        """Send rate limit alert."""
        if settings.alert_on_rate_limit:
            await self._send(
                f"\u30ec\u30fc\u30c8\u5236\u9650\u691c\u51fa: {platform} \u2014 \u6295\u7a3f\u3092\u4e00\u6642\u505c\u6b62\u4e2d",
                "warning"
            )

    async def notify_post_published(self, platform: str, post_id: str):
        """Send success notification (optional, mainly for testing)."""
        await self._send(f"\u6295\u7a3f\u6210\u529f: {platform} (ID: {post_id})", "success")

    async def notify_post_failed(self, platform: str, error: str):
        """Send post failure alert."""
        if settings.alert_on_error:
            await self._send(
                f"\u6295\u7a3f\u5931\u6557: {platform}\n\u30a8\u30e9\u30fc: {error}",
                "error"
            )

    async def notify_account_issue(self, platform: str, issue: str):
        """Send account issue alert (ban, suspension, etc)."""
        await self._send(
            f"\u30a2\u30ab\u30a6\u30f3\u30c8\u554f\u984c: {platform}\n{issue}",
            "error"
        )

    async def send_media_to_discord(
        self,
        media_path: str,
        platform: str,
        text: str = "",
        post_url: str = "",
    ):
        """Send generated image/video to Discord via webhook with file attachment.

        Called after a post with media is successfully published.
        """
        if not self.enabled or "discord" not in self.webhook_url:
            return

        file_path = Path(media_path)
        if not file_path.exists():
            logger.warning("send_media_to_discord: file not found: %s", media_path)
            return

        # Discord file size limit: 25 MB for free servers
        file_size = file_path.stat().st_size
        if file_size > 25 * 1024 * 1024:
            logger.warning("send_media_to_discord: file too large (%d bytes), sending link only", file_size)
            await self._send(
                f"\U0001f4f8 **{platform}** posted (file too large to attach)\n"
                f"{text[:200]}\n{post_url}",
                "success",
            )
            return

        # Determine MIME type
        ext = file_path.suffix.lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".mp4": "video/mp4",
            ".webm": "video/webm",
            ".webp": "image/webp",
        }
        content_type = mime_map.get(ext, "application/octet-stream")

        # Build embed description
        from zoneinfo import ZoneInfo
        timestamp = datetime.now(ZoneInfo(settings.default_timezone)).strftime("%Y-%m-%d %H:%M")
        caption = text[:200] + ("..." if len(text) > 200 else "") if text else ""
        description = f"\U0001f4f8 **{platform.upper()}** | {timestamp}"
        if caption:
            description += f"\n> {caption}"
        if post_url:
            description += f"\n\U0001f517 {post_url}"

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                with open(file_path, "rb") as f:
                    files = {"file": (file_path.name, f, content_type)}
                    data = {"content": description}
                    resp = await client.post(self.webhook_url, data=data, files=files)
                    resp.raise_for_status()
            logger.info("Sent media to Discord: %s (%s)", file_path.name, platform)
        except Exception as e:
            logger.warning("Failed to send media to Discord: %s", e)


# Global singleton
alert_service = AlertService()
