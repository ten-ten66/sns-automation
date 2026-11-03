"""Analytics and viral score prediction module."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import anthropic
from sqlalchemy import select, func

from config.settings import settings
from src.core.database import async_session
from src.core.models import Post, PostMetric, FollowerSnapshot, PostStatus
from src.core.utils import parse_llm_json, extract_claude_text, retry
from src.core.cost_tracker import cost_tracker, BudgetExceededError


class AnalyticsEngine:
    """Analyze post performance and predict viral potential."""

    def __init__(self):
        self.claude = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    @retry(max_retries=3, base_delay=2.0, max_delay=60.0)
    def _call_claude_json(self, prompt: str, max_tokens: int = 2000) -> dict | list:
        if cost_tracker.is_budget_exceeded():
            raise BudgetExceededError("Daily API budget exceeded.")
        response = self.claude.messages.create(
            model=settings.claude_model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if hasattr(response, "usage"):
            cost_tracker.record_claude_usage(
                "analytics", response.usage.input_tokens, response.usage.output_tokens,
            )
        return parse_llm_json(extract_claude_text(response))

    async def get_performance_summary(self, days: int = 30) -> dict:
        """Get overall performance summary for the last N days."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with async_session() as session:
            # Total posts
            post_count = await session.execute(
                select(func.count(Post.id)).where(
                    Post.published_at >= cutoff,
                    Post.status == PostStatus.PUBLISHED,
                )
            )
            total_posts = post_count.scalar() or 0

            # Aggregate metrics
            metrics = await session.execute(
                select(
                    func.sum(PostMetric.impressions),
                    func.sum(PostMetric.likes),
                    func.sum(PostMetric.comments),
                    func.sum(PostMetric.shares),
                    func.sum(PostMetric.clicks),
                    func.avg(PostMetric.engagement_rate),
                ).join(Post).where(Post.published_at >= cutoff)
            )
            row = metrics.one()

            # Per-platform breakdown
            platform_stats = await session.execute(
                select(
                    Post.platform,
                    func.count(Post.id),
                    func.avg(PostMetric.engagement_rate),
                    func.sum(PostMetric.likes),
                )
                .join(PostMetric)
                .where(Post.published_at >= cutoff)
                .group_by(Post.platform)
            )

            platforms = {}
            for p_name, p_count, p_eng, p_likes in platform_stats:
                platforms[p_name] = {
                    "posts": p_count,
                    "avg_engagement_rate": round(float(p_eng or 0), 4),
                    "total_likes": int(p_likes or 0),
                }

            # Follower growth
            follower_data = await session.execute(
                select(FollowerSnapshot)
                .where(FollowerSnapshot.recorded_at >= cutoff)
                .order_by(FollowerSnapshot.recorded_at)
            )
            snapshots = follower_data.scalars().all()

            follower_growth = {}
            for s in snapshots:
                if s.platform not in follower_growth:
                    follower_growth[s.platform] = {"start": s.follower_count, "end": s.follower_count}
                follower_growth[s.platform]["end"] = s.follower_count

            for platform, data in follower_growth.items():
                data["growth"] = data["end"] - data["start"]
                data["growth_rate"] = (
                    round(data["growth"] / max(data["start"], 1) * 100, 2)
                )

            return {
                "period_days": days,
                "total_posts": total_posts,
                "total_impressions": int(row[0] or 0),
                "total_likes": int(row[1] or 0),
                "total_comments": int(row[2] or 0),
                "total_shares": int(row[3] or 0),
                "total_clicks": int(row[4] or 0),
                "avg_engagement_rate": round(float(row[5] or 0), 4),
                "platform_breakdown": platforms,
                "follower_growth": follower_growth,
            }

    async def get_best_performing_posts(self, limit: int = 10) -> list[dict]:
        """Get the best performing posts by engagement."""
        async with async_session() as session:
            result = await session.execute(
                select(Post, PostMetric)
                .join(PostMetric)
                .order_by(PostMetric.engagement_rate.desc())
                .limit(limit)
            )
            posts = []
            for post, metric in result:
                posts.append({
                    "id": post.id,
                    "platform": post.platform,
                    "text": post.text_content[:200] if post.text_content else "",
                    "published_at": str(post.published_at),
                    "engagement_rate": metric.engagement_rate,
                    "likes": metric.likes,
                    "comments": metric.comments,
                    "shares": metric.shares,
                    "impressions": metric.impressions,
                })
            return posts

    async def get_optimal_posting_times(self) -> dict:
        """Analyze historical data to find optimal posting times."""
        async with async_session() as session:
            result = await session.execute(
                select(
                    Post.platform,
                    func.extract("hour", Post.published_at).label("hour"),
                    func.extract("dow", Post.published_at).label("dow"),
                    func.avg(PostMetric.engagement_rate).label("avg_engagement"),
                )
                .join(PostMetric)
                .where(Post.status == PostStatus.PUBLISHED)
                .group_by(Post.platform, "hour", "dow")
                .order_by(func.avg(PostMetric.engagement_rate).desc())
            )

            optimal = {}
            for row in result:
                platform = row.platform
                if platform not in optimal:
                    optimal[platform] = []
                optimal[platform].append({
                    "hour": int(row.hour),
                    "day_of_week": int(row.dow),
                    "avg_engagement": round(float(row.avg_engagement), 4),
                })

            return optimal

    def predict_viral_score(self, text: str, platform: str, context: dict | None = None) -> dict:
        """Use Claude to predict the viral potential of a post."""
        prompt = f"""以下の{platform}投稿のバイラルスコアを予測してください。

## 投稿内容
{text}

{"## 追加コンテキスト: " + json.dumps(context, ensure_ascii=False) if context else ""}

## 分析項目（JSON出力）
{{
    "viral_score": 0.0-1.0,
    "confidence": 0.0-1.0,
    "strengths": ["この投稿の強み"],
    "weaknesses": ["改善すべき点"],
    "predicted_engagement": {{
        "likes_range": [最小, 最大],
        "shares_range": [最小, 最大],
        "comments_range": [最小, 最大]
    }},
    "improvement_suggestions": [
        {{
            "change": "具体的な変更",
            "expected_impact": "high/medium/low",
            "reason": "理由"
        }}
    ],
    "best_posting_time": "最適な投稿時間",
    "target_hashtags": ["推奨ハッシュタグ"]
}}

JSONのみを出力してください。"""

        return self._call_claude_json(prompt, max_tokens=2000)

    async def generate_insights_report(self, days: int = 7) -> dict:
        """Generate an AI-powered insights report."""
        summary = await self.get_performance_summary(days)
        top_posts = await self.get_best_performing_posts(5)
        optimal_times = await self.get_optimal_posting_times()

        prompt = f"""以下のSNS運用データを分析し、アクション可能なインサイトレポートを生成してください。

## パフォーマンスサマリー（過去{days}日）
{json.dumps(summary, ensure_ascii=False, default=str)}

## トップパフォーマンス投稿
{json.dumps(top_posts, ensure_ascii=False, default=str)}

## 最適投稿時間データ
{json.dumps(optimal_times, ensure_ascii=False, default=str)}

## 出力（JSON）
{{
    "executive_summary": "エグゼクティブサマリー（3行）",
    "key_metrics": {{
        "best_platform": "最も成果の高いプラットフォーム",
        "best_content_type": "最も効果的なコンテンツタイプ",
        "growth_trajectory": "成長軌道（上昇/横ばい/下降）"
    }},
    "what_worked": ["うまくいった施策"],
    "what_didnt_work": ["改善が必要な施策"],
    "action_items": [
        {{
            "action": "具体的なアクション",
            "priority": "high/medium/low",
            "expected_impact": "期待される効果",
            "platform": "対象プラットフォーム"
        }}
    ],
    "content_recommendations": [
        {{
            "topic": "トピック",
            "platform": "プラットフォーム",
            "content_type": "コンテンツタイプ",
            "reason": "推奨理由"
        }}
    ],
    "follower_growth_forecast": "フォロワー成長予測",
    "revenue_opportunities": ["収益機会"]
}}

JSONのみを出力してください。"""

        return await asyncio.to_thread(self._call_claude_json, prompt, 3000)

    async def get_ctr_analysis(self, days: int = 30) -> dict:
        """Analyze CTR performance across platforms and content types."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with async_session() as session:
            # Overall CTR by platform
            platform_ctr = await session.execute(
                select(
                    Post.platform,
                    func.avg(PostMetric.ctr).label("avg_ctr"),
                    func.sum(PostMetric.clicks).label("total_clicks"),
                    func.sum(PostMetric.impressions).label("total_impressions"),
                    func.count(Post.id).label("post_count"),
                )
                .join(PostMetric)
                .where(
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                    PostMetric.impressions > 0,
                )
                .group_by(Post.platform)
            )

            platforms = {}
            for row in platform_ctr:
                total_imp = int(row.total_impressions or 0)
                total_clk = int(row.total_clicks or 0)
                platforms[row.platform] = {
                    "avg_ctr": round(float(row.avg_ctr or 0) * 100, 3),
                    "total_clicks": total_clk,
                    "total_impressions": total_imp,
                    "overall_ctr_pct": round(total_clk / max(total_imp, 1) * 100, 3),
                    "post_count": row.post_count,
                }

            # CTR by content type
            type_ctr = await session.execute(
                select(
                    Post.content_type,
                    func.avg(PostMetric.ctr).label("avg_ctr"),
                    func.count(Post.id).label("count"),
                )
                .join(PostMetric)
                .where(
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                    PostMetric.impressions > 0,
                )
                .group_by(Post.content_type)
                .order_by(func.avg(PostMetric.ctr).desc())
            )

            content_types = [
                {
                    "type": row.content_type,
                    "avg_ctr_pct": round(float(row.avg_ctr or 0) * 100, 3),
                    "sample_size": row.count,
                }
                for row in type_ctr
            ]

            # CTR by hour of day
            hourly_ctr = await session.execute(
                select(
                    func.extract("hour", Post.published_at).label("hour"),
                    func.avg(PostMetric.ctr).label("avg_ctr"),
                )
                .join(PostMetric)
                .where(
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                    PostMetric.impressions > 0,
                )
                .group_by("hour")
                .order_by(func.avg(PostMetric.ctr).desc())
            )

            best_hours = [
                {"hour": int(row.hour), "avg_ctr_pct": round(float(row.avg_ctr or 0) * 100, 3)}
                for row in hourly_ctr
            ]

            # Top 10 highest CTR posts
            top_ctr_posts = await session.execute(
                select(Post, PostMetric)
                .join(PostMetric)
                .where(
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                    PostMetric.impressions >= 100,  # Minimum sample size
                )
                .order_by(PostMetric.ctr.desc())
                .limit(10)
            )

            top_posts = []
            for post, metric in top_ctr_posts:
                top_posts.append({
                    "id": post.id,
                    "platform": post.platform,
                    "text_preview": (post.text_content or "")[:120],
                    "ctr_pct": round(float(metric.ctr or 0) * 100, 3),
                    "clicks": metric.clicks,
                    "impressions": metric.impressions,
                    "content_type": post.content_type,
                })

            # Bottom 10 lowest CTR posts (with enough impressions)
            bottom_ctr_posts = await session.execute(
                select(Post, PostMetric)
                .join(PostMetric)
                .where(
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                    PostMetric.impressions >= 100,
                )
                .order_by(PostMetric.ctr.asc())
                .limit(10)
            )

            bottom_posts = []
            for post, metric in bottom_ctr_posts:
                bottom_posts.append({
                    "id": post.id,
                    "platform": post.platform,
                    "text_preview": (post.text_content or "")[:120],
                    "ctr_pct": round(float(metric.ctr or 0) * 100, 3),
                    "clicks": metric.clicks,
                    "impressions": metric.impressions,
                })

            return {
                "period_days": days,
                "by_platform": platforms,
                "by_content_type": content_types,
                "by_hour": best_hours[:10],
                "top_ctr_posts": top_posts,
                "bottom_ctr_posts": bottom_posts,
                "recommendations": self._generate_ctr_recommendations(platforms, content_types, best_hours),
            }

    def _generate_ctr_recommendations(self, platforms: dict, content_types: list, best_hours: list) -> list[str]:
        """Generate actionable CTR improvement recommendations."""
        recs = []

        # Best platform
        if platforms:
            best_platform = max(platforms.items(), key=lambda x: x[1]["overall_ctr_pct"])
            worst_platform = min(platforms.items(), key=lambda x: x[1]["overall_ctr_pct"])
            recs.append(
                f"最もCTRが高いプラットフォーム: {best_platform[0]} ({best_platform[1]['overall_ctr_pct']}%). "
                f"{worst_platform[0]}のCTRが低い({worst_platform[1]['overall_ctr_pct']}%) - CTA改善を検討"
            )

        # Best content type
        if content_types:
            best_type = content_types[0]
            recs.append(
                f"最もCTRが高いコンテンツタイプ: {best_type['type']} ({best_type['avg_ctr_pct']}%). "
                f"このタイプの投稿比率を増やすことを推奨"
            )

        # Best hours
        if best_hours:
            top_hours = [str(h["hour"]) + "時" for h in best_hours[:3]]
            recs.append(f"CTRが高い時間帯: {', '.join(top_hours)}. リンク付き投稿はこの時間帯に集中")

        # General tips
        recs.extend([
            "CTR改善: 冒頭3秒のフック強化、プロフィールリンク誘導、ストーリーのスワイプアップ活用",
            "低CTR投稿の共通点を分析し、CTA文言・配置のA/Bテストを実施",
        ])

        return recs

    def analyze_ctr_patterns(self, top_posts: list[dict], bottom_posts: list[dict]) -> dict:
        """Use Claude to analyze CTR patterns and suggest improvements."""
        prompt = f"""以下のSNS投稿データからCTR（クリック率）のパターンを分析し、改善策を提案してください。

