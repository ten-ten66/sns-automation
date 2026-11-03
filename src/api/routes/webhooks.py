"""Payment webhook receivers for HoshimiLive.

Handles incoming notifications from Paddle and Lemon Squeezy, verifies
signatures, and upserts ``customers`` / ``orders`` / ``subscriptions`` rows.

Routes in this module are PUBLIC (no API key) — they are authenticated
via HMAC signature headers instead. The secrets are configured via env
vars ``PADDLE_WEBHOOK_SECRET`` and ``LEMON_SQUEEZY_WEBHOOK_SECRET``.

Both handlers are idempotent: a duplicate notification produces no
additional row. Idempotency is enforced at the DB layer via the unique
index on ``(provider, external_id)`` for orders and subscriptions.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from src.core.database import async_session
from src.core.models import (
    Customer,
    Order,
    OrderStatus,
    Subscription,
    SubscriptionStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

# Max age of a webhook signature before we reject it as a replay (seconds).
# Paddle recommends 5 minutes; Lemon Squeezy does not sign a timestamp so
# the freshness check only applies to Paddle.
_MAX_SIGNATURE_AGE_SEC = 5 * 60


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _parse_paddle_signature_header(header: str) -> tuple[int, str] | None:
    """Parse ``ts=...;h1=...`` into ``(timestamp, hmac_hex)``.

    Returns ``None`` if the header is malformed.
    """
    ts = None
    h1 = None
    for part in header.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k == "ts":
            try:
                ts = int(v)
            except ValueError:
                return None
        elif k == "h1":
            h1 = v
    if ts is None or not h1:
        return None
    return ts, h1


def verify_paddle_signature(raw_body: bytes, header: str, secret: str) -> bool:
    """Verify a Paddle ``Paddle-Signature`` header against the raw body.

    See https://developer.paddle.com/webhooks/signature-verification
    """
    if not secret or not header:
        return False
    parsed = _parse_paddle_signature_header(header)
    if parsed is None:
        return False
    ts, h1 = parsed

    # Replay protection: reject signatures older than 5 minutes. This assumes
    # the server clock is reasonably synced via NTP (it is — systemd-timesyncd).
    if abs(time.time() - ts) > _MAX_SIGNATURE_AGE_SEC:
        logger.warning("Paddle signature rejected: stale timestamp (age=%ss)",
                       int(time.time() - ts))
        return False

    signed_payload = f"{ts}:".encode("utf-8") + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, sha256).hexdigest()
    return hmac.compare_digest(expected, h1)


def verify_lemon_squeezy_signature(raw_body: bytes, header: str, secret: str) -> bool:
    """Verify a Lemon Squeezy ``X-Signature`` header against the raw body.

    LS signs the raw JSON body with HMAC-SHA256 and sends the hex digest
    in the ``X-Signature`` header. See:
    https://docs.lemonsqueezy.com/help/webhooks#signing-requests
    """
    if not secret or not header:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()
    return hmac.compare_digest(expected, header)


# ---------------------------------------------------------------------------
# Shared persistence helpers
# ---------------------------------------------------------------------------


async def _upsert_customer(
    session: AsyncSession,
    *,
    email: str,
    provider: str,
    external_id: str | None = None,
    country: str | None = None,
    locale: str | None = None,
    display_name: str | None = None,
    extra_data: dict[str, Any] | None = None,
) -> Customer:
    """Find a customer by ``(provider, external_id)`` or email, or create one."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("customer email is required")

    row: Customer | None = None
    if external_id:
        res = await session.execute(
            select(Customer).where(
                Customer.provider == provider,
                Customer.external_id == external_id,
            )
        )
        row = res.scalar_one_or_none()
    if row is None:
        res = await session.execute(select(Customer).where(Customer.email == email))
        row = res.scalar_one_or_none()

    if row is None:
        row = Customer(
            email=email,
            provider=provider,
            external_id=external_id,
            country=country,
            locale=locale,
            display_name=display_name,
            extra_data=extra_data,
        )
        session.add(row)
        await session.flush()
        return row

    # Update known-blank fields in place — never overwrite something non-blank
    # with a blank incoming value, since different webhook payloads ship
    # different subsets.
    changed = False
    if not row.external_id and external_id:
        row.external_id = external_id
        changed = True
    if not row.provider and provider:
        row.provider = provider
        changed = True
    if not row.country and country:
        row.country = country
        changed = True
    if not row.locale and locale:
        row.locale = locale
        changed = True
    if not row.display_name and display_name:
        row.display_name = display_name
        changed = True
    if extra_data is not None:
        merged = dict(row.extra_data or {})
        merged.update(extra_data)
        row.extra_data = merged
        changed = True
    if changed:
        await session.flush()
    return row


