"""PM agent: rollout roadmap + DB-backed AgentTask queue for HoshimiLive."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from src.agents.base import AgentBase, AgentResult
from src.core.database import async_session
from src.core.models import AgentTask, AgentTaskStatus

# ---------------------------------------------------------------------------
# Static rollout roadmap — used only by "phases" / "next_tasks" tasks.
# Kept in code (not DB) so PM agent is useful even before any task row exists.
# ---------------------------------------------------------------------------
PHASES: list[dict[str, Any]] = [
    {
        "id": 1,
        "name": "Character & brand rebrand",
        "tasks": [
            "Archive old miku_desu_cosme content",
            "Rename X/YouTube/TikTok handles to @miku_oracle",
            "Update profile images + bios",
        ],
    },
    {
        "id": 2,
        "name": "Fortune content engine",
        "tasks": [
            "fortune_generator.py (tarot/horoscope/numerology/love/angel)",
            "Cosplay rotation schedule in auto_poster.py",
            "Week 1-8 Kie.ai prompt table",
        ],
    },
    {
        "id": 3,
        "name": "Monetization layer",
        "tasks": [
            "Paddle / Lemon Squeezy integration",
            "Customer DB schema (Alembic migration)",
            "Free reading funnel on LP",
        ],
    },
    {
        "id": 4,
        "name": "Public LP launch",
        "tasks": [
            "Domain hoshimilive.com acquisition",
            "Cloudflare DNS + Universal SSL",
            "FastAPI public routes",
        ],
    },
]

_VALID_STATUSES: set[str] = {s.value for s in AgentTaskStatus}


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _task_to_dict(r: AgentTask) -> dict[str, Any]:
    return {
        "id": r.id,
        "department": r.department,
        "title": r.title,
        "description": r.description,
        "status": r.status,
        "priority": r.priority,
        "assigned_to": r.assigned_to,
        "due_at": r.due_at.isoformat() if r.due_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "parent_task_id": r.parent_task_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


class PMAgent(AgentBase):
    """Supported tasks:

    - ``phases`` — return the static roadmap (read-only)
    - ``next_tasks(phase_id)`` — roadmap tasks for one phase
    - ``create_task(department, title, description, priority, due_at, assigned_to, parent_task_id)``
    - ``list_tasks(department, status, limit)``
    - ``update_task(id, status, assigned_to, priority, description)``
    - ``complete_task(id)``
    - ``status`` — aggregate counts by status + surfaced blockers
    """

    name = "pm"

    async def handle(self, task: str, **kwargs: Any) -> AgentResult:
        if task == "phases":
            return AgentResult(
                agent=self.name, task=task, ok=True, data={"phases": PHASES}
            )
        if task == "next_tasks":
            phase_id = int(kwargs.get("phase_id", 1))
            phase = next((p for p in PHASES if p["id"] == phase_id), None)
            return AgentResult(
                agent=self.name,
                task=task,
                ok=bool(phase),
                data={"phase": phase or {}, "tasks": phase["tasks"] if phase else []},
                error=None if phase else f"unknown phase_id: {phase_id}",
            )
        if task == "create_task":
            return await self._create_task(**kwargs)
        if task == "list_tasks":
            return await self._list_tasks(**kwargs)
        if task == "update_task":
            return await self._update_task(**kwargs)
        if task == "complete_task":
            return await self._complete_task(**kwargs)
        if task == "status":
            return await self._status()
        return await super().handle(task, **kwargs)

    async def _create_task(
        self,
        department: str = "",
        title: str = "",
        description: str | None = None,
        priority: int = 50,
        due_at: str | None = None,
        assigned_to: str | None = None,
        parent_task_id: int | None = None,
    ) -> AgentResult:
        if not department or not title:
            return AgentResult(
                agent=self.name,
                task="create_task",
                ok=False,
                error="department and title are required",
            )
        async with async_session() as session:
            row = AgentTask(
                department=department,
                title=title[:200],
                description=description,
                status=AgentTaskStatus.PENDING.value,
                priority=max(0, min(100, int(priority))),
                due_at=_parse_iso(due_at),
                assigned_to=assigned_to,
                parent_task_id=parent_task_id,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return AgentResult(
                agent=self.name,
                task="create_task",
                ok=True,
                data=_task_to_dict(row),
            )

    async def _list_tasks(
        self,
        department: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> AgentResult:
        limit = max(1, min(500, int(limit)))
        async with async_session() as session:
            q = select(AgentTask)
            if department:
                q = q.where(AgentTask.department == department)
            if status:
                q = q.where(AgentTask.status == status)
            q = q.order_by(
                AgentTask.priority.desc(), AgentTask.created_at.desc()
            ).limit(limit)
            rows = (await session.execute(q)).scalars().all()
        return AgentResult(
            agent=self.name,
            task="list_tasks",
            ok=True,
            data={
                "count": len(rows),
                "filters": {"department": department, "status": status},
                "tasks": [_task_to_dict(r) for r in rows],
            },
        )

    async def _update_task(
        self,
        id: int | None = None,
        status: str | None = None,
        assigned_to: str | None = None,
        priority: int | None = None,
        description: str | None = None,
    ) -> AgentResult:
        if id is None:
            return AgentResult(
                agent=self.name, task="update_task", ok=False,
                error="id is required",
            )
        if status is not None and status not in _VALID_STATUSES:
            return AgentResult(
                agent=self.name, task="update_task", ok=False,
                error=f"invalid status: {status}",
            )
        async with async_session() as session:
            row = await session.get(AgentTask, int(id))
            if row is None:
                return AgentResult(
                    agent=self.name, task="update_task", ok=False,
                    error=f"task {id} not found",
                )
            if status is not None:
                row.status = status
                if (
                    status == AgentTaskStatus.COMPLETED.value
                    and row.completed_at is None
                ):
                    row.completed_at = datetime.now(timezone.utc)
            if assigned_to is not None:
                row.assigned_to = assigned_to
            if priority is not None:
                row.priority = max(0, min(100, int(priority)))
            if description is not None:
                row.description = description
            await session.commit()
            await session.refresh(row)
            return AgentResult(
                agent=self.name,
                task="update_task",
                ok=True,
                data=_task_to_dict(row),
            )

    async def _complete_task(self, id: int | None = None) -> AgentResult:
        return await self._update_task(
            id=id, status=AgentTaskStatus.COMPLETED.value
        )

    async def _status(self) -> AgentResult:
        async with async_session() as session:
            breakdown_q = (
                select(AgentTask.status, func.count(AgentTask.id))
                .group_by(AgentTask.status)
            )
            by_status = {s: c for s, c in (await session.execute(breakdown_q)).all()}
            total = sum(by_status.values())
            blockers_q = (
                select(AgentTask)
                .where(AgentTask.status == AgentTaskStatus.BLOCKED.value)
                .order_by(AgentTask.priority.desc())
                .limit(10)
            )
            blockers = (await session.execute(blockers_q)).scalars().all()
        return AgentResult(
            agent=self.name,
            task="status",
            ok=True,
            data={
                "total_tasks": total,
                "by_status": by_status,
                "blockers": [_task_to_dict(r) for r in blockers],
                "roadmap_phases": len(PHASES),
                "ready": True,
            },
        )
