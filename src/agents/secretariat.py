"""Secretariat agent: daily reports, routing, inter-department coordination."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.agents.base import AgentBase, AgentResult


class SecretariatAgent(AgentBase):
    """Central coordinator. Supported tasks:

    - ``daily_report(date)`` — cross-department digest (re-dispatches through
      the orchestrator so every sub-call is also recorded in ``agent_logs``)
    - ``route(request)`` — keyword-based routing of a free-text request
    - ``status`` — orchestrator readiness
    """

    name = "secretariat"

    def __init__(self, orchestrator=None) -> None:
        super().__init__()
        # Late-bound to avoid circular import with Orchestrator
        self.orchestrator = orchestrator

    async def handle(self, task: str, **kwargs: Any) -> AgentResult:
        if task == "daily_report":
            return await self._daily_report(**kwargs)
        if task == "route":
            return await self._route(**kwargs)
        if task == "status":
            return AgentResult(
                agent=self.name, task=task, ok=True,
                data={
                    "ready": self.orchestrator is not None,
                    "departments": (
                        self.orchestrator.departments if self.orchestrator else []
                    ),
                },
            )
        return await super().handle(task, **kwargs)

    async def _daily_report(self, date: str | None = None) -> AgentResult:
        target_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.orchestrator is None:
            return AgentResult(
                agent=self.name, task="daily_report", ok=False,
                error="orchestrator reference missing",
            )

        summary: dict[str, Any] = {
            "date": target_date,
            "departments": {},
            "finance_digest": None,
            "pm_open_tasks": None,
        }

        # Per-department status via orchestrator.dispatch so every sub-call
        # is also persisted to agent_logs with a distinct triggered_by tag.
        for dept in ("pm", "research", "marketing", "finance"):
            r = await self.orchestrator.dispatch(
                dept, "status", triggered_by="secretariat.daily_report"
            )
            summary["departments"][dept] = {
                "ok": r.ok,
                "data": r.data,
                "error": r.error,
            }

        finance = await self.orchestrator.dispatch(
            "finance", "daily_digest", triggered_by="secretariat.daily_report"
        )
        if finance.ok:
            summary["finance_digest"] = finance.data

        pm = await self.orchestrator.dispatch(
            "pm", "list_tasks",
            triggered_by="secretariat.daily_report",
            status="in_progress", limit=20,
        )
        if pm.ok:
            summary["pm_open_tasks"] = pm.data

        return AgentResult(
            agent=self.name, task="daily_report", ok=True, data=summary,
        )

    async def _route(self, request: str = "") -> AgentResult:
        req_lower = (request or "").lower()
        if any(
            k in req_lower
            for k in ("sales", "revenue", "cost", "budget", "paddle", "lemon", "price")
        ):
            dept = "finance"
        elif any(
            k in req_lower
            for k in (
                "post", "caption", "hashtag", "sns",
                "tarot", "horoscope", "fortune", "reading",
            )
        ):
            dept = "marketing"
        elif any(
            k in req_lower
            for k in ("trend", "competitor", "viral", "niche", "research", "keyword")
        ):
            dept = "research"
        else:
            dept = "pm"
        return AgentResult(
            agent=self.name, task="route", ok=True,
            data={"routed_to": dept, "request": request},
        )