async def _insert_order_idempotent(
    session: AsyncSession, order: Order
) -> tuple[Order, bool]:
    """Insert an order, treating ``(provider, external_id)`` unique index
    violations as idempotent.

    Returns ``(row, inserted)`` where ``inserted`` is False if the order
    already existed.
    """
    session.add(order)
    try:
        await session.flush()
        return order, True
    except IntegrityError:
        await session.rollback()
        res = await session.execute(
            select(Order).where(
                Order.provider == order.provider,
                Order.external_id == order.external_id,
            )
        )
        existing = res.scalar_one()
        return existing, False


async def _upsert_subscription(
    session: AsyncSession, sub: Subscription
) -> tuple[Subscription, bool]:
    """Upsert a subscription by ``(provider, external_id)``.

    Returns ``(row, inserted)`` where ``inserted`` is True on first insert.
    """
    res = await session.execute(
        select(Subscription).where(
            Subscription.provider == sub.provider,
            Subscription.external_id == sub.external_id,
        )
    )
    existing = res.scalar_one_or_none()
    if existing is None:
        session.add(sub)
        await session.flush()
        return sub, True

    # Update mutable fields in place
    existing.status = sub.status
    existing.plan_sku = sub.plan_sku or existing.plan_sku
    existing.amount = sub.amount or existing.amount
    existing.currency = sub.currency or existing.currency
    existing.interval = sub.interval or existing.interval
    existing.current_period_start = sub.current_period_start or existing.current_period_start
    existing.current_period_end = sub.current_period_end or existing.current_period_end
    existing.cancel_at_period_end = sub.cancel_at_period_end
    existing.canceled_at = sub.canceled_at or existing.canceled_at
    if sub.extra_data:
        merged = dict(existing.extra_data or {})
        merged.update(sub.extra_data)
        existing.extra_data = merged
    await session.flush()
    return existing, False


# ---------------------------------------------------------------------------
# Paddle event handling
# ---------------------------------------------------------------------------

# Paddle status → our OrderStatus
_PADDLE_TXN_STATUS_MAP = {
    "completed": OrderStatus.PAID,
    "paid": OrderStatus.PAID,
    "billed": OrderStatus.PAID,
    "canceled": OrderStatus.FAILED,
    "past_due": OrderStatus.FAILED,
}

_PADDLE_SUB_STATUS_MAP = {
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELED,
    "paused": SubscriptionStatus.PAST_DUE,
}


def _paddle_amount_to_decimal(totals: dict[str, Any] | None) -> tuple[float, str]:
    """Paddle sends amounts as minor-unit strings ("1999" = $19.99)."""
    if not totals:
        return 0.0, "USD"
    raw = totals.get("grand_total") or totals.get("total") or "0"
    try:
        amount = int(raw) / 100.0
    except (TypeError, ValueError):
        amount = 0.0
    return amount, (totals.get("currency_code") or "USD").upper()


def _parse_paddle_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Paddle uses ISO-8601 with trailing "Z"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


