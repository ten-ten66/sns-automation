"""Smart scheduling that uses feedback loop data for optimal timing."""

import logging

logger = logging.getLogger(__name__)

# Fallback optimal times if no data available
DEFAULT_OPTIMAL_TIMES = {
    "x": ["07:30", "12:15", "18:00", "21:30", "23:00"],
    "instagram": ["07:00", "12:00", "18:00", "21:00", "23:00"],
    "tiktok": ["07:00", "12:00", "19:00", "22:00"],
    "youtube": ["19:00", "21:00"],
}


def get_optimal_times(platform: str, feedback_hints: dict | None = None) -> list[str]:
    """Get optimal posting times, preferring feedback data over defaults.

    Args:
        platform: Platform name
        feedback_hints: Output from feedback_loop.get_optimization_hints()
    """
    if feedback_hints:
        # Use best_posting_hours from actual performance data
        best_hours = feedback_hints.get("best_posting_hours", [])
        if best_hours:
            times = [f"{h['hour']:02d}:00" for h in best_hours[:6]]
            if times:
                logger.info("Using data-driven posting times for %s: %s", platform, times)
                return times

        # Also check CTR hours for monetization content
        best_ctr_hours = feedback_hints.get("best_ctr_hours", [])
        if best_ctr_hours:
            logger.info("CTR-optimal hours for %s: %s", platform,
                       [f"{h['hour']}:00" for h in best_ctr_hours[:3]])

    return DEFAULT_OPTIMAL_TIMES.get(platform, ["12:00", "18:00", "21:00"])


def get_ctr_optimal_times(feedback_hints: dict | None = None) -> list[str]:
    """Get times when CTR is highest - use for monetization/affiliate posts."""
    if feedback_hints:
        best_ctr = feedback_hints.get("best_ctr_hours", [])
        if best_ctr:
            return [f"{h['hour']:02d}:00" for h in best_ctr[:3]]
    return ["21:00", "22:00", "23:00"]  # Default: evening for monetization
