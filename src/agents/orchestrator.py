"""Orchestrator: registers department agents, dispatches tasks, persists AgentLog.

Every ``dispatch()`` call writes one row to the ``agent_logs`` table regardless
of whether the agent succeeded or raised an exception. DB failures during
persistence are swallowed with a warning so that observability never blocks the
actual agent work.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from src.agents.base import AgentBase, AgentResult
from src.agents.finance import FinanceAgent
from src.agents.marketing import MarketingAgent
from src.agents.pm import PMAgent
from src.agents.research import ResearchAgent
from src.agents.secretariat import SecretariatAgent
from src.core.database import async_session
from src.core.models import AgentLog

logger = logging.getLogger(__name__)


def _sanitize_for_json(obj: Any, depth: int = 4) -> Any:
    """Best-effort JSON-safe sanitization of arbitrary Python objects.

    Enforces a maximum depth and truncates long scalars so that huge agent
    payloads do not blow up the ``agent_logs.input`` / ``output`` JSON columns.
    """
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return obj
    if isinstance(obj, str):
        return obj if len(obj) <= 2000 else obj[:2000] + "…"
    if depth <= 0:
        return str(obj)[:500]
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(x, depth - 1) for x in list(obj)[:50]]
    if isinstance(obj, dict):
        return {
            str(k): _sanitize_for_json(v, depth - 1)
            for k, v in list(obj.items())[:50]
        }
    # datetime, Decimal, custom objects — try isoformat, fall back to str
    for attr in ("isoformat", "__float__"):
        if hasattr(obj, attr):
            try:
                val = getattr(obj, attr)()
                return val if isinstance(val, (int, float, str)) else str(val)[:500]
            except Exception:
                break
    return str(obj)[:500]


class Orchestrator:
    """Central dispatcher for the 5-department agent system.

    Usage:
        orch = Orchestrator()
        result = await orch.dispatch("marketing", "generate_fortune", type="tarot")
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentBase] = {
            "pm": PMAgent(),
            "research": ResearchAgent(),
            "marketing": MarketingAgent(),
            "finance": FinanceAgent(),
        }
        # Secretariat needs a reference to the orchestrator for daily_report
        self._agents["secretariat"] = SecretariatAgent(orchestrator=self)

    @property
    def departments(self) -> list[str]:
        return sorted(self._agents.keys())

    def get(self, department: str) -> AgentBase | None:
        return self._agents.get(department)

    async def dispatch(
        self,
        department: str,
        task: str,
        /,
        *,
        triggered_by: str = "api",
        **kwargs: Any,
    ) -> AgentResult:
        """Dispatch a task to a department agent and persist an AgentLog row.

        ``department`` and ``task`` are positional-only so agents that take a
        ``department=`` or ``task=`` kwarg (e.g. ``pm.create_task``) can forward
        those names through ``**kwargs`` without colliding with the dispatch
        signature.

        Args:
            department: agent name (pm / research / marketing / finance / secretariat)
            task: task identifier the agent understands
            triggered_by: caller identifier (``api``, ``scheduler``,
                ``secretariat.daily_report``, ``test``, …)
            **kwargs: task-specific arguments forwarded to the agent

        Returns:
            The ``AgentResult`` the agent produced. Persisting to ``agent_logs``
            is a side-effect; DB failures are logged but never raised.
        """
        agent = self._agents.get(department)
        if agent is None:
            result = AgentResult(
                agent="orchestrator",
                task=task,
                ok=False,
                error=f"unknown department: {department}",
            )
            await self._persist_log(department, task, result, 0, triggered_by, kwargs)
            return result

        logger.info("dispatch %s.%s triggered_by=%s", department, task, triggered_by)
        start = time.perf_counter()
        result = await agent.run(task, **kwargs)
        duration_ms = int((time.perf_counter() - start) * 1000)
        await self._persist_log(
            department, task, result, duration_ms, triggered_by, kwargs
        )
        return result

    async def _persist_log(
        self,
        department: str,
        task: str,
        result: AgentResult,
        duration_ms: int,
        triggered_by: str,
        kwargs: dict[str, Any],
    ) -> None:
        """Write a single ``AgentLog`` row. Non-blocking on DB failure."""
        try:
            async with async_session() as session:
                row = AgentLog(
                    department=department,
                    task=task,
                    status="ok" if result.ok else "error",
                    duration_ms=duration_ms,
                    input=_sanitize_for_json(kwargs) if kwargs else None,
                    output=_sanitize_for_json(result.data) if result.ok else None,
                    error=result.error,
                    triggered_by=triggered_by,
                )
                session.add(row)
                await session.commit()
        except SQLAlchemyError as exc:
            logger.warning("failed to persist AgentLog: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("unexpected error persisting AgentLog: %s", exc)
