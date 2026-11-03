"""Tests for the FastAPI endpoints."""

import pytest
import pytest_asyncio
from unittest.mock import MagicMock, AsyncMock, patch

from httpx import AsyncClient, ASGITransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AUTH_HEADERS = {"Authorization": "Bearer test-api-key-12345"}
CONTENT_JSON = {"Content-Type": "application/json"}


def _auth_headers():
    return {"Authorization": "Bearer test-api-key-12345"}


# ---------------------------------------------------------------------------
# App fixture — patch out heavy startup dependencies
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app():
    """Import the FastAPI app with heavy lifespan tasks stubbed out."""
    with patch("src.api.app.PlatformManager") as mock_pm_cls, \
         patch("src.api.app.ClaudeContentGenerator"), \
         patch("src.api.app.TrendResearcher"), \
         patch("src.api.app.SEOOptimizer"), \
         patch("src.api.app.AnalyticsEngine"), \
         patch("src.api.app.RevenueTracker"), \
         patch("src.api.app.AutoPoster") as mock_ap_cls, \
         patch("src.api.app.init_db", new=AsyncMock()), \
         patch("src.api.app.engine"):

        mock_pm = MagicMock()
        mock_pm._platforms = {}
        mock_pm.register_all = MagicMock()
        mock_pm_cls.return_value = mock_pm

        mock_ap = MagicMock()
        mock_ap.start = AsyncMock()
        mock_ap.scheduler = MagicMock()
        mock_ap.scheduler.running = True
        mock_ap.scheduler.get_jobs.return_value = []
        mock_ap.trend_researcher = MagicMock()
        mock_ap.trend_researcher.close = AsyncMock()
        mock_ap_cls.return_value = mock_ap

        from src.api.app import app as fastapi_app
        yield fastapi_app


