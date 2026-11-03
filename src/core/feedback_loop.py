"""Feedback loop: use analytics data to improve content generation."""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select, func

from src.core.database import async_session
from src.core.models import Post, PostMetric, PostStatus

logger = logging.getLogger(__name__)


class FeedbackLoop:
    """Analyze past performance and generate content optimization hints."""

    async def get_optimization_hints(self, platform: str) -> dict:
        """Analyze top-performing content and return hints for content generation."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)

        async with async_session() as session:
            # Subquery: latest metric ID per post
            latest_metric_sq = (
                select(
                    PostMetric.post_id,
                    func.max(PostMetric.id).label("max_metric_id"),
                )
                .group_by(PostMetric.post_id)
                .subquery()
            )

            # Top 20 posts by engagement (latest metric only)
            top_result = await session.execute(
                select(Post, PostMetric)
                .join(PostMetric)
                .join(
                    latest_metric_sq,
                    and_(
                        PostMetric.post_id == latest_metric_sq.c.post_id,
                        PostMetric.id == latest_metric_sq.c.max_metric_id,
                    ),
                )
                .where(
                    Post.platform == platform,
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                )
                .order_by(PostMetric.engagement_rate.desc())
                .limit(20)
            )
            top_posts = top_result.all()

            # Bottom 20 posts
            bottom_result = await session.execute(
                select(Post, PostMetric)
                .join(PostMetric)
                .join(
                    latest_metric_sq,
                    and_(
                        PostMetric.post_id == latest_metric_sq.c.post_id,
                        PostMetric.id == latest_metric_sq.c.max_metric_id,
                    ),
                )
                .where(
                    Post.platform == platform,
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                )
                .order_by(PostMetric.engagement_rate.asc())
                .limit(20)
            )
            bottom_posts = bottom_result.all()

            # Best posting hours
            hour_result = await session.execute(
                select(
                    func.extract("hour", Post.published_at).label("hour"),
                    func.avg(PostMetric.engagement_rate).label("avg_eng"),
                )
                .join(PostMetric)
                .join(
                    latest_metric_sq,
                    and_(
                        PostMetric.post_id == latest_metric_sq.c.post_id,
                        PostMetric.id == latest_metric_sq.c.max_metric_id,
                    ),
                )
                .where(
                    Post.platform == platform,
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                )
                .group_by("hour")
                .order_by(func.avg(PostMetric.engagement_rate).desc())
                .limit(5)
            )
            best_hours = [{"hour": int(r.hour), "avg_engagement": round(float(r.avg_eng), 4)} for r in hour_result]

            # Best CTR hours (separate from engagement hours)
            ctr_hour_result = await session.execute(
                select(
                    func.extract("hour", Post.published_at).label("hour"),
                    func.avg(PostMetric.ctr).label("avg_ctr"),
                )
                .join(PostMetric)
                .join(
                    latest_metric_sq,
                    and_(
                        PostMetric.post_id == latest_metric_sq.c.post_id,
                        PostMetric.id == latest_metric_sq.c.max_metric_id,
                    ),
                )
                .where(
                    Post.platform == platform,
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                    PostMetric.impressions > 0,
                )
                .group_by("hour")
                .order_by(func.avg(PostMetric.ctr).desc())
                .limit(5)
            )
            best_ctr_hours = [
                {"hour": int(r.hour), "avg_ctr": round(float(r.avg_ctr or 0) * 100, 3)}
                for r in ctr_hour_result
            ]

            # Best content types
            type_result = await session.execute(
                select(
                    Post.content_type,
                    func.avg(PostMetric.engagement_rate).label("avg_eng"),
                    func.count(Post.id).label("count"),
                )
                .join(PostMetric)
                .join(
                    latest_metric_sq,
                    and_(
                        PostMetric.post_id == latest_metric_sq.c.post_id,
                        PostMetric.id == latest_metric_sq.c.max_metric_id,
                    ),
                )
                .where(
                    Post.platform == platform,
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                )
                .group_by(Post.content_type)
                .order_by(func.avg(PostMetric.engagement_rate).desc())
            )
            best_types = [
                {"type": r.content_type, "avg_engagement": round(float(r.avg_eng), 4), "count": r.count}
                for r in type_result
            ]

            # Extract common hashtags from top posts
            top_hashtags: dict[str, int] = {}
            top_text_lengths: list[int] = []
            for post, metric in top_posts:
                if post.hashtags:
                    for tag in post.hashtags:
                        top_hashtags[tag] = top_hashtags.get(tag, 0) + 1
                if post.text_content:
                    top_text_lengths.append(len(post.text_content))

            sorted_hashtags = sorted(top_hashtags.items(), key=lambda x: x[1], reverse=True)[:15]

            avg_top_length = sum(top_text_lengths) / max(len(top_text_lengths), 1)

            bottom_text_lengths = [
                len(p.text_content) for p, m in bottom_posts if p.text_content
            ]
            avg_bottom_length = sum(bottom_text_lengths) / max(len(bottom_text_lengths), 1)

            return {
                "platform": platform,
                "analysis_period_days": 30,
                "sample_size": {
                    "top_posts": len(top_posts),
                    "bottom_posts": len(bottom_posts),
                },
                "best_posting_hours": best_hours,
                "best_content_types": best_types,
                "top_performing_hashtags": [{"tag": t, "frequency": f} for t, f in sorted_hashtags],
                "optimal_text_length": {
                    "top_posts_avg": round(avg_top_length),
                    "bottom_posts_avg": round(avg_bottom_length),
                    "recommendation": f"Aim for around {round(avg_top_length)} characters" if top_text_lengths else "Not enough data",
                },
                "best_ctr_hours": best_ctr_hours,
                "hints_for_generation": self._build_hints(best_hours, best_types, sorted_hashtags, avg_top_length, best_ctr_hours),
            }

    def _build_hints(self, best_hours, best_types, top_hashtags, avg_length, best_ctr_hours=None) -> str:
        """Build a natural language hint string for the content generator."""
        parts = []
        if best_hours:
            hours_str = ", ".join(f"{h['hour']}時" for h in best_hours[:3])
            parts.append(f"最もエンゲージメントが高い時間帯: {hours_str}")
        if best_types:
            types_str = ", ".join(f"{t['type']}({t['avg_engagement']:.2%})" for t in best_types[:3])
            parts.append(f"効果的なコンテンツタイプ: {types_str}")
        if top_hashtags:
            tags_str = ", ".join(f"#{t}" for t, _ in top_hashtags[:5])
            parts.append(f"高パフォーマンスのハッシュタグ: {tags_str}")
        if avg_length > 0:
            parts.append(f"最適な文字数: 約{round(avg_length)}文字")
        if best_ctr_hours:
            ctr_hours_str = ", ".join(f"{h['hour']}時({h['avg_ctr']}%)" for h in best_ctr_hours[:3])
            parts.append(f"CTRが高い時間帯: {ctr_hours_str} — リンク・CTA付き投稿はこの時間帯に")
        return "\n".join(parts) if parts else "データ不足のため、デフォルト戦略を使用してください"


def merge_feedback_hashtags(
    generated_hashtags: list[str],
    feedback_hints: dict,
    merge_top_n: int = 3,
) -> list[str]:
    """Merge top-performing hashtags from feedback into generated hashtags.

    Ensures high-performing hashtags from past data aren't lost when
    the SEO optimizer generates new ones.
    """
    top_feedback_tags = [
        item["tag"]
        for item in feedback_hints.get("top_performing_hashtags", [])[:merge_top_n]
    ]
    merged = top_feedback_tags + [t for t in generated_hashtags if t not in top_feedback_tags]
    return merged[:30]  # Platform max (Instagram limit)


feedback_loop = FeedbackLoop()
