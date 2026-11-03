"""Adaptive content type ratio optimization based on engagement data."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func

from src.core.database import async_session
from src.core.models import Post, PostMetric, PostStatus
from config.persona import CONTENT_SCHEDULE_RATIO

logger = logging.getLogger(__name__)

# Safety bounds: no content type should go below 3% or above 50%
RATIO_MIN = 0.03
RATIO_MAX = 0.50
LEARNING_RATE = 0.1  # Max 10% adjustment per optimization cycle


class ContentRatioOptimizer:
    """Dynamically adjust content type ratios based on performance data."""

    async def get_engagement_by_type(
        self, platform: str, days: int = 30
    ) -> dict[str, float]:
        """Calculate average engagement rate per content type."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with async_session() as session:
            result = await session.execute(
                select(
                    Post.content_type,
                    func.avg(PostMetric.engagement_rate).label("avg_eng"),
                    func.count(Post.id).label("count"),
                )
                .join(PostMetric)
                .where(
                    Post.platform == platform,
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                )
                .group_by(Post.content_type)
                .having(func.count(Post.id) >= 3)  # Need at least 3 posts
            )

            return {
                row.content_type: round(float(row.avg_eng or 0), 4)
                for row in result
            }

    async def calculate_optimal_ratios(
        self, platform: str, days: int = 30
    ) -> dict[str, float]:
        """Calculate optimized ratios based on engagement data.

        Uses a conservative approach: blend current ratios with
        engagement-weighted ratios using LEARNING_RATE.
        """
        current_ratios = dict(CONTENT_SCHEDULE_RATIO.get(platform, {}))
        if not current_ratios:
            return {}

        engagement_data = await self.get_engagement_by_type(platform, days)

        if not engagement_data:
            logger.info("Not enough data for %s ratio optimization", platform)
            return current_ratios

        # Calculate engagement-weighted target ratios
        total_engagement = sum(engagement_data.values())
        if total_engagement == 0:
            return current_ratios

        target_ratios = {}
        for content_type in current_ratios:
            eng = engagement_data.get(content_type, 0)
            target_ratios[content_type] = eng / total_engagement if total_engagement > 0 else current_ratios[content_type]

        # Blend current with target using learning rate
        optimized = {}
        for content_type, current in current_ratios.items():
            target = target_ratios.get(content_type, current)
            new_ratio = current + LEARNING_RATE * (target - current)
            # Apply bounds
            new_ratio = max(RATIO_MIN, min(RATIO_MAX, new_ratio))
            optimized[content_type] = round(new_ratio, 3)

        # Normalize to sum to 1.0
        total = sum(optimized.values())
        if total > 0:
            optimized = {k: round(v / total, 3) for k, v in optimized.items()}

        logger.info(
            "Optimized ratios for %s: %s (from engagement data of %d types)",
            platform, optimized, len(engagement_data),
        )
        return optimized

    async def get_optimization_report(self, platform: str) -> dict:
        """Get a detailed report comparing current vs optimized ratios."""
        current = dict(CONTENT_SCHEDULE_RATIO.get(platform, {}))
        engagement = await self.get_engagement_by_type(platform)
        optimized = await self.calculate_optimal_ratios(platform)

        report = {
            "platform": platform,
            "current_ratios": current,
            "engagement_by_type": engagement,
            "optimized_ratios": optimized,
            "changes": {},
        }

        for ct in current:
            old = current.get(ct, 0)
            new = optimized.get(ct, 0)
            diff = new - old
            if abs(diff) > 0.005:
                report["changes"][ct] = {
                    "old": old,
                    "new": new,
                    "diff": round(diff, 3),
                    "direction": "↑" if diff > 0 else "↓",
                }

        return report


ratio_optimizer = ContentRatioOptimizer()
