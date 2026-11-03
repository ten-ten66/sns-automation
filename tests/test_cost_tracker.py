"""Tests for the cost tracking module."""

from unittest.mock import patch
from src.core.cost_tracker import CostTracker


class TestCostTracker:
    """Test API cost tracking and budget management."""

    def _make_tracker(self, **kwargs) -> CostTracker:
        """Create an isolated CostTracker that does not load persisted state."""
        with patch.object(CostTracker, "_load_state"):
            return CostTracker(**kwargs)

    def test_initial_state(self):
        tracker = self._make_tracker(daily_budget_usd=10.0, monthly_budget_usd=250.0)
        summary = tracker.get_summary()
        assert summary["total_cost_usd"] == 0.0
        assert summary["total_api_calls"] == 0
        assert summary["budget_exceeded"] is False

    def test_record_claude_usage(self):
        tracker = self._make_tracker()
        tracker.record_claude_usage("test_method", input_tokens=1000, output_tokens=500)
        summary = tracker.get_summary()
        assert summary["total_input_tokens"] == 1000
        assert summary["total_output_tokens"] == 500
        assert summary["total_cost_usd"] > 0
        assert summary["total_api_calls"] == 1

    def test_record_kie_image_usage(self):
        tracker = self._make_tracker()
        tracker.record_kie_image_usage("test_image", model="nano_banana_pro")
        summary = tracker.get_summary()
        assert summary["total_cost_usd"] == 0.12
        assert summary["total_api_calls"] == 1

    def test_kie_image_nano_banana_2_pricing(self):
        tracker = self._make_tracker()
        tracker.record_kie_image_usage("test_image", model="nano_banana_2")
        summary = tracker.get_summary()
        assert summary["total_cost_usd"] == 0.06

    def test_record_kie_video_usage(self):
        tracker = self._make_tracker()
        tracker.record_kie_video_usage("test_video", model="veo3_fast")
        summary = tracker.get_summary()
        assert summary["total_cost_usd"] == 0.40
        assert summary["total_api_calls"] == 1

    def test_budget_exceeded(self):
        tracker = self._make_tracker(daily_budget_usd=0.001)
        tracker.record_claude_usage("test", input_tokens=100000, output_tokens=50000)
        assert tracker.is_budget_exceeded() is True
        summary = tracker.get_summary()
        assert summary["budget_exceeded"] is True

    def test_budget_not_exceeded(self):
        tracker = self._make_tracker(daily_budget_usd=100.0)
        tracker.record_claude_usage("test", input_tokens=100, output_tokens=50)
        assert tracker.is_budget_exceeded() is False

    def test_multiple_records(self):
        tracker = self._make_tracker()
        tracker.record_claude_usage("method1", 500, 200)
        tracker.record_claude_usage("method2", 300, 100)
        tracker.record_kie_image_usage("image1")
        summary = tracker.get_summary()
        assert summary["total_api_calls"] == 3
        assert summary["total_input_tokens"] == 800
        assert summary["total_output_tokens"] == 300

    def test_daily_budget_remaining(self):
        tracker = self._make_tracker(daily_budget_usd=10.0)
        tracker.record_claude_usage("test", 1000, 500)
        summary = tracker.get_summary()
        cost = summary["daily_cost_usd"]
        assert summary["daily_budget_remaining"] == round(10.0 - cost, 4)
