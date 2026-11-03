"""Tests for the 5-department agent system.

Covers:
- Orchestrator.dispatch writes an AgentLog row for every call
- PMAgent create_task/list/update/complete round-trip through AgentTask table
- Finance/Research/Marketing/PM basic status tasks work end-to-end
- Secretariat daily_report fans out + persists sub-dispatches
- Unknown department / task paths still write an error row
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from unittest.mock import patch

from src.core.models import AgentLog, AgentTask, AgentTaskStatus


@pytest_asyncio.fixture
async def orch(db_engine):
    """Build an Orchestrator whose persistence goes through the test DB."""
    test_session_maker = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    # Patch every module that imports async_session BEFORE constructing the
    # orchestrator so agent instances pick up the test session maker. The
    # orchestrator and each agent captured `async_session` by name at import
    # time, so we patch the module attributes they look up.
    patches = [
        patch(f"src.agents.{m}.async_session", test_session_maker)
        for m in ("orchestrator", "pm", "research", "marketing")
    ]
    for p in patches:
        p.start()
    try:
        from src.agents import Orchestrator
        yield Orchestrator(), test_session_maker
    finally:
        for p in patches:
            p.stop()


async def _count_logs(session_maker) -> int:
    async with session_maker() as session:
        return (await session.execute(select(func.count(AgentLog.id)))).scalar_one()


# ---------------------------------------------------------------------------
# Orchestrator.dispatch persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_persists_agent_log_on_success(orch):
    o, session_maker = orch
    result = await o.dispatch("pm", "status", triggered_by="test")
    assert result.ok is True

    async with session_maker() as session:
        rows = (await session.execute(select(AgentLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.department == "pm"
    assert row.task == "status"
    assert row.status == "ok"
    assert row.triggered_by == "test"
    assert row.duration_ms >= 0
    assert row.error is None
    assert row.output is not None


@pytest.mark.asyncio
async def test_dispatch_unknown_department_writes_error_row(orch):
    o, session_maker = orch
    result = await o.dispatch("nonexistent", "status", triggered_by="test")
    assert result.ok is False
    assert "unknown department" in (result.error or "")

    async with session_maker() as session:
        rows = (await session.execute(select(AgentLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].department == "nonexistent"


@pytest.mark.asyncio
async def test_dispatch_unsupported_task_writes_error_row(orch):
    o, session_maker = orch
    result = await o.dispatch("pm", "bogus_task", triggered_by="test")
    assert result.ok is False

    async with session_maker() as session:
        rows = (await session.execute(select(AgentLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].task == "bogus_task"


# ---------------------------------------------------------------------------
# PM agent: AgentTask CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pm_create_list_update_complete_task(orch):
    o, session_maker = orch

    # Create
    r = await o.dispatch(
        "pm", "create_task", triggered_by="test",
        department="marketing",
        title="Write launch tweet thread",
        description="3-tweet thread announcing hoshimilive.com",
        priority=80,
    )
    assert r.ok is True
    tid = r.data["id"]
    assert tid >= 1
    assert r.data["status"] == AgentTaskStatus.PENDING.value
    assert r.data["priority"] == 80

    # List by department
    r = await o.dispatch(
        "pm", "list_tasks", triggered_by="test", department="marketing"
    )
    assert r.ok is True
    assert r.data["count"] == 1
    assert r.data["tasks"][0]["id"] == tid

    # Update to in_progress
    r = await o.dispatch(
        "pm", "update_task", triggered_by="test",
        id=tid, status=AgentTaskStatus.IN_PROGRESS.value,
        assigned_to="miku",
    )
    assert r.ok is True
    assert r.data["status"] == AgentTaskStatus.IN_PROGRESS.value
    assert r.data["assigned_to"] == "miku"

    # Complete
    r = await o.dispatch("pm", "complete_task", triggered_by="test", id=tid)
    assert r.ok is True
    assert r.data["status"] == AgentTaskStatus.COMPLETED.value
    assert r.data["completed_at"] is not None

    # DB reflects it
    async with session_maker() as session:
        row = await session.get(AgentTask, tid)
    assert row is not None
    assert row.status == AgentTaskStatus.COMPLETED.value
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_pm_update_task_rejects_invalid_status(orch):
    o, _ = orch
    r = await o.dispatch(
        "pm", "create_task", triggered_by="test",
        department="pm", title="x",
    )
    tid = r.data["id"]
    r = await o.dispatch(
        "pm", "update_task", triggered_by="test", id=tid, status="weird",
    )
    assert r.ok is False
    assert "invalid status" in (r.error or "")


@pytest.mark.asyncio
async def test_pm_update_task_missing_row(orch):
    o, _ = orch
    r = await o.dispatch(
        "pm", "update_task", triggered_by="test",
        id=99999, status=AgentTaskStatus.COMPLETED.value,
    )
    assert r.ok is False
    assert "not found" in (r.error or "")


@pytest.mark.asyncio
async def test_pm_status_aggregates_by_status(orch):
    o, _ = orch
    await o.dispatch(
        "pm", "create_task", triggered_by="test",
        department="finance", title="t1", priority=10,
    )
    await o.dispatch(
        "pm", "create_task", triggered_by="test",
        department="finance", title="t2", priority=20,
    )
    r = await o.dispatch(
        "pm", "create_task", triggered_by="test",
        department="finance", title="t3-blocked", priority=100,
    )
    await o.dispatch(
        "pm", "update_task", triggered_by="test",
        id=r.data["id"], status=AgentTaskStatus.BLOCKED.value,
    )

    r = await o.dispatch("pm", "status", triggered_by="test")
    assert r.ok is True
    assert r.data["total_tasks"] == 3
    assert r.data["by_status"][AgentTaskStatus.PENDING.value] == 2
    assert r.data["by_status"][AgentTaskStatus.BLOCKED.value] == 1
    assert len(r.data["blockers"]) == 1
    assert r.data["blockers"][0]["title"] == "t3-blocked"


@pytest.mark.asyncio
async def test_pm_phases_is_read_only(orch):
    o, _ = orch
    r = await o.dispatch("pm", "phases", triggered_by="test")
    assert r.ok is True
    assert len(r.data["phases"]) >= 4
    assert r.data["phases"][0]["id"] == 1


# ---------------------------------------------------------------------------
# Finance agent: cost_summary / status (revenue_summary uses real DB which
# works with the test fixture since Revenue table is part of Base metadata)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finance_cost_summary_and_status(orch):
    o, _ = orch
    r = await o.dispatch("finance", "cost_summary", triggered_by="test")
    assert r.ok is True
    for key in (
        "daily_cost_usd",
        "daily_budget_usd",
        "daily_budget_remaining",
        "monthly_cost_usd",
        "budget_exceeded",
        "total_api_calls",
    ):
        assert key in r.data

    r = await o.dispatch("finance", "status", triggered_by="test")
    assert r.ok is True
    assert r.data["ready"] is True
    assert "budget_exceeded" in r.data


# ---------------------------------------------------------------------------
# Research + Marketing: only the DB-only tasks (trend/fortune tasks are
# exercised manually in prod — they hit external APIs we don't mock here)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_status_reads_db(orch):
    o, _ = orch
    r = await o.dispatch("research", "status", triggered_by="test")
    assert r.ok is True
    assert r.data["ready"] is True
    assert r.data["trends_24h"] == 0
    assert r.data["competitors_total"] == 0


@pytest.mark.asyncio
async def test_research_competitors_empty(orch):
    o, _ = orch
    r = await o.dispatch("research", "competitors", triggered_by="test", limit=5)
    assert r.ok is True
    assert r.data["count"] == 0
    assert r.data["competitors"] == []


@pytest.mark.asyncio
async def test_marketing_status_and_recent_posts(orch):
    o, _ = orch
    r = await o.dispatch("marketing", "status", triggered_by="test")
    assert r.ok is True
    assert r.data["ready"] is True
    assert r.data["posts_24h"] == 0

    r = await o.dispatch("marketing", "recent_posts", triggered_by="test", days=7)
    assert r.ok is True
    assert r.data["days"] == 7
    assert r.data["by_platform_status"] == []


# ---------------------------------------------------------------------------
# Secretariat: daily_report + route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_secretariat_status(orch):
    o, _ = orch
    r = await o.dispatch("secretariat", "status", triggered_by="test")
    assert r.ok is True
    assert r.data["ready"] is True
    assert set(r.data["departments"]) == {"finance", "marketing", "pm", "research", "secretariat"}


@pytest.mark.asyncio
async def test_secretariat_route_heuristic(orch):
    o, _ = orch
    cases = {
        "please post a tarot reading": "marketing",
        "revenue looks weird today": "finance",
        "research competitor astrology accounts": "research",
        "create a new task for Q2": "pm",
    }
    for req, expected in cases.items():
        r = await o.dispatch("secretariat", "route", triggered_by="test", request=req)
        assert r.ok is True
        assert r.data["routed_to"] == expected, (req, r.data)


@pytest.mark.asyncio
async def test_secretariat_daily_report_fans_out(orch):
    o, session_maker = orch
    # Seed one task so pm.list_tasks returns something in the digest
    await o.dispatch(
        "pm", "create_task", triggered_by="test",
        department="marketing", title="seed", priority=50,
    )
    await o.dispatch(
        "pm", "update_task", triggered_by="test",
        id=1, status=AgentTaskStatus.IN_PROGRESS.value,
    )

    logs_before = await _count_logs(session_maker)
    r = await o.dispatch("secretariat", "daily_report", triggered_by="test")
    assert r.ok is True

    # daily_report itself + 4 dept.status calls + finance.daily_digest
    # + pm.list_tasks = at least 7 new log rows
    logs_after = await _count_logs(session_maker)
    assert logs_after - logs_before >= 7

    data = r.data
    assert data["date"]
    assert set(data["departments"].keys()) == {"pm", "research", "marketing", "finance"}
    assert data["finance_digest"] is not None
    assert data["finance_digest"]["cost"]["daily_budget_usd"] is not None
    assert data["pm_open_tasks"] is not None
    assert data["pm_open_tasks"]["count"] == 1

    # Sub-dispatches carry the richer triggered_by tag
    async with session_maker() as session:
        tags = (
            await session.execute(
                select(AgentLog.triggered_by).where(
                    AgentLog.triggered_by == "secretariat.daily_report"
                )
            )
        ).scalars().all()
    assert len(tags) >= 6


# ---------------------------------------------------------------------------
# Sanitization: huge payloads must not break persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_kwargs_sanitized_without_raising(orch):
    o, session_maker = orch
    huge = "x" * 20000
    r = await o.dispatch(
        "pm", "create_task", triggered_by="test",
        department="pm",
        title="big" + huge,
        description=huge,
    )
    assert r.ok is True
    async with session_maker() as session:
        row = (await session.execute(select(AgentLog))).scalars().first()
    assert row is not None
    # input column stored as JSON should not be None and should have a truncated body
    assert row.input is not None
