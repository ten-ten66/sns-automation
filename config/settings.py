"""Application settings loaded from environment variables."""

import logging
from pydantic_settings import BaseSettings
from pydantic import Field

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    # Claude API
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"

    # Kie.ai (image + video generation)
    kie_api_key: str = ""
    kie_base_url: str = "https://api.kie.ai/api/v1"
    kie_nano_banana_model: str = "gemini-3-pro-image-preview"  # options: gemini-3-pro-image-preview, gemini-3.1-flash-image-preview
    kie_veo_model: str = "veo3_fast"  # options: veo3, veo3_fast

    # X (Twitter)
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""
    x_bearer_token: str = ""

    # Instagram
    instagram_username: str = ""
    instagram_password: str = ""
    instagram_proxy: str = ""  # e.g., "socks5://ip:port" or "http://user:pass@ip:port"

    # TikTok
    tiktok_access_token: str = ""
    tiktok_proxy: str = ""  # e.g., "socks5://ip:port"

    # YouTube
    youtube_api_key: str = ""
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_credentials_path: str = ""  # OAuth credentials JSON file path

    # Database
    database_url: str = "sqlite+aiosqlite:///data/sns_automation.db"

    # Scheduling
    default_timezone: str = "Asia/Tokyo"
    max_posts_per_day: int = 10

    # Account warm-up (set to automation start date for new accounts)
    warmup_start_date: str = ""  # ISO format: "2026-04-01" or empty for no warm-up

    # SEO
    target_keywords: list[str] = Field(default_factory=lambda: [
        "skincare routine", "beauty review", "J-beauty", "best moisturizer",
        "makeup tutorial", "glass skin", "Korean skincare",
    ])
    target_audience: str = "18-34 year old beauty enthusiasts"
    content_language: str = "en"
    niche: str = "beauty & skincare (J-beauty)"

    # Monetization
    affiliate_networks: list[str] = Field(default_factory=lambda: [
        "amazon_associates", "shareasale", "cj_affiliate", "ltk", "rakuten_us",
    ])

    # Membership URLs (used for cross-promotion links)
    fanbox_url: str = ""       # e.g., "https://fanbox.cc/@username"
    fantia_url: str = ""       # e.g., "https://fantia.jp/fanclubs/xxxxx"
    patreon_url: str = ""      # e.g., "https://patreon.com/username"
    youtube_membership_url: str = ""

    # Alert notifications
    alert_webhook_url: str = ""  # Slack or Discord webhook URL
    alert_enabled: bool = True
    alert_on_error: bool = True
    alert_on_budget: bool = True
    alert_on_rate_limit: bool = True

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # API authentication key for the dashboard/API
    sns_api_key: str = ""

    # Discord Bot
    discord_bot_token: str = ""

    # HoshimiLive payment webhooks
    # Paddle: HMAC-SHA256 shared secret set in dashboard; header: Paddle-Signature
    paddle_webhook_secret: str = ""
    # Lemon Squeezy: HMAC-SHA256 signing secret set in store settings;
    # header: X-Signature (hex digest of raw body)
    lemon_squeezy_webhook_secret: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    def _reload_empty_from_dotenv(self):
        """Reload fields that are empty strings from .env file.

        Workaround for parent processes that set env vars to empty strings,
        which overrides .env values in pydantic-settings.
        """
        try:
            from dotenv import dotenv_values
            env_vals = dotenv_values(".env")
            for field_name in self.model_fields:
                current = getattr(self, field_name, "")
                if current == "" and env_vals.get(field_name.upper(), ""):
                    setattr(self, field_name, env_vals[field_name.upper()])
        except Exception:
            pass

    def validate_required(self) -> list[str]:
        """Validate required settings and return warnings for missing optional ones."""
        warnings = []

        self._reload_empty_from_dotenv()

        # Critical: cannot generate content without this
        if not self.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required. Set it in .env or environment variables."
            )

        if not self.sns_api_key:
            warnings.append("SNS_API_KEY is not set — API endpoints will return 503")

        if not self.discord_bot_token:
            warnings.append("DISCORD_BOT_TOKEN is not set — Discord bot will not start")

        # Check which platforms are configured
        platform_status = {
            "X (Twitter)": bool(self.x_api_key),
            "Instagram": bool(self.instagram_username),
            "TikTok": bool(self.tiktok_access_token),
            "YouTube": bool(self.youtube_api_key),
        }
        enabled = [name for name, ok in platform_status.items() if ok]
        disabled = [name for name, ok in platform_status.items() if not ok]

        if not enabled:
            warnings.append("No SNS platforms configured — automated posting will be disabled")
        else:
            logger.info("Enabled platforms: %s", ", ".join(enabled))

        if disabled:
            warnings.append(f"Disabled platforms (no credentials): {', '.join(disabled)}")

        for w in warnings:
            logger.warning(w)

        return warnings


settings = Settings()
settings.validate_required()
