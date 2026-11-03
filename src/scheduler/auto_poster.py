"""Auto-posting scheduler with optimal timing."""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from config.settings import settings
from src.core.database import async_session
from src.core.models import Post, PostStatus, PostMetric

logger = logging.getLogger(__name__)
from src.platforms.manager import PlatformManager, MEDIA_REQUIRED_PLATFORMS
from src.ai.claude_generator import ClaudeContentGenerator
from src.ai.image_generator import ImageGenerator
from src.ai.trend_researcher import TrendResearcher
from src.seo.optimizer import SEOOptimizer
from src.core.feedback_loop import feedback_loop
from config.persona import CONTENT_SCHEDULE_RATIO
from config.media_prompts import get_image_prompt, get_video_prompt, VIDEO_CONTENT_TYPES, PLATFORM_SIZES
import httpx

from src.core.safety import (
    warmup_manager, daily_tracker, shadow_ban_detector,
    emergency_stop, platform_cooldown, posting_jitter, duplicate_detector,
)
from src.core.smart_scheduler import get_optimal_times
from src.core.fatigue_tracker import fatigue_tracker
from src.core.cleanup import cleanup_old_media, cleanup_old_db_rows
from src.core.cross_promo import generate_cross_promo_cta
from src.core.utils import validate_platform_content, BoundedSet


# Substrings that indicate a daily-quota / upload-limit error from a platform.
# When detected we cool the offending platform down until its quota window resets
# instead of letting 3 consecutive failures trip the global emergency stop.
_QUOTA_ERROR_PATTERNS = (
    "uploadLimitExceeded",
    "exceeded the number of videos",
    "quotaExceeded",
    "rateLimitExceeded",
    "Daily Limit Exceeded",
)

# Substrings that indicate an OAuth/auth credential failure — these WILL NOT
# self-heal without human action (re-run setup_youtube_oauth.py etc.), so we
# cool the platform down long enough to avoid hammering it, and alert loudly.
# Precipitating incident: 2026-04-14 YouTube refresh_token revoked, causing
# 8 days of silent failure + infinite 300s timeout loop that starved the X
# pipeline via max_instances=1.
_AUTH_ERROR_PATTERNS = (
    "invalid_grant",
    "Token has been expired or revoked",
    "invalid_client",
    "unauthorized_client",
)

# Hour (in UTC) at which each platform's daily quota resets.
# - YouTube Data API resets at 00:00 Pacific Time = 08:00 UTC.
# - Most other services reset at 00:00 UTC.
_QUOTA_RESET_UTC_HOUR: dict[str, int] = {
    "youtube": 8,
}


def _is_quota_error(error_message: str | None) -> bool:
    """True iff the error message looks like a platform-side daily quota / upload limit."""
    if not error_message:
        return False
    return any(pattern in error_message for pattern in _QUOTA_ERROR_PATTERNS)


def _is_auth_error(error_message: str | None) -> bool:
    """True iff the error message indicates an OAuth / credential failure that
    requires human re-authentication (as opposed to a recoverable quota hit)."""
    if not error_message:
        return False
    return any(pattern in error_message for pattern in _AUTH_ERROR_PATTERNS)


def _apply_quota_cooldown(platform: str, reason: str) -> None:
    """Cool down a platform until its quota window resets, using a per-platform reset hour."""
    target_hour = _QUOTA_RESET_UTC_HOUR.get(platform)
    if target_hour is not None:
        platform_cooldown.cooldown_until_next_utc_hour(platform, target_hour, reason=reason)
    else:
        platform_cooldown.cooldown_until_utc_midnight(platform, reason=reason)


def _apply_auth_cooldown(platform: str, error_msg: str) -> None:
    """Auth errors won't self-recover — cool down 24h and alert loudly once
    per window. Cooldown duration is chosen so at most one alert fires per
    day even if the user doesn't re-auth immediately."""
    cooldown_until = datetime.now(timezone.utc) + timedelta(hours=24)
    platform_cooldown.set_cooldown(
        platform, cooldown_until,
        reason=f"auth error (re-auth required): {error_msg[:200]}",
    )
    try:
        from src.core.alerts import alert_service
        loop = asyncio.get_running_loop()
        loop.create_task(alert_service._send(
            f"🔑 {platform} の認証トークンが失効しました。"
            f"再認証が必要です (YouTube: setup_youtube_oauth.py)。"
            f"24時間のクールダウンを発動しました。\n詳細: {error_msg[:300]}",
            "error",
        ))
    except RuntimeError:
        pass


def _handle_platform_failure(platform: str, error_msg: str) -> None:
    """Classify a post failure and apply the appropriate response."""
    if _is_auth_error(error_msg):
        _apply_auth_cooldown(platform, error_msg)
        emergency_stop.record_success(platform)
    elif _is_quota_error(error_msg):
        _apply_quota_cooldown(platform, reason=f"quota error: {error_msg[:200]}")
        emergency_stop.record_success(platform)
    else:
        emergency_stop.record_failure(platform)


def _cleanup_failed_post_media(post: Post) -> None:
    """Delete local media files for a FAILED post — they can't be reused and
    would otherwise accumulate on disk. Remote URLs are left untouched."""
    import os
    urls = post.media_urls or []
    for url in urls:
        if not url or url.startswith(("http://", "https://")):
            continue
        path = url if os.path.isabs(url) else os.path.join(os.getcwd(), url)
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.debug("Deleted failed-post media: %s", path)
        except OSError as e:
            logger.warning("Could not delete failed-post media %s: %s", path, e)


