"""Public fortune-telling routes for hoshimilive.com.

All routes in this router are PUBLIC (no API key required) — they serve the
landing page and free-sample fortune endpoints that anonymous visitors hit.

Rate limiting is applied per-IP via the slowapi Limiter configured on the
main app. Claude API spend is protected by the existing cost_tracker budget
check inside FortuneGenerator.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field, field_validator
from slowapi import Limiter
from slowapi.util import get_remote_address

from src.ai.fortune_generator import FortuneGenerator

# Per-IP rate limiter for fortune API — protects Claude budget from abuse.
# 10 readings/minute is generous for genuine users, brutal for scrapers.
_fortune_limiter = Limiter(key_func=get_remote_address)
from src.core.cost_tracker import BudgetExceededError
from src.core.database import async_session
from src.core.models import FortuneReading

logger = logging.getLogger(__name__)

router = APIRouter(tags=["fortune-public"])

_fortune_gen: FortuneGenerator | None = None

# 24-hour cache for horoscope readings (date_str -> (data, timestamp))
_horoscope_cache: dict[str, tuple[dict, float]] = {}
_horoscope_lock = asyncio.Lock()  # Prevent duplicate Claude calls on cache miss

# Allowed characters for user-supplied text fields
_SAFE_INPUT_RE = re.compile(r"^[A-Za-z0-9\u3000-\u9fff\u30a0-\u30ff\u3040-\u309f .,!?'\-]+$")
# Injection keywords to reject outright
_INJECTION_KEYWORDS = ("ignore", "system prompt", "instructions", "forget")


def _get_fortune_gen() -> FortuneGenerator:
    """Lazy singleton so the module imports cleanly without API keys at load."""
    global _fortune_gen
    if _fortune_gen is None:
        _fortune_gen = FortuneGenerator()
    return _fortune_gen


# --- Visitor logging (bot vs human classification) ---
#
# Validates the "148 LP views are all cloud-provider bots" hypothesis from
# memory project_miku_oracle_zero_conversion_2026_04_20. Two-stage classify:
#   1. Server-side UA inspection on GET / (and other public LP pages)
#   2. Client-side JS heartbeat to /api/lp/heartbeat — only runs in real browsers
# A visitor with stage-1 "human_candidate" + stage-2 heartbeat = real human.

_LP_VISITORS_LOG = Path(__file__).resolve().parents[3] / "data" / "lp_visitors.jsonl"
_BOT_UA_RE = re.compile(
    r"(bot|crawler|spider|scrap|fetch|wget|curl|httpclient|python-requests|"
    r"go-http|libwww|java/|axios|okhttp|headlesschrome|phantomjs|slurp|"
    r"yahoofeed|facebookexternalhit|whatsapp|telegrambot|linkedinbot|"
    r"chatgpt|gptbot|anthropic|claudebot|perplexity)",
    re.IGNORECASE,
)


def _classify_ua(ua: str) -> str:
    if not ua:
        return "no_ua"
    if _BOT_UA_RE.search(ua):
        return "bot"
    return "human_candidate"


def _hash_ip(ip: str) -> str:
    """Privacy-preserving IP fingerprint (daily-salted)."""
    salt = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()[:16]


def _log_lp_visit(request: Request, stage: str, classification: str | None = None) -> None:
    try:
        ua = request.headers.get("user-agent", "")
        cls = classification or _classify_ua(ua)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "path": str(request.url.path),
            "ua": ua[:200],
            "ip_h": _hash_ip(get_remote_address(request) or ""),
            "ref": (request.headers.get("referer") or "")[:200],
            "classification": cls,
        }
        _LP_VISITORS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _LP_VISITORS_LOG.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("lp visit log skipped: %s", e)


# --- Landing page ---

_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates" / "hoshimilive"


def _load_template(name: str) -> str:
    path = _TEMPLATES_DIR / name
    if not path.exists():
        raise HTTPException(status_code=500, detail=f"template missing: {name}")
    return path.read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing_page(request: Request) -> HTMLResponse:
    """Serve the HoshimiLive landing page."""
    _log_lp_visit(request, stage="server_get")
    return HTMLResponse(_load_template("landing.html"))


@router.post("/api/lp/heartbeat", include_in_schema=False)
async def lp_heartbeat(request: Request) -> Response:
    """JS-execution heartbeat from the LP — only real browsers fire this."""
    _log_lp_visit(request, stage="js_heartbeat", classification="human")
    return Response(status_code=204)


@router.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_load_template("privacy.html"))


@router.get("/terms", response_class=HTMLResponse, include_in_schema=False)
async def terms_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_load_template("terms.html"))


@router.get("/pricing", response_class=HTMLResponse, include_in_schema=False)
async def pricing_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_load_template("pricing.html"))


@router.get("/refund", response_class=HTMLResponse, include_in_schema=False)
async def refund_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_load_template("refund.html"))


@router.get("/contact", response_class=HTMLResponse, include_in_schema=False)
async def contact_page(request: Request) -> HTMLResponse:
    return HTMLResponse(_load_template("contact.html"))


@router.get("/robots.txt", include_in_schema=False)
async def robots_txt() -> "Response":
    """robots.txt — let crawlers index the public pages, block the API."""
    from fastapi.responses import Response
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /pricing\n"
        "Allow: /terms\n"
        "Allow: /privacy\n"
        "Allow: /refund\n"
        "Allow: /contact\n"
        "Disallow: /api/\n"
        "Disallow: /webhooks/\n"
        "\n"
        "Sitemap: https://hoshimilive.com/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")


@router.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml() -> "Response":
    """sitemap.xml — lists the public LP pages for search engines."""
    from fastapi.responses import Response
    from datetime import date
    today = date.today().isoformat()
    urls = ["", "pricing", "terms", "privacy", "refund", "contact"]
    url_entries = "\n".join(
        f"  <url><loc>https://hoshimilive.com/{u}</loc>"
        f"<lastmod>{today}</lastmod>"
        f"<changefreq>weekly</changefreq>"
        f"<priority>{0.9 if u == '' else 0.5}</priority></url>"
        for u in urls
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{url_entries}\n"
        "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


# --- Fortune API endpoints ---


class TarotRequest(BaseModel):
    theme: str = Field(default="general daily guidance", max_length=120)

    @field_validator("theme")
    @classmethod
    def sanitize_theme(cls, v: str) -> str:
        v = v.strip()
        if "\n" in v or "\r" in v:
            raise ValueError("theme must be single line")
        v_lower = v.lower()
        for kw in _INJECTION_KEYWORDS:
            if kw in v_lower:
                raise ValueError("theme contains disallowed content")
        if not _SAFE_INPUT_RE.match(v):
            raise ValueError("theme contains disallowed characters")
        return v


class NumerologyRequest(BaseModel):
    birthdate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class LoveRequest(BaseModel):
    question: str = Field(default="general love energy", max_length=240)

    @field_validator("question")
    @classmethod
    def sanitize_question(cls, v: str) -> str:
        v = v.strip()
        if "\n" in v or "\r" in v:
            raise ValueError("question must be single line")
        v_lower = v.lower()
        for kw in _INJECTION_KEYWORDS:
            if kw in v_lower:
                raise ValueError("question contains disallowed content")
        if not _SAFE_INPUT_RE.match(v):
            raise ValueError("question contains disallowed characters")
        return v


def _client_ip(request: Request) -> str:
    """Extract real client IP, honoring Cloudflare + X-Forwarded-For headers."""
    # Cloudflare sets CF-Connecting-IP; Nginx populates it via real_ip_header
    # directive, so request.client.host is already the real IP. But we fall
    # back to the header if uvicorn is reached directly.
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def _hash_ip(ip: str) -> str | None:
    """Hash an IP address for storage (sha256 hex, ~64 chars)."""
    if not ip:
        return None
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def _extract_display_fields(raw: dict[str, Any]) -> dict[str, Any]:
    """Pick human-visible fields out of a generator result for the audit row.

    Different fortune types return different JSON shapes — we normalize to
    (card_name, text, cta) for the fortune_readings columns.
    """
    card_name = raw.get("card_name")
    text_body = (
        raw.get("text")
        or raw.get("reading")
        or raw.get("message")
        or raw.get("overall_vibe")
    )
    cta = raw.get("cta")
    return {"card_name": card_name, "text": text_body, "cta": cta}


async def _save_reading(
    request: Request,
    fortune_type: str,
    kwargs: dict[str, Any],
    raw: dict[str, Any],
    usage: dict[str, Any],
) -> None:
    """Persist one FortuneReading row. Failures are logged, never raised."""
    theme_or_input = (
        kwargs.get("theme")
        or kwargs.get("question")
        or kwargs.get("birthdate")
        or kwargs.get("mood")
        or ""
    )
    display = _extract_display_fields(raw) if isinstance(raw, dict) else {}
    ip_hash = _hash_ip(_client_ip(request))

    try:
        async with async_session() as session:
            reading = FortuneReading(
                customer_id=None,  # free samples are anonymous
                ip_hash=ip_hash,
                fortune_type=fortune_type,
                theme_or_input=str(theme_or_input)[:500] if theme_or_input else None,
                card_name=display.get("card_name"),
                text=display.get("text"),
                cta=display.get("cta"),
                model_used=usage.get("model"),
                cost_usd=usage.get("cost_usd") or 0,
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
            )
            session.add(reading)
            await session.commit()
    except Exception:  # noqa: BLE001
        # Never block the user's reading on an audit-log failure
        logger.exception("fortune_readings insert failed (non-fatal)")


async def _safe_generate(
    request: Request, fortune_type: str, **kwargs: Any
) -> dict[str, Any]:
    """Call FortuneGenerator off the event loop, persist the reading, map errors."""
    gen = _get_fortune_gen()
    try:
        raw, usage = await asyncio.to_thread(
            gen.generate_with_usage, fortune_type, **kwargs
        )
    except BudgetExceededError:
        raise HTTPException(
            status_code=429,
            detail="Daily oracle budget reached ~ come back tomorrow, starlight ✨",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("fortune generation failed: %s", fortune_type)
        raise HTTPException(
            status_code=500, detail="The stars are dim right now, try again ✨"
        )

    # Persist audit row (non-blocking on failure)
    await _save_reading(request, fortune_type, kwargs, raw, usage)
    return raw


@router.post("/api/fortune/tarot")
@_fortune_limiter.limit("10/minute")
async def api_tarot(request: Request, body: TarotRequest) -> JSONResponse:
    """Free single-card tarot reading."""
    data = await _safe_generate(request, "tarot", theme=body.theme)
    return JSONResponse(data)


@router.get("/api/fortune/horoscope")
@_fortune_limiter.limit("5/minute")
async def api_horoscope(request: Request) -> JSONResponse:
    """Daily horoscope for all 12 signs (cached 24h per UTC date)."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cached = _horoscope_cache.get(today)
    if cached is not None:
        data, _ts = cached
        logger.debug("horoscope cache hit for %s", today)
        return JSONResponse(data)

    async with _horoscope_lock:
        # Double-check after acquiring the lock (another request may have filled it)
        cached = _horoscope_cache.get(today)
        if cached is not None:
            return JSONResponse(cached[0])
        data = await _safe_generate(request, "horoscope")
        _horoscope_cache.clear()  # evict any stale date keys
        _horoscope_cache[today] = (data, time.monotonic())
    return JSONResponse(data)


@router.post("/api/fortune/numerology")
@_fortune_limiter.limit("10/minute")
async def api_numerology(request: Request, body: NumerologyRequest) -> JSONResponse:
    """Numerology life path reading."""
    data = await _safe_generate(request, "numerology", birthdate=body.birthdate)
    return JSONResponse(data)


@router.post("/api/fortune/love")
@_fortune_limiter.limit("10/minute")
async def api_love(request: Request, body: LoveRequest) -> JSONResponse:
    """Love reading. Energy-based, never predictive."""
    data = await _safe_generate(request, "love", question=body.question)
    return JSONResponse(data)


# --- Public paths list (imported by app.py to skip global auth) ---

PUBLIC_PATHS: set[str] = {
    "/",
    "/privacy",
    "/terms",
    "/pricing",
    "/refund",
    "/contact",
    "/robots.txt",
    "/sitemap.xml",
    "/api/fortune/tarot",
    "/api/fortune/horoscope",
    "/api/fortune/numerology",
    "/api/fortune/love",
}
