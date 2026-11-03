"""Marketing agent: SNS post + fortune content generation."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from src.agents.base import AgentBase, AgentResult
from src.core.database import async_session
from src.core.models import Post

logger = logging.getLogger(__name__)


class MarketingAgent(AgentBase):
    """Supported tasks:

    - ``generate_post(platform, topic, tone, seo_keywords)``
    - ``generate_fortune(type, **kwargs)`` — raw FortuneGenerator call
    - ``generate_fortune_post(type, platform, **kwargs)`` — fortune → SNS post wrapper
    - ``generate_oracle_image(category, variation_id, platform, apply_mosaic, save_dir)`` —
      Miku ★ Oracle cosplay image via Kie.ai + optional face mosaic
    - ``list_oracle_variations(category)`` — return 40-variation prompt table (read-only)
    - ``recent_posts(days=7)`` — platform × status breakdown from ``posts`` table
    - ``status``
    """

    name = "marketing"

    def __init__(self) -> None:
        super().__init__()
        self._content_gen = None
        self._fortune_gen = None
        self._image_gen = None

    def _get_content_gen(self):
        if self._content_gen is None:
            from src.ai.claude_generator import ClaudeContentGenerator
            self._content_gen = ClaudeContentGenerator()
        return self._content_gen

    def _get_fortune_gen(self):
        if self._fortune_gen is None:
            from src.ai.fortune_generator import FortuneGenerator
            self._fortune_gen = FortuneGenerator()
        return self._fortune_gen

    def _get_image_gen(self):
        if self._image_gen is None:
            from src.ai.image_generator import ImageGenerator
            self._image_gen = ImageGenerator()
        return self._image_gen

    async def handle(self, task: str, **kwargs: Any) -> AgentResult:
        if task == "generate_post":
            platform = kwargs.get("platform", "x")
            topic = kwargs.get("topic", "daily tarot")
            tone = kwargs.get("tone", "engaging")
            gen = self._get_content_gen()
            content = await asyncio.to_thread(
                gen.generate_post,
                platform=platform,
                topic=topic,
                tone=tone,
                target_audience=kwargs.get("target_audience"),
                seo_keywords=kwargs.get("seo_keywords"),
            )
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "platform": platform,
                    "text": content.text,
                    "hashtags": content.hashtags,
                    "image_prompt": content.suggested_media_prompt,
                },
            )

        if task == "generate_fortune":
            fortune_type = (
                kwargs.pop("type", None)
                or kwargs.pop("fortune_type", None)
                or "tarot"
            )
            gen = self._get_fortune_gen()
            parsed, usage = await asyncio.to_thread(
                gen.generate_with_usage, fortune_type, **kwargs
            )
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "fortune_type": fortune_type,
                    "result": parsed,
                    "usage": usage,
                },
            )

        if task == "generate_fortune_post":
            fortune_type = (
                kwargs.pop("type", None)
                or kwargs.pop("fortune_type", None)
                or "tarot"
            )
            platform = kwargs.pop("platform", "x")
            gen = self._get_fortune_gen()
            parsed, usage = await asyncio.to_thread(
                gen.generate_with_usage, fortune_type, **kwargs
            )
            text = (
                parsed.get("text")
                or parsed.get("message")
                or parsed.get("reading")
                or ""
            )
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "platform": platform,
                    "fortune_type": fortune_type,
                    "text": text,
                    "cta": parsed.get("cta") or "",
                    "hashtags": parsed.get("hashtags") or [],
                    "image_prompt": parsed.get("image_prompt"),
                    "raw": parsed,
                    "usage": usage,
                },
            )

        if task == "generate_oracle_image":
            return await self._generate_oracle_image(**kwargs)

        if task == "list_oracle_variations":
            return self._list_oracle_variations(**kwargs)

        if task == "recent_posts":
            days = max(1, min(90, int(kwargs.get("days", 7))))
            since = datetime.now(timezone.utc) - timedelta(days=days)
            async with async_session() as session:
                rows = (
                    await session.execute(
                        select(Post.platform, Post.status, func.count(Post.id))
                        .where(Post.created_at >= since)
                        .group_by(Post.platform, Post.status)
                    )
                ).all()
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "days": days,
                    "by_platform_status": [
                        {"platform": p, "status": s, "count": int(c)}
                        for p, s, c in rows
                    ],
                },
            )

        if task == "status":
            since = datetime.now(timezone.utc) - timedelta(days=1)
            async with async_session() as session:
                count_24h = (
                    await session.execute(
                        select(func.count(Post.id)).where(Post.created_at >= since)
                    )
                ).scalar_one()
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={"posts_24h": int(count_24h or 0), "ready": True},
            )

        return await super().handle(task, **kwargs)

    # ------------------------------------------------------------------
    # Miku ★ Oracle image pipeline
    # ------------------------------------------------------------------

    async def _generate_oracle_image(
        self,
        category: str | None = None,
        variation_id: str | None = None,
        platform: str = "instagram",
        apply_mosaic: bool = True,
        save_dir: str = "data/media/oracle",
        download: bool = True,
    ) -> AgentResult:
        """Generate a Miku ★ Oracle cosplay image and optionally pixelate faces.

        Steps:
          1. Pick a prompt from ``config.miku_oracle_prompts`` (category or id).
          2. Call ``ImageGenerator.generate()`` against Kie.ai Nano Banana Pro.
          3. Download the first result to ``save_dir``.
          4. If ``apply_mosaic`` (default True), run the image through
             ``src.media.face_mosaic.apply_face_mosaic`` — detected faces get
             pixelated, and a defensive top-center rectangle is always applied
             when no face is detected.

        Returns ``AgentResult.data`` with:
          - ``variation_id`` / ``category`` / ``prompt`` / ``aspect_ratio``
          - ``remote_url`` — URL returned by Kie.ai
          - ``local_path`` — downloaded file path (if ``download`` is True)
          - ``mosaic_path`` — pixelated output (if ``apply_mosaic`` is True)
          - ``mosaic_applied`` — bool, True if a mosaic was written
        """
        from config.miku_oracle_prompts import (
            ALL_VARIATIONS,
            CATEGORIES,
            get_oracle_prompt,
        )

        # Resolve which variation this call used (for the response payload).
        used_id: str | None = variation_id
        if used_id is None:
            import random
            pool = (
                CATEGORIES[category]
                if category and category in CATEGORIES
                else ALL_VARIATIONS
            )
            used_id = random.choice(pool)["id"]

        try:
            prompt, aspect_ratio = get_oracle_prompt(
                category=category,
                variation_id=used_id,
                platform=platform,
            )
        except ValueError as exc:
            return AgentResult(
                agent=self.name, task="generate_oracle_image",
                ok=False, error=str(exc),
            )

        img_gen = self._get_image_gen()
        urls: list[str] = []
        try:
            urls = await img_gen.generate(prompt=prompt, aspect_ratio=aspect_ratio)
        except Exception as exc:  # noqa: BLE001 — surface API errors to caller
            return AgentResult(
                agent=self.name, task="generate_oracle_image", ok=False,
                error=f"kie_generation_failed: {type(exc).__name__}: {exc}",
            )

        if not urls:
            return AgentResult(
                agent=self.name, task="generate_oracle_image", ok=False,
                error="kie returned no image urls",
            )

        remote_url = urls[0]
        data: dict[str, Any] = {
            "variation_id": used_id,
            "category": category,
            "aspect_ratio": aspect_ratio,
            "prompt_preview": prompt[:200],
            "remote_url": remote_url,
            "local_path": None,
            "mosaic_path": None,
            "mosaic_applied": False,
        }

        if not download:
            return AgentResult(
                agent=self.name, task="generate_oracle_image",
                ok=True, data=data,
            )

        save_root = Path(save_dir)
        save_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        raw_path = save_root / f"{used_id}_{stamp}.png"

        try:
            await img_gen.download_image(remote_url, str(raw_path))
            data["local_path"] = str(raw_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("oracle image download failed: %s", exc)
            return AgentResult(
                agent=self.name, task="generate_oracle_image", ok=True,
                data=data, error=f"download_failed: {exc}",
            )

        if apply_mosaic:
            try:
                from src.media.face_mosaic import apply_face_mosaic
                mosaic_path = await asyncio.to_thread(
                    apply_face_mosaic,
                    raw_path,
                    save_root / f"{used_id}_{stamp}_mosaic.png",
                )
                data["mosaic_path"] = str(mosaic_path)
                data["mosaic_applied"] = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("face_mosaic failed for %s: %s", raw_path, exc)
                data["mosaic_error"] = f"{type(exc).__name__}: {exc}"

        # Send generated image to Discord for review
        final_path = data.get("mosaic_path") or data.get("local_path")
        if final_path:
            try:
                from src.core.alerts import alert_service
                await alert_service.send_media_to_discord(
                    media_path=final_path,
                    platform="oracle",
                    text=f"Miku Oracle: {used_id} ({category or 'random'})",
                )
            except Exception as exc:
                logger.warning("Discord send failed for oracle image (non-fatal): %s", exc)

        return AgentResult(
            agent=self.name, task="generate_oracle_image", ok=True, data=data,
        )

    def _list_oracle_variations(
        self, category: str | None = None, **_: Any
    ) -> AgentResult:
        """Return the Miku ★ Oracle variation index (id / title / category)."""
        from config.miku_oracle_prompts import CATEGORIES, list_variations

        try:
            raw = list_variations(category)
        except ValueError as exc:
            return AgentResult(
                agent=self.name, task="list_oracle_variations",
                ok=False, error=str(exc),
            )

        def _cat_of(vid: str) -> str:
            for name, items in CATEGORIES.items():
                if any(x["id"] == vid for x in items):
                    return name
            return "unknown"

        return AgentResult(
            agent=self.name, task="list_oracle_variations", ok=True,
            data={
                "count": len(raw),
                "category_filter": category,
                "variations": [
                    {
                        "id": v["id"],
                        "title": v["title"],
                        "category": _cat_of(v["id"]),
                        "prompt_preview": v["prompt"][:180],
                    }
                    for v in raw
                ],
            },
        )