## CTRが高い投稿TOP10
{json.dumps(top_posts, ensure_ascii=False)}

## CTRが低い投稿BOTTOM10
{json.dumps(bottom_posts, ensure_ascii=False)}

## 分析項目（JSON出力）
{{
    "high_ctr_patterns": {{
        "common_hooks": ["冒頭フックの共通パターン"],
        "cta_patterns": ["効果的なCTAパターン"],
        "content_characteristics": ["高CTRコンテンツの特徴"],
        "word_triggers": ["CTRを上げるキーワード/フレーズ"]
    }},
    "low_ctr_patterns": {{
        "common_issues": ["低CTRの原因"],
        "missing_elements": ["不足している要素"],
        "improvement_areas": ["改善すべき領域"]
    }},
    "cta_optimization": {{
        "best_cta_templates": ["効果的なCTAテンプレート5つ"],
        "cta_placement": "最適なCTA配置",
        "urgency_triggers": ["緊急性を出すフレーズ"],
        "curiosity_triggers": ["好奇心を刺激するフレーズ"]
    }},
    "platform_specific_tips": {{
        "x": ["X固有のCTR改善策"],
        "instagram": ["Instagram固有のCTR改善策"],
        "tiktok": ["TikTok固有のCTR改善策"],
        "youtube": ["YouTube固有のCTR改善策"]
    }},
    "ab_test_suggestions": [
        {{
            "hypothesis": "仮説",
            "variant_a": "パターンA",
            "variant_b": "パターンB",
            "metric": "測定指標",
            "expected_lift": "期待されるCTR改善率"
        }}
    ],
    "immediate_actions": ["今すぐ実行すべきCTR改善アクション（優先度順）"]
}}

