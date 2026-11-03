"""Shared utilities for the SNS automation system."""

import asyncio
import json
import logging
import math
import random
import re
import time
from datetime import datetime
from functools import wraps
from typing import Any, Callable, TypeVar

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Active hours: 7:00-23:00 JST (shared across growth engines)
# ---------------------------------------------------------------------------
ACTIVE_HOUR_START = 7
ACTIVE_HOUR_END = 23


def is_active_hours(
    start: int = ACTIVE_HOUR_START,
    end: int = ACTIVE_HOUR_END,
    tz: str = "Asia/Tokyo",
) -> bool:
    """Return True if the current time is within the active-hours window.

    Args:
        start: First active hour (inclusive).
        end: Last active hour (inclusive).
        tz: IANA timezone name.
    """
    current_hour = datetime.now(ZoneInfo(tz)).hour
    return start <= current_hour <= end


def lognormal_delay(low: float, high: float, sigma: float = 0.5) -> float:
    """Return a random delay from a log-normal distribution clamped to [low, high].

    The median sits at the geometric mean of *low* and *high*, giving a
    human-like distribution that skews toward shorter pauses with
    occasional longer ones.
    """
    median = math.sqrt(low * high)
    mu = math.log(median)
    delay = random.lognormvariate(mu, sigma)
    return max(low, min(delay, high))


class TruncatedResponseError(ValueError):
    """Raised when Claude response is truncated due to max_tokens."""
    pass


# ---------------------------------------------------------------------------
# Retry with exponential backoff
# ---------------------------------------------------------------------------

# Exceptions that should trigger a retry (transient errors)
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = ()

def _load_retryable_exceptions() -> tuple[type[Exception], ...]:
    """Lazy-load retryable exception types from optional dependencies."""
    exceptions: list[type[Exception]] = []
    try:
        import anthropic
        exceptions.extend([
            anthropic.APIConnectionError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ])
    except ImportError:
        pass
    try:
        import httpx
        exceptions.extend([
            httpx.ConnectError,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
        ])
    except ImportError:
        pass
    try:
        import tweepy
        exceptions.append(tweepy.TooManyRequests)
    except ImportError:
        pass
    # Generic transient errors
    exceptions.extend([ConnectionError, TimeoutError, OSError])
    # Application-level retryable errors
    exceptions.append(TruncatedResponseError)
    return tuple(exceptions)


def retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
):
    """Decorator: retry a **sync** function on transient errors with exponential backoff.

    Usage::

        @retry(max_retries=3)
        def call_api():
            ...
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            retryable = _load_retryable_exceptions()
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        logger.error(
                            f"[retry] {func.__name__} failed after {max_retries + 1} attempts: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        raise
                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                    if jitter:
                        delay *= 0.5 + random.random()  # 50-150% of delay
                    logger.warning(
                        f"[retry] {func.__name__} attempt {attempt + 1}/{max_retries + 1} "
                        f"failed ({type(exc).__name__}), retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]   # unreachable but satisfies mypy
        return wrapper  # type: ignore[return-value]
    return decorator


def async_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
):
    """Decorator: retry an **async** function on transient errors with exponential backoff.

    Usage::

        @async_retry(max_retries=3)
        async def call_api():
            ...
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            retryable = _load_retryable_exceptions()
            last_exc: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except retryable as exc:
                    last_exc = exc
                    if attempt == max_retries:
                        logger.error(
                            f"[retry] {func.__name__} failed after {max_retries + 1} attempts: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        raise
                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                    if jitter:
                        delay *= 0.5 + random.random()
                    logger.warning(
                        f"[retry] {func.__name__} attempt {attempt + 1}/{max_retries + 1} "
                        f"failed ({type(exc).__name__}), retrying in {delay:.1f}s..."
                    )
                    await asyncio.sleep(delay)
            raise last_exc  # type: ignore[misc]
        return wrapper
    return decorator

# --- Compliance: 薬機法禁止ワードリスト ---
PROHIBITED_HEALTH_CLAIMS = [
    "シミが消え", "シミを消す", "シミがなくなる",
    "毛穴がなくなる", "毛穴が消え", "毛穴レス",
    "ニキビが治る", "ニキビを治す", "ニキビが消え",
    "肌が白くなる", "美白効果", "ホワイトニング効果",
    "若返る", "若返り効果", "アンチエイジング効果",
    "たるみが消え", "シワがなくなる", "シワが消え",
    "痩せる", "脂肪が燃える", "ダイエット効果",
    "バストアップ効果", "胸が大きくなる",
    "治療", "治癒", "完治", "医療効果",
]


def extract_claude_text(response) -> str:
    """Safely extract text from Claude API response with guards."""
    if not response.content:
        raise ValueError("Claude API returned empty content")
    block = response.content[0]
    if not hasattr(block, "text"):
        raise ValueError(f"Unexpected content block type: {getattr(block, 'type', 'unknown')}")
    if response.stop_reason == "max_tokens":
        logger.warning("Claude response truncated due to max_tokens limit")
        raise TruncatedResponseError(
            f"Claude response truncated (stop_reason=max_tokens). "
            f"Partial length: {len(block.text)} chars. Consider increasing max_tokens."
        )
    return block.text


def parse_llm_json(text: str) -> dict | list:
    """Safely parse JSON from LLM output."""
    if not text or not text.strip():
        raise ValueError("Empty LLM response")

    cleaned = text.strip()

    # Remove markdown code fences
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback: extract balanced JSON (match opening/closing brackets)
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        # Try all possible start positions
        search_from = 0
        while True:
            start_idx = cleaned.find(start_char, search_from)
            if start_idx == -1:
                break
            # Find matching end bracket by counting depth
            depth = 0
            for i in range(start_idx, len(cleaned)):
                if cleaned[i] == start_char:
                    depth += 1
                elif cleaned[i] == end_char:
                    depth -= 1
                if depth == 0:
                    candidate = cleaned[start_idx:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break  # Try next start position
            search_from = start_idx + 1

    raise ValueError(f"Failed to parse JSON from LLM response: {cleaned[:300]}...")


def to_generated_content(data):
    """Safely convert dict (or list of dicts) to GeneratedContent, filtering unknown fields.

    Claude occasionally returns a top-level JSON array instead of an object
    (e.g. ``[{...}]``). parse_llm_json honors whichever bracket Claude used,
    so we normalize here instead of crashing with 'list has no attribute items'.
    """
    from src.ai.claude_generator import GeneratedContent
    if isinstance(data, list):
        first_dict = next((item for item in data if isinstance(item, dict)), None)
        if first_dict is None:
            raise ValueError("LLM returned a list with no dict entries")
        data = first_dict
    if not isinstance(data, dict):
        raise ValueError(f"LLM response is not a dict: {type(data).__name__}")
    valid_fields = GeneratedContent.__dataclass_fields__
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    if "text" not in filtered:
        raise ValueError("LLM response missing required 'text' field")
    if "hashtags" not in filtered:
        filtered["hashtags"] = []
    if "keywords" not in filtered:
        filtered["keywords"] = []
    return GeneratedContent(**filtered)


def validate_platform_content(text: str, platform: str) -> tuple[bool, str]:
    """Check content against platform-specific content limits.

    Returns (is_safe, reason). If not safe, reason explains why.
    """
    from config.persona import PERSONA

    content_limits = PERSONA.get("platform_content_limits", {}).get(platform, {})
    banned_keywords = content_limits.get("banned", "").lower().split("、")

    text_lower = text.lower()
    for keyword in banned_keywords:
        keyword = keyword.strip()
        if keyword and keyword in text_lower:
            return False, f"Banned content for {platform}: '{keyword}'"

    # Check global banned topics
    for topic in PERSONA.get("banned_topics", []):
        # Extract key phrases from the topic description
        key_phrases = [p.strip() for p in topic.split("（")[0].split("・")]
        for phrase in key_phrases:
            if len(phrase) >= 3 and phrase.lower() in text_lower:
                return False, f"Banned topic: '{phrase}'"

    return True, ""


def validate_compliance(text: str, is_pr: bool = False) -> tuple[str, list[str]]:
    """Validate and fix generated text for 薬機法/ステマ規制 compliance.

    Returns (fixed_text, list_of_warnings).
    """
    warnings = []

    # 1. Check for prohibited health claims (薬機法)
    for phrase in PROHIBITED_HEALTH_CLAIMS:
        if phrase in text:
            warnings.append(f"薬機法違反の可能性: '{phrase}' を検出。削除しました。")
            text = text.replace(phrase, "")

    # 2. Force PR disclosure at the beginning (ステマ規制)
    if is_pr:
        pr_markers = ["【広告】", "【PR】", "【プロモーション】"]
        has_marker = any(text.startswith(marker) for marker in pr_markers)
        if not has_marker:
            text = "【PR】" + text
            warnings.append("PR表記を冒頭に自動挿入しました（景品表示法対応）")

    return text, warnings


class BoundedSet:
    """Fixed-capacity set with FIFO eviction. Tracks recently-seen IDs
    without growing unbounded for the lifetime of the process.

    Used by the comment-reply dedup trackers to avoid unbounded memory
    growth (auto_poster.py and youtube_growth.py both held the full history
    forever — see 2026-04-20 code review item #4).
    """

    __slots__ = ("_order", "_capacity")

    def __init__(self, capacity: int = 5000):
        from collections import OrderedDict
        self._capacity = capacity
        self._order: "OrderedDict[str, None]" = OrderedDict()

    def __contains__(self, item: str) -> bool:
        return item in self._order

    def add(self, item: str) -> None:
        if item in self._order:
            self._order.move_to_end(item)
            return
        self._order[item] = None
        while len(self._order) > self._capacity:
            self._order.popitem(last=False)

    def __len__(self) -> int:
        return len(self._order)


def add_utm_params(url: str, post_id: int | str, platform: str, content_type: str = "") -> str:
    """Add UTM tracking parameters to a URL for revenue attribution."""
    from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.update({
        "utm_source": [platform],
        "utm_medium": ["sns_auto"],
        "utm_campaign": [f"post_{post_id}"],
    })
    if content_type:
        params["utm_content"] = [content_type]

    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))
