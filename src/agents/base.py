"""Base class and data types for department agents."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Standard return value from any agent task."""

    agent: str
    task: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: datetime = field(default_factory=datetime.utcnow)
    finished_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "task": self.task,
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class AgentBase:
    """Base class that every department agent extends.

    Subclasses implement `handle(task, **kwargs)` and declare `name`.
    """

    name: str = "base"

    def __init__(self):
        self.log = logging.getLogger(f"agents.{self.name}")

    async def handle(self, task: str, **kwargs: Any) -> AgentResult:
        """Handle one task. Override in subclasses.

        Args:
            task: Task name (agent-specific vocabulary)
            **kwargs: Task arguments
        """
        raise NotImplementedError(
            f"Agent {self.name} has no handler for task '{task}'"
        )

    async def run(self, task: str, **kwargs: Any) -> AgentResult:
        """Public entry — wraps handle() with timing and error capture.

        Trusts whatever ``ok`` value ``handle()`` returns; only forces
        ``ok=False`` when an exception propagates out of ``handle()``.
        """
        result = AgentResult(agent=self.name, task=task, ok=False)
        try:
            result = await self.handle(task, **kwargs)
        except NotImplementedError as exc:
            result.error = str(exc)
            self.log.warning("unsupported task: %s", task)
        except Exception as exc:  # noqa: BLE001
            result.error = f"{type(exc).__name__}: {exc}"
            self.log.exception("agent %s failed on task %s", self.name, task)
        result.finished_at = datetime.utcnow()
        return result
