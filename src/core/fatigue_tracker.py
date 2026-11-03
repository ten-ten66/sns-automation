"""Content fatigue prevention - tracks recently used content patterns."""

import logging
import threading
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class FatigueTracker:
    """Track recently used content types, hooks, and prompts to prevent repetition."""

    def __init__(self, max_history: int = 100):
        self._lock = threading.Lock()
        self._content_types: deque[tuple[str, str, datetime]] = deque(maxlen=max_history)  # (platform, type, time)
        self._hooks_used: deque[str] = deque(maxlen=50)
        self._image_prompts_used: deque[str] = deque(maxlen=30)

    def record_content(self, platform: str, content_type: str):
        with self._lock:
            self._content_types.append((platform, content_type, datetime.now(timezone.utc)))

    def record_hook(self, hook: str):
        with self._lock:
            self._hooks_used.append(hook)

    def record_image_prompt(self, prompt_key: str):
        with self._lock:
            self._image_prompts_used.append(prompt_key)

    def get_recently_used_types(self, platform: str, last_n: int = 5) -> list[str]:
        """Get the last N content types used for a platform."""
        with self._lock:
            platform_types = [ct for p, ct, _ in self._content_types if p == platform]
            return platform_types[-last_n:]

    def should_avoid_type(self, platform: str, content_type: str) -> bool:
        """Returns True if this content type was used in the last 2 posts for this platform."""
        recent = self.get_recently_used_types(platform, last_n=2)
        return recent.count(content_type) >= 2

    def get_avoid_hooks(self, last_n: int = 10) -> list[str]:
        """Get hooks to avoid (recently used)."""
        with self._lock:
            return list(self._hooks_used)[-last_n:]

    def get_avoid_prompts(self, last_n: int = 5) -> list[str]:
        """Get image prompt keys to avoid."""
        with self._lock:
            return list(self._image_prompts_used)[-last_n:]


# Global singleton
fatigue_tracker = FatigueTracker()
