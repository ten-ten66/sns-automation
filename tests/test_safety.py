"""Tests for the safety system: DailyActionTracker, EmergencyStop, PostingJitter, DuplicateDetector."""

import pytest
from datetime import datetime, timezone, timedelta

from src.core.safety import (
    DailyActionTracker,
    EmergencyStop,
    PlatformCooldown,
    PostingJitter,
    DuplicateDetector,
    WarmupManager,
    ShadowBanDetector,
)


# ---------------------------------------------------------------------------
# DailyActionTracker
# ---------------------------------------------------------------------------

class TestDailyActionTracker:
    def setup_method(self):
        self.tracker = DailyActionTracker()

    def test_can_perform_initially_true(self):
        assert self.tracker.can_perform("x", "posts") is True

    def test_get_count_starts_at_zero(self):
        assert self.tracker.get_count("x", "posts") == 0

    def test_record_action_increments_count(self):
        self.tracker.record_action("x", "posts")
        assert self.tracker.get_count("x", "posts") == 1

    def test_record_multiple_actions(self):
        for _ in range(5):
            self.tracker.record_action("instagram", "replies")
        assert self.tracker.get_count("instagram", "replies") == 5

    def test_can_perform_false_at_limit(self):
        # x.posts limit is defined in DAILY_LIMITS
        limit = DailyActionTracker.DAILY_LIMITS["x"]["posts"]
        for _ in range(limit):
            self.tracker.record_action("x", "posts")
        assert self.tracker.can_perform("x", "posts") is False

    def test_can_perform_still_true_just_below_limit(self):
        limit = DailyActionTracker.DAILY_LIMITS["instagram"]["posts"]
        for _ in range(limit - 1):
            self.tracker.record_action("instagram", "posts")
        assert self.tracker.can_perform("instagram", "posts") is True

    def test_get_remaining_full_capacity(self):
        remaining = self.tracker.get_remaining("x", "posts")
        assert remaining == DailyActionTracker.DAILY_LIMITS["x"]["posts"]

    def test_get_remaining_decreases_after_action(self):
        limit = DailyActionTracker.DAILY_LIMITS["x"]["posts"]
        self.tracker.record_action("x", "posts")
        assert self.tracker.get_remaining("x", "posts") == limit - 1

    def test_get_remaining_not_negative(self):
        # Overshoot the limit
        limit = DailyActionTracker.DAILY_LIMITS["x"]["posts"]
        for _ in range(limit + 5):
            self.tracker.record_action("x", "posts")
        assert self.tracker.get_remaining("x", "posts") == 0

    def test_different_platforms_independent(self):
        self.tracker.record_action("x", "posts")
        assert self.tracker.get_count("instagram", "posts") == 0

    def test_different_actions_independent(self):
        self.tracker.record_action("x", "posts")
        assert self.tracker.get_count("x", "replies") == 0

    def test_unknown_platform_uses_high_limit(self):
        """Unknown platforms should use a very high default limit (999)."""
        assert self.tracker.can_perform("unknown_platform", "posts") is True

    def test_get_summary_includes_all_platforms(self):
        summary = self.tracker.get_summary()
        for platform in ["x", "instagram", "tiktok", "youtube"]:
            assert platform in summary

    def test_get_summary_structure(self):
        summary = self.tracker.get_summary()
        x_summary = summary["x"]
        assert "posts" in x_summary
        post_info = x_summary["posts"]
        assert "used" in post_info
        assert "limit" in post_info
        assert "remaining" in post_info

    def test_fresh_tracker_has_zero_counts(self):
        new_tracker = DailyActionTracker()
        assert new_tracker.get_count("x", "posts") == 0

    def test_thread_safety_record_action(self):
        """record_action should be safe under concurrent calls."""
        import threading

        def record_many():
            for _ in range(10):
                self.tracker.record_action("x", "replies")

        threads = [threading.Thread(target=record_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 5 threads x 10 records = 50 total
        assert self.tracker.get_count("x", "replies") == 50


# ---------------------------------------------------------------------------
# EmergencyStop
# ---------------------------------------------------------------------------

class TestEmergencyStop:
    def setup_method(self):
        self.stop = EmergencyStop()

    def test_not_stopped_initially(self):
        assert self.stop.is_stopped() is False

    def test_activate_stops_operations(self):
        self.stop.activate("Test reason")
        assert self.stop.is_stopped() is True

    def test_deactivate_resumes_operations(self):
        self.stop.activate("Test reason")
        self.stop.deactivate()
        assert self.stop.is_stopped() is False

    def test_activate_stores_reason(self):
        self.stop.activate("Critical failure")
        status = self.stop.get_status()
        assert status["reason"] == "Critical failure"

    def test_activate_records_timestamp(self):
        before = datetime.now(timezone.utc)
        self.stop.activate("Test")
        after = datetime.now(timezone.utc)
        status = self.stop.get_status()
        assert status["stopped_at"] is not None
        stopped_at = datetime.fromisoformat(status["stopped_at"])
        assert before <= stopped_at <= after

    def test_deactivate_clears_reason(self):
        self.stop.activate("Test reason")
        self.stop.deactivate()
        status = self.stop.get_status()
        assert status["reason"] == ""

    def test_deactivate_clears_timestamp(self):
        self.stop.activate("Test")
        self.stop.deactivate()
        status = self.stop.get_status()
        assert status["stopped_at"] is None

    def test_get_status_not_stopped(self):
        status = self.stop.get_status()
        assert status["stopped"] is False
        assert status["reason"] == ""
        assert status["stopped_at"] is None

    def test_get_status_stopped(self):
        self.stop.activate("Emergency")
        status = self.stop.get_status()
        assert status["stopped"] is True

    def test_record_failure_increments(self):
        self.stop.record_failure("x")
        status = self.stop.get_status()
        assert status["consecutive_failures"].get("x", 0) >= 1

    def test_auto_stop_after_threshold(self):
        """After auto_stop_threshold consecutive failures, should auto-stop."""
        threshold = self.stop.auto_stop_threshold
        for _ in range(threshold):
            self.stop.record_failure("instagram")
        assert self.stop.is_stopped() is True

    def test_record_success_resets_failures(self):
        self.stop.record_failure("x")
        self.stop.record_failure("x")
        self.stop.record_success("x")
        status = self.stop.get_status()
        assert status["consecutive_failures"].get("x", 0) == 0

    def test_deactivate_clears_failure_counts(self):
        self.stop.record_failure("x")
        self.stop.activate("test")
        self.stop.deactivate()
        status = self.stop.get_status()
        assert status["consecutive_failures"] == {}

    def test_default_reason_non_empty(self):
        self.stop.activate()
        status = self.stop.get_status()
        assert len(status["reason"]) > 0

    def test_multiple_platforms_failures_independent(self):
        """Failures on one platform should not affect another."""
        self.stop.record_failure("x")
        self.stop.record_failure("x")
        status = self.stop.get_status()
        assert status["consecutive_failures"].get("instagram", 0) == 0


# ---------------------------------------------------------------------------
# PlatformCooldown
# ---------------------------------------------------------------------------

class TestPlatformCooldown:
    def setup_method(self):
        self.cooldown = PlatformCooldown()

    def test_no_cooldown_by_default(self):
        assert self.cooldown.is_on_cooldown("youtube") is False
        assert self.cooldown.get_status() == {}

    def test_set_and_check_cooldown(self):
        until = datetime.now(timezone.utc) + timedelta(hours=1)
        self.cooldown.set_cooldown("youtube", until, reason="quota")
        assert self.cooldown.is_on_cooldown("youtube") is True
        # Other platforms unaffected
        assert self.cooldown.is_on_cooldown("x") is False

    def test_status_includes_reason(self):
        until = datetime.now(timezone.utc) + timedelta(hours=1)
        self.cooldown.set_cooldown("youtube", until, reason="uploadLimitExceeded")
        status = self.cooldown.get_status()
        assert "youtube" in status
        assert status["youtube"]["reason"] == "uploadLimitExceeded"
        assert status["youtube"]["until"].startswith(str(until.year))

    def test_expired_cooldown_auto_cleared(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.cooldown.set_cooldown("youtube", past, reason="should be expired")
        # First check returns False AND cleans up
        assert self.cooldown.is_on_cooldown("youtube") is False
        assert "youtube" not in self.cooldown.get_status()

    def test_clear_removes_cooldown(self):
        until = datetime.now(timezone.utc) + timedelta(hours=1)
        self.cooldown.set_cooldown("youtube", until, reason="manual test")
        self.cooldown.clear("youtube")
        assert self.cooldown.is_on_cooldown("youtube") is False
        assert self.cooldown.get_status() == {}

    def test_clear_unknown_platform_is_noop(self):
        # Should not raise
        self.cooldown.clear("never_set")

    def test_cooldown_until_utc_midnight_sets_future_time(self):
        self.cooldown.cooldown_until_utc_midnight("youtube", reason="quota")
        assert self.cooldown.is_on_cooldown("youtube") is True
        status = self.cooldown.get_status()
        until_str = status["youtube"]["until"]
        until = datetime.fromisoformat(until_str)
        now = datetime.now(timezone.utc)
        # Must be in the future and within 24 hours and 1 minute
        assert until > now
        assert (until - now) <= timedelta(hours=24, minutes=1)
        # Must be at exactly 00:00:00 UTC
        assert until.hour == 0 and until.minute == 0 and until.second == 0

    def test_naive_datetime_is_normalized_to_utc(self):
        naive = datetime.utcnow() + timedelta(hours=1)
        self.cooldown.set_cooldown("youtube", naive, reason="naive test")
        assert self.cooldown.is_on_cooldown("youtube") is True

    def test_cooldown_until_next_utc_hour_future(self):
        """If target_hour_utc is later today, cooldown ends today at that hour."""
        now = datetime.now(timezone.utc)
        # Pick an hour at least 2 hours in the future, today
        future_hour = (now.hour + 2) % 24
        # If wrapping past midnight, this lands on tomorrow — that's fine, just check it's > now
        self.cooldown.cooldown_until_next_utc_hour("youtube", future_hour, reason="test")
        status = self.cooldown.get_status()
        until = datetime.fromisoformat(status["youtube"]["until"])
        assert until > now
        assert until.hour == future_hour
        assert until.minute == 0

    def test_cooldown_until_next_utc_hour_rolls_over(self):
        """If target_hour_utc has already passed today, cooldown rolls to tomorrow."""
        now = datetime.now(timezone.utc)
        past_hour = (now.hour - 1) % 24  # 1 hour ago (today, or yesterday near midnight)
        self.cooldown.cooldown_until_next_utc_hour("youtube", past_hour, reason="rollover test")
        status = self.cooldown.get_status()
        until = datetime.fromisoformat(status["youtube"]["until"])
        # Must be in the future regardless of hour-of-day
        assert until > now
        assert until.hour == past_hour

    def test_cooldown_until_next_utc_hour_rejects_invalid(self):
        with pytest.raises(ValueError):
            self.cooldown.cooldown_until_next_utc_hour("youtube", -1, reason="bad")
        with pytest.raises(ValueError):
            self.cooldown.cooldown_until_next_utc_hour("youtube", 24, reason="bad")


# ---------------------------------------------------------------------------
# _is_quota_error helper (lives in scheduler.auto_poster but small enough to test here)
# ---------------------------------------------------------------------------

class TestIsQuotaError:
    def test_youtube_upload_limit_detected(self):
        from src.scheduler.auto_poster import _is_quota_error
        assert _is_quota_error(
            'HttpError 400: "The user has exceeded the number of videos they may upload."'
        )
        assert _is_quota_error("uploadLimitExceeded")

    def test_generic_quota_strings_detected(self):
        from src.scheduler.auto_poster import _is_quota_error
        assert _is_quota_error("quotaExceeded: per-user")
        assert _is_quota_error("rateLimitExceeded: please retry")
        assert _is_quota_error("Daily Limit Exceeded for this resource")

    def test_unrelated_errors_not_detected(self):
        from src.scheduler.auto_poster import _is_quota_error
        assert _is_quota_error("Network unreachable") is False
        assert _is_quota_error("403 Forbidden") is False
        assert _is_quota_error("") is False
        assert _is_quota_error(None) is False


# ---------------------------------------------------------------------------
# PostingJitter
# ---------------------------------------------------------------------------

class TestPostingJitter:
    def test_add_jitter_returns_datetime(self):
        base_time = datetime.now(timezone.utc)
        result = PostingJitter.add_jitter(base_time)
        assert isinstance(result, datetime)

    def test_add_jitter_within_range(self):
        """Jitter should stay within the min/max bounds."""
        base_time = datetime.now(timezone.utc)
        min_minutes = -15
        max_minutes = 15

        for _ in range(50):
            result = PostingJitter.add_jitter(base_time, min_minutes, max_minutes)
            delta_minutes = (result - base_time).total_seconds() / 60
            assert min_minutes <= delta_minutes <= max_minutes, (
                f"Jitter {delta_minutes:.2f} minutes out of range [{min_minutes}, {max_minutes}]"
            )

    def test_add_jitter_produces_variation(self):
        """Multiple calls should produce different results (not all identical)."""
        base_time = datetime.now(timezone.utc)
        results = {PostingJitter.add_jitter(base_time) for _ in range(20)}
        # With Gaussian distribution, very unlikely to get all identical values
        assert len(results) > 1

    def test_add_jitter_custom_bounds(self):
        base_time = datetime.now(timezone.utc)
        for _ in range(30):
            result = PostingJitter.add_jitter(base_time, min_minutes=-5, max_minutes=5)
            delta_minutes = (result - base_time).total_seconds() / 60
            assert -5 <= delta_minutes <= 5

    def test_action_delay_returns_float(self):
        delay = PostingJitter.action_delay("post")
        assert isinstance(delay, (int, float))

    def test_action_delay_within_bounds(self):
        """action_delay should respect the defined min/max for each action type."""
        bounds = {
            "post": (10, 45),
            "story": (3, 15),
            "carousel": (15, 60),
            "reply": (5, 30),
            "like": (1, 5),
            "thread_tweet": (30, 120),
        }
        for action_type, (min_d, max_d) in bounds.items():
            for _ in range(20):
                delay = PostingJitter.action_delay(action_type)
                assert min_d <= delay <= max_d, (
                    f"action_delay({action_type}) = {delay:.2f}s out of bounds [{min_d}, {max_d}]"
                )

    def test_action_delay_unknown_type_uses_default(self):
        delay = PostingJitter.action_delay("unknown_action")
        assert isinstance(delay, (int, float))
        assert delay > 0

    def test_add_jitter_is_static_method(self):
        """Should be callable on class without instance."""
        base_time = datetime.now(timezone.utc)
        result = PostingJitter.add_jitter(base_time)
        assert isinstance(result, datetime)

    def test_action_delay_is_static_method(self):
        """Should be callable on class without instance."""
        result = PostingJitter.action_delay("post")
        assert isinstance(result, (int, float))


# ---------------------------------------------------------------------------
# DuplicateDetector
# ---------------------------------------------------------------------------

class TestDuplicateDetector:
    def setup_method(self):
        self.detector = DuplicateDetector(similarity_threshold=0.8, lookback_days=7)

    def test_new_text_not_duplicate(self):
        assert self.detector.is_duplicate("This is a brand new post about AI.") is False

    def test_registered_text_is_duplicate(self):
        text = "This is a unique post about machine learning trends."
        self.detector.register(text)
        assert self.detector.is_duplicate(text) is True

    def test_exact_match_detected(self):
        text = "Hello world!"
        self.detector.register(text)
        assert self.detector.is_duplicate("Hello world!") is True

    def test_near_duplicate_detected(self):
        original = "Amazing content about travel and adventure in Japan this summer."
        self.detector.register(original)
        # Slightly different but very similar
        near_dup = "Amazing content about travel and adventure in Japan this summer!"
        assert self.detector.is_duplicate(near_dup) is True

    def test_different_text_not_duplicate(self):
        self.detector.register("Post about cooking recipes and food preparation tips.")
        # Completely different text
        assert self.detector.is_duplicate("The stock market analysis shows growth in tech sector.") is False

    def test_hashtags_stripped_in_comparison(self):
        """Hashtags are stripped before comparison, so same core text with different hashtags = duplicate."""
        text = "Beautiful sunset at the beach."
        self.detector.register(text + " #beach #sunset #travel")
        assert self.detector.is_duplicate(text + " #ocean #evening #photo") is True

    def test_register_multiple_texts(self):
        texts = [
            "First post about technology developments today.",
            "Second post about science and innovation here.",
            "Third post about art and culture around us.",
        ]
        for text in texts:
            self.detector.register(text)

        for text in texts:
            assert self.detector.is_duplicate(text) is True

    def test_case_insensitive_matching(self):
        """Normalization lowercases text."""
        self.detector.register("Hello World This Is A Test Post.")
        assert self.detector.is_duplicate("hello world this is a test post.") is True

    def test_empty_detector_nothing_is_duplicate(self):
        fresh = DuplicateDetector()
        assert fresh.is_duplicate("Any text at all.") is False

    def test_similarity_threshold_respected(self):
        """At very high threshold, moderately similar texts should not be flagged."""
        strict_detector = DuplicateDetector(similarity_threshold=0.99)
        strict_detector.register("The quick brown fox jumps over the lazy dog.")
        # Similar but not nearly identical enough for 0.99 threshold
        assert strict_detector.is_duplicate("A quick brown fox jumped over a lazy dog.") is False

    def test_old_entries_cleaned_up(self):
        """Texts older than lookback_days should not be considered duplicates."""
        short_lookback = DuplicateDetector(similarity_threshold=0.8, lookback_days=1)
        text = "Old post about ancient history of Rome and its empire."

        # Manually insert an old entry bypassing the lock
        old_time = datetime.now(timezone.utc) - timedelta(days=2)
        with short_lookback._lock:
            short_lookback._recent_hashes.append((
                short_lookback._text_hash(text),
                short_lookback._normalize(text),
                old_time,
            ))

        # Should NOT be detected as duplicate since it's older than lookback
        assert short_lookback.is_duplicate(text) is False

    def test_recent_entries_still_detected(self):
        """Texts within lookback window should still be detected."""
        text = "Recent post about modern technology developments worldwide."
        self.detector.register(text)
        assert self.detector.is_duplicate(text) is True

    def test_thread_safety(self):
        """register and is_duplicate should be safe under concurrent access."""
        import threading
        results = []

        def register_and_check(i):
            text = f"Thread safety test post number {i} with unique content here for testing purposes."
            self.detector.register(text)
            results.append(self.detector.is_duplicate(text))

        threads = [threading.Thread(target=register_and_check, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All registered texts should be detected as duplicates
        assert all(results)

    def test_at_mentions_stripped(self):
        """@mentions are stripped during normalization."""
        self.detector.register("Great day out @friend1 @friend2")
        assert self.detector.is_duplicate("Great day out @person3 @someone4") is True


# ---------------------------------------------------------------------------
# WarmupManager
# ---------------------------------------------------------------------------

class TestWarmupManager:
    def test_no_start_date_unlimited(self):
        mgr = WarmupManager(start_date=None)
        assert mgr.get_max_posts_today("x") == 999

    def test_no_start_date_is_warmed_up(self):
        mgr = WarmupManager(start_date=None)
        assert mgr.is_warmed_up() is True

    def test_week_1_limit(self):
        start = datetime.now(timezone.utc) - timedelta(days=3)  # Day 3 = Week 1
        mgr = WarmupManager(start_date=start)
        assert mgr.get_max_posts_today("x") == WarmupManager.WARMUP_SCHEDULE[1]

    def test_week_2_limit(self):
        start = datetime.now(timezone.utc) - timedelta(days=10)  # Day 10 = Week 2
        mgr = WarmupManager(start_date=start)
        assert mgr.get_max_posts_today("x") == WarmupManager.WARMUP_SCHEDULE[2]

    def test_week_3_limit(self):
        start = datetime.now(timezone.utc) - timedelta(days=17)  # Day 17 = Week 3
        mgr = WarmupManager(start_date=start)
        assert mgr.get_max_posts_today("x") == WarmupManager.WARMUP_SCHEDULE[3]

    def test_fully_warmed_up_after_35_days(self):
        start = datetime.now(timezone.utc) - timedelta(days=36)
        mgr = WarmupManager(start_date=start)
        assert mgr.is_warmed_up() is True

    def test_not_warmed_up_at_day_34(self):
        start = datetime.now(timezone.utc) - timedelta(days=34)
        mgr = WarmupManager(start_date=start)
        assert mgr.is_warmed_up() is False

    def test_platform_ignored_for_max_posts(self):
        """get_max_posts_today ignores platform (same schedule for all)."""
        start = datetime.now(timezone.utc) - timedelta(days=3)
        mgr = WarmupManager(start_date=start)
        assert mgr.get_max_posts_today("x") == mgr.get_max_posts_today("instagram")


# ---------------------------------------------------------------------------
# ShadowBanDetector
# ---------------------------------------------------------------------------

class TestShadowBanDetector:
    def setup_method(self):
        self.detector = ShadowBanDetector(drop_threshold=0.5, consecutive_required=3)

    def test_not_flagged_initially(self):
        assert self.detector.is_flagged("x") is False

    def test_not_flagged_with_insufficient_data(self):
        # Need at least 7 data points before evaluation starts
        for _ in range(6):
            self.detector.record_engagement("x", 0.1)
        assert self.detector.is_flagged("x") is False

    def test_flagged_after_consecutive_drops(self):
        # Build history of normal engagement
        for _ in range(7):
            self.detector.record_engagement("x", 0.5)
        # Now record consecutive very low engagement
        for _ in range(3):
            self.detector.record_engagement("x", 0.01)
        assert self.detector.is_flagged("x") is True

    def test_clear_flag(self):
        for _ in range(7):
            self.detector.record_engagement("x", 0.5)
        for _ in range(3):
            self.detector.record_engagement("x", 0.01)
        assert self.detector.is_flagged("x") is True

        self.detector.clear_flag("x")
        assert self.detector.is_flagged("x") is False

    def test_different_platforms_independent(self):
        for _ in range(7):
            self.detector.record_engagement("x", 0.5)
        for _ in range(3):
            self.detector.record_engagement("x", 0.01)
        # instagram should not be flagged
        assert self.detector.is_flagged("instagram") is False

    def test_recovery_after_good_engagement(self):
        """Good engagement after a partial dip should reset consecutive counter."""
        for _ in range(7):
            self.detector.record_engagement("x", 0.5)
        # Two low posts — not enough to trigger flag (need 3)
        self.detector.record_engagement("x", 0.01)
        self.detector.record_engagement("x", 0.01)
        # One good post — should reset counter
        self.detector.record_engagement("x", 0.6)
        assert self.detector.is_flagged("x") is False