@pytest_asyncio.fixture
async def client(app):
    """Async HTTP client that bypasses the lifespan to avoid scheduler startup."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# /health - no auth required
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health_no_auth_required(self, client):
        """Health endpoint must be accessible without authentication."""
        with patch("src.api.app.async_session") as mock_sess_ctx, \
             patch("src.api.app.cost_tracker") as mock_cost:

            # Mock DB session context manager
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock()
            mock_sess_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sess_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_cost.is_budget_exceeded.return_value = False

            response = await client.get("/health")

        assert response.status_code in (200, 503)
        body = response.json()
        assert "status" in body
        assert "timestamp" in body

    @pytest.mark.asyncio
    async def test_health_returns_json(self, client):
        with patch("src.api.app.async_session") as mock_sess_ctx, \
             patch("src.api.app.cost_tracker") as mock_cost:

            mock_session = AsyncMock()
            mock_session.execute = AsyncMock()
            mock_sess_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sess_ctx.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_cost.is_budget_exceeded.return_value = False

            response = await client.get("/health")

        assert response.headers["content-type"].startswith("application/json")


# ---------------------------------------------------------------------------
# Auth checks — endpoints require Bearer token
# ---------------------------------------------------------------------------

class TestAuthRequired:
    @pytest.mark.asyncio
    async def test_content_generate_requires_auth(self, client):
        response = await client.post(
            "/api/content/generate",
            json={"topic": "test", "platform": "x"},
        )
        # No credentials → HTTPBearer returns 403 (missing scheme) or 401
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_post_now_requires_auth(self, client):
        response = await client.post(
            "/api/post/now",
            json={"text": "Hello", "platform": "x"},
        )
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_analytics_summary_requires_auth(self, client):
        response = await client.get("/api/analytics/summary")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_wrong_api_key_returns_401(self, client):
        response = await client.post(
            "/api/content/generate",
            json={"topic": "test", "platform": "x"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_emergency_stop_requires_auth(self, client):
        response = await client.post(
            "/api/safety/emergency-stop",
            json={"reason": "test"},
        )
        assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /api/content/generate
# ---------------------------------------------------------------------------

class TestContentGenerate:
    @pytest.mark.asyncio
    async def test_generate_content_success(self, client):
        mock_content = MagicMock()
        mock_content.text = "Generated post text"
        mock_content.hashtags = ["test", "ai"]
        mock_content.keywords = ["keyword1"]
        mock_content.suggested_media_prompt = "A beautiful image"
        mock_content.viral_score_estimate = 7.5
        mock_content.variations = []

        with patch("src.api.app.content_generator") as mock_gen, \
             patch("asyncio.to_thread", new=AsyncMock(return_value=mock_content)):
            mock_gen.generate_post.return_value = mock_content

            response = await client.post(
                "/api/content/generate",
                json={"topic": "AI trends", "platform": "x", "tone": "casual"},
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        body = response.json()
        assert "text" in body
        assert "hashtags" in body

    @pytest.mark.asyncio
    async def test_generate_content_accepts_null_seo_keywords(self, client):
        """The dashboard sends seo_keywords=null when the field is left empty."""
        mock_content = MagicMock()
        mock_content.text = "Generated post text"
        mock_content.hashtags = []
        mock_content.keywords = []
        mock_content.suggested_media_prompt = ""
        mock_content.viral_score_estimate = 0.5
        mock_content.variations = []

        with patch("src.api.app.content_generator"),              patch("asyncio.to_thread", new=AsyncMock(return_value=mock_content)):
            response = await client.post(
                "/api/content/generate",
                json={"topic": "lip review", "platform": "x", "seo_keywords": None},
                headers=_auth_headers(),
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_generate_content_invalid_platform(self, client):
        response = await client.post(
            "/api/content/generate",
            json={"topic": "test", "platform": "invalid_platform"},
            headers=_auth_headers(),
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_generate_content_all_platforms(self, client):
        """Valid platforms should not raise a 400."""
        mock_content = MagicMock()
        mock_content.text = "Post"
        mock_content.hashtags = []
        mock_content.keywords = []
        mock_content.suggested_media_prompt = ""
        mock_content.viral_score_estimate = 5.0
        mock_content.variations = []

        for platform in ["x", "instagram", "tiktok", "youtube"]:
            with patch("asyncio.to_thread", new=AsyncMock(return_value=mock_content)):
                response = await client.post(
                    "/api/content/generate",
                    json={"topic": "test", "platform": platform},
                    headers=_auth_headers(),
                )
            assert response.status_code == 200, f"Platform {platform} failed: {response.text}"


# ---------------------------------------------------------------------------
# POST /api/post/now
# ---------------------------------------------------------------------------

class TestPostNow:
    @pytest.mark.asyncio
    async def test_post_now_platform_not_found(self, client):
        """When the platform is not registered, should return 404."""
        with patch("src.api.app.platform_manager") as mock_pm, \
             patch("src.api.app.validate_media_path", return_value=None):
            mock_pm.get.return_value = None

            response = await client.post(
                "/api/post/now",
                json={"text": "Hello world!", "platform": "x"},
                headers=_auth_headers(),
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_post_now_text_success(self, client):
        """Successful text post returns success dict."""
        from src.platforms.base import PostResult
        mock_result = PostResult(success=True, platform="x", post_id="123", url="https://x.com/i/status/123")

        mock_platform = AsyncMock()
        mock_platform.post_text = AsyncMock(return_value=mock_result)

        with patch("src.api.app.platform_manager") as mock_pm, \
             patch("src.api.app.validate_media_path", return_value=None):
            mock_pm.get.return_value = mock_platform

            response = await client.post(
                "/api/post/now",
                json={"text": "Hello world!", "platform": "x"},
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["post_id"] == "123"

    @pytest.mark.asyncio
    async def test_post_now_invalid_platform_returns_400(self, client):
        response = await client.post(
            "/api/post/now",
            json={"text": "Hello", "platform": "myspace"},
            headers=_auth_headers(),
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_post_now_with_image(self, client):
        from src.platforms.base import PostResult
        mock_result = PostResult(success=True, platform="x", post_id="img123")

        mock_platform = AsyncMock()
        mock_platform.post_image = AsyncMock(return_value=mock_result)

        # validate_media_path is a regular function — use a lambda, not AsyncMock
        with patch("src.api.app.platform_manager") as mock_pm, \
             patch("src.api.app.validate_media_path", side_effect=lambda p: "/opt/sns-automation/data/media/test.jpg" if p else None):
            mock_pm.get.return_value = mock_platform

            response = await client.post(
                "/api/post/now",
                json={"text": "Image post", "platform": "x", "image_path": "/opt/sns-automation/data/media/test.jpg"},
                headers=_auth_headers(),
            )

        assert response.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/analytics/summary
# ---------------------------------------------------------------------------

class TestAnalyticsSummary:
    @pytest.mark.asyncio
    async def test_analytics_summary_success(self, client):
        mock_summary = {
            "total_posts": 10,
            "published_posts": 8,
            "total_impressions": 50000,
            "total_likes": 1200,
        }

        with patch("src.api.app.analytics") as mock_analytics:
            mock_analytics.get_performance_summary = AsyncMock(return_value=mock_summary)

            response = await client.get(
                "/api/analytics/summary",
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        body = response.json()
        assert "total_posts" in body

    @pytest.mark.asyncio
    async def test_analytics_summary_custom_days(self, client):
        mock_summary = {"total_posts": 5}

        with patch("src.api.app.analytics") as mock_analytics:
            mock_analytics.get_performance_summary = AsyncMock(return_value=mock_summary)

            response = await client.get(
                "/api/analytics/summary?days=7",
                headers=_auth_headers(),
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_analytics_summary_invalid_days(self, client):
        """days must be between 1 and 365."""
        response = await client.get(
            "/api/analytics/summary?days=0",
            headers=_auth_headers(),
        )
        assert response.status_code == 422  # Validation error


# ---------------------------------------------------------------------------
# POST /api/safety/emergency-stop and /api/safety/resume
# ---------------------------------------------------------------------------

class TestSafetyEndpoints:
    @pytest.mark.asyncio
    async def test_emergency_stop_activates(self, client):
        with patch("src.core.safety.emergency_stop") as mock_stop:
            mock_stop.activate = MagicMock()

            response = await client.post(
                "/api/safety/emergency-stop",
                json={"reason": "Test emergency stop"},
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "stopped"
        assert body["reason"] == "Test emergency stop"

    @pytest.mark.asyncio
    async def test_emergency_stop_default_reason(self, client):
        response = await client.post(
            "/api/safety/emergency-stop",
            json={},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "stopped"
        assert "reason" in body

    @pytest.mark.asyncio
    async def test_resume_deactivates_stop(self, client):
        with patch("src.core.safety.emergency_stop") as mock_stop:
            mock_stop.deactivate = MagicMock()

            response = await client.post(
                "/api/safety/resume",
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "running"

    @pytest.mark.asyncio
    async def test_safety_status_endpoint(self, client):
        with patch("src.core.safety.emergency_stop") as mock_stop, \
             patch("src.core.safety.daily_tracker") as mock_tracker, \
             patch("src.core.safety.shadow_ban_detector") as mock_sbd, \
             patch("src.core.safety.warmup_manager") as mock_warmup:

            mock_stop.get_status.return_value = {
                "stopped": False,
                "reason": "",
                "stopped_at": None,
                "consecutive_failures": {},
            }
            mock_stop.is_stopped.return_value = False
            mock_tracker.get_summary.return_value = {}
            mock_sbd.is_flagged.return_value = False
            mock_warmup.is_warmed_up.return_value = True
            mock_warmup.start_date = None

            response = await client.get(
                "/api/safety/status",
                headers=_auth_headers(),
            )

        assert response.status_code == 200
        body = response.json()
        assert "emergency_stop" in body

    @pytest.mark.asyncio
    async def test_stop_then_resume_cycle(self, client):
        """Full stop → resume cycle should work without errors."""
        # Stop
        stop_response = await client.post(
            "/api/safety/emergency-stop",
            json={"reason": "Integration test"},
            headers=_auth_headers(),
        )
        assert stop_response.status_code == 200
        assert stop_response.json()["status"] == "stopped"

        # Resume
        resume_response = await client.post(
            "/api/safety/resume",
            headers=_auth_headers(),
        )
        assert resume_response.status_code == 200
        assert resume_response.json()["status"] == "running"
