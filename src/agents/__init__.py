"""5-department agent system for HoshimiLive / Miku Oracle service.

Departments:
- Secretariat: task routing, daily reports, inter-agent coordination
- PM: phase/progress tracking, task queue management
- Research: trend research, competitor analysis (reuses TrendResearcher)
- Marketing: SNS post generation, LP suggestions (reuses ClaudeContentGenerator)
- Finance: revenue aggregation, cost tracking (reuses cost_tracker + revenue_tracker)

The Orchestrator dispatches work to the appropriate agent.
"""

from src.agents.base import AgentBase, AgentResult
from src.agents.orchestrator import Orchestrator

__all__ = ["AgentBase", "AgentResult", "Orchestrator"]