class AutoPoster:
    """Automated posting system with intelligent scheduling."""

    def __init__(self, platform_manager: PlatformManager):
        self.platform_manager = platform_manager
        self.content_generator = ClaudeContentGenerator()
        self.seo_optimizer = SEOOptimizer()
        self.trend_researcher = TrendResearcher()
        self.image_generator = ImageGenerator()
        self.scheduler = AsyncIOScheduler(timezone=ZoneInfo(settings.default_timezone))
        # Bounded dedup set: capped at 5000 most-recent IDs to prevent
        # unbounded memory growth for a long-running systemd process.
        self._replied_comment_ids = BoundedSet(capacity=5000)
        # Persistent growth engine instances — keep dedup sets across scheduled runs
        self._growth_engines: dict[str, object] = {}

        # Heartbeat monitor state: alert if no successful publish in N hours.
        # Monotonic timestamp so the dedup survives wall-clock changes.
        self._heartbeat_last_alert_mono: float = 0.0
        self._heartbeat_threshold_hours: float = 6.0
        self._heartbeat_alert_interval_hours: float = 6.0

        # Optimal posting times by platform (JST).
        # Instagram + TikTok intentionally absent — both stopped 2026-04-12
        # per user directive ("TikTok IG 止めて"). IG VPS-IP banned, TikTok
        # has no real publishing path on Basic credentials.
        self.optimal_times = {
            "x": ["07:00", "08:30", "10:00", "12:00", "13:30", "15:00", "17:00", "18:30", "20:00", "21:30", "23:00", "00:30"],
            "youtube": ["17:00", "20:00"],
        }

    async def start(self):
        """Start the auto-posting scheduler."""
        # Schedule trend research every hour
        self.scheduler.add_job(
            self._research_and_queue,
            CronTrigger(minute=0),
            id="trend_research",
            replace_existing=True,
            max_instances=1,
        )

        # Schedule post publishing (check every 5 minutes)
        self.scheduler.add_job(
            self._publish_scheduled,
            CronTrigger(minute="*/5"),
            id="publish_scheduled",
            replace_existing=True,
            max_instances=1,
        )

        # Schedule metrics collection every hour
        self.scheduler.add_job(
            self._collect_metrics,
            CronTrigger(minute=0),
            id="collect_metrics",
            replace_existing=True,
            max_instances=1,
        )

        # Schedule daily follower snapshot
        self.scheduler.add_job(
            self._snapshot_followers,
            CronTrigger(hour=23, minute=59),
            id="follower_snapshot",
            replace_existing=True,
            max_instances=1,
        )

        # Recover stuck PUBLISHING posts every 15 minutes
        self.scheduler.add_job(
            self._recover_stuck_posts,
            CronTrigger(minute="*/15"),
            id="recover_stuck",
            replace_existing=True,
            max_instances=1,
        )

        # Instagram-specific jobs (stories/reels) and IG growth jobs all
        # intentionally absent — Instagram completely stopped 2026-04-12 per
        # user directive ("TikTok IG 止めて"). See note above the
        # _auto_reply_comments method for the re-enable checklist.

        # Schedule comment auto-reply (9:00-25:00, irregular intervals)
        # Use IntervalTrigger with jitter for truly irregular timing
        from apscheduler.triggers.interval import IntervalTrigger
        self.scheduler.add_job(
            self._auto_reply_comments,
            IntervalTrigger(hours=2, jitter=3600),  # Every ~2h ±60min for irregularity
            id="auto_reply_comments",
            replace_existing=True,
            max_instances=1,
        )

        # Daily media cleanup at 4:00 AM
        self.scheduler.add_job(
            cleanup_old_media,
            CronTrigger(hour=4, minute=0),
            id="media_cleanup",
            replace_existing=True,
        )

        # Weekly DB cleanup (Sunday 4:30 AM)
        self.scheduler.add_job(
            cleanup_old_db_rows,
            CronTrigger(day_of_week="sun", hour=4, minute=30),
            id="db_cleanup",
            replace_existing=True,
        )

        # Check for viral posts every 30 minutes for repurposing
        self.scheduler.add_job(
            self._check_viral_posts,
            CronTrigger(minute="*/30"),
            id="viral_check",
            replace_existing=True,
            max_instances=1,
        )

        # Daily competitor scan at 3:00 AM
        self.scheduler.add_job(
            self._scan_competitors,
            CronTrigger(hour=3, minute=0),
            id="competitor_scan",
            replace_existing=True,
            max_instances=1,
        )

        # Daily DB backup at 3:00 AM
        self.scheduler.add_job(
            self._backup_database,
            CronTrigger(hour=3, minute=0),
            id="db_backup",
            replace_existing=True,
            max_instances=1,
        )

        # Daily failed-post retry queue at 6:00 AM
        self.scheduler.add_job(
            self._retry_failed_posts,
            CronTrigger(hour=6, minute=0),
            id="retry_failed_posts",
            replace_existing=True,
            max_instances=1,
        )

        # --- Growth jobs ---
        from apscheduler.triggers.interval import IntervalTrigger as _IT

        # X: Post a thread on beauty topic (once daily at 12:30)
        self.scheduler.add_job(
            self._x_thread_post,
            CronTrigger(hour=12, minute=30),
            id="x_thread_post",
            replace_existing=True,
            max_instances=1,
        )

        # X: Ride a JP trending topic with a top-level post (Wed/Sat at 18:00 JST).
        # Top-level is the only post mechanism that survives the account-standing
        # throttle (cf. project_x_reply_restriction_2026_04_23).
        self.scheduler.add_job(
            self._x_trend_top_level_post,
            CronTrigger(day_of_week="wed,sat", hour=9, minute=0),  # 18:00 JST = 09:00 UTC
            id="x_trend_top_level_post",
            replace_existing=True,
            max_instances=1,
        )

        # IG growth jobs (like/unfollow/follow) intentionally NOT scheduled
        # — IG completely stopped 2026-04-12. The handler stubs below stay
        # for symmetry but never run unless someone re-adds the add_job calls.

        # YouTube: Comment on trending videos (every 4h ± 1.5h)
        self.scheduler.add_job(
            self._youtube_trending_comment,
            _IT(hours=4, jitter=5400),
            id="youtube_trending_comment",
            replace_existing=True,
            max_instances=1,
        )

        # YouTube: Reply to comments on own videos (every 3h ± 1h)
        self.scheduler.add_job(
            self._youtube_reply_cycle,
            _IT(hours=3, jitter=3600),
            id="youtube_reply_cycle",
            replace_existing=True,
            max_instances=1,
        )

        # Daily profile SEO optimization (once daily at 5:00 AM)
        self.scheduler.add_job(
            self._optimize_profiles,
            CronTrigger(hour=5, minute=0),
            id="profile_seo",
            replace_existing=True,
            max_instances=1,
        )

        # Viral content repurposing (every 6h)
        self.scheduler.add_job(
            self._repurpose_viral_content,
            _IT(hours=6, jitter=3600),
            id="viral_repurpose",
            replace_existing=True,
            max_instances=1,
        )

        # Heartbeat monitor: alerts if no successful publish in N hours.
        # The 8-day silent outage of 2026-04-14→22 (YouTube OAuth expired +
        # cascaded to X via max_instances=1) would have surfaced within 6h
        # with this check in place.
        self.scheduler.add_job(
            self._heartbeat_check,
            CronTrigger(minute="*/30"),
            id="heartbeat_check",
            replace_existing=True,
            max_instances=1,
        )

        self.scheduler.start()

        # Stagger any past-due SCHEDULED posts over 30 minutes to avoid a
        # flood of API calls immediately on startup.
        try:
            await self._stagger_pastdue_posts()
        except Exception as e:
            logger.warning("Startup stagger of past-due posts failed: %s", e)

    async def _stagger_pastdue_posts(self):
        """Re-schedule past-due SCHEDULED posts staggered over the next 30 minutes."""
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            result = await session.execute(
                select(Post).where(
                    Post.status == PostStatus.SCHEDULED,
                    Post.scheduled_at <= now,
                )
            )
            pastdue = result.scalars().all()
            if not pastdue:
                return
            logger.info("Startup: staggering %d past-due posts over 30 minutes", len(pastdue))
            stagger_seconds = 1800 / max(len(pastdue), 1)  # spread evenly over 30 min
            for i, post in enumerate(pastdue):
                post.scheduled_at = now + timedelta(seconds=stagger_seconds * (i + 1))
            await session.commit()

    async def _backup_database(self):
        """Daily DB backup job (3:00 AM)."""
        try:
            from src.core.cleanup import backup_database
            await backup_database()
        except Exception as e:
            logger.error("DB backup job failed: %s", e)

    async def _retry_failed_posts(self):
        """Daily retry queue job (6:00 AM).

        Posts with status FAILED and retry_count < 3 within the last 48 hours
        are re-queued as SCHEDULED with incremented retry_count.
        Posts with retry_count >= 3 are set to status 'dead' and trigger a
        Discord alert.
        """
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            async with async_session() as session:
                result = await session.execute(
                    select(Post).where(
                        Post.status == PostStatus.FAILED,
                        Post.created_at >= cutoff,
                    )
                )
                failed_posts = result.scalars().all()

                requeued = 0
                dead = 0
                for post in failed_posts:
                    retry_count = getattr(post, "retry_count", None)
                    if retry_count is None:
                        # retry_count column not yet in schema — derive from error_message
                        # by counting "retry:" markers, default to 0
                        retry_count = 0

                    if retry_count >= 3:
                        post.status = PostStatus.DEAD
                        dead += 1
                        logger.warning(
                            "Post %d on %s has retry_count=%d — marking DEAD",
                            post.id, post.platform, retry_count,
                        )
                    else:
                        post.status = PostStatus.SCHEDULED
                        post.scheduled_at = datetime.now(timezone.utc) + timedelta(minutes=30)
                        # Increment retry_count
                        if hasattr(post, "retry_count") and post.retry_count is not None:
                            post.retry_count = retry_count + 1
                        else:
                            post.error_message = (
                                (post.error_message or "") + f" [retry:{retry_count + 1}]"
                            )[:2000]
                        requeued += 1

                await session.commit()

            logger.info(
                "Retry queue: %d posts re-queued, %d posts marked dead", requeued, dead
            )

            if dead > 0:
                try:
                    from src.core.alerts import alert_service
                    await alert_service.notify_error(
                        f"[RetryQueue] {dead} post(s) exceeded max retries and were marked DEAD"
                    )
                except Exception as alert_err:
                    logger.warning("Failed to send dead-post alert: %s", alert_err)

        except Exception as e:
            logger.error("Retry failed posts job failed: %s", e)

    # --- Growth job handlers ---
    #
    # Each handler follows the same pattern: look up the platform, create a
    # growth engine, call one method.  ``_run_growth_job`` captures the shared
    # boilerplate so individual handlers stay one-liners.

    async def _run_growth_job(
        self,
        platform_key: str,
        engine_factory,
        method_name: str,
        job_label: str,
        **kwargs,
    ):
        """Generic dispatcher for growth-engine jobs.

        Args:
            platform_key: Key passed to ``platform_manager.get()``.
            engine_factory: Callable that accepts a platform instance and
                returns a growth engine (e.g. ``IGGrowthEngine``).
            method_name: Name of the async method to call on the engine.
            job_label: Human-readable label for error logging.
            **kwargs: Keyword arguments forwarded to the engine method.
        """
        try:
            platform = self.platform_manager.get(platform_key)
            if not platform:
                return
            # Reuse growth engine instance to preserve dedup sets (_replied_tweet_ids etc.)
            cache_key = f"{platform_key}:{engine_factory.__name__}"
            engine = self._growth_engines.get(cache_key)
            if engine is None:
                engine = engine_factory(platform)
                self._growth_engines[cache_key] = engine
            await getattr(engine, method_name)(**kwargs)
        except Exception as e:
            logger.error("%s job failed: %s", job_label, e)

    # Trends to never ride — death/disaster/politics/violence/illness signal
    # space. Topical riding here is high-risk for the persona's reputation.
    _UNSAFE_TREND_RE = re.compile(
        r"訃報|死亡|死去|急逝|逝去|殺人|殺害|暴行|事件|事故|災害|地震|津波|爆発|"
        r"火災|テロ|戦争|侵攻|逮捕|不祥事|炎上|誹謗|中傷|"
        r"政治|選挙|自民|立憲|首相|国会|大統領|"
        r"病気|入院|うつ|自殺|訃|墜落|死"
    )

    async def _x_trend_top_level_post(self):
        """Ride a Google Trends JP topic with a top-level X post.

        Top-level posting is the only X reply/quote/post mechanism that bypasses
        the account-standing throttle (cf. project_x_reply_restriction_2026_04_23).
        Riding fresh trending topics is the cheapest way to surface for non-followers
        without the search/reply API paths that Basic plan blocks.
        """
        from src.core.cost_tracker import cost_tracker
        if cost_tracker.is_budget_exceeded():
            logger.info("Budget exceeded, skipping X trend top-level post")
            return
        if not daily_tracker.can_perform("x", "posts"):
            logger.info("X post daily limit reached, skipping trend post")
            return

        platform = self.platform_manager.get("x")
        if not platform:
            return

        try:
            from src.ai.trend_researcher import TrendResearcher
            tr = TrendResearcher()
            try:
                trends = await tr.fetch_google_trends(geo="JP")
            finally:
                await tr.close()

            safe = [
                t["topic"] for t in trends
                if t.get("topic") and not self._UNSAFE_TREND_RE.search(t["topic"])
            ]
            if not safe:
                logger.info("No safe JP trends found this cycle, skipping")
                return

            import random
            topic = random.choice(safe[:8])
            content = self.content_generator.generate_post(
                platform="x",
                topic=topic,
                tone="kawaii and friendly",
                trending_context=f"Riding the JP trending topic: {topic}",
            )
            text = (content.text or "").strip()
            if not text:
                logger.warning("Empty trend post for topic: %s", topic)
                return
            if len(text) > 270:
                text = text[:267] + "..."

            result = await platform.post_text(text)
            if result.success:
                daily_tracker.record_action("x", "posts")
                logger.info("X trend post sent (topic=%s, len=%d)", topic, len(text))
            else:
                logger.warning("X trend post failed (topic=%s): %s", topic, result.error)
        except Exception as e:
            logger.error("X trend post job failed: %s", e)

    async def _x_thread_post(self):
        """Generate a beauty thread via Claude and post to X (daily)."""
        from src.core.cost_tracker import cost_tracker
        if cost_tracker.is_budget_exceeded():
            logger.info("Budget exceeded, skipping X thread post")
            return
        if not daily_tracker.can_perform("x", "posts"):
            logger.info("X post daily limit reached, skipping thread")
            return
        try:
            import random
            topics = [
                "top 3 skincare mistakes people make",
                "my morning skincare routine in 3 steps",
                "why SPF is non-negotiable even on cloudy days",
                "K-beauty vs J-beauty: what works for me",
                "the best drugstore products I've tried this month",
                "how to build a simple nighttime routine",
                "my favorite makeup hacks for busy mornings",
            ]
            topic = random.choice(topics)
            tweets = self.content_generator.generate_thread(topic)
            if not tweets:
                logger.warning("generate_thread returned empty for topic: %s", topic)
                return
            platform = self.platform_manager.get("x")
            if not platform:
                return
            results = await platform.post_thread(tweets)
            if results and results[0].success:
                daily_tracker.record_action("x", "posts")
                logger.info("X thread posted: %d tweets on '%s'", len(results), topic)
            else:
                logger.warning("X thread post failed: %s", results[0].error if results else "no results")
        except Exception as e:
            logger.error("X thread post job failed: %s", e)

    # Instagram and TikTok growth handlers removed 2026-04-12 per user
    # directive ("TikTok IG 止めて"). The IG ig_like/unfollow/follow cycles
    # used to live here; the TikTok stubs were no-ops anyway. The growth
    # engines themselves (src/growth/ig_growth.py, src/growth/tiktok_growth.py)
    # still exist on disk for reference and as a one-line revert path.

    async def _youtube_trending_comment(self):
        from src.growth.youtube_growth import YouTubeGrowthEngine
        await self._run_growth_job("youtube", YouTubeGrowthEngine, "run_trending_comment_cycle", "YouTube trending comment", max_comments=8)

    async def _youtube_reply_cycle(self):
        from src.growth.youtube_growth import YouTubeGrowthEngine
        await self._run_growth_job("youtube", YouTubeGrowthEngine, "run_own_video_reply_cycle", "YouTube reply cycle", max_replies=5)

    async def _optimize_profiles(self):
        """Run daily profile SEO optimization across all platforms.

        Also updates the X bio via the v1.1 update_profile API if an optimized
        description is available.
        """
        try:
            from src.seo.optimizer import SEOOptimizer
            from config.persona import PERSONA
            optimizer = SEOOptimizer()
            # IG removed 2026-04-12 per user directive ("TikTok IG 止めて").
            # TikTok was never in this list to begin with.
            platforms = ["x", "youtube"]
            for platform in platforms:
                p = self.platform_manager.get(platform)
                if not p:
                    continue
                try:
                    result = optimizer.optimize_profile(
                        current_bio=PERSONA.get("name", "") or "beauty & skincare enthusiast",
                        platform=platform,
                        niche=PERSONA.get("niche", "beauty & skincare"),
                    )
                    logger.info("Profile SEO for %s: %s", platform, result.get("tips", [])[:2])

                    # Apply optimized bio to X via update_profile()
                    if platform == "x" and hasattr(p, "update_profile"):
                        bio = result.get("optimized_bio") or result.get("description")
                        if bio and isinstance(bio, str) and len(bio) <= 160:
                            try:
                                await p.update_profile(description=bio)
                                logger.info("X profile bio updated: %s", bio[:60])
                            except Exception as bio_err:
                                logger.warning("X bio update failed: %s", bio_err)
                except Exception as e:
                    logger.warning("Profile SEO failed for %s: %s", platform, e)
        except Exception as e:
            logger.error("Profile optimization job failed: %s", e)

    async def _repurpose_viral_content(self):
        """Find high-performing posts and repurpose across platforms."""
        try:
            from src.core.cross_promo import generate_repurpose_plan
            from sqlalchemy import select, desc

            async with async_session() as session:
                # Find posts with high engagement in the last 48 hours
                # Exclude posts that were themselves repurposed (ab_test_group='repurposed')
                cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
                result = await session.execute(
                    select(Post, PostMetric)
                    .join(PostMetric)
                    .where(
                        Post.status == PostStatus.PUBLISHED,
                        Post.published_at >= cutoff,
                        PostMetric.engagement_rate > 0.05,  # >5% engagement
                        Post.ab_test_group != "repurposed",  # Prevent infinite repurpose loops
                    )
                    .order_by(desc(PostMetric.engagement_rate))
                    .limit(5)
                )
                viral_posts = result.all()

                for post, metric in viral_posts:
                    try:
                        plan = generate_repurpose_plan(
                            original_platform=post.platform,
                            post_text=post.text_content or "",
                            engagement_rate=metric.engagement_rate or 0.0,
                        )
                        if plan.get("should_repurpose"):
                            for suggestion in plan.get("suggestions", []):
                                target_platform = suggestion.get("destination", "")
                                if not target_platform or not self.platform_manager.get(target_platform):
                                    continue
                                repurposed = Post(
                                    platform=target_platform,
                                    content_type=suggestion.get("format", "text"),
                                    text_content=post.text_content,
                                    media_urls=post.media_urls,
                                    hashtags=post.hashtags,
                                    keywords=post.keywords,
                                    status=PostStatus.SCHEDULED,
                                    scheduled_at=self._get_next_optimal_time(target_platform),
                                    ab_test_group="repurposed",  # Mark to prevent re-repurposing
                                )
                                session.add(repurposed)
                                logger.info(
                                    "Repurposing viral %s post (%.1f%% eng) → %s (%s)",
                                    post.platform,
                                    plan.get("engagement_rate_pct", 0),
                                    target_platform,
                                    suggestion.get("format", ""),
                                )
                    except Exception as e:
                        logger.warning("Repurpose planning failed: %s", e)

                await session.commit()
        except Exception as e:
            logger.error("Viral repurpose job failed: %s", e)

    async def stop(self):
        self.scheduler.shutdown()
        try:
            await self.trend_researcher.close()
        except Exception as e:
            logger.warning("Error closing trend researcher: %s", e)

    async def _research_and_queue(self):
        """Research trends and queue new posts."""
        try:
            await self._do_research_and_queue()
        except Exception as e:
            logger.error(f"Trend research job failed: {type(e).__name__}: {e}")

    def _select_content_type(self, platform: str) -> str:
        """Select content type based on CONTENT_SCHEDULE_RATIO for the platform."""
        ratio = CONTENT_SCHEDULE_RATIO.get(platform, {})
        if not ratio:
            return "female_beauty"

        types = list(ratio.keys())
        weights = list(ratio.values())
        selected_type = random.choices(types, weights=weights, k=1)[0]

        # Retry content type selection if fatigued (max 3 retries)
        for _ in range(3):
            if not fatigue_tracker.should_avoid_type(platform, selected_type):
                break
            selected_type = random.choices(types, weights=weights, k=1)[0]

        return selected_type

    async def _generate_media_for_post(self, content_type: str, platform: str, fallback_prompt: str | None = None) -> tuple[str | None, str]:
        """Generate media (image or video) using AI.

        Returns:
            tuple: (file_path or None, media_type: "image" | "video" | "text")
        """
        try:
            if content_type in VIDEO_CONTENT_TYPES:
                # Video generation via Kie.ai VEO 3.1
                video_prompt = get_video_prompt(content_type, platform)
                try:
                    from src.ai.video_generator import KieVideoGenerator
                    vg = KieVideoGenerator()
                    result = await vg.generate_video(prompt=video_prompt)
                    video_url = result.video_url if result else ""
                    if video_url:
                        import uuid
                        import os
                        filename = f"data/media/{uuid.uuid4().hex}.mp4"
                        # Atomic download: write to .part first, verify size
                        # is non-zero, then rename. Previously a mid-download
                        # failure would leave a corrupt .mp4 that YouTube
                        # would reject (wasting quota) — see 2026-04-20
                        # code review item #6.
                        tmp = filename + ".part"
                        for attempt in range(2):
                            try:
                                async with httpx.AsyncClient(timeout=120) as client:
                                    resp = await client.get(video_url)
                                if resp.status_code != 200:
                                    logger.warning(
                                        "Video DL HTTP %d for %s (attempt %d)",
                                        resp.status_code, video_url, attempt + 1,
                                    )
                                    continue
                                import aiofiles
                                async with aiofiles.open(tmp, "wb") as f:
                                    await f.write(resp.content)
                                if os.path.getsize(tmp) == 0:
                                    os.remove(tmp)
                                    logger.warning("Video DL produced empty file (attempt %d)", attempt + 1)
                                    continue
                                os.rename(tmp, filename)
                                await self._send_generated_media_to_discord(filename, platform, content_type)
                                return filename, "video"
                            except (httpx.HTTPError, OSError) as dl_err:
                                logger.warning("Video DL failed (attempt %d): %s", attempt + 1, dl_err)
                                try:
                                    if os.path.exists(tmp):
                                        os.remove(tmp)
                                except OSError:
                                    pass
                except Exception as e:
                    logger.warning(f"Video generation failed, falling back to image: {e}")
                # Fallback to image if video fails — but YouTube only accepts video,
                # so skip entirely rather than creating an unpostable image post.
                if platform == "youtube":
                    logger.warning(
                        "Skipping YouTube post: VEO video generation failed and "
                        "YouTube does not accept image fallback."
                    )
                    return None, "text"

            # Image generation via Kie.ai Nano Banana Pro
            prompt, aspect_ratio = get_image_prompt(content_type, platform)
            if not prompt and fallback_prompt:
                prompt = fallback_prompt
                aspect_ratio = PLATFORM_SIZES.get(platform, "1:1")

            if not prompt:
                return None, "text"

            urls = await self.image_generator.generate(
                prompt=prompt,
                aspect_ratio=aspect_ratio,
            )
            if urls:
                import uuid
                import aiofiles
                filename = f"data/media/{uuid.uuid4().hex}.png"
                path = await self.image_generator.download_image(urls[0], filename)

                # Background blur removed — AI-generated images have no real
                # room/background to protect. Blur was degrading image quality
                # for SNS posting. face_mosaic.apply_background_blur() is still
                # available for real-photo pipelines if ever needed.

                # Send to Discord on generation
                await self._send_generated_media_to_discord(path, platform, content_type)
                return path, "image"
        except Exception as e:
            logger.warning(f"Media generation failed for {content_type}/{platform}: {e}")

        return None, "text"

    async def _send_generated_media_to_discord(self, path: str | None, platform: str, content_type: str):
        """Send freshly generated media to Discord for review (non-blocking)."""
        if not path:
            return
        try:
            from src.core.alerts import alert_service
            await alert_service.send_media_to_discord(
                media_path=path,
                platform=platform,
                text=f"[Generated] {content_type}",
            )
        except Exception as e:
            logger.warning(f"Discord media send on generation failed (non-fatal): {e}")

    async def _do_research_and_queue(self):
        # IG removed 2026-04-12 per user directive ("TikTok IG 止めて").
        trends = await self.trend_researcher.research_niche_trends(
            niche=settings.niche,
            platforms=["x"],
        )

        hot_topics = trends.get("hot_topics", [])
        if not hot_topics:
            return

        # Pre-cache optimization hints per unique platform
        all_platforms = set()
        for topic_info in hot_topics[:3]:
            all_platforms.update(topic_info.get("platforms", ["x"]))

        hints_cache = {}
        for platform in all_platforms:
            try:
                hints_cache[platform] = await feedback_loop.get_optimization_hints(platform)
            except Exception as e:
                logger.warning(f"Failed to get hints for {platform}: {e}")
                hints_cache[platform] = {"hints_for_generation": ""}

        async with async_session() as session:
            for topic_info in hot_topics[:3]:
                topic = topic_info.get("topic", "")
                raw_platforms = topic_info.get("platforms", ["x"])
                # Normalize platform names from Claude output (e.g. "Instagram" → "instagram")
                target_platforms = [
                    self.platform_manager._normalize_platform_name(p) for p in raw_platforms
                ]
                target_platforms = [p for p in target_platforms if p]  # Remove empty (unsupported)
                # Defensive filter: only generate content for registered platforms.
                # Claude may return platforms like "instagram" even if they're disabled;
                # without this filter we'd waste API calls on text+image generation that
                # can never be published.
                target_platforms = [p for p in target_platforms if self.platform_manager.get(p)]
                # YouTube new-video queueing is gated by backlog size: stop queueing
                # while >5 SCHEDULED YT posts already exist. Drains the 2026-04-23
                # DB-corruption backlog (70 posts at 2/day) without a hard-coded date
                # we'd have to remember to remove. Existing SCHEDULED posts still
                # publish normally; this only blocks NEW queueing so we don't compound.
                if "youtube" in target_platforms:
                    async with async_session() as s:
                        yt_pending = await s.scalar(
                            select(func.count(Post.id)).where(
                                Post.platform == "youtube",
                                Post.status == PostStatus.SCHEDULED,
                            )
                        )
                    if (yt_pending or 0) > 5:
                        target_platforms = [p for p in target_platforms if p != "youtube"]

                for platform in target_platforms:
                    try:
                        # Fetch performance hints
                        hints = hints_cache.get(platform, {"hints_for_generation": ""})
                        hints_context = hints.get("hints_for_generation", "")
                        trending_ctx = json.dumps(topic_info, ensure_ascii=False) + "\n\n## パフォーマンスヒント\n" + hints_context

                        # Select content type based on ratio
                        content_type = self._select_content_type(platform)

                        # Generate content based on selected type
                        if content_type == "daily_casual":
                            content = await asyncio.to_thread(
                                self.content_generator.generate_post,
                                platform=platform,
                                topic=topic,
                                tone="casual",
                                target_audience="20-30代の美容好き",
                                trending_context=(
                                    trending_ctx + "\n\n## Important Instructions\n"
                                    "Write a casual, everyday tweet in 1-3 short lines.\n"
                                    "Do NOT include hashtags.\n"
                                    "Use 1-2 emoji max. Keep it natural and effortless.\n"
                                    "Examples: 'ice cream after a bath hits different' "
                                    "'someone come hang out with meee' "
                                    "'did my full skincare routine before bed, proud of myself'"
                                ),
                            )
                        elif content_type == "monetization":
                            # Disclosed affiliate recommendation (ステマ規制対応: PR表記必須)
                            content = await asyncio.to_thread(
                                self.content_generator.generate_post,
                                platform=platform,
                                topic=f"{topic} (honest product recommendation)",
                                target_audience=settings.target_audience,
                                trending_context=(
                                    trending_ctx + "\n\n## Important Instructions\n"
                                    "This is a sponsored/affiliate recommendation.\n"
                                    "Start the post with [PR] and keep the recommendation honest "
                                    "(mention one downside or who it is NOT for)."
                                ),
                            )
                        elif content_type in ("grwm_vlog", "try_on", "beauty_tutorial"):
                            # Video-oriented content - generate as regular post with video hint
                            content = await asyncio.to_thread(
                                self.content_generator.generate_post,
                                platform=platform,
                                topic=f"{topic} (video content)",
                                target_audience=settings.target_audience,
                                trending_context=trending_ctx,
                            )
                        elif content_type == "engagement":
                            # Engagement content (polls, questions)
                            content = await asyncio.to_thread(
                                self.content_generator.generate_post,
                                platform=platform,
                                topic=topic,
                                tone="playful",
                                target_audience=settings.target_audience,
                                trending_context=trending_ctx,
                            )
                        else:
                            # female_beauty, routine, review, etc.
                            content = await asyncio.to_thread(
                                self.content_generator.generate_post,
                                platform=platform,
                                topic=topic,
                                target_audience=settings.target_audience,
                                trending_context=trending_ctx,
                            )

                        # daily_casual: skip SEO, hashtags, A/B test, and media
                        is_text_only = content_type == "daily_casual"

                        if is_text_only:
                            final_text = content.text
                            final_hashtags = []
                            final_keywords = []
                        else:
                            # Use content generator output directly — skip separate
                            # SEO optimizer API call to save ~33% Claude cost.
                            # The content generator already outputs hashtags and keywords.
                            final_text = content.text
                            final_hashtags = content.hashtags or []
                            final_keywords = content.keywords or []

                        # Duplicate detection
                        if duplicate_detector.is_duplicate(final_text):
                            logger.info("Duplicate content detected — regenerating")
                            continue  # Skip this content, loop will try next

                        # Platform content-limit validation
                        is_safe, reason = validate_platform_content(final_text, platform)
                        if not is_safe:
                            logger.warning("Content blocked for %s: %s — regenerating", platform, reason)
                            continue

                        # Register content to prevent future duplicates
                        duplicate_detector.register(final_text)

                        # Record content type for fatigue tracking
                        fatigue_tracker.record_content(platform, content_type)

                        # A/B test: 20% of the time (skip for text-only).
                        # Raised from 5% on 2026-04-20 — too few variants
                        # were produced to drive useful feedback-loop
                        # learning on which hooks perform best.
                        ab_variants = None
                        if not is_text_only and random.random() < 0.20:
                            try:
                                ab_variants = await asyncio.to_thread(
                                    self.content_generator.ab_test_variants,
                                    final_text,
                                    platform,
                                    num_variants=2,
                                )
                                logger.info(f"Generated A/B test variants for {platform} post")
                            except Exception as e:
                                logger.warning(f"A/B variant generation failed: {e}")
                                ab_variants = None

                        # Generate media (skip for text-only content types)
                        if is_text_only:
                            media_result = (None, "text")
                        else:
                            media_result = await self._generate_media_for_post(
                                content_type=content_type,
                                platform=platform,
                                fallback_prompt=content.suggested_media_prompt,
                        )
                        media_path, media_type = media_result

                        media_urls = None
                        if media_path:
                            media_urls = [media_path]
                            post_content_type = media_type
                        elif platform in MEDIA_REQUIRED_PLATFORMS:
                            logger.warning(f"Skipping {platform} post: media generation failed")
                            continue
                        else:
                            post_content_type = "text"

                        # Schedule at optimal time (using feedback-driven smart scheduler)
                        scheduled_time = self._get_next_optimal_time(platform, feedback_hints=hints)

                        if ab_variants and len(ab_variants) >= 2:
                            # Each variant gets its own copy so publish-path
                            # mutations (e.g. stripping a failed media entry)
                            # on one post don't silently corrupt the other.
                            post_a = Post(
                                platform=platform,
                                content_type=post_content_type,
                                text_content=ab_variants[0].get("text", final_text),
                                media_urls=list(media_urls) if media_urls else None,
                                hashtags=final_hashtags,
                                keywords=final_keywords,
                                status=PostStatus.SCHEDULED,
                                scheduled_at=scheduled_time,
                                viral_score=content.viral_score_estimate,
                                ab_test_group="A",
                            )
                            session.add(post_a)

                            # Create variant B post 1 hour later
                            post_b = Post(
                                platform=platform,
                                content_type=post_content_type,
                                text_content=ab_variants[1].get("text", final_text),
                                media_urls=list(media_urls) if media_urls else None,
                                hashtags=final_hashtags,
                                keywords=final_keywords,
                                status=PostStatus.SCHEDULED,
                                scheduled_at=scheduled_time + timedelta(hours=1),
                                viral_score=content.viral_score_estimate,
                                ab_test_group="B",
                            )
                            session.add(post_b)
                        else:
                            post = Post(
                                platform=platform,
                                content_type=post_content_type,
                                text_content=final_text,
                                media_urls=media_urls,
                                hashtags=final_hashtags,
                                keywords=final_keywords,
                                status=PostStatus.SCHEDULED,
                                scheduled_at=scheduled_time,
                                viral_score=content.viral_score_estimate,
                            )
                            session.add(post)

                    except RuntimeError as e:
                        if "budget exceeded" in str(e).lower():
                            logger.warning(f"Budget exceeded, saving partial work: {e}")
                            await session.commit()
                            return
                        raise
                    except Exception as e:
                        logger.error(f"Content generation failed for {platform}/{content_type}: {e}")
                        continue

            await session.commit()

    async def _publish_scheduled(self):
        """Publish posts that are due."""
        try:
            await self._do_publish_scheduled()
        except Exception as e:
            logger.error(f"Publish job failed: {type(e).__name__}: {e}")

    async def _do_publish_scheduled(self):
        if emergency_stop.is_stopped():
            logger.warning("Emergency stop active — skipping publish cycle")
            return

        now = datetime.now(timezone.utc)

        async with async_session() as session:
            result = await session.execute(
                select(Post).where(
                    Post.status == PostStatus.SCHEDULED,
                    Post.scheduled_at <= now,
                )
            )
            posts = result.scalars().all()

            # Also pick up naive-datetime posts (stored without timezone)
            if not posts:
                result = await session.execute(
                    select(Post).where(
                        Post.status == PostStatus.SCHEDULED,
                        Post.scheduled_at <= now.replace(tzinfo=None),
                    )
                )
                posts = result.scalars().all()

            post_ids = [post.id for post in posts]

        # Human-like pacing WITHOUT blocking the scheduler: process at most one
        # post per platform this cycle, then stagger the rest into the future.
        # Previously we processed the whole batch with 10-90min sleeps inside
        # the loop, which held max_instances=1 and blocked every other job for
        # hours. See code-review item #1 (2026-04-20).
        posts_by_platform: dict[str, list[int]] = {}
        for pid in post_ids:
            async with async_session() as _sess:
                p = await _sess.get(Post, pid)
                if p and p.status == PostStatus.SCHEDULED:
                    posts_by_platform.setdefault(p.platform, []).append(pid)

        process_ids: list[int] = []
        for _plat, _ids in posts_by_platform.items():
            process_ids.append(_ids[0])
            rest = _ids[1:]
            if rest:
                import math
                async with async_session() as _sess:
                    # Cumulative stagger: post N+1 is +stagger from post N,
                    # so a batch of 5 posts spans a few hours naturally.
                    cursor = datetime.now(timezone.utc)
                    for rid in rest:
                        stagger_minutes = random.lognormvariate(math.log(25), 0.6)
                        stagger_minutes = max(10, min(stagger_minutes, 90))
                        cursor = cursor + timedelta(minutes=stagger_minutes)
                        rp = await _sess.get(Post, rid)
                        if rp:
                            rp.scheduled_at = cursor
                    await _sess.commit()
                logger.info(
                    "Staggered %d extra %s posts over the next ~%dmin",
                    len(rest), _plat, int((cursor - datetime.now(timezone.utc)).total_seconds() / 60),
                )

        # Process at most one post per platform this cycle.
        for i, post_id in enumerate(process_ids):
            post_result = None
            try:
                async with async_session() as session:
                    post = await session.get(Post, post_id)
                    if not post or post.status != PostStatus.SCHEDULED:
                        continue

                    # Platform-level cooldown (e.g. YouTube uploadLimitExceeded) — re-queue
                    if platform_cooldown.is_on_cooldown(post.platform):
                        logger.info(
                            "Platform %s on cooldown — re-scheduling post %d",
                            post.platform, post.id,
                        )
                        post.status = PostStatus.SCHEDULED
                        post.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)
                        await session.commit()
                        continue

                    # Daily limit check
                    if not daily_tracker.can_perform(post.platform, "posts"):
                        logger.info("Daily limit reached for %s — skipping post %d", post.platform, post.id)
                        post.status = PostStatus.SCHEDULED  # Re-schedule for tomorrow
                        post.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)
                        await session.commit()
                        continue

                    # Warm-up limit check
                    max_today = warmup_manager.get_max_posts_today(post.platform)
                    if daily_tracker.get_count(post.platform, "posts") >= max_today:
                        logger.info("Warm-up limit reached for %s (%d/%d) — skipping", post.platform, daily_tracker.get_count(post.platform, "posts"), max_today)
                        post.status = PostStatus.SCHEDULED
                        post.scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)
                        await session.commit()
                        continue

                    # Mark as PUBLISHING
                    post.status = PostStatus.PUBLISHING
                    await session.commit()

                    try:
                        platform = self.platform_manager.get(post.platform)
                        if not platform:
                            post.status = PostStatus.FAILED
                            await session.commit()
                            continue

                        # Check if platform needs media but post has none
                        if not post.media_urls and post.platform in MEDIA_REQUIRED_PLATFORMS:
                            logger.warning(
                                f"Post {post.id} targets {post.platform} but has no media. Skipping."
                            )
                            post.status = PostStatus.FAILED
                            await session.commit()
                            continue

                        # Build hashtag string
                        hashtags_str = ""
                        if post.hashtags:
                            hashtags_str = " " + " ".join(f"#{tag}" for tag in post.hashtags)

                        full_text = (post.text_content or "") + hashtags_str

                        # Pass hashtags + is_short hint to YouTube so its
                        # _sync_upload_video can build a proper title/desc
                        # instead of truncating the caption mid-word.
                        yt_kwargs: dict = {}
                        if post.platform == "youtube":
                            yt_kwargs["hashtags"] = post.hashtags or []
                            yt_kwargs["tags"] = [
                                (t if isinstance(t, str) else "").lstrip("#")
                                for t in (post.hashtags or [])
                            ][:15]
                            yt_kwargs["is_short"] = True  # all auto-posts are Shorts

                        # X thread posting: split long content into thread
                        async def _do_publish():
                            if post.platform == "x" and len(full_text) > 280:
                                return await self._post_x_thread(platform, full_text)
                            # Post based on content type
                            elif post.media_urls and post.content_type == "video":
                                return await platform.post_video(full_text, post.media_urls[0], **yt_kwargs)
                            elif post.media_urls and post.content_type == "image":
                                return await platform.post_image(full_text, post.media_urls[0])
                            else:
                                return await platform.post_text(full_text)

                        try:
                            post_result = await asyncio.wait_for(_do_publish(), timeout=300)
                        except asyncio.TimeoutError:
                            post.status = PostStatus.FAILED
                            post.error_message = "Publish timeout after 300s"
                            logger.error(
                                "Post %d on %s timed out after 300s — marked FAILED",
                                post.id, post.platform,
                            )
                            _cleanup_failed_post_media(post)
                            await session.commit()
                            continue

                        if post_result is not None and post_result.success:
                            post.status = PostStatus.PUBLISHED
                            post.published_at = datetime.now(timezone.utc)
                            post.platform_post_id = post_result.post_id
                            logger.info(f"Published post {post.id} to {post.platform}: {post_result.url}")

                            # Record success for emergency stop + daily tracker
                            emergency_stop.record_success(post.platform)
                            daily_tracker.record_action(post.platform, "posts")

                            # Cross-platform promotion: 20% of the time
                            if random.random() < 0.20:
                                try:
                                    # IG removed 2026-04-12 per user directive.
                                    other_platforms = [
                                        p for p in ["x", "youtube"]
                                        if p != post.platform and self.platform_manager.get(p)
                                    ]
                                    if other_platforms:
                                        target = random.choice(other_platforms)
                                        cta = generate_cross_promo_cta(post.platform, target, getattr(post_result, "url", ""))
                                        if cta:
                                            logger.info(f"Cross-promo CTA for post {post.id}: {cta}")
                                            # Post the CTA on the target platform
                                            target_platform_obj = self.platform_manager.get(target)
                                            if target_platform_obj and daily_tracker.can_perform(target, "posts"):
                                                try:
                                                    cta_result = await asyncio.wait_for(
                                                        target_platform_obj.post_text(cta),
                                                        timeout=60,
                                                    )
                                                    if cta_result and cta_result.success:
                                                        daily_tracker.record_action(target, "posts")
                                                        logger.info(
                                                            "Cross-promo CTA posted to %s: %s",
                                                            target, cta_result.url,
                                                        )
                                                    else:
                                                        logger.warning(
                                                            "Cross-promo CTA post to %s failed: %s",
                                                            target,
                                                            getattr(cta_result, "error", "unknown"),
                                                        )
                                                except asyncio.TimeoutError:
                                                    logger.warning("Cross-promo CTA post to %s timed out", target)
                                                except Exception as cta_post_err:
                                                    logger.warning("Cross-promo CTA post failed: %s", cta_post_err)
                                except Exception as e:
                                    logger.warning(f"Cross-promo CTA generation failed: {e}")
                        else:
                            post.status = PostStatus.FAILED
                            error_msg = post_result.error if post_result else "Unknown error"
                            post.error_message = error_msg[:2000] if error_msg else None
                            logger.warning(f"Post {post.id} failed on {post.platform}: {error_msg}")
                            _cleanup_failed_post_media(post)
                            _handle_platform_failure(post.platform, error_msg or "")

                    except Exception as e:
                        post.status = PostStatus.FAILED
                        post.error_message = f"{type(e).__name__}: {e}"[:2000]
                        logger.error(f"Post {post.id} exception: {type(e).__name__}: {e}")
                        _cleanup_failed_post_media(post)
                        _handle_platform_failure(post.platform, f"{type(e).__name__}: {e}")

                    await session.commit()

                    # Human-like pacing is now handled by the stagger loop
                    # above (different platforms get processed in the same
                    # cycle; same-platform posts are pushed into the future).
                    # The in-loop sleep was removed to unblock the scheduler.

            except Exception as e:
                logger.error(f"Per-post transaction failed for post {post_id}: {type(e).__name__}: {e}")

    async def _heartbeat_check(self):
        """Alert if no successful publish in N hours across ALL platforms.

        Individual failure alerts can be rate-limited or suppressed, so a
        total silent outage (like the 8-day YouTube OAuth + X cascade
        incident on 2026-04-14→22) needs its own signal. This runs every
        30min and fires at most once per `_heartbeat_alert_interval_hours`.
        """
        try:
            from sqlalchemy import func as _func
            async with async_session() as session:
                result = await session.execute(
                    select(_func.max(Post.published_at)).where(
                        Post.status == PostStatus.PUBLISHED,
                    )
                )
                last_published = result.scalar()
            if last_published is None:
                return  # Cold start — nothing to flag
            if last_published.tzinfo is None:
                last_published = last_published.replace(tzinfo=timezone.utc)
            gap_hours = (datetime.now(timezone.utc) - last_published).total_seconds() / 3600
            if gap_hours < self._heartbeat_threshold_hours:
                return
            import time as _time
            now_mono = _time.monotonic()
            gap_since_alert = now_mono - self._heartbeat_last_alert_mono
            if gap_since_alert < self._heartbeat_alert_interval_hours * 3600:
                return
            self._heartbeat_last_alert_mono = now_mono
            from src.core.alerts import alert_service
            await alert_service._send(
                f"💤 ハートビート警告: 全プラットフォームで {gap_hours:.1f} 時間、"
                f"投稿成功がありません。スケジューラ・認証・ネットワークの詰まりを確認してください。\n"
                f"最終成功 (UTC): {last_published.isoformat()}",
                "error",
            )
            logger.error(
                "Heartbeat alert fired: no published post for %.1fh (last=%s)",
                gap_hours, last_published.isoformat(),
            )
        except Exception as e:
            logger.warning("Heartbeat check failed: %s", e)

    async def _recover_stuck_posts(self):
        """Recover posts stuck in PUBLISHING state for more than 5 minutes."""
        try:
            async with async_session() as session:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
                result = await session.execute(
                    select(Post).where(
                        Post.status == PostStatus.PUBLISHING,
                        Post.updated_at < cutoff,
                    )
                )
                stuck = result.scalars().all()
                for post in stuck:
                    logger.warning(f"Recovering stuck post {post.id} (platform={post.platform}), resetting to SCHEDULED")
                    post.status = PostStatus.SCHEDULED
                await session.commit()
                if stuck:
                    logger.info(f"Recovered {len(stuck)} stuck posts")
        except Exception as e:
            logger.error(f"Stuck post recovery failed: {type(e).__name__}: {e}")

    async def _collect_metrics(self):
        """Collect metrics for published posts."""
        try:
            await self._do_collect_metrics()
        except Exception as e:
            logger.error(f"Metrics collection job failed: {type(e).__name__}: {e}")

    async def _do_collect_metrics(self):
        async with async_session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)

            result = await session.execute(
                select(Post).where(
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                    Post.platform_post_id.isnot(None),
                )
            )
            posts = result.scalars().all()

            # One batch query to find which posts were already collected in
            # the last hour — replaces the N+1 pattern of opening a session
            # per post just to check existence (2026-04-20 code review #7).
            if posts:
                recent_q = await session.execute(
                    select(PostMetric.post_id).where(
                        PostMetric.post_id.in_([p.id for p in posts]),
                        PostMetric.recorded_at >= one_hour_ago,
                    )
                )
                recently_collected = {row[0] for row in recent_q.all()}
            else:
                recently_collected = set()

            # Parallel metrics fetching with concurrency limit
            sem = asyncio.Semaphore(5)

            async def fetch_one(post):
                if post.id in recently_collected:
                    return None

                platform = self.platform_manager.get(post.platform)
                if not platform or not post.platform_post_id:
                    return None

                async with sem:
                    try:
                        metrics_data = await platform.get_metrics(post.platform_post_id)
                        if metrics_data:
                            return (post.id, metrics_data)
                    except Exception as e:
                        logger.warning(f"Metrics fetch failed for post {post.id}: {e}")
                return None

            results = await asyncio.gather(
                *[fetch_one(p) for p in posts],
                return_exceptions=True,
            )

            # Collect valid results
            valid_results = []
            for r in results:
                if isinstance(r, Exception) or r is None:
                    continue
                valid_results.append(r)

        # Batch insert metrics in a separate session (sequential, not concurrent)
        if valid_results:
            # Build a mapping of post_id -> platform from the original posts
            post_platform_map = {p.id: p.platform for p in posts}

            async with async_session() as insert_session:
                for post_id, metrics_data in valid_results:
                    metric = PostMetric(
                        post_id=post_id,
                        impressions=metrics_data.get("impressions", 0),
                        likes=metrics_data.get("likes", 0),
                        comments=metrics_data.get("comments", 0),
                        shares=metrics_data.get("shares", 0),
                        saves=metrics_data.get("saves", 0),
                        clicks=metrics_data.get("clicks", 0),
                    )
                    total_engagement = metric.likes + metric.comments + metric.shares
                    impressions = metric.impressions or 1
                    metric.engagement_rate = total_engagement / impressions
                    metric.ctr = metric.clicks / max(metric.impressions, 1)
                    insert_session.add(metric)

                    # Shadow ban detection
                    platform = post_platform_map.get(post_id)
                    if platform and metric.engagement_rate is not None:
                        shadow_ban_detector.record_engagement(platform, metric.engagement_rate)
                        if shadow_ban_detector.is_flagged(platform):
                            from src.core.alerts import alert_service
                            _task = asyncio.create_task(alert_service.notify_account_issue(
                                platform, "シャドウバン疑い: エンゲージメント急落を検出"
                            ))

                await insert_session.commit()

    async def _snapshot_followers(self):
        """Take daily follower snapshot."""
        try:
            await self._do_snapshot_followers()
        except Exception as e:
            logger.error(f"Follower snapshot job failed: {type(e).__name__}: {e}")

    async def _do_snapshot_followers(self):
        from src.core.models import FollowerSnapshot

        counts = await self.platform_manager.get_all_follower_counts()

        async with async_session() as session:
            for platform, count in counts.items():
                yesterday = datetime.now(timezone.utc) - timedelta(days=1)
                result = await session.execute(
                    select(FollowerSnapshot)
                    .where(
                        FollowerSnapshot.platform == platform,
                        FollowerSnapshot.recorded_at >= yesterday,
                    )
                    .order_by(FollowerSnapshot.recorded_at.desc())
                    .limit(1)
                )
                prev = result.scalar_one_or_none()
                daily_gain = count - prev.follower_count if prev else 0

                snapshot = FollowerSnapshot(
                    platform=platform,
                    follower_count=count,
                    daily_gain=daily_gain,
                )
                session.add(snapshot)

            await session.commit()

    async def _post_x_thread(self, platform, full_text: str):
        """Split long text into a thread and post via X platform."""
        texts = self._split_into_thread(full_text)
        results = await platform.post_thread(texts)
        # Return the first result as the representative result
        if results:
            return results[0]
        from src.platforms.base import PostResult
        return PostResult(success=False, platform="x", error="Thread posting returned no results")

    @staticmethod
    def _split_into_thread(text: str, max_len: int = 280) -> list[str]:
        """Split text into thread-sized chunks at sentence boundaries."""
        if len(text) <= max_len:
            return [text]

        import re
        # Split on Japanese/English sentence endings
        sentences = re.split(r'(?<=[。！？\.\!\?])\s*', text)
        chunks = []
        current = ""

        for sentence in sentences:
            if not sentence:
                continue
            # If a single sentence exceeds max_len, force-split it
            if len(sentence) > max_len:
                if current:
                    chunks.append(current.strip())
                    current = ""
                for i in range(0, len(sentence), max_len):
                    chunks.append(sentence[i:i + max_len])
                continue

            if len(current) + len(sentence) <= max_len:
                current += sentence
            else:
                if current:
                    chunks.append(current.strip())
                current = sentence

        if current.strip():
            chunks.append(current.strip())

        return chunks if chunks else [text[:max_len]]

    # _post_instagram_stories / _do_post_instagram_stories / _post_instagram_reels
    # removed 2026-04-12 per user directive ("TikTok IG 止めて"). The Instagram
    # platform itself (src/platforms/instagram_platform.py) is unchanged on
    # disk and re-enable would mean: configure proxy → uncomment .env creds
    # → restore add_job calls in start() → restore handlers here. Until then,
    # the publish path has zero IG entry points.

    async def _auto_reply_comments(self):
        """Fetch recent comments and generate friendly replies."""
        try:
            await self._do_auto_reply_comments()
        except Exception as e:
            logger.error(f"Auto-reply comments job failed: {type(e).__name__}: {e}")

    async def _do_auto_reply_comments(self):
        """Scan platforms for comments and generate reply suggestions."""
        # Only run between 9:00-25:00 JST
        jst_now = datetime.now(ZoneInfo(settings.default_timezone))
        hour = jst_now.hour
        if not (9 <= hour <= 23 or hour <= 1):
            logger.debug("Outside reply hours (9:00-25:00 JST), skipping")
            return

        async with async_session() as session:
            # Get recently published posts with platform IDs
            cutoff = datetime.now(timezone.utc) - timedelta(days=3)
            result = await session.execute(
                select(Post).where(
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                    Post.platform_post_id.isnot(None),
                )
            )
            posts = result.scalars().all()

        generated_replies = []
        total_comments_processed = 0
        max_comments_per_session = 15

        for post in posts:
            if total_comments_processed >= max_comments_per_session:
                logger.info("Reached max comments per session (%d), stopping", max_comments_per_session)
                break

            platform = self.platform_manager.get(post.platform)
            if not platform or not hasattr(platform, "get_comments"):
                continue

            try:
                comments = await platform.get_comments(post.platform_post_id)
            except Exception as e:
                logger.warning(f"Failed to fetch comments for post {post.id} on {post.platform}: {e}")
                continue

            for comment in comments[:10]:  # Limit to 10 most recent per post
                if total_comments_processed >= max_comments_per_session:
                    break

                comment_text = comment.get("text", "")
                commenter = comment.get("username", "unknown")
                comment_id = comment.get("id", "")

                if not comment_text:
                    continue

                # Skip already-replied comments
                if comment_id and comment_id in self._replied_comment_ids:
                    continue

                total_comments_processed += 1

                try:
                    # Random delay between replies to appear natural
                    await asyncio.sleep(random.uniform(10, 120))

                    reply = await asyncio.to_thread(
                        self.content_generator.generate_comment_reply,
                        comment_text,
                    )

                    reply_entry = {
                        "post_id": post.id,
                        "platform": post.platform,
                        "comment_id": comment_id,
                        "commenter": commenter,
                        "comment_text": comment_text,
                        "generated_reply": reply,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "posted": False,
                    }

                    # Actually post the reply if the platform supports it
                    if hasattr(platform, "post_reply") and daily_tracker.can_perform(post.platform, "replies"):
                        try:
                            reply_result = await platform.post_reply(
                                post_id=post.platform_post_id,
                                comment_id=comment_id,
                                text=reply,
                            )
                            if reply_result and getattr(reply_result, "success", False):
                                daily_tracker.record_action(post.platform, "replies")
                                reply_entry["posted"] = True
                                if comment_id:
                                    self._replied_comment_ids.add(comment_id)
                                logger.info(
                                    f"Posted reply on {post.platform} to @{commenter}: \"{reply[:50]}...\""
                                )
                            else:
                                logger.warning(f"Reply post failed on {post.platform} for comment {comment_id}")
                        except Exception as e:
                            logger.warning(f"Failed to post reply on {post.platform}: {e}")

                    generated_replies.append(reply_entry)
                    logger.info(
                        f"Generated reply for {post.platform} comment by @{commenter}: "
                        f"\"{comment_text[:50]}...\" -> \"{reply[:50]}...\""
                    )
                except Exception as e:
                    logger.warning(f"Reply generation failed for comment by @{commenter}: {e}")
                    continue

        if generated_replies:
            logger.info(f"Generated {len(generated_replies)} comment replies across all platforms")
            # Store replies to a JSON log file for manual review
            try:
                import aiofiles
                log_path = "data/comment_replies.jsonl"
                async with aiofiles.open(log_path, "a") as f:
                    for entry in generated_replies:
                        await f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                logger.info(f"Saved {len(generated_replies)} reply suggestions to {log_path}")
            except Exception as e:
                logger.warning(f"Failed to save reply log: {e}")

    def _get_next_optimal_time(self, platform: str, feedback_hints: dict | None = None) -> datetime:
        """Get the next optimal posting time for a platform."""
        tz = ZoneInfo(settings.default_timezone)
        now = datetime.now(tz)

        # Use smart scheduler with feedback data, fallback to hardcoded times
        times = get_optimal_times(platform, feedback_hints)
        if not times:
            times = self.optimal_times.get(platform, ["12:00"])

        for time_str in sorted(times):
            hour, minute = map(int, time_str.split(":"))
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate > now:
                # Add jitter for human-like timing, convert to UTC for DB storage
                scheduled_time = posting_jitter.add_jitter(candidate)
                return scheduled_time.astimezone(timezone.utc)

        # All times today have passed; schedule for tomorrow's first slot
        hour, minute = map(int, times[0].split(":"))
        tomorrow = now + timedelta(days=1)
        scheduled_time = tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Add jitter for human-like timing, convert to UTC for DB storage
        scheduled_time = posting_jitter.add_jitter(scheduled_time)
        return scheduled_time.astimezone(timezone.utc)

    async def schedule_post(
        self,
        platform: str,
        text: str,
        scheduled_at: datetime,
        content_type: str = "text",
        media_urls: list[str] | None = None,
        hashtags: list[str] | None = None,
    ) -> int:
        """Manually schedule a post."""
        async with async_session() as session:
            post = Post(
                platform=platform,
                content_type=content_type,
                text_content=text,
                media_urls=media_urls,
                hashtags=hashtags,
                status=PostStatus.SCHEDULED,
                scheduled_at=scheduled_at,
            )
            session.add(post)
            await session.commit()
            return post.id

    async def generate_and_schedule_batch(
        self,
        topic: str,
        platforms: list[str],
        num_posts: int = 5,
    ) -> list[int]:
        """Generate multiple posts and schedule them optimally."""
        post_ids = []

        for platform in platforms:
            for i in range(num_posts):
                try:
                    # Generate unique content for each post.
                    # The main queue path dropped the separate SEO optimizer
                    # call to save ~33% Claude tokens per post (2026-04); this
                    # batch endpoint was missed in that change and was burning
                    # double Claude calls until the 2026-04-20 code review.
                    content = await asyncio.to_thread(
                        self.content_generator.generate_post,
                        platform=platform,
                        topic=topic,
                    )

                    scheduled_time = self._get_next_optimal_time(platform) + timedelta(days=i)
                    post_id = await self.schedule_post(
                        platform=platform,
                        text=content.text,
                        scheduled_at=scheduled_time,
                        hashtags=content.hashtags or [],
                    )
                    post_ids.append(post_id)
                except Exception as e:
                    logger.error(f"Batch generation failed for {platform} post {i}: {e}")
                    continue

        return post_ids

    async def _check_viral_posts(self):
        """Check for viral posts and repurpose them on other platforms."""
        try:
            await self._do_check_viral_posts()
        except Exception as e:
            logger.error(f"Viral post check failed: {type(e).__name__}: {e}")

    async def _do_check_viral_posts(self):
        """Query posts from last 24 hours and repurpose high-performing ones."""
        from sqlalchemy import func

        async with async_session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

            # Get posts published in last 24 hours with metrics
            result = await session.execute(
                select(Post).where(
                    Post.status == PostStatus.PUBLISHED,
                    Post.published_at >= cutoff,
                    Post.platform_post_id.isnot(None),
                )
            )
            posts = result.scalars().all()

            for post in posts:
                # Get the latest metric for this post
                metric_result = await session.execute(
                    select(PostMetric)
                    .where(PostMetric.post_id == post.id)
                    .order_by(PostMetric.recorded_at.desc())
                    .limit(1)
                )
                metric = metric_result.scalar_one_or_none()
                if not metric or not metric.engagement_rate:
                    continue

                # Get platform average engagement rate (last 30 days)
                avg_result = await session.execute(
                    select(func.avg(PostMetric.engagement_rate)).where(
                        PostMetric.recorded_at >= datetime.now(timezone.utc) - timedelta(days=30),
                    ).join(Post).where(Post.platform == post.platform)
                )
                platform_avg = avg_result.scalar() or 0.0

                # If engagement is 2x the platform average, it's viral
                if platform_avg > 0 and metric.engagement_rate > 2 * platform_avg:
                    logger.info(
                        f"VIRAL POST detected: post {post.id} on {post.platform} "
                        f"(engagement {metric.engagement_rate:.4f} vs avg {platform_avg:.4f})"
                    )

                    # Create follow-up content on other platforms.
                    # IG removed 2026-04-12 per user directive.
                    other_platforms = [
                        p for p in ["x", "youtube"]
                        if p != post.platform and self.platform_manager.get(p)
                    ]
                    for target_platform in other_platforms:
                        try:
                            # Check if we already repurposed this post
                            existing = await session.execute(
                                select(Post).where(
                                    Post.keywords.contains(f"repurposed:{post.id}"),
                                    Post.platform == target_platform,
                                )
                            )
                            if existing.scalar_one_or_none():
                                continue

                            content = await asyncio.to_thread(
                                self.content_generator.generate_post,
                                platform=target_platform,
                                topic=post.text_content[:200],
                                target_audience=settings.target_audience,
                                trending_context=f"このコンテンツが{post.platform}でバズっています。別プラットフォーム向けにリライトしてください。",
                            )

                            scheduled_time = self._get_next_optimal_time(target_platform)

                            # Generate new media with correct aspect ratio for target platform
                            repurpose_media = None
                            try:
                                media_result = await self._generate_media_for_post(
                                    content_type=post.content_type or "female_beauty",
                                    platform=target_platform,
                                    fallback_prompt=content.suggested_media_prompt,
                                )
                                if media_result[0]:
                                    repurpose_media = [media_result[0]]
                            except Exception as media_err:
                                logger.warning(f"Repurpose media gen failed, using original: {media_err}")
                                repurpose_media = post.media_urls

                            new_post = Post(
                                platform=target_platform,
                                content_type=post.content_type,
                                text_content=content.text,
                                media_urls=repurpose_media,
                                keywords=[f"repurposed:{post.id}"],  # Track source in metadata, not visible text
                                status=PostStatus.SCHEDULED,
                                scheduled_at=scheduled_time,
                                viral_score=content.viral_score_estimate,
                            )
                            session.add(new_post)
                            logger.info(f"Repurposed viral post {post.id} to {target_platform}")
                        except Exception as e:
                            logger.warning(f"Failed to repurpose post {post.id} to {target_platform}: {e}")
                            continue

            await session.commit()

    async def _scan_competitors(self):
        """Daily competitor analysis scan."""
        try:
            await self._do_scan_competitors()
        except Exception as e:
            logger.error(f"Competitor scan failed: {type(e).__name__}: {e}")

    async def _do_scan_competitors(self):
        """Query all Competitor records and analyze each."""
        from src.core.models import Competitor

        async with async_session() as session:
            result = await session.execute(select(Competitor))
            competitors = result.scalars().all()

        if not competitors:
            logger.debug("No competitors configured, skipping scan")
            return

        async with async_session() as session:
            for competitor in competitors:
                try:
                    analysis = await self.trend_researcher.analyze_competitor(
                        username=competitor.username,
                        platform=competitor.platform,
                    )
                    # Persist analysis results to DB
                    comp = await session.get(Competitor, competitor.id)
                    if comp and isinstance(analysis, dict):
                        comp.top_hashtags = analysis.get("content_strategy", {}).get("main_themes", [])
                        comp.content_strategy = json.dumps(analysis, ensure_ascii=False)
                        comp.avg_engagement_rate = analysis.get("engagement_analysis", {}).get("avg_rate")
                        posting = analysis.get("posting_patterns", {})
                        if isinstance(posting.get("frequency"), (int, float)):
                            comp.posting_frequency = float(posting["frequency"])
                        comp.last_analyzed_at = datetime.now(timezone.utc)

                    logger.info(
                        "Competitor analysis for @%s on %s saved to DB",
                        competitor.username, competitor.platform,
                    )
                except Exception as e:
                    logger.warning(f"Failed to analyze competitor @{competitor.username}: {e}")
                    continue

            await session.commit()
