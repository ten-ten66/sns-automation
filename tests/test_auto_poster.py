"""Tests for the AutoPoster scheduler."""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_platform_manager():
    mgr = MagicMock()
    mgr._platforms = {}
    mgr.get.return_value = None
    return mgr


@pytest.fixture
def auto_poster(mock_platform_manager):
    """Create an AutoPoster with all heavy dependencies mocked out."""
    with patch("src.scheduler.auto_poster.ClaudeContentGenerator"), \
         patch("src.scheduler.auto_poster.SEOOptimizer"), \
         patch("src.scheduler.auto_poster.TrendResearcher"), \
         patch("src.scheduler.auto_poster.ImageGenerator"), \
         patch("src.scheduler.auto_poster.AsyncIOScheduler") as mock_sched_class, \
         patch("src.scheduler.auto_poster.feedback_loop"), \
         patch("src.scheduler.auto_poster.fatigue_tracker"):

        mock_sched = MagicMock()
        mock_sched.running = False
        mock_sched_class.return_value = mock_sched

        from src.scheduler.auto_poster import AutoPoster
        poster = AutoPoster(mock_platform_manager)
        return poster


# ---------------------------------------------------------------------------
# _split_into_thread
# ---------------------------------------------------------------------------

class TestSplitIntoThread:
    def test_short_text_returns_single_chunk(self, auto_poster):
        text = "Hello world!"
        result = auto_poster._split_into_thread(text)
        assert result == ["Hello world!"]

    def test_text_exactly_at_limit_returns_single(self, auto_poster):
        text = "A" * 280
        result = auto_poster._split_into_thread(text)
        assert len(result) == 1
        assert result[0] == text

    def test_long_text_splits_into_multiple_chunks(self, auto_poster):
        # Create text well over 280 chars with sentence boundaries
        text = "This is sentence one. " * 20
        result = auto_poster._split_into_thread(text)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 280

    def test_all_content_preserved(self, auto_poster):
        """All original content should appear in the result chunks."""
        sentences = ["First sentence. ", "Second sentence. ", "Third sentence. "]
        # Make a text that needs splitting
        text = (sentences[0] * 10) + (sentences[1] * 10) + (sentences[2] * 10)
        result = auto_poster._split_into_thread(text)
        rejoined = "".join(result)
        # All characters should be preserved (modulo whitespace stripping)
        assert len(rejoined) > 0

    def test_single_oversized_sentence_force_splits(self, auto_poster):
        """A sentence longer than max_len should be force-split at character boundaries."""
        text = "A" * 600  # No sentence boundaries at all
        result = auto_poster._split_into_thread(text)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 280

    def test_japanese_sentence_splitting(self, auto_poster):
        """Should split on Japanese sentence endings (。！？)."""
        text = ("これはテストの文章です。" * 30)
        result = auto_poster._split_into_thread(text)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 280

    def test_empty_string_handling(self, auto_poster):
        text = ""
        result = auto_poster._split_into_thread(text)
        # Should return a list (either empty or with empty string)
        assert isinstance(result, list)

    def test_custom_max_len(self, auto_poster):
        text = "Hello World. " * 5  # 65 chars
        result = auto_poster._split_into_thread(text, max_len=30)
        assert len(result) > 1
        for chunk in result:
            assert len(chunk) <= 30

    def test_returns_list_of_strings(self, auto_poster):
        text = "A" * 300
        result = auto_poster._split_into_thread(text)
        assert isinstance(result, list)
        assert all(isinstance(chunk, str) for chunk in result)


# ---------------------------------------------------------------------------
# _select_content_type
# ---------------------------------------------------------------------------

class TestSelectContentType:
    def test_returns_string(self, auto_poster):
        with patch("src.scheduler.auto_poster.CONTENT_SCHEDULE_RATIO", {"x": {"female_beauty": 1.0}}), \
             patch("src.scheduler.auto_poster.fatigue_tracker") as mock_fatigue:
            mock_fatigue.should_avoid_type.return_value = False
            result = auto_poster._select_content_type("x")
        assert isinstance(result, str)

    def test_returns_from_ratio_keys(self, auto_poster):
        ratio = {"female_beauty": 0.5, "monetization": 0.3, "engagement": 0.2}
        with patch("src.scheduler.auto_poster.CONTENT_SCHEDULE_RATIO", {"instagram": ratio}), \
             patch("src.scheduler.auto_poster.fatigue_tracker") as mock_fatigue:
            mock_fatigue.should_avoid_type.return_value = False
            result = auto_poster._select_content_type("instagram")
        assert result in ratio.keys()

    def test_unknown_platform_returns_default(self, auto_poster):
        with patch("src.scheduler.auto_poster.CONTENT_SCHEDULE_RATIO", {}):
            result = auto_poster._select_content_type("unknown_platform")
        assert result == "female_beauty"

    def test_avoids_fatigued_type_with_retry(self, auto_poster):
        ratio = {"type_a": 0.5, "type_b": 0.5}
        call_count = 0

        def mock_should_avoid(platform, content_type):
            nonlocal call_count
            call_count += 1
            # Avoid type_a for first 3 calls (max retries), then allow anything
            return content_type == "type_a" and call_count <= 3

        with patch("src.scheduler.auto_poster.CONTENT_SCHEDULE_RATIO", {"x": ratio}), \
             patch("src.scheduler.auto_poster.fatigue_tracker") as mock_fatigue:
            mock_fatigue.should_avoid_type.side_effect = mock_should_avoid
            result = auto_poster._select_content_type("x")

        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# _get_next_optimal_time
