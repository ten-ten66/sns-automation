"""FastAPI application for the SNS automation dashboard."""

import asyncio
import hmac
import json
import logging
import time
from datetime import datetime, date, timedelta, timezone
from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add baseline security headers to every response.

    The public LP (hoshimilive.com) previously served HTML without CSP,
    X-Frame-Options, or HSTS, enabling clickjacking and weakening XSS
    defenses. These headers are cheap and uncontroversial.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
        # Permissive CSP: the LP loads inline scripts/styles and external
        # fonts/analytics; tightening further needs a per-page audit.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self' https: data:; "
            "script-src 'self' 'unsafe-inline' https:; "
            "style-src 'self' 'unsafe-inline' https:; "
            "img-src 'self' data: https:; "
            "font-src 'self' data: https:; "
            "frame-ancestors 'none'",
        )
        return response

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", _PROJECT_ROOT / "data" / "media"))

from sqlalchemy import select, func, text

from src.core.database import init_db, async_session, engine
from src.core.cost_tracker import cost_tracker, BudgetExceededError
from src.core.feedback_loop import feedback_loop
from src.core.models import (
    Post,
    PostStatus,
    AffiliateProduct,
    FortuneReading,
    AgentLog,
    AgentTask,
)
from src.platforms.manager import PlatformManager
from src.ai.claude_generator import ClaudeContentGenerator
from src.ai.trend_researcher import TrendResearcher
from src.seo.optimizer import SEOOptimizer
from src.analytics.analyzer import AnalyticsEngine
from src.monetization.revenue_tracker import RevenueTracker
from src.scheduler.auto_poster import AutoPoster
from src.api.routes import fortune as fortune_routes
from src.api.routes import webhooks as webhook_routes
from src.agents import Orchestrator

logger = logging.getLogger(__name__)

platform_manager = PlatformManager()
content_generator = ClaudeContentGenerator()
trend_researcher = TrendResearcher()
seo_optimizer = SEOOptimizer()
analytics = AnalyticsEngine()
revenue_tracker = RevenueTracker()
auto_poster = AutoPoster(platform_manager)
orchestrator = Orchestrator()

VALID_PLATFORMS = {"x", "instagram", "tiktok", "youtube"}

def validate_platform(platform: str) -> str:
    if platform not in VALID_PLATFORMS:
        raise HTTPException(400, f"Invalid platform '{platform}'. Must be one of: {VALID_PLATFORMS}")
    return platform

def validate_platforms(platforms: list[str]) -> list[str]:
    for p in platforms:
        validate_platform(p)
    return platforms

_start_time = time.time()

# --- Authentication ---
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
security = HTTPBearer(auto_error=False)

# Paths that do not require authentication
_PUBLIC_PATHS = (
    {"/docs", "/redoc", "/openapi.json", "/health"}
    | fortune_routes.PUBLIC_PATHS
    | webhook_routes.PUBLIC_PATHS
)


