"""ROI calculation: API costs vs revenue tracking."""

import logging

from src.core.cost_tracker import cost_tracker
from src.monetization.revenue_tracker import RevenueTracker

logger = logging.getLogger(__name__)

# JPY/USD exchange rate (approximate)
USD_TO_JPY = 155.0


class ROICalculator:
    """Calculate return on investment for the SNS automation system."""

    def __init__(self):
        self.revenue_tracker = RevenueTracker()

    async def calculate_roi(self, days: int = 30) -> dict:
        """Calculate ROI for the specified period."""
        # Get costs
        cost_summary = cost_tracker.get_summary()

        if days <= 1:
            api_cost_usd = cost_summary["daily_cost_usd"]
        elif days <= 31:
            api_cost_usd = cost_summary["monthly_cost_usd"]
        else:
            api_cost_usd = cost_summary["total_cost_usd"]

        api_cost_jpy = round(api_cost_usd * USD_TO_JPY)

        # Get revenue
        revenue_summary = await self.revenue_tracker.get_revenue_summary(days=days)
        total_revenue_jpy = revenue_summary.get("total_revenue", 0)

        # Calculate
        profit_jpy = total_revenue_jpy - api_cost_jpy
        roi_pct = (profit_jpy / api_cost_jpy * 100) if api_cost_jpy > 0 else 0

        # Cost per post
        total_posts = cost_summary.get("total_api_calls", 0)
        cost_per_post_jpy = round(api_cost_jpy / max(total_posts, 1))

        # Break-even analysis
        avg_revenue_per_post = total_revenue_jpy / max(total_posts, 1) if total_posts > 0 else 0
        break_even_posts = round(api_cost_jpy / max(avg_revenue_per_post, 1)) if avg_revenue_per_post > 0 else 0

        result = {
            "period_days": days,
            "api_cost_usd": round(api_cost_usd, 2),
            "api_cost_jpy": api_cost_jpy,
            "total_revenue_jpy": total_revenue_jpy,
            "profit_jpy": profit_jpy,
            "roi_pct": round(roi_pct, 1),
            "cost_per_post_jpy": cost_per_post_jpy,
            "total_posts": total_posts,
            "break_even_posts_per_day": break_even_posts,
            "status": "profitable" if profit_jpy > 0 else "investing",
            "budget_utilization_pct": round(
                cost_summary["daily_cost_usd"] / cost_summary["daily_budget_usd"] * 100, 1
            ) if cost_summary["daily_budget_usd"] > 0 else 0,
        }

        # Alert thresholds
        if api_cost_jpy > 0 and total_revenue_jpy == 0:
            result["alert"] = "収益ゼロ: アフィリエイト商品の登録とFANBOX/Fantia設定を確認してください"
        elif roi_pct < -50:
            result["alert"] = "ROIが-50%以下: コンテンツ戦略の見直しを推奨"

        return result


roi_calculator = ROICalculator()
