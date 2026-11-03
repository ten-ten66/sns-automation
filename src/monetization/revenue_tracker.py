"""Monetization and revenue tracking module."""

import json
from datetime import datetime, timedelta, timezone

import anthropic
from sqlalchemy import select, func

from config.settings import settings
from src.core.database import async_session
from src.core.models import Revenue, Post
from src.core.utils import parse_llm_json, extract_claude_text, retry
from src.core.cost_tracker import cost_tracker, BudgetExceededError


class RevenueTracker:
    """Track and optimize monetization across platforms."""

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
                "revenue_tracker", response.usage.input_tokens, response.usage.output_tokens,
            )
        return parse_llm_json(extract_claude_text(response))

    async def record_revenue(
        self,
        source: str,
        amount: float,
        post_id: int | None = None,
        network: str | None = None,
        clicks: int = 0,
        conversions: int = 0,
        currency: str = "JPY",
    ) -> int:
        """Record a revenue entry."""
        async with async_session() as session:
            rev = Revenue(
                post_id=post_id,
                source=source,
                network=network,
                amount=amount,
                currency=currency,
                clicks=clicks,
                conversions=conversions,
            )
            session.add(rev)
            await session.commit()
            return rev.id

    async def get_revenue_summary(self, days: int = 30) -> dict:
        """Get revenue summary for the specified period."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        async with async_session() as session:
            # Total revenue
            total = await session.execute(
                select(func.sum(Revenue.amount)).where(Revenue.recorded_at >= cutoff)
            )
            total_amount = total.scalar() or 0

            # By source
            by_source = await session.execute(
                select(
                    Revenue.source,
                    func.sum(Revenue.amount),
                    func.sum(Revenue.clicks),
                    func.sum(Revenue.conversions),
                )
                .where(Revenue.recorded_at >= cutoff)
                .group_by(Revenue.source)
            )
            sources = {}
            for source, amount, clicks, conversions in by_source:
                conv_rate = conversions / max(clicks, 1) * 100
                sources[source] = {
                    "amount": float(amount or 0),
                    "clicks": int(clicks or 0),
                    "conversions": int(conversions or 0),
                    "conversion_rate": round(conv_rate, 2),
                }

            # By network
            by_network = await session.execute(
                select(
                    Revenue.network,
                    func.sum(Revenue.amount),
                )
                .where(Revenue.recorded_at >= cutoff, Revenue.network.isnot(None))
                .group_by(Revenue.network)
            )
            networks = {net: float(amt or 0) for net, amt in by_network}

            # Daily trend
            daily = await session.execute(
                select(
                    func.date(Revenue.recorded_at),
                    func.sum(Revenue.amount),
                )
                .where(Revenue.recorded_at >= cutoff)
                .group_by(func.date(Revenue.recorded_at))
                .order_by(func.date(Revenue.recorded_at))
            )
            daily_trend = [{"date": str(d), "amount": float(a or 0)} for d, a in daily]

            # Top earning posts
            top_posts = await session.execute(
                select(
                    Post.id,
                    Post.platform,
                    Post.text_content,
                    func.sum(Revenue.amount).label("total_revenue"),
                )
                .join(Revenue)
                .where(Revenue.recorded_at >= cutoff)
                .group_by(Post.id, Post.platform, Post.text_content)
                .order_by(func.sum(Revenue.amount).desc())
                .limit(10)
            )

            top = []
            for pid, platform, text, rev in top_posts:
                top.append({
                    "post_id": pid,
                    "platform": platform,
                    "text_preview": (text or "")[:100],
                    "revenue": float(rev or 0),
                })

            return {
                "period_days": days,
                "total_revenue": float(total_amount),
                "currency": "JPY",
                "by_source": sources,
                "by_network": networks,
                "daily_trend": daily_trend,
                "top_earning_posts": top,
                "avg_daily_revenue": round(float(total_amount) / max(days, 1), 2),
            }

    def generate_monetization_strategy(self, performance_data: dict, niche: str) -> dict:
        """Use Claude to generate monetization strategy."""
        prompt = f"""以下のSNSパフォーマンスデータに基づき、収益最大化戦略を提案してください。

## パフォーマンスデータ
{json.dumps(performance_data, ensure_ascii=False, default=str)}

## ジャンル: {niche}

## 出力（JSON）
{{
    "current_assessment": "現状評価",
    "revenue_streams": [
        {{
            "stream": "収益源",
            "potential_monthly": "月間ポテンシャル（円）",
            "difficulty": "easy/medium/hard",
            "time_to_revenue": "収益化までの期間",
            "action_steps": ["具体的手順"]
        }}
    ],
    "affiliate_recommendations": [
        {{
            "network": "ASPネットワーク",
            "product_category": "商品カテゴリ",
            "estimated_commission": "推定コミッション",
            "integration_method": "統合方法"
        }}
    ],
    "pricing_strategy": {{
        "sponsored_post_rate": "スポンサー投稿単価目安",
        "story_rate": "ストーリー単価目安",
        "factors": ["価格設定の根拠"]
    }},
    "content_monetization_tips": [
        "コンテンツ収益化のコツ"
    ],
    "growth_milestones": [
        {{
            "followers": "フォロワー数目標",
            "unlocked_revenue": "解放される収益源",
            "estimated_monthly": "推定月間収益"
        }}
    ],
    "immediate_actions": ["今すぐ実行すべきアクション"]
}}

JSONのみを出力してください。"""

        return self._call_claude_json(prompt, max_tokens=3000)

    def generate_affiliate_post(
        self,
        product: str,
        affiliate_link: str,
        platform: str,
    ) -> dict:
        """Generate an optimized affiliate post."""
        prompt = f"""以下の商品のアフィリエイト投稿を{platform}用に作成してください。

## 商品: {product}
## プラットフォーム: {platform}

## 注意事項
- PR表記を必ず含める（ステマ規制対応）
- 自然な紹介文にする
- CTAを明確に
- 過度な誇張は避ける

## 出力（JSON）
{{
    "text": "投稿テキスト（PR表記含む）",
    "hashtags": ["ハッシュタグ"],
    "cta": "CTA文",
    "media_suggestion": "画像/動画の提案",
    "disclosure": "PR表記テキスト",
    "posting_tips": ["投稿時のコツ"]
}}

JSONのみを出力してください。"""

        return self._call_claude_json(prompt, max_tokens=1500)