JSONのみを出力してください。"""

        return self._call_claude_json(prompt, max_tokens=3000)

    async def ab_test_results(self, test_group_a: str, test_group_b: str) -> dict:
        """Compare A/B test results."""
        async with async_session() as session:
            results = {}
            for group in [test_group_a, test_group_b]:
                data = await session.execute(
                    select(
                        func.avg(PostMetric.engagement_rate),
                        func.avg(PostMetric.likes),
                        func.avg(PostMetric.shares),
                        func.count(Post.id),
                    )
                    .join(Post)
                    .where(Post.ab_test_group == group)
                )
                row = data.one()
                results[group] = {
                    "avg_engagement_rate": round(float(row[0] or 0), 4),
                    "avg_likes": round(float(row[1] or 0), 1),
                    "avg_shares": round(float(row[2] or 0), 1),
                    "sample_size": row[3],
                }

            # Determine winner
            a_eng = results[test_group_a]["avg_engagement_rate"]
            b_eng = results[test_group_b]["avg_engagement_rate"]
            winner = test_group_a if a_eng > b_eng else test_group_b
            lift = abs(a_eng - b_eng) / max(min(a_eng, b_eng), 0.0001) * 100

            return {
                "groups": results,
                "winner": winner,
                "lift_percentage": round(lift, 2),
            }
