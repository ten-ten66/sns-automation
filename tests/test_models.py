"""Tests for database models."""

import pytest
from datetime import datetime, timezone
from sqlalchemy import select

from src.core.models import Post, PostMetric, Revenue, PostStatus, FollowerSnapshot


@pytest.mark.asyncio
class TestPostModel:

    async def test_create_post(self, db_session):
        post = Post(
            platform="x",
            content_type="text",
            text_content="テスト投稿",
            status=PostStatus.DRAFT,
        )
        db_session.add(post)
        await db_session.commit()
        assert post.id is not None

    async def test_post_with_metrics(self, db_session):
        post = Post(
            platform="instagram",
            content_type="image",
            text_content="テスト",
            status=PostStatus.PUBLISHED,
            published_at=datetime.now(timezone.utc),
        )
        db_session.add(post)
        await db_session.commit()

        metric = PostMetric(
            post_id=post.id,
            impressions=1000,
            likes=100,
            comments=10,
            shares=5,
            engagement_rate=0.115,
        )
        db_session.add(metric)
        await db_session.commit()

        result = await db_session.execute(
            select(PostMetric).where(PostMetric.post_id == post.id)
        )
        saved_metric = result.scalar_one()
        assert saved_metric.likes == 100
        assert saved_metric.engagement_rate == 0.115

    async def test_post_status_enum(self, db_session):
        for status in PostStatus:
            post = Post(
                platform="x",
                content_type="text",
                text_content=f"Status: {status.value}",
                status=status,
            )
            db_session.add(post)
        await db_session.commit()

        result = await db_session.execute(select(Post))
        posts = result.scalars().all()
        assert len(posts) >= len(PostStatus)


@pytest.mark.asyncio
class TestRevenueModel:

    async def test_create_revenue(self, db_session):
        rev = Revenue(
            source="affiliate",
            network="a8",
            amount=5000,
            clicks=200,
            conversions=5,
        )
        db_session.add(rev)
        await db_session.commit()
        assert rev.id is not None
        assert rev.currency == "JPY"


@pytest.mark.asyncio
class TestFollowerSnapshot:

    async def test_create_snapshot(self, db_session):
        snapshot = FollowerSnapshot(
            platform="x",
            follower_count=10000,
            daily_gain=50,
        )
        db_session.add(snapshot)
        await db_session.commit()
        assert snapshot.id is not None
