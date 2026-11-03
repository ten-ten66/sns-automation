"""Anti-ban safety system: warm-up, daily limits, shadow ban detection, emergency stop, jitter, duplicate detection."""

import asyncio
import hashlib
import logging
import math
import random
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


class WarmupManager:
    """Gradually increase posting frequency for new accounts."""

    # Week number -> max posts per day per platform
    WARMUP_SCHEDULE = {
        1: 5,    # Week 1: 5 posts/day
        2: 10,   # Week 2: 10 posts/day
        3: 20,   # Week 3: 20 posts/day
        4: 30,   # Week 4: 30 posts/day
        5: 40,   # Week 5: 40 posts/day
        6: 50,   # Week 6+: full speed
    }

    def __init__(self, start_date: datetime | None = None):
        """
        Args:
            start_date: When automation started. If None, assumes fully warmed up.
        """
        self.start_date = start_date

    def get_max_posts_today(self, platform: str) -> int:
        """Get the max allowed posts for today based on warm-up stage."""
        if not self.start_date:
            return 999  # No warm-up, unlimited

        days_active = (datetime.now(timezone.utc) - self.start_date).days
        week = (days_active // 7) + 1

        for week_num in sorted(self.WARMUP_SCHEDULE.keys(), reverse=True):
            if week >= week_num:
                return self.WARMUP_SCHEDULE[week_num]
        return 1

    def is_warmed_up(self) -> bool:
        if not self.start_date:
            return True
        days_active = (datetime.now(timezone.utc) - self.start_date).days
        return days_active >= 35  # 5 weeks


class DailyActionTracker:
    """Track daily action counts per platform to stay under soft caps."""

    # Safe daily limits per platform
    # IMPORTANT: These are CONSERVATIVE limits for new accounts (< 3 months).
    # Increase gradually as accounts mature and warm up.
    # X Basic plan: ~17 tweets/day (posts + replies combined)
    # IG new account: ~50 total actions/day to avoid action blocks
    DAILY_LIMITS = {
        "x": {"posts": 10, "replies": 7, "likes": 50},
        "instagram": {"posts": 5, "stories": 5, "replies": 10, "likes": 30, "follows": 10},
        "tiktok": {"posts": 5, "replies": 10, "likes": 30},
        "youtube": {"posts": 3, "replies": 10},
    }

    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._reset_date: str = ""

    def _maybe_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._reset_date != today:
            self._counts.clear()
            self._reset_date = today

    def record_action(self, platform: str, action: str = "posts"):
        with self._lock:
            self._maybe_reset()
            self._counts[platform][action] += 1

    def can_perform(self, platform: str, action: str = "posts") -> bool:
        with self._lock:
            self._maybe_reset()
            limits = self.DAILY_LIMITS.get(platform, {})
            limit = limits.get(action, 999)
            return self._counts[platform][action] < limit

    def get_remaining(self, platform: str, action: str = "posts") -> int:
        with self._lock:
            self._maybe_reset()
            limits = self.DAILY_LIMITS.get(platform, {})
            limit = limits.get(action, 999)
            return max(0, limit - self._counts[platform][action])

    def get_count(self, platform: str, action: str = "posts") -> int:
        with self._lock:
            self._maybe_reset()
            return self._counts[platform][action]

    def get_summary(self) -> dict:
        with self._lock:
            self._maybe_reset()
            summary = {}
            for platform, limits in self.DAILY_LIMITS.items():
                summary[platform] = {}
                for action, limit in limits.items():
                    used = self._counts[platform][action]
                    summary[platform][action] = {"used": used, "limit": limit, "remaining": limit - used}
            return summary


class ShadowBanDetector:
    """Detect potential shadow bans by monitoring engagement drops."""

    # Absolute floor below which rolling averages are meaningless. A baseline
    # of 0.001 or less means there's effectively no signal (typical for new
    # accounts or platforms with mostly-failed posts), and the relative drop
    # comparison produces spurious alerts.
    _MIN_MEANINGFUL_AVG = 0.001
    # Suppress repeat alerts for the same platform within this many seconds.
    _ALERT_COOLDOWN_SEC = 6 * 3600

    def __init__(self, drop_threshold: float = 0.5, consecutive_required: int = 3):
        """
        Args:
            drop_threshold: Flag if engagement drops below this fraction of the rolling average.
            consecutive_required: Number of consecutive low-engagement posts before flagging.
        """
        self.drop_threshold = drop_threshold
        self.consecutive_required = consecutive_required
        self._lock = threading.Lock()
        self._engagement_history: dict[str, list[float]] = defaultdict(list)
        self._consecutive_low: dict[str, int] = defaultdict(int)
        self._flagged: dict[str, bool] = defaultdict(bool)
        self._last_alert_at: dict[str, float] = {}

    def record_engagement(self, platform: str, engagement_rate: float):
        """Record engagement for a published post."""
        with self._lock:
            history = self._engagement_history[platform]
            history.append(engagement_rate)

            # Keep last 30 data points
            if len(history) > 30:
                history.pop(0)

            # Need at least 7 data points for comparison
            if len(history) < 7:
                return

            # Compare current to rolling average of last 7 (excluding current)
            past = history[-8:-1] if len(history) > 7 else history[:-1]
            rolling_avg = sum(past) / len(past)

            # Skip the comparison entirely when the baseline is too small to
            # be meaningful — otherwise a 0.0007 average produces constant
            # "shadow ban suspected" noise for accounts with almost no signal.
            if rolling_avg < self._MIN_MEANINGFUL_AVG:
                self._consecutive_low[platform] = 0
                return

            if engagement_rate < rolling_avg * self.drop_threshold:
                self._consecutive_low[platform] += 1
                if self._consecutive_low[platform] >= self.consecutive_required:
                    self._flagged[platform] = True
                    import time as _time
                    now = _time.time()
                    last = self._last_alert_at.get(platform, 0)
                    if now - last >= self._ALERT_COOLDOWN_SEC:
                        self._last_alert_at[platform] = now
                        logger.warning(
                            "SHADOW BAN SUSPECTED: %s — engagement %.4f vs avg %.4f (%d consecutive low posts)",
                            platform, engagement_rate, rolling_avg, self._consecutive_low[platform],
                        )
            else:
                self._consecutive_low[platform] = 0
                self._flagged[platform] = False

    def is_flagged(self, platform: str) -> bool:
        with self._lock:
            return self._flagged.get(platform, False)

    def clear_flag(self, platform: str):
        with self._lock:
            self._flagged[platform] = False
            self._consecutive_low[platform] = 0


class PlatformCooldown:
    """Per-platform cooldown — temporarily skip a single platform without tripping emergency stop.

    Used when a platform-specific quota error (e.g. YouTube uploadLimitExceeded) is detected:
    we don't want to halt the entire system, just pause that one platform until the quota resets.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cooldowns: dict[str, datetime] = {}
        self._reasons: dict[str, str] = {}

    def set_cooldown(self, platform: str, until: datetime, reason: str = ""):
        """Place a platform on cooldown until the given UTC timestamp."""
        with self._lock:
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            self._cooldowns[platform] = until
            self._reasons[platform] = reason
        logger.warning(
            "Platform cooldown set: %s until %s (%s)",
            platform, until.isoformat(), reason or "no reason given",
        )

    def cooldown_until_utc_midnight(self, platform: str, reason: str = ""):
        """Convenience: cool down until 00:00 UTC of the next day (when daily quotas usually reset)."""
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        self.set_cooldown(platform, tomorrow, reason)

    def cooldown_until_next_utc_hour(self, platform: str, target_hour_utc: int, reason: str = ""):
        """Cool down until the next time the UTC clock reaches `target_hour_utc:00:00`.

        Useful for services whose daily quota resets at a non-UTC-midnight wall time.
        For example, YouTube Data API resets at 00:00 Pacific Time, which is 08:00 UTC,
        so call ``cooldown_until_next_utc_hour("youtube", 8)``.
        """
        if not 0 <= target_hour_utc <= 23:
            raise ValueError(f"target_hour_utc must be 0-23, got {target_hour_utc}")
        now = datetime.now(timezone.utc)
        target = now.replace(hour=target_hour_utc, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        self.set_cooldown(platform, target, reason)

    def is_on_cooldown(self, platform: str) -> bool:
        """True iff the platform is currently on cooldown (auto-expires past `until`)."""
        with self._lock:
            until = self._cooldowns.get(platform)
            if until is None:
                return False
            if datetime.now(timezone.utc) >= until:
                # Expired — clean up
                self._cooldowns.pop(platform, None)
                self._reasons.pop(platform, None)
                return False
            return True

    def clear(self, platform: str):
        """Manually clear a platform's cooldown."""
        with self._lock:
            self._cooldowns.pop(platform, None)
            self._reasons.pop(platform, None)
        logger.info("Platform cooldown cleared: %s", platform)

    def get_status(self) -> dict:
        with self._lock:
            return {
                platform: {
                    "until": until.isoformat(),
                    "reason": self._reasons.get(platform, ""),
                }
                for platform, until in self._cooldowns.items()
            }


class EmergencyStop:
    """Global emergency stop for all posting. Thread-safe via threading primitives."""

    def __init__(self, cooldown_minutes: int = 30):
        self._lock = threading.RLock()  # RLock: is_stopped→_auto_recover re-entrancy
        self._running = threading.Event()
        self._running.set()  # Start in "running" state (event is set = running)
        self._reason: str = ""
        self._stopped_at: datetime | None = None
        self._stopped_at_mono: float | None = None  # monotonic timestamp for cooldown
        self._consecutive_failures: dict[str, int] = defaultdict(int)
        self.auto_stop_threshold = 3  # Auto-stop after N consecutive failures
        self.cooldown_minutes = cooldown_minutes

    def _send_alert(self, message: str, level: str = "error"):
        """Send an alert via alert_service without blocking. Fire-and-forget."""
        try:
            from src.core.alerts import alert_service
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(alert_service._send(message, level))
            except RuntimeError:
                # No running event loop (e.g. called from a non-async thread).
                # Best-effort: start a tiny event loop just for the alert.
                asyncio.run(alert_service._send(message, level))
        except Exception:
            pass  # Alert failure must never suppress the stop logic

    def activate(self, reason: str = "Manual emergency stop"):
        """Activate emergency stop."""
        with self._lock:
            self._running.clear()  # Clear = stopped
            self._reason = reason
            self._stopped_at = datetime.now(timezone.utc)
            self._stopped_at_mono = time.monotonic()
        logger.critical("EMERGENCY STOP ACTIVATED: %s", reason)
        self._send_alert(
            f"EMERGENCY STOP 発動: {reason}\n{self.cooldown_minutes}分後に自動回復します。",
            "error",
        )

    def deactivate(self):
        """Resume operations."""
        with self._lock:
            self._running.set()
            self._reason = ""
            self._stopped_at = None
            self._stopped_at_mono = None
            self._consecutive_failures.clear()
        logger.info("Emergency stop deactivated. Operations resumed.")

    def _auto_recover(self):
        """Reset the stop flag after cooldown expires. Called from is_stopped()."""
        with self._lock:
            self._running.set()
            recovered_reason = self._reason
            self._reason = ""
            self._stopped_at = None
            self._stopped_at_mono = None
            self._consecutive_failures.clear()
        logger.info(
            "Emergency stop auto-recovered after %d min cooldown (was: %s)",
            self.cooldown_minutes,
            recovered_reason,
        )
        self._send_alert(
            f"EMERGENCY STOP 自動回復: {self.cooldown_minutes}分のクールダウン後に投稿を再開します。\n"
            f"(元の理由: {recovered_reason})",
            "warning",
        )

    def is_stopped(self) -> bool:
        # Must hold the lock for the whole check: without it, two threads can
        # both observe a stopped state, race into _auto_recover, and fire
        # duplicate Discord alerts. RLock makes the re-entry into _auto_recover
        # (which re-acquires) safe.
        with self._lock:
            if not self._running.is_set() and self._stopped_at_mono is not None:
                elapsed_minutes = (time.monotonic() - self._stopped_at_mono) / 60
                if elapsed_minutes >= self.cooldown_minutes:
                    self._auto_recover()
                    return False
            return not self._running.is_set()

    def record_failure(self, platform: str):
        """Record a posting failure. Auto-triggers stop after threshold."""
        with self._lock:
            self._consecutive_failures[platform] += 1
            count = self._consecutive_failures[platform]
        if count >= self.auto_stop_threshold:
            self.activate(
                f"Auto-triggered: {count} consecutive failures on {platform}"
            )

    def record_success(self, platform: str):
        """Reset failure counter on success."""
        with self._lock:
            self._consecutive_failures[platform] = 0

    def get_status(self) -> dict:
        with self._lock:
            return {
                "stopped": self.is_stopped(),
                "reason": self._reason,
                "stopped_at": self._stopped_at.isoformat() if self._stopped_at else None,
                "consecutive_failures": dict(self._consecutive_failures),
                "cooldown_minutes": self.cooldown_minutes,
            }


class PostingJitter:
    """Add human-like randomness to posting times."""

    @staticmethod
    def add_jitter(base_time: datetime, min_minutes: int = -15, max_minutes: int = 15) -> datetime:
        """Add random jitter to a posting time using Gaussian distribution."""
        # Gaussian centered at 0 with std dev of ~7 minutes
        jitter_minutes = random.gauss(0, 7)
        jitter_minutes = max(min_minutes, min(max_minutes, jitter_minutes))
        return base_time + timedelta(minutes=jitter_minutes)

    @staticmethod
    def action_delay(action_type: str = "post") -> float:
        """Get a human-like delay in seconds for an action type."""
        delays = {
            "post": (10, 45),           # Writing a caption
            "story": (3, 15),           # Quick story post
            "carousel": (15, 60),       # Multiple images need time
            "reply": (5, 30),           # Reading + typing reply
            "like": (1, 5),             # Quick tap
            "thread_tweet": (30, 120),  # Typing next tweet in thread
        }
        min_delay, max_delay = delays.get(action_type, (5, 30))
        # Log-normal distribution for more human-like timing
        mean = (min_delay + max_delay) / 2
        log_mean = math.log(mean)
        return max(min_delay, min(max_delay, random.lognormvariate(log_mean, 0.5)))


class DuplicateDetector:
    """Detect and prevent duplicate or near-duplicate content.

    State is rehydrated from the DB on first use so that a systemd
    restart does not wipe the dedup history. Previously the in-memory
    set was lost on every restart, which is why the same caption ended
    up being published 11+ times on YouTube before 2026-04-20.
    """

    def __init__(self, similarity_threshold: float = 0.8, lookback_days: int = 7):
        self.similarity_threshold = similarity_threshold
        self.lookback_days = lookback_days
        self._lock = threading.Lock()
        self._recent_hashes: list[tuple[str, str, datetime]] = []
        self._loaded_from_db = False

    def _normalize(self, text: str) -> str:
        """Normalize text for comparison."""
        normalized = re.sub(r'[#@]\S+', '', text.lower())
        normalized = re.sub(r'\s+', ' ', normalized).strip()
        return normalized

    def _text_hash(self, text: str) -> str:
        """Generate a normalized hash of text content."""
        normalized = self._normalize(text)
        return hashlib.md5(normalized.encode()).hexdigest()

    def _cleanup_old(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        self._recent_hashes = [
            (h, t, ts) for h, t, ts in self._recent_hashes if ts > cutoff
        ]

    def _lazy_load_from_db(self):
        """Rehydrate the dedup set from recently-published rows. Called on
        first use to avoid touching the DB at module import time.
        """
        if self._loaded_from_db:
            return
        self._loaded_from_db = True
        try:
            import sqlite3
            from config.settings import settings
            # Extract the path from DATABASE_URL like sqlite+aiosqlite:///data/sns_automation.db
            db_url = getattr(settings, "database_url", "")
            if "sqlite" not in db_url:
                return
            db_path = db_url.split("///")[-1]
            if not db_path.startswith("/"):
                import os
                db_path = os.path.join(os.getcwd(), db_path)
            with sqlite3.connect(db_path) as conn:
                c = conn.cursor()
                cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=self.lookback_days)).isoformat()
                c.execute(
                    "SELECT text_content, published_at FROM posts "
                    "WHERE status='published' AND published_at >= ? "
                    "ORDER BY published_at DESC LIMIT 500",
                    (cutoff_iso,),
                )
                rows = c.fetchall()
            for text, published_at in rows:
                if not text:
                    continue
                try:
                    ts = datetime.fromisoformat(published_at.replace("Z", "+00:00")) if published_at else datetime.now(timezone.utc)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (ValueError, AttributeError):
                    ts = datetime.now(timezone.utc)
                normalized = self._normalize(text)
                self._recent_hashes.append((self._text_hash(text), normalized, ts))
            if self._recent_hashes:
                logger.info(
                    "DuplicateDetector rehydrated %d recent posts from DB",
                    len(self._recent_hashes),
                )
        except Exception as e:
            logger.warning("DuplicateDetector rehydrate from DB failed: %s", e)

    def is_duplicate(self, text: str) -> bool:
        """Check if text is too similar to recent posts."""
        with self._lock:
            self._lazy_load_from_db()
            self._cleanup_old()

            # Exact hash match
            new_hash = self._text_hash(text)
            for existing_hash, existing_text, _ in self._recent_hashes:
                if new_hash == existing_hash:
                    logger.warning("Duplicate content detected (exact match): %s...", text[:50])
                    return True

            # Fuzzy similarity check
            normalized_new = self._normalize(text)

            for _, existing_text, _ in self._recent_hashes:
                similarity = SequenceMatcher(None, normalized_new, existing_text).ratio()
                if similarity >= self.similarity_threshold:
                    logger.warning(
                        "Near-duplicate content detected (%.1f%% similar): %s...",
                        similarity * 100, text[:50],
                    )
                    return True

            return False

    def register(self, text: str):
        """Register a post as published."""
        with self._lock:
            normalized = self._normalize(text)
            self._recent_hashes.append((
                self._text_hash(text),
                normalized,
                datetime.now(timezone.utc),
            ))


# Global singletons
def _init_warmup() -> WarmupManager:
    from config.settings import settings
    start_str = getattr(settings, "warmup_start_date", "")
    if start_str:
        try:
            from datetime import datetime, timezone
            start = datetime.fromisoformat(start_str)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            else:
                start = start.astimezone(timezone.utc)
            return WarmupManager(start_date=start)
        except (ValueError, TypeError):
            pass
    return WarmupManager()

warmup_manager = _init_warmup()
daily_tracker = DailyActionTracker()
shadow_ban_detector = ShadowBanDetector()
emergency_stop = EmergencyStop()
platform_cooldown = PlatformCooldown()
posting_jitter = PostingJitter()
duplicate_detector = DuplicateDetector()