async def _handle_paddle_event(
    session: AsyncSession, event_type: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Dispatch a Paddle event to the right persistence path."""
    if event_type in ("customer.created", "customer.updated"):
        await _upsert_customer(
            session,
            email=data.get("email", ""),
            provider="paddle",
            external_id=data.get("id"),
            country=(data.get("custom_data") or {}).get("country"),
            locale=data.get("locale"),
            display_name=data.get("name"),
            extra_data={"source_event": event_type},
        )
        return {"handled": event_type}

    if event_type in ("transaction.completed", "transaction.paid", "transaction.billed"):
        # customer_id may be either a string id or an expanded customer object
        customer_info = data.get("customer") or {}
        customer_email = customer_info.get("email") or (data.get("billing_details") or {}).get("email") or ""
        paddle_customer_id = data.get("customer_id") or customer_info.get("id")
        if not customer_email:
            logger.warning("Paddle %s missing customer email — skipping", event_type)
            return {"skipped": "no_email"}
        customer = await _upsert_customer(
            session,
            email=customer_email,
            provider="paddle",
            external_id=paddle_customer_id,
        )
        amount, currency = _paddle_amount_to_decimal(data.get("details", {}).get("totals") or data.get("totals"))
        items = data.get("items") or []
        product_sku = ""
        if items:
            price = (items[0] or {}).get("price") or {}
            product_sku = price.get("product_id") or price.get("id") or ""
        order = Order(
            customer_id=customer.id,
            external_id=data.get("id"),
            provider="paddle",
            product_sku=product_sku or "unknown",
            amount=amount,
            currency=currency,
            status=_PADDLE_TXN_STATUS_MAP.get(data.get("status", ""), OrderStatus.PAID),
            paid_at=_parse_paddle_datetime(data.get("billed_at") or data.get("created_at")),
            extra_data={"event": event_type, "status": data.get("status")},
        )
        _, inserted = await _insert_order_idempotent(session, order)
        return {"handled": event_type, "inserted": inserted}

    if event_type in ("transaction.payment_failed",):
        # Best-effort: mark any existing order as failed
        txn_id = data.get("id")
        if txn_id:
            res = await session.execute(
                select(Order).where(Order.provider == "paddle", Order.external_id == txn_id)
            )
            row = res.scalar_one_or_none()
            if row:
                row.status = OrderStatus.FAILED
        return {"handled": event_type}

    if event_type in (
        "subscription.created",
        "subscription.activated",
        "subscription.updated",
        "subscription.canceled",
        "subscription.paused",
    ):
        customer_info = data.get("customer") or {}
        customer_email = customer_info.get("email") or ""
        paddle_customer_id = data.get("customer_id") or customer_info.get("id")
        if not customer_email and not paddle_customer_id:
            logger.warning("Paddle %s missing customer ref — skipping", event_type)
            return {"skipped": "no_customer"}
        customer = await _upsert_customer(
            session,
            email=customer_email or f"paddle_{paddle_customer_id}@placeholder.local",
            provider="paddle",
            external_id=paddle_customer_id,
        )
        items = data.get("items") or []
        plan_sku = ""
        amount = 0.0
        currency = "USD"
        interval = "month"
        if items:
            price = (items[0] or {}).get("price") or {}
            plan_sku = price.get("product_id") or price.get("id") or ""
            unit_price = price.get("unit_price") or {}
            try:
                amount = int(unit_price.get("amount", "0")) / 100.0
            except (TypeError, ValueError):
                amount = 0.0
            currency = (unit_price.get("currency_code") or currency).upper()
            billing_cycle = (price.get("billing_cycle") or {})
            interval = billing_cycle.get("interval") or interval

        sub = Subscription(
            customer_id=customer.id,
            external_id=data.get("id"),
            provider="paddle",
            plan_sku=plan_sku or "unknown",
            status=_PADDLE_SUB_STATUS_MAP.get(
                data.get("status", ""),
                SubscriptionStatus.ACTIVE,
            ),
            amount=amount,
            currency=currency,
            interval=interval,
            current_period_start=_parse_paddle_datetime(
                (data.get("current_billing_period") or {}).get("starts_at")
            ),
            current_period_end=_parse_paddle_datetime(
                (data.get("current_billing_period") or {}).get("ends_at")
            ),
            cancel_at_period_end=bool(data.get("scheduled_change")),
            canceled_at=_parse_paddle_datetime(data.get("canceled_at")),
            extra_data={"event": event_type},
        )
        _, inserted = await _upsert_subscription(session, sub)
        return {"handled": event_type, "inserted": inserted}

    return {"handled": False, "reason": f"unknown event: {event_type}"}


# ---------------------------------------------------------------------------
# Lemon Squeezy event handling
# ---------------------------------------------------------------------------

_LS_SUB_STATUS_MAP = {
    "on_trial": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "cancelled": SubscriptionStatus.CANCELED,
    "expired": SubscriptionStatus.EXPIRED,
    "unpaid": SubscriptionStatus.PAST_DUE,
}


def _ls_amount_to_decimal(attrs: dict[str, Any], total_key: str = "total") -> tuple[float, str]:
    """LS sends amounts as minor-unit integers (cents)."""
    raw = attrs.get(total_key) or 0
    try:
        amount = int(raw) / 100.0
    except (TypeError, ValueError):
        amount = 0.0
    currency = (attrs.get("currency") or "USD").upper()
    return amount, currency


def _parse_ls_datetime(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


async def _handle_lemon_squeezy_event(
    session: AsyncSession, event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """LS sends ``{meta: {event_name, ...}, data: {id, type, attributes}}``."""
    data = payload.get("data") or {}
    attrs = data.get("attributes") or {}
    resource_id = str(data.get("id") or "")

    if event_type == "order_created":
        customer = await _upsert_customer(
            session,
            email=attrs.get("user_email", ""),
            provider="lemon_squeezy",
            external_id=str(attrs.get("customer_id") or ""),
            display_name=attrs.get("user_name"),
            extra_data={"store_id": attrs.get("store_id")},
        )
        amount, currency = _ls_amount_to_decimal(attrs)
        order = Order(
            customer_id=customer.id,
            external_id=resource_id,
            provider="lemon_squeezy",
            product_sku=str(attrs.get("first_order_item", {}).get("product_id") or "unknown"),
            amount=amount,
            currency=currency,
            status=OrderStatus.PAID if attrs.get("status") == "paid" else OrderStatus.PENDING,
            paid_at=_parse_ls_datetime(attrs.get("created_at")),
            extra_data={"identifier": attrs.get("identifier"), "order_number": attrs.get("order_number")},
        )
        _, inserted = await _insert_order_idempotent(session, order)
        return {"handled": event_type, "inserted": inserted}

    if event_type == "order_refunded":
        if resource_id:
            res = await session.execute(
                select(Order).where(
                    Order.provider == "lemon_squeezy",
                    Order.external_id == resource_id,
                )
            )
            row = res.scalar_one_or_none()
            if row:
                row.status = OrderStatus.REFUNDED
                row.refunded_at = _parse_ls_datetime(attrs.get("refunded_at")) or datetime.now(timezone.utc)
        return {"handled": event_type}

    if event_type in (
        "subscription_created",
        "subscription_updated",
        "subscription_cancelled",
        "subscription_resumed",
        "subscription_expired",
        "subscription_paused",
        "subscription_unpaused",
    ):
        customer = await _upsert_customer(
            session,
            email=attrs.get("user_email", "")
                  or f"ls_{attrs.get('customer_id')}@placeholder.local",
            provider="lemon_squeezy",
            external_id=str(attrs.get("customer_id") or ""),
            display_name=attrs.get("user_name"),
        )
        amount, currency = _ls_amount_to_decimal(attrs)
        # LS variant id is the per-plan SKU; product_id groups variants
        plan_sku = str(attrs.get("variant_id") or attrs.get("product_id") or "unknown")
        interval = "month"  # LS does not expose interval on subscription attrs;
                            # we'd derive from the variant lookup via API, not critical here

        sub = Subscription(
            customer_id=customer.id,
            external_id=resource_id,
            provider="lemon_squeezy",
            plan_sku=plan_sku,
            status=_LS_SUB_STATUS_MAP.get(attrs.get("status", ""), SubscriptionStatus.ACTIVE),
            amount=amount,
            currency=currency,
            interval=interval,
            current_period_start=_parse_ls_datetime(attrs.get("created_at")),
            current_period_end=_parse_ls_datetime(attrs.get("renews_at") or attrs.get("ends_at")),
            cancel_at_period_end=bool(attrs.get("cancelled")),
            canceled_at=_parse_ls_datetime(attrs.get("ends_at") if attrs.get("cancelled") else None),
            extra_data={"event": event_type, "trial_ends_at": attrs.get("trial_ends_at")},
        )
        _, inserted = await _upsert_subscription(session, sub)
        return {"handled": event_type, "inserted": inserted}

    if event_type in ("subscription_payment_success", "subscription_payment_failed",
                      "subscription_payment_recovered", "subscription_payment_refunded"):
        # Not persisted as a separate row — LS handles these as subscription-invoice
        # events; we just log them for audit.
        logger.info("LS payment event %s for subscription %s", event_type, resource_id)
        return {"handled": event_type, "note": "logged only"}

    return {"handled": False, "reason": f"unknown event: {event_type}"}


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


async def _read_raw_body(request: Request) -> bytes:
    # Fast path: reject oversized payloads before buffering them
    content_length = request.headers.get("content-length")
    try:
        cl = int(content_length) if content_length else 0
    except (ValueError, TypeError):
        cl = 0
    if cl > 1_000_000:
        raise HTTPException(status_code=413, detail="Payload too large")
    raw = await request.body()
    if len(raw) > 1_000_000:  # 1 MB cap — legit webhooks are much smaller
        raise HTTPException(status_code=413, detail="payload too large")
    return raw


@router.post("/api/webhook/paddle")
async def paddle_webhook(request: Request) -> JSONResponse:
    if not settings.paddle_webhook_secret:
        logger.error("Paddle webhook received but PADDLE_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=503, detail="webhook receiver not configured")

    raw = await _read_raw_body(request)
    sig_header = request.headers.get("paddle-signature", "")
    if not verify_paddle_signature(raw, sig_header, settings.paddle_webhook_secret):
        logger.warning("Paddle webhook: invalid signature")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed JSON")

    event_type = payload.get("event_type", "")
    data = payload.get("data") or {}
    if not event_type:
        raise HTTPException(status_code=400, detail="missing event_type")

    try:
        async with async_session() as session:
            result = await _handle_paddle_event(session, event_type, data)
            await session.commit()
    except Exception:
        logger.exception("Paddle webhook handler failed: %s", event_type)
        # Return 500 so Paddle retries per their spec
        raise HTTPException(status_code=500, detail="handler error")

    logger.info("Paddle webhook handled: %s -> %s", event_type, result)
    return JSONResponse({"ok": True, "event": event_type, "result": result})


@router.post("/api/webhook/lemon-squeezy")
async def lemon_squeezy_webhook(request: Request) -> JSONResponse:
    if not settings.lemon_squeezy_webhook_secret:
        logger.error("LS webhook received but LEMON_SQUEEZY_WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=503, detail="webhook receiver not configured")

    raw = await _read_raw_body(request)
    sig_header = request.headers.get("x-signature", "")
    if not verify_lemon_squeezy_signature(raw, sig_header, settings.lemon_squeezy_webhook_secret):
        logger.warning("LS webhook: invalid signature")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="malformed JSON")

    meta = payload.get("meta") or {}
    event_type = meta.get("event_name", "")
    if not event_type:
        raise HTTPException(status_code=400, detail="missing meta.event_name")

    try:
        async with async_session() as session:
            result = await _handle_lemon_squeezy_event(session, event_type, payload)
            await session.commit()
    except Exception:
        logger.exception("LS webhook handler failed: %s", event_type)
        raise HTTPException(status_code=500, detail="handler error")

    logger.info("LS webhook handled: %s -> %s", event_type, result)
    return JSONResponse({"ok": True, "event": event_type, "result": result})


# Paths that must bypass the global API-key auth (signed via HMAC instead)
PUBLIC_PATHS: set[str] = {
    "/api/webhook/paddle",
    "/api/webhook/lemon-squeezy",
}