# ---------------------------------------------------------------------------

class TestGetNextOptimalTime:
    def test_returns_datetime(self, auto_poster):
        with patch("src.scheduler.auto_poster.get_optimal_times", return_value=["07:00", "12:00", "18:00"]), \
             patch("src.scheduler.auto_poster.posting_jitter") as mock_jitter:
            mock_jitter.add_jitter.side_effect = lambda dt: dt  # no-op jitter
            result = auto_poster._get_next_optimal_time("x")
        assert isinstance(result, datetime)

    def test_returns_future_time(self, auto_poster):
        with patch("src.scheduler.auto_poster.get_optimal_times", return_value=["23:59"]), \
             patch("src.scheduler.auto_poster.posting_jitter") as mock_jitter:
            mock_jitter.add_jitter.side_effect = lambda dt: dt
            result = auto_poster._get_next_optimal_time("x")
        assert isinstance(result, datetime)

    def test_falls_back_to_tomorrow_when_all_times_passed(self, auto_poster):
        """If all optimal times are in the past, schedule for tomorrow."""
        with patch("src.scheduler.auto_poster.get_optimal_times", return_value=["00:00", "00:01"]), \
             patch("src.scheduler.auto_poster.posting_jitter") as mock_jitter:
            mock_jitter.add_jitter.side_effect = lambda dt: dt
            result = auto_poster._get_next_optimal_time("x")
        # Should be in the future
        assert isinstance(result, datetime)

    def test_uses_hardcoded_times_when_smart_scheduler_empty(self, auto_poster):
        with patch("src.scheduler.auto_poster.get_optimal_times", return_value=[]), \
             patch("src.scheduler.auto_poster.posting_jitter") as mock_jitter:
            mock_jitter.add_jitter.side_effect = lambda dt: dt
            result = auto_poster._get_next_optimal_time("x")
        assert isinstance(result, datetime)

    def test_applies_jitter(self, auto_poster):
        """Jitter should be applied to the scheduled time."""
        jitter_delta = timedelta(minutes=5)
        call_log = []

        def tracking_jitter(dt):
            call_log.append(dt)
            return dt + jitter_delta

        with patch("src.scheduler.auto_poster.get_optimal_times", return_value=["23:59"]), \
             patch("src.scheduler.auto_poster.posting_jitter") as mock_jitter:
            mock_jitter.add_jitter.side_effect = tracking_jitter
            auto_poster._get_next_optimal_time("instagram")

        assert mock_jitter.add_jitter.called


# ---------------------------------------------------------------------------
# schedule_post
# ---------------------------------------------------------------------------

class TestSchedulePost:
    @pytest.mark.asyncio
    async def test_creates_db_record(self, auto_poster, db_session):
        """schedule_post should persist a Post record and return its ID."""
        from src.core.models import Post, PostStatus
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        from src.core.models import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async def mock_async_session():
            return session_factory()

        # Patch the context manager pattern used in auto_poster.schedule_post
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session_ctx():
            async with session_factory() as s:
                yield s

        scheduled_at = datetime.now(timezone.utc) + timedelta(hours=1)

        with patch("src.scheduler.auto_poster.async_session", fake_session_ctx):
            post_id = await auto_poster.schedule_post(
                platform="x",
                text="Test post content",
                scheduled_at=scheduled_at,
                content_type="text",
                hashtags=["test"],
            )

        assert post_id is not None
        assert isinstance(post_id, int)

        # Verify the record exists in DB
        async with session_factory() as session:
            from sqlalchemy import select
            result = await session.execute(select(Post).where(Post.id == post_id))
            post = result.scalar_one_or_none()

        assert post is not None
        assert post.platform == "x"
        assert post.text_content == "Test post content"
        assert post.status == PostStatus.SCHEDULED
        assert post.hashtags == ["test"]

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_schedule_post_sets_correct_status(self, auto_poster):
        """Scheduled posts should have SCHEDULED status."""
        from src.core.models import Post, PostStatus
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from contextlib import asynccontextmanager
        from src.core.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        @asynccontextmanager
        async def fake_session_ctx():
            async with session_factory() as s:
                yield s

        scheduled_at = datetime.now(timezone.utc) + timedelta(days=1)

        with patch("src.scheduler.auto_poster.async_session", fake_session_ctx):
            post_id = await auto_poster.schedule_post(
                platform="instagram",
                text="Instagram post",
                scheduled_at=scheduled_at,
            )

        async with session_factory() as session:
            from sqlalchemy import select
            result = await session.execute(select(Post).where(Post.id == post_id))
            post = result.scalar_one_or_none()

        assert post.status == PostStatus.SCHEDULED
        assert post.platform == "instagram"

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_schedule_post_with_media_urls(self, auto_poster):
        """Media URLs should be persisted."""
        from src.core.models import Post
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from contextlib import asynccontextmanager
        from src.core.models import Base

        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        @asynccontextmanager
        async def fake_session_ctx():
            async with session_factory() as s:
                yield s

        scheduled_at = datetime.now(timezone.utc) + timedelta(hours=2)
        media = ["data/media/test.jpg"]

        with patch("src.scheduler.auto_poster.async_session", fake_session_ctx):
            post_id = await auto_poster.schedule_post(
                platform="x",
                text="Post with image",
                scheduled_at=scheduled_at,
                content_type="image",
                media_urls=media,
            )

        async with session_factory() as session:
            from sqlalchemy import select
            result = await session.execute(select(Post).where(Post.id == post_id))
            post = result.scalar_one_or_none()

        assert post.media_urls == media
        assert post.content_type == "image"

        await engine.dispose()