async def verify_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Verify API key from header. Skips auth for docs and health endpoints."""
    if request.url.path in _PUBLIC_PATHS:
        return True
    if credentials is None:
        raise HTTPException(status_code=401, detail="Authorization header required")
    from config.settings import settings as _s
    api_key = _s.sns_api_key
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="Server not configured: SNS_API_KEY environment variable is required",
        )
    if not hmac.compare_digest(credentials.credentials, api_key):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return True


def validate_media_path(path: str | None) -> str | None:
    """Validate media file path to prevent path traversal."""
    if not path:
        return None
    resolved = Path(path).resolve()
    ALLOWED_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if not resolved.is_relative_to(ALLOWED_MEDIA_DIR.resolve()):
        raise HTTPException(status_code=400, detail=f"Media path must be under {ALLOWED_MEDIA_DIR}")
    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Media file not found: {path}")
    return str(resolved)


def _setup_log_rotation():
    """Configure a rotating file handler for the root logger."""
    from logging.handlers import RotatingFileHandler
    log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "sns_automation.log",
        maxBytes=50 * 1024 * 1024,  # 50 MB
        backupCount=5,
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    logging.getLogger().addHandler(file_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_log_rotation()
    await init_db()
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=32))
    # Store event loop reference for thread-safe budget alerts
    cost_tracker.set_event_loop(loop)
    platform_manager.register_all()
    await auto_poster.start()
    try:
        yield
    finally:
        try:
            logger.info("Shutting down scheduler (waiting up to 30s for in-flight jobs)...")
            auto_poster.scheduler.shutdown(wait=True)
            # Wait for in-flight async tasks to complete
            pending = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task() and not t.done()]
            if pending:
                await asyncio.wait(pending, timeout=30)
            logger.info("Scheduler shutdown complete.")
        except Exception as e:
            logger.error(f"Error stopping auto_poster: {e}")
        # Close platform HTTP clients
        try:
            await auto_poster.trend_researcher.close()
        except Exception as e:
            logger.warning("Error closing auto_poster trend_researcher: %s", e)
        for _name, _plat in platform_manager._platforms.items():
            if hasattr(_plat, "close"):
                try:
                    await _plat.close()
                except Exception as e:
                    logger.warning("Error closing platform %s: %s", _name, e)
        try:
            await trend_researcher.close()
        except Exception as e:
            logger.error(f"Error closing trend_researcher: {e}")
        try:
            await engine.dispose()
        except Exception as e:
            logger.error(f"Error disposing engine: {e}")


app = FastAPI(
    title="SNS Automation System",
    description="AI-powered SNS automation for content creation, posting, and monetization",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    dependencies=[Depends(verify_api_key)],  # Global auth on all endpoints
)

# Baseline security headers on every response (incl. public LP)
app.add_middleware(SecurityHeadersMiddleware)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter

# Serve static legal pages (terms, privacy) without auth for TikTok app review
from fastapi.staticfiles import StaticFiles
_static_dir = Path(__file__).parent.parent.parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Public fortune routes (hoshimilive.com LP + free fortune API)
# Auth bypass is handled via fortune_routes.PUBLIC_PATHS merged into _PUBLIC_PATHS above.
app.include_router(fortune_routes.router)

# Payment webhook receivers (Paddle + Lemon Squeezy).
# Authenticated via HMAC signature in each handler, not via global API key.
app.include_router(webhook_routes.router)

app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Global exception handler
@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled error: {type(exc).__name__}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"},  # Never expose internal details
    )


@app.exception_handler(BudgetExceededError)
async def budget_exceeded_handler(request, exc):
    return JSONResponse(
        status_code=429,
        content={
            "error": "Daily API budget exceeded. Try again tomorrow.",
            "budget": cost_tracker.get_summary(),
        },
    )


# --- Health Check & Metrics ---

@app.get("/health", dependencies=[])
async def health_check():
    """Health check endpoint (no auth required)."""
    # Database check with timeout
    db_status = "ok"
    last_success: dict[str, str | None] = {}
    try:
        async with asyncio.timeout(5):
            async with async_session() as session:
                await session.execute(text("SELECT 1"))
                # Per-platform last successful publish — surfaces silent post
                # failure (cf. project_error_check_2026_04_22: 8 days went by
                # before anyone noticed YT OAuth had broken).
                rows = (await session.execute(
                    select(Post.platform, func.max(Post.published_at))
                    .where(Post.status == PostStatus.PUBLISHED)
                    .group_by(Post.platform)
                )).all()
                last_success = {p: (ts.isoformat() if ts else None) for p, ts in rows}
    except Exception as e:
        logger.warning("Health check: database unavailable: %s", e)
        db_status = "error"

    # Scheduler check
    try:
        scheduler_status = "running" if auto_poster.scheduler.running else "stopped"
    except Exception as e:
        logger.warning("Health check: scheduler status unknown: %s", e)
        scheduler_status = "stopped"

    # Stale-platform detection: any platform with no successful post in >24h.
    # Informational only — overall status doesn't flip on stale (e.g. YT may
    # be intentionally paused via the backlog gate).
    now = datetime.now(timezone.utc)
    stale_platforms: list[str] = []
    for plat, iso in last_success.items():
        if iso is None:
            stale_platforms.append(plat)
            continue
        try:
            ts = datetime.fromisoformat(iso)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if (now - ts) > timedelta(hours=24):
                stale_platforms.append(plat)
        except (ValueError, TypeError):
            stale_platforms.append(plat)

    # Determine overall status
    overall = "healthy"
    if db_status == "error" or scheduler_status == "stopped":
        overall = "degraded"

    status_code = 200 if overall == "healthy" else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "timestamp": now.isoformat(),
            "last_success": last_success,
            "stale_platforms": stale_platforms,
        },
    )


@app.get("/metrics")
@limiter.limit("30/minute")
async def metrics(request: Request):
    """System metrics endpoint (auth required)."""
    # Scheduler jobs info
    scheduler_jobs = []
    try:
        for job in auto_poster.scheduler.get_jobs():
            scheduler_jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
            })
    except Exception as e:
        logger.warning("Failed to retrieve scheduler jobs: %s", e)

    # Posts today
    posts_today = 0
    try:
        today = date.today()
        async with async_session() as session:
            result = await session.execute(
                select(func.count(Post.id)).where(
                    Post.status == PostStatus.PUBLISHED,
                    func.date(Post.published_at) == today,
                )
            )
            posts_today = result.scalar() or 0
    except Exception as e:
        logger.warning("Failed to count today's posts: %s", e)

    # Registered platforms
    platforms_registered = list(platform_manager._platforms.keys()) if hasattr(platform_manager, "_platforms") else []

    return {
        "api_usage": cost_tracker.get_summary(),
        "scheduler_jobs": scheduler_jobs,
        "uptime_seconds": round(time.time() - _start_time, 2),
        "posts_today": posts_today,
        "platforms_registered": platforms_registered,
    }


# --- Request/Response Models ---

class GeneratePostRequest(BaseModel):
    topic: str = Field(max_length=1000)
    platform: str = "x"
    tone: str = Field(default="casual", max_length=50)
    target_audience: str = Field(default="", max_length=500)
    include_cta: bool = True
    seo_keywords: list[str] | None = Field(default=None, max_length=20)


class SchedulePostRequest(BaseModel):
    platform: str
    text: str
    scheduled_at: datetime
    content_type: Literal["text", "image", "video", "carousel", "short_video"] = "text"
    media_urls: list[str] | None = None
    hashtags: list[str] | None = None


class BatchScheduleRequest(BaseModel):
    topic: str
    platforms: list[str]
    num_posts: int = Field(default=5, ge=1, le=20)


class RecordRevenueRequest(BaseModel):
    source: str = Field(max_length=200)
    amount: float = Field(ge=0, le=10_000_000)
    post_id: int | None = None
    network: str | None = Field(default=None, max_length=200)
    clicks: int = Field(default=0, ge=0)
    conversions: int = Field(default=0, ge=0)


class AffiliatePostRequest(BaseModel):
    product: str = Field(max_length=500)
    affiliate_link: str = Field(max_length=2000)
    platform: str

    @field_validator("affiliate_link")
    @classmethod
    def validate_affiliate_url(cls, v: str) -> str:
        import re
        pattern = r"^https?://[^\s<>\"']+$"
        if not re.match(pattern, v):
            raise ValueError("affiliate_link must be a valid HTTP or HTTPS URL")
        return v


class ABTestRequest(BaseModel):
    text: str = Field(max_length=5000)
    platform: str = "x"
    num_variants: int = Field(default=3, ge=1, le=10)


class CalendarRequest(BaseModel):
    platforms: list[str] = ["x", "instagram"]
    days: int = Field(default=7, ge=1, le=90)
    niche: str = Field(default="", max_length=500)


class CTAOptimizeRequest(BaseModel):
    text: str = Field(max_length=5000)
    platform: str = "x"


class SEOOptimizeRequest(BaseModel):
    text: str = Field(max_length=5000)
    platform: str = "x"
    target_keywords: list[str] = []


class HashtagRequest(BaseModel):
    platform: str = "x"
    niche: str = Field(default="", max_length=500)


class KeywordRequest(BaseModel):
    niche: str = Field(max_length=500)
    platform: str = "x"


class CompetitorRequest(BaseModel):
    competitor_url: str = Field(max_length=2000)
    platform: str = "x"


class PostNowRequest(BaseModel):
    text: str = Field(max_length=5000)
    platform: str = "x"
    image_path: str | None = None
    video_path: str | None = None


class PostMultiRequest(BaseModel):
    text: str = Field(max_length=5000)
    platforms: list[str] = ["x"]
    image_path: str | None = None
    video_path: str | None = None


class PredictViralRequest(BaseModel):
    text: str = Field(max_length=5000)
    platform: str = "x"


class StrategyRequest(BaseModel):
    niche: str = Field(default="general", max_length=500)
    current_followers: int = Field(default=0, ge=0, le=100000000)


# --- Content Generation ---

@app.post("/api/content/generate")
@limiter.limit("10/minute")
async def generate_content(request: Request, req: GeneratePostRequest):
    """Generate AI-powered SNS content."""
    validate_platform(req.platform)
    content = await asyncio.to_thread(
        content_generator.generate_post,
        platform=req.platform,
        topic=req.topic,
        tone=req.tone,
        target_audience=req.target_audience,
        seo_keywords=req.seo_keywords or [],
        include_cta=req.include_cta,
    )
    return {
        "text": content.text,
        "hashtags": content.hashtags,
        "keywords": content.keywords,
        "suggested_media_prompt": content.suggested_media_prompt,
        "viral_score_estimate": content.viral_score_estimate,
        "variations": content.variations,
    }


@app.post("/api/content/ab-test")
@limiter.limit("10/minute")
async def generate_ab_test(request: Request, req: ABTestRequest):
    """Generate A/B test variants."""
    validate_platform(req.platform)
    return await asyncio.to_thread(
        content_generator.ab_test_variants, req.text, req.platform, req.num_variants
    )


@app.post("/api/content/calendar")
@limiter.limit("10/minute")
async def generate_calendar(request: Request, req: CalendarRequest):
    """Generate a content calendar."""
    return await asyncio.to_thread(
        content_generator.generate_content_calendar, req.days, req.platforms[0] if req.platforms else "x", req.niche
    )


@app.post("/api/content/optimize-cta")
@limiter.limit("10/minute")
async def optimize_cta(request: Request, req: CTAOptimizeRequest):
    """Optimize CTA in a post to maximize CTR."""
    return await asyncio.to_thread(
        content_generator.optimize_cta,
        text=req.text,
        platform=req.platform,
    )


# --- SEO ---

@app.post("/api/seo/optimize")
@limiter.limit("10/minute")
async def optimize_seo(request: Request, req: SEOOptimizeRequest):
    """Optimize content for SEO."""
    validate_platform(req.platform)
    result = await asyncio.to_thread(
        seo_optimizer.optimize_post, req.text, req.platform, req.target_keywords
    )
    return {
        "optimized_text": result.optimized_text,
        "primary_keywords": result.primary_keywords,
        "secondary_keywords": result.secondary_keywords,
        "hashtags": result.hashtags,
        "seo_score": result.seo_score,
        "suggestions": result.suggestions,
    }


@app.post("/api/seo/hashtags")
@limiter.limit("10/minute")
async def hashtag_strategy(request: Request, req: HashtagRequest):
    """Generate hashtag strategy."""
    return await asyncio.to_thread(
        seo_optimizer.generate_hashtag_strategy, req.niche, req.platform
    )


@app.post("/api/seo/keywords")
@limiter.limit("10/minute")
async def keyword_research(request: Request, req: KeywordRequest):
    """Research keywords."""
    return await asyncio.to_thread(
        seo_optimizer.keyword_research, [req.niche], req.platform
    )


# --- Trends ---

@app.get("/api/trends/research")
@limiter.limit("20/minute")
async def research_trends(request: Request, niche: str = "", platforms: str = "x,instagram,tiktok"):
    """Research current trends."""
    platform_list = [p.strip() for p in platforms.split(",")]
    validate_platforms(platform_list)
    if len(niche) > 500:
        raise HTTPException(400, "niche parameter too long (max 500 chars)")
    return await trend_researcher.research_niche_trends(niche, platform_list)


@app.get("/api/trends/google")
@limiter.limit("20/minute")
async def google_trends(request: Request):
    """Get Google Trends for Japan."""
    return await trend_researcher.fetch_google_trends()


@app.get("/api/trends/youtube")
@limiter.limit("20/minute")
async def youtube_trending(request: Request):
    """Get YouTube trending videos."""
    return await trend_researcher.fetch_youtube_trending()


@app.post("/api/trends/competitor")
@limiter.limit("20/minute")
async def analyze_competitor(request: Request, req: CompetitorRequest):
    """Analyze a competitor account."""
    return await trend_researcher.analyze_competitor(req.competitor_url, req.platform)


# --- Scheduling ---

@app.post("/api/schedule/post")
@limiter.limit("20/hour")
async def schedule_post(request: Request, req: SchedulePostRequest):
    """Schedule a single post."""
    # Validate scheduled_at is in the future
    now = datetime.now(timezone.utc)
    scheduled_utc = req.scheduled_at if req.scheduled_at.tzinfo else req.scheduled_at.replace(tzinfo=timezone.utc)
    if scheduled_utc < now:
        raise HTTPException(400, "scheduled_at must be in the future")

    # Validate platform
    valid_platforms = {"x", "instagram", "tiktok", "youtube"}
    if req.platform not in valid_platforms:
        raise HTTPException(400, f"Invalid platform. Must be one of: {valid_platforms}")

    # Validate media paths
    if req.media_urls:
        for path in req.media_urls:
            validate_media_path(path)

    post_id = await auto_poster.schedule_post(
        platform=req.platform,
        text=req.text,
        scheduled_at=req.scheduled_at,
        content_type=req.content_type,
        media_urls=req.media_urls,
        hashtags=req.hashtags,
    )
    return {"post_id": post_id, "status": "scheduled"}


@app.post("/api/schedule/batch")
@limiter.limit("5/minute")
async def schedule_batch(request: Request, req: BatchScheduleRequest):
    """Generate and schedule a batch of posts."""
    post_ids = await auto_poster.generate_and_schedule_batch(
        topic=req.topic,
        platforms=req.platforms,
        num_posts=req.num_posts,
    )
    return {"post_ids": post_ids, "count": len(post_ids)}


# --- Posting ---

@app.post("/api/post/now")
@limiter.limit("5/minute")
async def post_now(request: Request, req: PostNowRequest):
    """Post immediately to a platform."""
    validate_platform(req.platform)
    image_path = validate_media_path(req.image_path)
    video_path = validate_media_path(req.video_path)
    p = platform_manager.get(req.platform)
    if not p:
        raise HTTPException(404, f"Platform '{req.platform}' not found")
    if video_path:
        result = await p.post_video(req.text, video_path)
    elif image_path:
        result = await p.post_image(req.text, image_path)
    else:
        result = await p.post_text(req.text)
    return {"success": result.success, "post_id": result.post_id, "url": result.url, "error": result.error}


@app.post("/api/post/multi")
@limiter.limit("5/minute")
async def post_multi(request: Request, req: PostMultiRequest):
    """Post to multiple platforms at once."""
    validate_platforms(req.platforms)
    image_path = validate_media_path(req.image_path)
    video_path = validate_media_path(req.video_path)
    results = await platform_manager.post_to_all(req.text, req.platforms, image_path, video_path)
    return {name: {"success": r.success, "post_id": r.post_id, "url": r.url, "error": r.error} for name, r in results.items()}


# --- Analytics ---

@app.get("/api/analytics/summary")
@limiter.limit("20/minute")
async def analytics_summary(request: Request, days: int = Query(default=30, ge=1, le=365)):
    """Get performance summary."""
    return await analytics.get_performance_summary(days)


@app.get("/api/analytics/top-posts")
@limiter.limit("20/minute")
async def top_posts(request: Request, limit: int = Query(default=10, ge=1, le=100)):
    """Get top performing posts."""
    return await analytics.get_best_performing_posts(limit)


@app.get("/api/analytics/optimal-times")
@limiter.limit("20/minute")
async def optimal_times(request: Request):
    """Get optimal posting times."""
    return await analytics.get_optimal_posting_times()


@app.post("/api/analytics/predict-viral")
@limiter.limit("10/minute")
async def predict_viral(request: Request, req: PredictViralRequest):
    """Predict viral score for a post."""
    validate_platform(req.platform)
    return await asyncio.to_thread(
        analytics.predict_viral_score, req.text, req.platform
    )


@app.get("/api/analytics/insights")
@limiter.limit("10/minute")
async def insights_report(request: Request, days: int = 7):
    """Generate AI-powered insights report."""
    return await analytics.generate_insights_report(days)


@app.get("/api/analytics/ctr")
@limiter.limit("20/minute")
async def ctr_analysis(request: Request, days: int = Query(default=30, ge=1, le=365)):
    """Get CTR analysis across platforms and content types."""
    return await analytics.get_ctr_analysis(days)


class CTRPatternsRequest(BaseModel):
    days: int = Field(default=30, ge=1, le=365)

@app.post("/api/analytics/ctr/patterns")
@limiter.limit("10/minute")
async def ctr_patterns(request: Request, req: CTRPatternsRequest):
    """AI-powered CTR pattern analysis with improvement suggestions."""
    ctr_data = await analytics.get_ctr_analysis(req.days)
    return await asyncio.to_thread(
        analytics.analyze_ctr_patterns,
        ctr_data.get("top_ctr_posts", []),
        ctr_data.get("bottom_ctr_posts", []),
    )


@app.get("/api/analytics/ab-test")
@limiter.limit("20/minute")
async def ab_test_results(request: Request, group_a: str, group_b: str):
    """Get A/B test comparison results."""
    return await analytics.ab_test_results(group_a, group_b)


@app.get("/api/analytics/feedback-hints")
@limiter.limit("20/minute")
async def get_feedback_hints(request: Request, platform: str = "x"):
    """Get AI-powered content optimization hints based on past performance."""
    return await feedback_loop.get_optimization_hints(platform)


# --- Monetization ---

@app.post("/api/revenue/record")
@limiter.limit("10/minute")
async def record_revenue(request: Request, req: RecordRevenueRequest):
    """Record a revenue entry."""
    rev_id = await revenue_tracker.record_revenue(
        source=req.source,
        amount=req.amount,
        post_id=req.post_id,
        network=req.network,
        clicks=req.clicks,
        conversions=req.conversions,
    )
    return {"revenue_id": rev_id}


@app.get("/api/revenue/summary")
@limiter.limit("20/minute")
async def revenue_summary(request: Request, days: int = Query(default=30, ge=1, le=365)):
    """Get revenue summary."""
    return await revenue_tracker.get_revenue_summary(days)


@app.post("/api/revenue/strategy")
@limiter.limit("10/minute")
async def monetization_strategy(request: Request, req: StrategyRequest):
    """Generate monetization strategy."""
    summary = await analytics.get_performance_summary(30)
    return await asyncio.to_thread(
        revenue_tracker.generate_monetization_strategy, summary, req.niche
    )


@app.post("/api/revenue/affiliate-post")
@limiter.limit("10/minute")
async def create_affiliate_post(request: Request, req: AffiliatePostRequest):
    """Generate an affiliate post."""
    return await asyncio.to_thread(
        revenue_tracker.generate_affiliate_post, req.product, req.affiliate_link, req.platform
    )


# --- Safety ---

class EmergencyStopRequest(BaseModel):
    reason: str = "Manual emergency stop via API"

@app.post("/api/safety/emergency-stop")
@limiter.limit("3/minute")
async def activate_emergency_stop(request: Request, req: EmergencyStopRequest):
    """Activate emergency stop - halts all posting."""
    from src.core.safety import emergency_stop
    emergency_stop.activate(req.reason)
    return {"status": "stopped", "reason": req.reason}

@app.post("/api/safety/resume")
@limiter.limit("3/minute")
async def deactivate_emergency_stop(request: Request):
    """Resume posting after emergency stop."""
    from src.core.safety import emergency_stop
    emergency_stop.deactivate()
    return {"status": "running"}

@app.get("/api/safety/status")
@limiter.limit("20/minute")
async def get_safety_status(request: Request):
    """Get comprehensive safety system status."""
    from src.core.safety import (
        emergency_stop, daily_tracker, shadow_ban_detector, warmup_manager, platform_cooldown,
    )
    return {
        "emergency_stop": emergency_stop.get_status(),
        "daily_limits": daily_tracker.get_summary(),
        "shadow_ban": {
            platform: shadow_ban_detector.is_flagged(platform)
            for platform in ["x", "instagram", "tiktok", "youtube"]
        },
        "platform_cooldown": platform_cooldown.get_status(),
        "warmup": {
            "is_warmed_up": warmup_manager.is_warmed_up(),
            "start_date": warmup_manager.start_date.isoformat() if warmup_manager.start_date else None,
        },
    }

@app.post("/api/safety/shadow-ban/clear/{platform}")
@limiter.limit("3/minute")
async def clear_shadow_ban(request: Request, platform: str):
    """Clear shadow ban flag for a platform."""
    validate_platform(platform)
    from src.core.safety import shadow_ban_detector
    shadow_ban_detector.clear_flag(platform)
    return {"platform": platform, "flagged": False}

@app.post("/api/safety/cooldown/clear/{platform}")
@limiter.limit("3/minute")
async def clear_platform_cooldown(request: Request, platform: str):
    """Manually clear a platform cooldown (e.g. when YouTube quota was raised early)."""
    validate_platform(platform)
    from src.core.safety import platform_cooldown
    platform_cooldown.clear(platform)
    return {"platform": platform, "on_cooldown": False}


# --- Affiliate Products ---

class CreateAffiliateProductRequest(BaseModel):
    product_name: str = Field(max_length=200)
    product_url: str = Field(max_length=500)
    affiliate_link: str = Field(max_length=500)
    network: str = Field(max_length=50)
    category: str = "cosmetics"
    commission_rate: float = Field(ge=0, le=100, default=0.0)
    price_jpy: int = Field(ge=0, default=0)
    description: str = ""

@app.get("/api/affiliate/products")
@limiter.limit("20/minute")
async def list_affiliate_products(request: Request, active_only: bool = True):
    """List affiliate products."""
    async with async_session() as session:
        query = select(AffiliateProduct)
        if active_only:
            query = query.where(AffiliateProduct.is_active.is_(True))
        result = await session.execute(query.order_by(AffiliateProduct.created_at.desc()))
        products = result.scalars().all()
        return [
            {
                "id": p.id,
                "product_name": p.product_name,
                "affiliate_link": p.affiliate_link,
                "network": p.network,
                "category": p.category,
                "commission_rate": p.commission_rate,
                "price_jpy": p.price_jpy,
                "is_active": p.is_active,
                "times_promoted": p.times_promoted,
                "total_clicks": p.total_clicks,
                "total_conversions": p.total_conversions,
                "total_revenue_jpy": p.total_revenue_jpy,
            }
            for p in products
        ]

@app.post("/api/affiliate/products")
@limiter.limit("10/minute")
async def create_affiliate_product(request: Request, req: CreateAffiliateProductRequest):
    """Add a new affiliate product."""
    async with async_session() as session:
        product = AffiliateProduct(
            product_name=req.product_name,
            product_url=req.product_url,
            affiliate_link=req.affiliate_link,
            network=req.network,
            category=req.category,
            commission_rate=req.commission_rate,
            price_jpy=req.price_jpy,
            description=req.description,
        )
        session.add(product)
        await session.commit()
        return {"id": product.id, "product_name": product.product_name, "status": "created"}

@app.delete("/api/affiliate/products/{product_id}")
@limiter.limit("10/minute")
async def delete_affiliate_product(request: Request, product_id: int):
    """Deactivate an affiliate product."""
    async with async_session() as session:
        result = await session.execute(select(AffiliateProduct).where(AffiliateProduct.id == product_id))
        product = result.scalar_one_or_none()
        if not product:
            raise HTTPException(404, "Product not found")
        product.is_active = False
        await session.commit()
        return {"id": product_id, "status": "deactivated"}


# --- Followers ---

@app.get("/api/followers/counts")
@limiter.limit("20/minute")
async def follower_counts(request: Request):
    """Get follower counts across all platforms."""
    return await platform_manager.get_all_follower_counts()


# --- HoshimiLive: 5-department agent dispatch ---


class AgentDispatchRequest(BaseModel):
    department: str = Field(..., description="pm / research / marketing / finance / secretariat")
    task: str = Field(..., description="agent-specific task name")
    kwargs: dict[str, Any] = Field(default_factory=dict)


@app.get("/api/agents/departments")
@limiter.limit("60/minute")
async def list_agent_departments(request: Request):
    """List registered agent departments."""
    return {"departments": orchestrator.departments}


@app.post("/api/agents/dispatch")
@limiter.limit("30/minute")
async def agent_dispatch(request: Request, req: AgentDispatchRequest):
    """Dispatch a task to a department agent.

    Each call is persisted to the ``agent_logs`` table via Orchestrator.dispatch,
    so even failed dispatches are auditable from the Agent Logs dashboard tab.
    """
    result = await orchestrator.dispatch(
        req.department, req.task, triggered_by="api", **req.kwargs,
    )
    return result.to_dict()


@app.post("/api/agents/daily-report")
@limiter.limit("10/minute")
async def agent_daily_report(request: Request):
    """Shortcut: dispatch secretariat.daily_report.

    Triggers a fan-out that writes 7+ rows to ``agent_logs`` (one per sub-call)
    plus the top-level daily_report row itself.
    """
    result = await orchestrator.dispatch(
        "secretariat", "daily_report", triggered_by="api",
    )
    return result.to_dict()


# --- HoshimiLive: Fortune Readings / Agent Logs / Agent Tasks ---


@app.get("/api/hoshimi/readings")
@limiter.limit("30/minute")
async def list_fortune_readings(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    fortune_type: str | None = None,
    days: int = Query(7, ge=1, le=365),
):
    """Recent fortune readings with per-type aggregates."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session() as session:
        items_q = select(FortuneReading).where(FortuneReading.created_at >= since)
        count_q = select(func.count(FortuneReading.id)).where(
            FortuneReading.created_at >= since
        )
        if fortune_type:
            items_q = items_q.where(FortuneReading.fortune_type == fortune_type)
            count_q = count_q.where(FortuneReading.fortune_type == fortune_type)

        total = (await session.execute(count_q)).scalar_one()
        rows = (
            await session.execute(
                items_q.order_by(FortuneReading.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()

        by_type_q = (
            select(
                FortuneReading.fortune_type,
                func.count(FortuneReading.id),
                func.coalesce(func.sum(FortuneReading.cost_usd), 0),
                func.coalesce(func.sum(FortuneReading.input_tokens), 0),
                func.coalesce(func.sum(FortuneReading.output_tokens), 0),
            )
            .where(FortuneReading.created_at >= since)
            .group_by(FortuneReading.fortune_type)
        )
        by_type = [
            {
                "type": t,
                "count": c,
                "cost_usd": round(float(cost or 0), 4),
                "input_tokens": int(itok or 0),
                "output_tokens": int(otok or 0),
            }
            for t, c, cost, itok, otok in (await session.execute(by_type_q)).all()
        ]
        total_cost = round(sum(b["cost_usd"] for b in by_type), 4)

        return {
            "days": days,
            "total": total,
            "total_cost_usd": total_cost,
            "by_type": by_type,
            "items": [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "fortune_type": r.fortune_type,
                    "theme_or_input": r.theme_or_input,
                    "card_name": r.card_name,
                    "text_preview": (r.text or "")[:280],
                    "cta": r.cta,
                    "model_used": r.model_used,
                    "cost_usd": round(float(r.cost_usd or 0), 6),
                    "input_tokens": r.input_tokens or 0,
                    "output_tokens": r.output_tokens or 0,
                    "customer_id": r.customer_id,
                    "ip_hash_prefix": (r.ip_hash[:10] if r.ip_hash else None),
                }
                for r in rows
            ],
        }


@app.get("/api/hoshimi/agent-logs")
@limiter.limit("30/minute")
async def list_agent_logs(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    department: str | None = None,
    status: str | None = None,
    days: int = Query(7, ge=1, le=90),
):
    """Recent agent dispatches with dept × status breakdown."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session() as session:
        items_q = select(AgentLog).where(AgentLog.created_at >= since)
        count_q = select(func.count(AgentLog.id)).where(AgentLog.created_at >= since)
        if department:
            items_q = items_q.where(AgentLog.department == department)
            count_q = count_q.where(AgentLog.department == department)
        if status:
            items_q = items_q.where(AgentLog.status == status)
            count_q = count_q.where(AgentLog.status == status)

        total = (await session.execute(count_q)).scalar_one()
        rows = (
            await session.execute(
                items_q.order_by(AgentLog.created_at.desc()).limit(limit).offset(offset)
            )
        ).scalars().all()

        breakdown_q = (
            select(
                AgentLog.department,
                AgentLog.status,
                func.count(AgentLog.id),
                func.coalesce(func.avg(AgentLog.duration_ms), 0),
            )
            .where(AgentLog.created_at >= since)
            .group_by(AgentLog.department, AgentLog.status)
        )
        breakdown = [
            {
                "department": dep,
                "status": st,
                "count": c,
                "avg_duration_ms": round(float(avg or 0), 1),
            }
            for dep, st, c, avg in (await session.execute(breakdown_q)).all()
        ]

        def _preview(obj):
            if obj is None:
                return None
            try:
                return json.dumps(obj, ensure_ascii=False, default=str)[:400]
            except Exception:
                return str(obj)[:400]

        return {
            "days": days,
            "total": total,
            "by_dept_status": breakdown,
            "items": [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "department": r.department,
                    "task": r.task,
                    "status": r.status,
                    "duration_ms": r.duration_ms or 0,
                    "triggered_by": r.triggered_by,
                    "error": (r.error[:500] if r.error else None),
                    "input_preview": _preview(r.input),
                    "output_preview": _preview(r.output),
                }
                for r in rows
            ],
        }


@app.get("/api/hoshimi/agent-tasks")
@limiter.limit("30/minute")
async def list_agent_tasks(
    request: Request,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    department: str | None = None,
    status: str | None = None,
):
    """Persistent agent tasks, ordered by priority then due date."""
    async with async_session() as session:
        items_q = select(AgentTask)
        count_q = select(func.count(AgentTask.id))
        if department:
            items_q = items_q.where(AgentTask.department == department)
            count_q = count_q.where(AgentTask.department == department)
        if status:
            items_q = items_q.where(AgentTask.status == status)
            count_q = count_q.where(AgentTask.status == status)

        total = (await session.execute(count_q)).scalar_one()
        rows = (
            await session.execute(
                items_q.order_by(
                    AgentTask.priority.desc(),
                    AgentTask.created_at.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()

        # Unfiltered breakdown so the Kanban-style overview stays stable
        # regardless of which filter the user has applied.
        breakdown_q = (
            select(AgentTask.department, AgentTask.status, func.count(AgentTask.id))
            .group_by(AgentTask.department, AgentTask.status)
        )
        breakdown = [
            {"department": d, "status": s, "count": c}
            for d, s, c in (await session.execute(breakdown_q)).all()
        ]

        return {
            "total": total,
            "by_dept_status": breakdown,
            "items": [
                {
                    "id": r.id,
                    "department": r.department,
                    "title": r.title,
                    "description": (r.description or "")[:400],
                    "status": r.status,
                    "priority": r.priority,
                    "assigned_to": r.assigned_to,
                    "due_at": r.due_at.isoformat() if r.due_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "parent_task_id": r.parent_task_id,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                }
                for r in rows
            ],
        }


# --- Dashboard ---

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard HTML.

    Moved from "/" so the public HoshimiLive landing page (src/api/routes/fortune.py)
    can own the root path. Still behind global auth.
    """
    return DASHBOARD_HTML


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SNS Automation Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', sans-serif; background: #0f0f23; color: #e0e0e0; }
        .header { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 20px 30px; }
        .header h1 { font-size: 24px; }
        .header p { opacity: 0.8; font-size: 14px; }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin: 20px 0; }
        .card { background: #1a1a2e; border-radius: 12px; padding: 20px; border: 1px solid #2a2a4a; }
        .card h3 { color: #667eea; margin-bottom: 15px; font-size: 16px; }
        .metric { display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #2a2a4a; }
        .metric-value { font-weight: bold; color: #4ade80; }
        .btn { background: linear-gradient(135deg, #667eea, #764ba2); color: white; border: none; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-size: 14px; margin: 5px; }
        .btn:hover { opacity: 0.9; }
        .btn-secondary { background: #2a2a4a; }
        textarea, input, select { background: #16213e; color: #e0e0e0; border: 1px solid #2a2a4a; border-radius: 8px; padding: 10px; width: 100%; margin: 5px 0; }
        textarea { min-height: 100px; resize: vertical; }
        .status { display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; }
        .status-active { background: #065f46; color: #4ade80; }
        .status-scheduled { background: #1e3a5f; color: #60a5fa; }
        .tabs { display: flex; gap: 5px; margin: 20px 0; flex-wrap: wrap; }
        .tab { padding: 10px 20px; background: #1a1a2e; border: 1px solid #2a2a4a; border-radius: 8px 8px 0 0; cursor: pointer; }
        .tab.active { background: #667eea; border-color: #667eea; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        #output { background: #16213e; padding: 15px; border-radius: 8px; margin-top: 10px; white-space: pre-wrap; max-height: 400px; overflow-y: auto; font-family: monospace; font-size: 13px; }
        .loading { color: #667eea; }
    </style>
</head>
<body>
    <div class="header">
        <h1>SNS Automation Dashboard</h1>
        <p>AI-Powered Content Generation / Auto-Posting / Analytics / Monetization</p>
        <button class="btn btn-secondary" onclick="sessionStorage.removeItem('sns_api_key'); location.reload();">🔑 Change API Key</button>
    </div>
    <div class="container">
        <div class="tabs">
            <div class="tab active" onclick="showTab('overview')">Overview</div>
            <div class="tab" onclick="showTab('generate')">Content Generation</div>
            <div class="tab" onclick="showTab('trends')">Trends</div>
            <div class="tab" onclick="showTab('schedule')">Schedule</div>
            <div class="tab" onclick="showTab('analytics')">Analytics</div>
            <div class="tab" onclick="showTab('monetization')">Monetization</div>
            <div class="tab" onclick="showTab('seo')">SEO</div>
            <div class="tab" onclick="showTab('readings')">Oracle Readings</div>
            <div class="tab" onclick="showTab('agent-logs')">Agent Logs</div>
            <div class="tab" onclick="showTab('agent-tasks')">Agent Tasks</div>
        </div>

        <!-- Overview -->
        <div id="overview" class="tab-content active">
            <div class="grid">
                <div class="card">
                    <h3>System Status</h3>
                    <div class="metric"><span>Auto-Poster</span><span class="status status-active">Active</span></div>
                    <div class="metric"><span>Trend Research</span><span class="status status-active">Active</span></div>
                    <div class="metric"><span>Metrics Collection</span><span class="status status-active">Active</span></div>
                </div>
                <div class="card">
                    <h3>Quick Actions</h3>
                    <button class="btn" onclick="loadSummary()">Load Summary</button>
                    <button class="btn" onclick="loadTrends()">Fetch Trends</button>
                    <button class="btn" onclick="loadInsights()">AI Insights</button>
                </div>
                <div class="card">
                    <h3>Followers</h3>
                    <div id="followers-data"><span class="loading">Click Load Summary...</span></div>
                </div>
            </div>
            <div class="card"><h3>Summary</h3><div id="output">Ready. Use the tabs above to get started.</div></div>
        </div>

        <!-- Content Generation -->
        <div id="generate" class="tab-content">
            <div class="grid">
                <div class="card">
                    <h3>Generate Post</h3>
                    <select id="gen-platform"><option value="x">X (Twitter)</option><option value="instagram">Instagram</option><option value="tiktok">TikTok</option><option value="youtube">YouTube</option></select>
                    <input id="gen-topic" placeholder="Topic / Theme">
                    <select id="gen-tone"><option value="engaging">Engaging</option><option value="informative">Informative</option><option value="humorous">Humorous</option><option value="inspiring">Inspiring</option><option value="controversial">Controversial</option></select>
                    <input id="gen-audience" placeholder="Target Audience (optional)">
                    <input id="gen-keywords" placeholder="SEO Keywords (comma-separated)">
                    <button class="btn" onclick="generatePost()">Generate</button>
                    <button class="btn btn-secondary" onclick="generateCalendar()">Generate Calendar</button>
                </div>
                <div class="card">
                    <h3>Generated Content</h3>
                    <div id="gen-output" style="white-space:pre-wrap;font-family:monospace;font-size:13px;">...</div>
                </div>
            </div>
        </div>

        <!-- Trends -->
        <div id="trends" class="tab-content">
            <div class="card">
                <h3>Trend Research</h3>
                <input id="trend-niche" placeholder="Niche / Genre">
                <button class="btn" onclick="researchTrends()">Research Trends</button>
                <button class="btn btn-secondary" onclick="fetchGoogleTrends()">Google Trends</button>
                <button class="btn btn-secondary" onclick="fetchYouTubeTrends()">YouTube Trending</button>
            </div>
            <div class="card"><h3>Results</h3><div id="trend-output" style="white-space:pre-wrap;font-family:monospace;font-size:13px;">...</div></div>
        </div>

        <!-- Schedule -->
        <div id="schedule" class="tab-content">
            <div class="grid">
                <div class="card">
                    <h3>Schedule Post</h3>
                    <select id="sched-platform"><option value="x">X</option><option value="instagram">Instagram</option><option value="tiktok">TikTok</option><option value="youtube">YouTube</option></select>
                    <textarea id="sched-text" placeholder="Post text..."></textarea>
                    <input id="sched-time" type="datetime-local">
                    <input id="sched-hashtags" placeholder="Hashtags (comma-separated)">
                    <button class="btn" onclick="schedulePost()">Schedule</button>
                </div>
                <div class="card">
                    <h3>Batch Schedule</h3>
                    <input id="batch-topic" placeholder="Topic">
                    <input id="batch-platforms" placeholder="Platforms (comma-separated)" value="x,instagram">
                    <input id="batch-count" type="number" value="5" min="1" max="20">
                    <button class="btn" onclick="batchSchedule()">Generate & Schedule Batch</button>
                </div>
            </div>
            <div class="card"><h3>Result</h3><div id="sched-output" style="white-space:pre-wrap;font-family:monospace;font-size:13px;">...</div></div>
        </div>

        <!-- Analytics -->
        <div id="analytics" class="tab-content">
            <div class="card">
                <h3>Analytics</h3>
                <button class="btn" onclick="loadAnalytics(7)">7 Days</button>
                <button class="btn" onclick="loadAnalytics(30)">30 Days</button>
                <button class="btn" onclick="loadAnalytics(90)">90 Days</button>
                <button class="btn btn-secondary" onclick="loadTopPosts()">Top Posts</button>
                <button class="btn btn-secondary" onclick="loadOptimalTimes()">Optimal Times</button>
                <button class="btn btn-secondary" onclick="loadInsights()">AI Insights</button>
            </div>
            <div class="card">
                <h3>CTR Analysis</h3>
                <button class="btn" onclick="loadCTR(7)">7 Days</button>
                <button class="btn" onclick="loadCTR(30)">30 Days</button>
                <button class="btn btn-secondary" onclick="loadCTRPatterns()">AI CTR Patterns</button>
            </div>
            <div class="card">
                <h3>Viral Score Predictor</h3>
                <select id="viral-platform"><option value="x">X</option><option value="instagram">Instagram</option><option value="tiktok">TikTok</option></select>
                <textarea id="viral-text" placeholder="Paste post text to predict viral score..."></textarea>
                <button class="btn" onclick="predictViral()">Predict</button>
            </div>
            <div class="card"><h3>Results</h3><div id="analytics-output" style="white-space:pre-wrap;font-family:monospace;font-size:13px;">...</div></div>
        </div>

        <!-- Monetization -->
        <div id="monetization" class="tab-content">
            <div class="grid">
                <div class="card">
                    <h3>Revenue</h3>
                    <button class="btn" onclick="loadRevenue(7)">7 Days</button>
                    <button class="btn" onclick="loadRevenue(30)">30 Days</button>
                    <button class="btn btn-secondary" onclick="loadStrategy()">AI Strategy</button>
                </div>
                <div class="card">
                    <h3>Record Revenue</h3>
                    <select id="rev-source"><option value="affiliate">Affiliate</option><option value="sponsored">Sponsored</option><option value="ad_revenue">Ad Revenue</option><option value="product_sales">Product Sales</option></select>
                    <input id="rev-amount" type="number" placeholder="Amount (JPY)">
                    <input id="rev-network" placeholder="Network (e.g., a8, amazon)">
                    <button class="btn" onclick="recordRevenue()">Record</button>
                </div>
                <div class="card">
                    <h3>Affiliate Post Generator</h3>
                    <input id="aff-product" placeholder="Product name">
                    <input id="aff-link" placeholder="Affiliate link">
                    <select id="aff-platform"><option value="x">X</option><option value="instagram">Instagram</option><option value="tiktok">TikTok</option></select>
                    <button class="btn" onclick="generateAffiliatePost()">Generate</button>
                </div>
            </div>
            <div class="card"><h3>Results</h3><div id="monetization-output" style="white-space:pre-wrap;font-family:monospace;font-size:13px;">...</div></div>
        </div>

        <!-- SEO -->
        <div id="seo" class="tab-content">
            <div class="grid">
                <div class="card">
                    <h3>SEO Optimize</h3>
                    <select id="seo-platform"><option value="x">X</option><option value="instagram">Instagram</option><option value="tiktok">TikTok</option><option value="youtube">YouTube</option></select>
                    <textarea id="seo-text" placeholder="Post text to optimize..."></textarea>
                    <input id="seo-keywords" placeholder="Target keywords (comma-separated)">
                    <button class="btn" onclick="optimizeSEO()">Optimize</button>
                </div>
                <div class="card">
                    <h3>Tools</h3>
                    <input id="ht-topic" placeholder="Topic for hashtag strategy">
                    <button class="btn btn-secondary" onclick="hashtagStrategy()">Hashtag Strategy</button>
                    <input id="kw-seeds" placeholder="Seed keywords (comma-separated)">
                    <button class="btn btn-secondary" onclick="keywordResearch()">Keyword Research</button>
                </div>
            </div>
            <div class="card"><h3>Results</h3><div id="seo-output" style="white-space:pre-wrap;font-family:monospace;font-size:13px;">...</div></div>
        </div>

        <!-- Oracle Readings (HoshimiLive fortune_readings audit log) -->
        <div id="readings" class="tab-content">
            <div class="grid">
                <div class="card">
                    <h3>Filters</h3>
                    <select id="readings-type">
                        <option value="">All types</option>
                        <option value="tarot">Tarot</option>
                        <option value="horoscope">Horoscope</option>
                        <option value="numerology">Numerology</option>
                        <option value="love">Love</option>
                        <option value="guardian_angel">Guardian Angel</option>
                    </select>
                    <select id="readings-days">
                        <option value="1">Last 24h</option>
                        <option value="7" selected>Last 7 days</option>
                        <option value="30">Last 30 days</option>
                        <option value="90">Last 90 days</option>
                    </select>
                    <button class="btn" onclick="loadReadings()">Refresh</button>
                </div>
                <div class="card">
                    <h3>By Type</h3>
                    <div id="readings-stats" style="white-space:pre-wrap;font-family:monospace;font-size:13px;"><span class="loading">Click Refresh...</span></div>
                </div>
                <div class="card">
                    <h3>Totals</h3>
                    <div id="readings-totals" style="white-space:pre-wrap;font-family:monospace;font-size:13px;"><span class="loading">Click Refresh...</span></div>
                </div>
            </div>
            <div class="card"><h3>Recent Readings</h3><div id="readings-output" style="white-space:pre-wrap;font-family:monospace;font-size:12px;">...</div></div>
        </div>

        <!-- Agent Logs (5-department dispatch audit log) -->
        <div id="agent-logs" class="tab-content">
            <div class="grid">
                <div class="card">
                    <h3>Filters</h3>
                    <select id="logs-department">
                        <option value="">All departments</option>
                        <option value="secretariat">Secretariat</option>
                        <option value="pm">PM</option>
                        <option value="research">Research</option>
                        <option value="marketing">Marketing</option>
                        <option value="finance">Finance</option>
                    </select>
                    <select id="logs-status">
                        <option value="">All statuses</option>
                        <option value="ok">OK</option>
                        <option value="error">Error</option>
                    </select>
                    <select id="logs-days">
                        <option value="1">Last 24h</option>
                        <option value="7" selected>Last 7 days</option>
                        <option value="30">Last 30 days</option>
                    </select>
                    <button class="btn" onclick="loadAgentLogs()">Refresh</button>
                </div>
                <div class="card">
                    <h3>Dept × Status</h3>
                    <div id="logs-stats" style="white-space:pre-wrap;font-family:monospace;font-size:13px;"><span class="loading">Click Refresh...</span></div>
                </div>
            </div>
            <div class="card"><h3>Recent Dispatches</h3><div id="logs-output" style="white-space:pre-wrap;font-family:monospace;font-size:12px;">...</div></div>
        </div>

        <!-- Agent Tasks (persistent department todos) -->
        <div id="agent-tasks" class="tab-content">
            <div class="grid">
                <div class="card">
                    <h3>Filters</h3>
                    <select id="tasks-department">
                        <option value="">All departments</option>
                        <option value="secretariat">Secretariat</option>
                        <option value="pm">PM</option>
                        <option value="research">Research</option>
                        <option value="marketing">Marketing</option>
                        <option value="finance">Finance</option>
                    </select>
                    <select id="tasks-status">
                        <option value="">All statuses</option>
                        <option value="pending">Pending</option>
                        <option value="in_progress">In progress</option>
                        <option value="blocked">Blocked</option>
                        <option value="completed">Completed</option>
                        <option value="canceled">Canceled</option>
                    </select>
                    <button class="btn" onclick="loadAgentTasks()">Refresh</button>
                </div>
                <div class="card">
                    <h3>Kanban Overview</h3>
                    <div id="tasks-stats" style="white-space:pre-wrap;font-family:monospace;font-size:13px;"><span class="loading">Click Refresh...</span></div>
                </div>
            </div>
            <div class="card"><h3>Tasks</h3><div id="tasks-output" style="white-space:pre-wrap;font-family:monospace;font-size:12px;">...</div></div>
        </div>
    </div>

    <script>
        function showTab(id) {
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
            document.getElementById(id).classList.add('active');
            event.target.classList.add('active');
        }

        function getApiKey() {
            let key = sessionStorage.getItem('sns_api_key');
            if (!key) {
                key = prompt('Enter your API key:');
                if (key) sessionStorage.setItem('sns_api_key', key);
            }
            return key || '';
        }

        async function apiCall(url, method = 'GET', body = null) {
            const opts = {
                method,
                headers: {
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + getApiKey(),
                },
            };
            if (body) opts.body = JSON.stringify(body);
            const res = await fetch(url, opts);
            return res.json();
        }

        function display(elementId, data) {
            document.getElementById(elementId).textContent = JSON.stringify(data, null, 2);
        }

        async function loadSummary() {
            display('output', 'Loading...');
            const data = await apiCall('/api/analytics/summary?days=30');
            display('output', data);
        }

        async function loadTrends() {
            display('output', 'Loading...');
            const data = await apiCall('/api/trends/google');
            display('output', data);
        }

        async function loadInsights() {
            const target = document.getElementById('analytics-output').offsetParent ? 'analytics-output' : 'output';
            display(target, 'Generating AI insights...');
            const data = await apiCall('/api/analytics/insights?days=7');
            display(target, data);
        }

        async function generatePost() {
            display('gen-output', 'Generating...');
            const kw = document.getElementById('gen-keywords').value;
            const data = await apiCall('/api/content/generate', 'POST', {
                platform: document.getElementById('gen-platform').value,
                topic: document.getElementById('gen-topic').value,
                tone: document.getElementById('gen-tone').value,
                target_audience: document.getElementById('gen-audience').value,
                seo_keywords: kw ? kw.split(',').map(s => s.trim()).filter(Boolean) : [],
            });
            display('gen-output', data);
        }

        async function generateCalendar() {
            display('gen-output', 'Generating calendar...');
            const data = await apiCall('/api/content/calendar', 'POST', {
                days: 7,
                platform: document.getElementById('gen-platform').value,
                niche: document.getElementById('gen-topic').value,
            });
            display('gen-output', data);
        }

        async function researchTrends() {
            display('trend-output', 'Researching...');
            const niche = document.getElementById('trend-niche').value;
            const data = await apiCall('/api/trends/research?niche=' + encodeURIComponent(niche));
            display('trend-output', data);
        }

        async function fetchGoogleTrends() {
            display('trend-output', 'Fetching...');
            const data = await apiCall('/api/trends/google');
            display('trend-output', data);
        }

        async function fetchYouTubeTrends() {
            display('trend-output', 'Fetching...');
            const data = await apiCall('/api/trends/youtube');
            display('trend-output', data);
        }

        async function schedulePost() {
            const hashtags = document.getElementById('sched-hashtags').value;
            const data = await apiCall('/api/schedule/post', 'POST', {
                platform: document.getElementById('sched-platform').value,
                text: document.getElementById('sched-text').value,
                scheduled_at: new Date(document.getElementById('sched-time').value).toISOString(),
                hashtags: hashtags ? hashtags.split(',').map(s => s.trim()) : null,
            });
            display('sched-output', data);
        }

        async function batchSchedule() {
            display('sched-output', 'Generating and scheduling...');
            const data = await apiCall('/api/schedule/batch', 'POST', {
                topic: document.getElementById('batch-topic').value,
                platforms: document.getElementById('batch-platforms').value.split(',').map(s => s.trim()),
                num_posts: parseInt(document.getElementById('batch-count').value),
            });
            display('sched-output', data);
        }

        async function loadAnalytics(days) {
            display('analytics-output', 'Loading...');
            const data = await apiCall('/api/analytics/summary?days=' + days);
            display('analytics-output', data);
        }

        async function loadTopPosts() {
            display('analytics-output', 'Loading...');
            const data = await apiCall('/api/analytics/top-posts?limit=10');
            display('analytics-output', data);
        }

        async function loadOptimalTimes() {
            display('analytics-output', 'Loading...');
            const data = await apiCall('/api/analytics/optimal-times');
            display('analytics-output', data);
        }

        async function predictViral() {
            display('analytics-output', 'Predicting...');
            const data = await apiCall('/api/analytics/predict-viral', 'POST', {
                text: document.getElementById('viral-text').value,
                platform: document.getElementById('viral-platform').value,
            });
            display('analytics-output', data);
        }

        async function loadRevenue(days) {
            display('monetization-output', 'Loading...');
            const data = await apiCall('/api/revenue/summary?days=' + days);
            display('monetization-output', data);
        }

        async function loadStrategy() {
            display('monetization-output', 'Generating strategy...');
            const data = await apiCall('/api/revenue/strategy', 'POST', {
                niche: 'general',
            });
            display('monetization-output', data);
        }

        async function recordRevenue() {
            const data = await apiCall('/api/revenue/record', 'POST', {
                source: document.getElementById('rev-source').value,
                amount: parseFloat(document.getElementById('rev-amount').value),
                network: document.getElementById('rev-network').value || null,
            });
            display('monetization-output', data);
        }

        async function generateAffiliatePost() {
            display('monetization-output', 'Generating...');
            const data = await apiCall('/api/revenue/affiliate-post', 'POST', {
                product: document.getElementById('aff-product').value,
                affiliate_link: document.getElementById('aff-link').value,
                platform: document.getElementById('aff-platform').value,
            });
            display('monetization-output', data);
        }

        async function optimizeSEO() {
            display('seo-output', 'Optimizing...');
            const kw = document.getElementById('seo-keywords').value;
            const data = await apiCall('/api/seo/optimize', 'POST', {
                platform: document.getElementById('seo-platform').value,
                text: document.getElementById('seo-text').value,
                target_keywords: kw ? kw.split(',').map(s => s.trim()).filter(Boolean) : [],
            });
            display('seo-output', data);
        }

        async function hashtagStrategy() {
            display('seo-output', 'Generating...');
            const data = await apiCall('/api/seo/hashtags', 'POST', {
                topic: document.getElementById('ht-topic').value,
                platform: document.getElementById('seo-platform').value,
            });
            display('seo-output', data);
        }

        async function keywordResearch() {
            display('seo-output', 'Researching...');
            const seeds = document.getElementById('kw-seeds').value.split(',').map(s => s.trim());
            const data = await apiCall('/api/seo/keywords', 'POST', {
                keywords: seeds,
                platform: document.getElementById('seo-platform').value,
            });
            display('seo-output', data);
        }

        async function loadCTR(days) {
            display('analytics-output', 'Analyzing CTR...');
            const data = await apiCall('/api/analytics/ctr?days=' + days);
            display('analytics-output', data);
        }

        async function loadCTRPatterns() {
            display('analytics-output', 'Analyzing CTR patterns with AI...');
            const data = await apiCall('/api/analytics/ctr/patterns', 'POST', {days: 30});
            display('analytics-output', data);
        }

        // --- HoshimiLive: readings / agent-logs / agent-tasks ---

        function pad(s, n) {
            s = (s == null ? '' : String(s));
            return s.length >= n ? s.slice(0, n) : s + ' '.repeat(n - s.length);
        }

        function fmtTs(iso) {
            if (!iso) return '';
            return iso.replace('T', ' ').slice(0, 19);
        }

        async function loadReadings() {
            document.getElementById('readings-output').textContent = 'Loading...';
            document.getElementById('readings-stats').textContent = 'Loading...';
            document.getElementById('readings-totals').textContent = 'Loading...';
            const type = document.getElementById('readings-type').value;
            const days = document.getElementById('readings-days').value;
            let url = '/api/hoshimi/readings?limit=100&days=' + encodeURIComponent(days);
            if (type) url += '&fortune_type=' + encodeURIComponent(type);
            const data = await apiCall(url);
            if (data.error || data.detail) {
                display('readings-output', data);
                document.getElementById('readings-stats').textContent = data.error || data.detail || 'error';
                document.getElementById('readings-totals').textContent = '';
                return;
            }
            // Totals
            document.getElementById('readings-totals').textContent = [
                'Window:      last ' + data.days + ' days',
                'Total rows:  ' + data.total,
                'Total cost:  $' + (data.total_cost_usd || 0).toFixed(4),
            ].join('\n');
            // By type
            const byType = (data.by_type || []).slice().sort((a, b) => b.count - a.count);
            if (byType.length === 0) {
                document.getElementById('readings-stats').textContent = '(no readings)';
            } else {
                const lines = byType.map(b =>
                    pad(b.type, 16) + pad(b.count + 'x', 8) + '$' + b.cost_usd.toFixed(4)
                    + '  (' + b.input_tokens + 'in/' + b.output_tokens + 'out)'
                );
                document.getElementById('readings-stats').textContent = lines.join('\n');
            }
            // Items
            if (!data.items || data.items.length === 0) {
                document.getElementById('readings-output').textContent = '(no readings in window)';
                return;
            }
            const rows = data.items.map(r => {
                const head = '[' + fmtTs(r.created_at) + '] ' + pad(r.fortune_type, 15)
                    + ' ' + pad(r.card_name || '-', 20)
                    + ' $' + (r.cost_usd || 0).toFixed(4)
                    + '  ip:' + (r.ip_hash_prefix || '-');
                const theme = '    theme: ' + (r.theme_or_input || '-');
                const preview = '    text:  ' + (r.text_preview || '').replace(/\\s+/g, ' ').slice(0, 160);
                return head + '\n' + theme + '\n' + preview;
            });
            document.getElementById('readings-output').textContent = rows.join('\n\n');
        }

        async function loadAgentLogs() {
            document.getElementById('logs-output').textContent = 'Loading...';
            document.getElementById('logs-stats').textContent = 'Loading...';
            const dept = document.getElementById('logs-department').value;
            const st = document.getElementById('logs-status').value;
            const days = document.getElementById('logs-days').value;
            let url = '/api/hoshimi/agent-logs?limit=100&days=' + encodeURIComponent(days);
            if (dept) url += '&department=' + encodeURIComponent(dept);
            if (st) url += '&status=' + encodeURIComponent(st);
            const data = await apiCall(url);
            if (data.error || data.detail) {
                display('logs-output', data);
                document.getElementById('logs-stats').textContent = data.error || data.detail || 'error';
                return;
            }
            // Breakdown
            const bd = (data.by_dept_status || []).slice().sort((a, b) => {
                if (a.department !== b.department) return a.department.localeCompare(b.department);
                return a.status.localeCompare(b.status);
            });
            if (bd.length === 0) {
                document.getElementById('logs-stats').textContent = '(no dispatches in window)';
            } else {
                const lines = ['Window: last ' + data.days + ' days, total ' + data.total + ' rows', ''];
                for (const row of bd) {
                    lines.push(
                        pad(row.department, 14) + pad(row.status, 8)
                        + pad(row.count + 'x', 8) + 'avg ' + row.avg_duration_ms + 'ms'
                    );
                }
                document.getElementById('logs-stats').textContent = lines.join('\n');
            }
            // Items
            if (!data.items || data.items.length === 0) {
                document.getElementById('logs-output').textContent = '(no dispatches in window)';
                return;
            }
            const rows = data.items.map(r => {
                const head = '[' + fmtTs(r.created_at) + '] '
                    + pad(r.department, 14) + pad(r.task, 24)
                    + pad(r.status, 7) + pad(r.duration_ms + 'ms', 9)
                    + (r.triggered_by ? 'via ' + r.triggered_by : '');
                const parts = [head];
                if (r.input_preview) parts.push('  in:  ' + r.input_preview);
                if (r.output_preview) parts.push('  out: ' + r.output_preview);
                if (r.error) parts.push('  ERR: ' + r.error);
                return parts.join('\n');
            });
            document.getElementById('logs-output').textContent = rows.join('\n\n');
        }

        async function loadAgentTasks() {
            document.getElementById('tasks-output').textContent = 'Loading...';
            document.getElementById('tasks-stats').textContent = 'Loading...';
            const dept = document.getElementById('tasks-department').value;
            const st = document.getElementById('tasks-status').value;
            let url = '/api/hoshimi/agent-tasks?limit=200';
            if (dept) url += '&department=' + encodeURIComponent(dept);
            if (st) url += '&status=' + encodeURIComponent(st);
            const data = await apiCall(url);
            if (data.error || data.detail) {
                display('tasks-output', data);
                document.getElementById('tasks-stats').textContent = data.error || data.detail || 'error';
                return;
            }
            // Kanban: dept rows × status columns
            const STATUSES = ['pending', 'in_progress', 'blocked', 'completed', 'canceled'];
            const deptSet = new Set();
            const cube = {};
            for (const row of data.by_dept_status || []) {
                deptSet.add(row.department);
                cube[row.department] = cube[row.department] || {};
                cube[row.department][row.status] = row.count;
            }
            const depts = Array.from(deptSet).sort();
            if (depts.length === 0) {
                document.getElementById('tasks-stats').textContent = '(no tasks yet)';
            } else {
                const header = pad('', 14) + STATUSES.map(s => pad(s, 13)).join('');
                const lines = ['Total: ' + data.total + ' tasks', '', header];
                for (const d of depts) {
                    const row = [pad(d, 14)];
                    for (const s of STATUSES) {
                        const v = (cube[d] && cube[d][s]) || 0;
                        row.push(pad(v ? String(v) : '-', 13));
                    }
                    lines.push(row.join(''));
                }
                document.getElementById('tasks-stats').textContent = lines.join('\n');
            }
            // Items
            if (!data.items || data.items.length === 0) {
                document.getElementById('tasks-output').textContent = '(no tasks match filter)';
                return;
            }
            const rows = data.items.map(r => {
                const head = '#' + r.id + ' [P' + r.priority + '] '
                    + pad(r.department, 14) + pad(r.status, 13) + r.title;
                const parts = [head];
                if (r.assigned_to) parts.push('  assigned: ' + r.assigned_to);
                if (r.due_at) parts.push('  due: ' + fmtTs(r.due_at));
                if (r.description) parts.push('  ' + r.description.replace(/\\s+/g, ' ').slice(0, 200));
                return parts.join('\n');
            });
            document.getElementById('tasks-output').textContent = rows.join('\n\n');
        }
    </script>
</body>
</html>"""
