"""API usage tracking and budget management."""

import asyncio
import json
import logging
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """Raised when daily API budget is exceeded."""
    pass


@dataclass
class APIUsageRecord:
    """Single API call record."""
    service: str  # "claude", "kie"
    method: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)


class CostTracker:
    """Track API usage and enforce budget limits."""

    # Pricing (USD per 1M tokens for Claude, USD per unit for Kie.ai)
    PRICING = {
        "claude_sonnet_input": 3.0,
        "claude_sonnet_output": 15.0,
        "claude_haiku_input": 0.25,
        "claude_haiku_output": 1.25,
        "kie_nano_banana_pro_image": 0.12,   # per image (~24 credits at $0.005/credit)
        "kie_nano_banana_2_image": 0.06,     # per image (cheaper model)
        "kie_veo3_quality": 2.00,            # per 8sec video
        "kie_veo3_fast": 0.40,               # per 8sec video
    }

    STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "cost_tracker_state.json")

    def __init__(self, daily_budget_usd: float = 20.0, monthly_budget_usd: float = 250.0):
        self.daily_budget_usd = daily_budget_usd
        self.monthly_budget_usd = monthly_budget_usd
        self._records: deque[APIUsageRecord] = deque(maxlen=10000)
        self._lock = Lock()
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._total_cost_usd = 0.0
        self._daily_cost_usd = 0.0
        self._daily_reset_date = ""
        self._monthly_cost_usd = 0.0
        self._monthly_reset_month = ""
        self._event_loop: Any = None
        self._last_persist_time = 0.0
        self._load_state()

    def record_claude_usage(self, method: str, input_tokens: int, output_tokens: int):
        """Record a Claude API call."""
        cost = (
            input_tokens * self.PRICING["claude_sonnet_input"] / 1_000_000
            + output_tokens * self.PRICING["claude_sonnet_output"] / 1_000_000
        )
        self._record("claude", method, input_tokens, output_tokens, cost)

    def record_kie_image_usage(self, method: str, model: str = "nano_banana_pro"):
        """Record a Kie.ai image generation API call."""
        price_key = f"kie_{model}_image"
        cost = self.PRICING.get(price_key, self.PRICING["kie_nano_banana_pro_image"])
        self._record("kie", method, 0, 0, cost)

    def record_kie_video_usage(self, method: str, model: str = "veo3_fast"):
        """Record a Kie.ai video generation API call."""
        price_key = f"kie_{model}"
        cost = self.PRICING.get(price_key, self.PRICING["kie_veo3_fast"])
        self._record("kie", method, 0, 0, cost)

    def _maybe_reset_counters_locked(self) -> None:
        """Reset daily/monthly counters if the calendar day/month has changed.

        Caller MUST hold ``self._lock``. Separated out so that both write paths
        (``_record``) and read paths (``is_budget_exceeded``, ``get_summary``)
        can keep the rolling counters in sync with wall-clock time — otherwise
        a process that loaded a stale ``daily_reset_date`` from disk and then
        only does reads would think the budget is still exceeded even after
        midnight.
        """
        today = time.strftime("%Y-%m-%d")
        this_month = time.strftime("%Y-%m")
        if self._daily_reset_date != today:
            self._daily_cost_usd = 0.0
            self._daily_reset_date = today
        if self._monthly_reset_month != this_month:
            self._monthly_cost_usd = 0.0
            self._monthly_reset_month = this_month

    def _record(self, service: str, method: str, inp: int, out: int, cost: float):
        with self._lock:
            self._maybe_reset_counters_locked()
            self._total_input_tokens += inp
            self._total_output_tokens += out
            self._total_cost_usd += cost
            self._daily_cost_usd += cost
            self._monthly_cost_usd += cost

            self._records.append(APIUsageRecord(
                service=service, method=method,
                input_tokens=inp, output_tokens=out, cost_usd=cost,
            ))

            # Persist state (debounced: max once per 60 sec)
            now = time.time()
            if now - self._last_persist_time > 60:
                self._persist_state()
                self._last_persist_time = now

            # Budget alerts
            if self._daily_cost_usd > self.daily_budget_usd:
                logger.critical(
                    f"BUDGET ALERT: Daily cost ${self._daily_cost_usd:.2f} "
                    f"exceeds limit ${self.daily_budget_usd:.2f}"
                )
                self._send_budget_alert(self._daily_cost_usd, self.daily_budget_usd)
            if self._monthly_cost_usd > self.monthly_budget_usd:
                logger.critical(
                    f"BUDGET ALERT: Monthly cost ${self._monthly_cost_usd:.2f} "
                    f"exceeds limit ${self.monthly_budget_usd:.2f}"
                )

    def _load_state(self):
        """Load persisted cost state from disk."""
        try:
            if os.path.exists(self.STATE_FILE):
                with open(self.STATE_FILE) as f:
                    state = json.load(f)
                self._daily_cost_usd = state.get("daily_cost_usd", 0.0)
                self._daily_reset_date = state.get("daily_reset_date", "")
                self._monthly_cost_usd = state.get("monthly_cost_usd", 0.0)
                self._monthly_reset_month = state.get("monthly_reset_month", "")
                self._total_cost_usd = state.get("total_cost_usd", 0.0)
                self._total_input_tokens = state.get("total_input_tokens", 0)
                self._total_output_tokens = state.get("total_output_tokens", 0)
                logger.info("Cost tracker state loaded: daily=$%.2f monthly=$%.2f",
                            self._daily_cost_usd, self._monthly_cost_usd)
        except Exception as e:
            logger.warning("Failed to load cost tracker state: %s", e)

    def _persist_state(self):
        """Save cost state to disk using an atomic temp-file + rename."""
        try:
            state_dir = os.path.dirname(self.STATE_FILE) or "."
            os.makedirs(state_dir, exist_ok=True)
            state = {
                "daily_cost_usd": self._daily_cost_usd,
                "daily_reset_date": self._daily_reset_date,
                "monthly_cost_usd": self._monthly_cost_usd,
                "monthly_reset_month": self._monthly_reset_month,
                "total_cost_usd": self._total_cost_usd,
                "total_input_tokens": self._total_input_tokens,
                "total_output_tokens": self._total_output_tokens,
            }
            tmp_fd, tmp_path = tempfile.mkstemp(dir=state_dir)
            try:
                with os.fdopen(tmp_fd, "w") as f:
                    json.dump(state, f)
                os.replace(tmp_path, self.STATE_FILE)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning("Failed to persist cost tracker state: %s", e)

    def set_event_loop(self, loop):
        """Store a reference to the main event loop for thread-safe alert dispatch."""
        self._event_loop = loop

    def _send_budget_alert(self, daily_cost: float, limit: float):
        """Send budget alert from any thread context."""
        try:
            from src.core.alerts import alert_service
            # Try to get the running loop (works in async context)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(alert_service.notify_budget_exceeded(daily_cost, limit))
            except RuntimeError:
                # No running loop (called from thread) — use stored loop reference
                if self._event_loop and not self._event_loop.is_closed():
                    self._event_loop.call_soon_threadsafe(
                        self._event_loop.create_task,
                        alert_service.notify_budget_exceeded(daily_cost, limit),
                    )
        except Exception:
            pass  # Alert failure should never break cost tracking

    def get_summary(self) -> dict:
        """Get usage summary."""
        with self._lock:
            self._maybe_reset_counters_locked()
            return {
                "total_input_tokens": self._total_input_tokens,
                "total_output_tokens": self._total_output_tokens,
                "total_cost_usd": round(self._total_cost_usd, 4),
                "daily_cost_usd": round(self._daily_cost_usd, 4),
                "monthly_cost_usd": round(self._monthly_cost_usd, 4),
                "daily_budget_usd": self.daily_budget_usd,
                "monthly_budget_usd": self.monthly_budget_usd,
                "daily_budget_remaining": round(self.daily_budget_usd - self._daily_cost_usd, 4),
                "monthly_budget_remaining": round(self.monthly_budget_usd - self._monthly_cost_usd, 4),
                "total_api_calls": len(self._records),
                "budget_exceeded": self._daily_cost_usd > self.daily_budget_usd,
            }

    def is_budget_exceeded(self) -> bool:
        with self._lock:
            self._maybe_reset_counters_locked()
            return self._daily_cost_usd > self.daily_budget_usd


# Global singleton
cost_tracker = CostTracker()
