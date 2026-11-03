"""Base platform interface."""

import asyncio
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass


class RateLimiter:
    """Platform-aware rate limiter with sliding window."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Wait until a request slot is available."""
        async with self._lock:
            now = time.monotonic()
            # Remove expired timestamps
            while self._timestamps and self._timestamps[0] <= now - self.window_seconds:
                self._timestamps.popleft()

            if len(self._timestamps) >= self.max_requests:
                wait_time = self._timestamps[0] + self.window_seconds - now
                await asyncio.sleep(wait_time)
                # Clean again after wait
                now = time.monotonic()
                while self._timestamps and self._timestamps[0] <= now - self.window_seconds:
                    self._timestamps.popleft()

            self._timestamps.append(time.monotonic())


@dataclass
class PostResult:
    success: bool
    platform: str
    post_id: str | None = None
    url: str | None = None
    error: str | None = None


class BasePlatform(ABC):
    """Abstract base class for SNS platform integrations."""

    rate_limiter: RateLimiter | None = None

    @property
    @abstractmethod
    def platform_name(self) -> str:
        ...

    @abstractmethod
    async def post_text(self, text: str, **kwargs) -> PostResult:
        ...

    @abstractmethod
    async def post_image(self, text: str, image_path: str, **kwargs) -> PostResult:
        ...

    @abstractmethod
    async def post_video(self, text: str, video_path: str, **kwargs) -> PostResult:
        ...

    @abstractmethod
    async def get_metrics(self, post_id: str) -> dict:
        ...

    @abstractmethod
    async def get_follower_count(self) -> int:
        ...

    async def delete_post(self, post_id: str) -> bool:
        raise NotImplementedError

    async def get_comments(self, post_id: str) -> list[dict]:
        raise NotImplementedError

    async def post_reply(self, post_id: str, comment_id: str, text: str, **kwargs) -> PostResult:
        """Reply to a comment. Override in subclasses that support it."""
        raise NotImplementedError(f"{self.platform_name} does not support post_reply")
