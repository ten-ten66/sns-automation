"""Finance agent: cost tracker + revenue tracker + daily digest."""

from __future__ import annotations

from typing import Any

from src.agents.base import AgentBase, AgentResult
from src.core.cost_tracker import cost_tracker


class FinanceAgent(AgentBase):
    """Supported tasks:

    - ``cost_summary`` / ``costs_today`` — cost_tracker.get_summary()
    - ``revenue_summary(days=30)`` — revenue_tracker.get_revenue_summary()
    - ``daily_digest`` — combined cost + 24h revenue snapshot
    - ``status`` — minimal readiness + budget flag
    """

    name = "finance"

    def __init__(self) -> None:
        super().__init__()
        self._revenue_tracker = None

    def _get_revenue_tracker(self):
        if self._revenue_tracker is None:
            from src.monetization.revenue_tracker import RevenueTracker
            self._revenue_tracker = RevenueTracker()
        return self._revenue_tracker

    async def handle(self, task: str, **kwargs: Any) -> AgentResult:
        if task in ("cost_summary", "costs_today"):
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data=cost_tracker.get_summary(),
            )

        if task == "revenue_summary":
            days = max(1, min(365, int(kwargs.get("days", 30))))
            rt = self._get_revenue_tracker()
            data = await rt.get_revenue_summary(days=days)
            return AgentResult(
                agent=self.name, task=task, ok=True, data=data,
            )

        if task == "daily_digest":
            cost = cost_tracker.get_summary()
            rt = self._get_revenue_tracker()
            try:
                revenue = await rt.get_revenue_summary(days=1)
            except Exception as exc:  # pragma: no cover — DB glitch shouldn't block digest
                self.log.warning("revenue_summary failed in daily_digest: %s", exc)
                revenue = {
                    "total_revenue": 0.0,
                    "currency": "JPY",
                    "by_source": {},
                }
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "cost": {
                        "daily_cost_usd": cost["daily_cost_usd"],
                        "daily_budget_usd": cost["daily_budget_usd"],
                        "daily_budget_remaining": cost["daily_budget_remaining"],
                        "monthly_cost_usd": cost["monthly_cost_usd"],
                        "budget_exceeded": cost["budget_exceeded"],
                    },
                    "revenue_24h": {
                        "amount": revenue.get("total_revenue", 0.0),
                        "currency": revenue.get("currency", "JPY"),
                        "by_source": revenue.get("by_source", {}),
                    },
                },
            )

        if task == "status":
            summary = cost_tracker.get_summary()
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "daily_cost_usd": summary["daily_cost_usd"],
                    "daily_budget_remaining": summary["daily_budget_remaining"],
                    "budget_exceeded": summary["budget_exceeded"],
                    "ready": True,
                },
            )

        return await super().handle(task, **kwargs)
