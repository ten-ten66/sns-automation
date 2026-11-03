#!/usr/bin/env python3
"""End-to-end verification harness for Paddle + Lemon Squeezy webhooks.

This script replays realistic webhook payloads (matching the exact schemas
documented at developer.paddle.com/webhooks and docs.lemonsqueezy.com/help/webhooks)
against our handlers and reports, for each event:

  1. Signature verification — HMAC-SHA256 hash is computed with the
     real production algorithm (Paddle: ``{ts}:<body>`` prefix; Lemon
     Squeezy: raw body) and checked against our ``verify_*_signature``
     helpers.
  2. Event dispatch — handler returns a dict describing what it did.
  3. DB persistence — we query customers / orders / subscriptions
     afterwards and print the row counts + a couple of spot-check fields.

Two modes:

    --mode=offline   (default) imports the handlers and runs them directly
                     against a fresh SQLite DB at ``/tmp/verify_webhooks.db``.
                     No HTTP, no running service required.

    --mode=live      requires the service to be running at
                     ``http://127.0.0.1:9000`` AND the env vars
                     ``PADDLE_WEBHOOK_SECRET`` + ``LEMON_SQUEEZY_WEBHOOK_SECRET``
                     to be set in the service process. Posts signed payloads
                     over HTTP and inspects the returned JSON.

Usage::

    python scripts/verify_webhooks.py
    python scripts/verify_webhooks.py --mode=live --base-url http://127.0.0.1:9000

Exits non-zero if any event fails signature verification, dispatch, or
DB persistence.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sys
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

# Allow running from the repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Realistic payload fixtures — mirrors the schema Paddle/LS actually deliver
# ---------------------------------------------------------------------------

PADDLE_EVENTS: list[dict[str, Any]] = [
    {
        "event_type": "customer.created",
        "event_id": "evt_01h1vk11111111111111111111",
        "occurred_at": "2026-04-11T12:00:00.000Z",
        "data": {
            "id": "ctm_01h1vkparticipant111111111111",
            "email": "oracle-fan-01@example.com",
            "name": "Alice Customer",
            "locale": "ja-JP",
            "marketing_consent": True,
            "custom_data": {"country": "JP"},
            "status": "active",
            "created_at": "2026-04-11T12:00:00.000Z",
            "updated_at": "2026-04-11T12:00:00.000Z",
        },
    },
    {
        "event_type": "transaction.completed",
        "event_id": "evt_01h1vk22222222222222222222",
        "occurred_at": "2026-04-11T12:05:00.000Z",
        "data": {
            "id": "txn_01h1vktransaction111111111111",
            "status": "completed",
            "customer_id": "ctm_01h1vkparticipant111111111111",
            "currency_code": "USD",
            "billed_at": "2026-04-11T12:05:00.000Z",
            "created_at": "2026-04-11T12:05:00.000Z",
            "items": [
                {
                    "price": {
                        "id": "pri_01hproduct11111111111111111",
                        "product_id": "pro_01hstarter1111111111111111",
                        "unit_price": {"amount": "999", "currency_code": "USD"},
                    },
                    "quantity": 1,
                }
            ],
            "details": {
                "totals": {
                    "grand_total": "999",
                    "total": "999",
                    "currency_code": "USD",
                }
            },
            "billing_details": {"email": "oracle-fan-01@example.com"},
        },
    },
    {
        "event_type": "subscription.created",
        "event_id": "evt_01h1vk33333333333333333333",
        "occurred_at": "2026-04-11T12:10:00.000Z",
        "data": {
            "id": "sub_01h1vksubscription11111111111",
            "status": "active",
            "customer_id": "ctm_01h1vkparticipant111111111111",
            "items": [
                {
                    "price": {
                        "id": "pri_01hmonthly1111111111111111",
                        "product_id": "pro_01hpremium1111111111111111",
                        "unit_price": {"amount": "1999", "currency_code": "USD"},
                        "billing_cycle": {"interval": "month", "frequency": 1},
                    },
                    "status": "active",
                }
            ],
            "current_billing_period": {
                "starts_at": "2026-04-11T12:10:00.000Z",
                "ends_at": "2026-05-11T12:10:00.000Z",
            },
            "created_at": "2026-04-11T12:10:00.000Z",
        },
    },
    {
        "event_type": "subscription.canceled",
        "event_id": "evt_01h1vk44444444444444444444",
        "occurred_at": "2026-04-11T12:15:00.000Z",
        "data": {
            "id": "sub_01h1vksubscription11111111111",
            "status": "canceled",
            "customer_id": "ctm_01h1vkparticipant111111111111",
            "canceled_at": "2026-04-11T12:15:00.000Z",
            "items": [
                {
                    "price": {
                        "id": "pri_01hmonthly1111111111111111",
                        "product_id": "pro_01hpremium1111111111111111",
                        "unit_price": {"amount": "1999", "currency_code": "USD"},
                        "billing_cycle": {"interval": "month", "frequency": 1},
                    },
                    "status": "canceled",
                }
            ],
            "scheduled_change": {
                "action": "cancel",
                "effective_at": "2026-05-11T12:10:00.000Z",
            },
        },
    },
]


LEMON_SQUEEZY_EVENTS: list[dict[str, Any]] = [
    {
        "meta": {
            "event_name": "order_created",
            "custom_data": {"source": "hoshimilive"},
        },
        "data": {
            "type": "orders",
            "id": "42001",
            "attributes": {
                "store_id": 12345,
                "customer_id": 98765,
                "identifier": "aabbccdd-eeff-0011-2233-445566778899",
                "order_number": 42001,
                "user_name": "Bob Customer",
                "user_email": "ls-fan-01@example.com",
                "currency": "USD",
                "currency_rate": "1.00",
                "subtotal": 1999,
                "discount_total": 0,
                "tax": 0,
                "total": 1999,
                "subtotal_usd": 1999,
                "status": "paid",
                "refunded": False,
                "first_order_item": {"product_id": 777, "variant_id": 8888, "price": 1999},
                "created_at": "2026-04-11T12:20:00.000000Z",
                "updated_at": "2026-04-11T12:20:00.000000Z",
            },
        },
    },
    {
        "meta": {"event_name": "subscription_created"},
        "data": {
            "type": "subscriptions",
            "id": "51001",
            "attributes": {
                "store_id": 12345,
                "customer_id": 98765,
                "order_id": 42001,
                "user_name": "Bob Customer",
                "user_email": "ls-fan-01@example.com",
                "variant_id": 8888,
                "product_id": 777,
                "status": "active",
                "status_formatted": "Active",
                "currency": "USD",
                "total": 1999,
                "cancelled": False,
                "trial_ends_at": None,
                "renews_at": "2026-05-11T12:20:00.000000Z",
                "ends_at": None,
                "created_at": "2026-04-11T12:20:00.000000Z",
            },
        },
    },
    {
        "meta": {"event_name": "subscription_updated"},
        "data": {
            "type": "subscriptions",
            "id": "51001",
            "attributes": {
                "store_id": 12345,
                "customer_id": 98765,
                "user_name": "Bob Customer",
                "user_email": "ls-fan-01@example.com",
                "variant_id": 8888,
                "product_id": 777,
                "status": "past_due",
                "currency": "USD",
                "total": 1999,
                "cancelled": False,
                "renews_at": "2026-05-11T12:20:00.000000Z",
                "created_at": "2026-04-11T12:20:00.000000Z",
            },
        },
    },
    {
        "meta": {"event_name": "subscription_cancelled"},
        "data": {
            "type": "subscriptions",
            "id": "51001",
            "attributes": {
                "store_id": 12345,
                "customer_id": 98765,
                "user_name": "Bob Customer",
                "user_email": "ls-fan-01@example.com",
                "variant_id": 8888,
                "product_id": 777,
                "status": "cancelled",
                "currency": "USD",
                "total": 1999,
                "cancelled": True,
                "ends_at": "2026-05-11T12:20:00.000000Z",
                "created_at": "2026-04-11T12:20:00.000000Z",
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Signature helpers — MUST match verify_paddle_signature / verify_lemon_squeezy_signature
# ---------------------------------------------------------------------------


def sign_paddle(raw_body: bytes, secret: str, ts: int | None = None) -> tuple[str, int]:
    """Produce a ``Paddle-Signature`` header value for ``raw_body``."""
    ts = ts or int(time.time())
    signed_payload = f"{ts}:".encode("utf-8") + raw_body
    mac = hmac.new(secret.encode("utf-8"), signed_payload, sha256).hexdigest()
    return f"ts={ts};h1={mac}", ts


def sign_lemon_squeezy(raw_body: bytes, secret: str) -> str:
    """Produce an ``X-Signature`` header value for ``raw_body``."""
    return hmac.new(secret.encode("utf-8"), raw_body, sha256).hexdigest()


# ---------------------------------------------------------------------------
# Offline mode — drive handlers directly against an in-memory async SQLite DB
# ---------------------------------------------------------------------------


async def run_offline() -> int:
    """Directly invoke handlers with a clean SQLite DB. Returns exit code."""
    # Use a dedicated file for the verification run so we don't touch prod data
    verify_db = Path("/tmp/verify_webhooks.db")
    if verify_db.exists():
        verify_db.unlink()
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{verify_db}"
    os.environ["PADDLE_WEBHOOK_SECRET"] = "verify_paddle_secret"
    os.environ["LEMON_SQUEEZY_WEBHOOK_SECRET"] = "verify_ls_secret"

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.api.routes.webhooks import (
        _handle_lemon_squeezy_event,
        _handle_paddle_event,
        verify_lemon_squeezy_signature,
        verify_paddle_signature,
    )
    from src.core.models import (
        Base,
        Customer,
        Order,
        Subscription,
    )

    engine = create_async_engine(os.environ["DATABASE_URL"], echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    paddle_secret = "verify_paddle_secret"
    ls_secret = "verify_ls_secret"

    rows_report: list[str] = []
    failures = 0

    print("\n==== Paddle offline verification ====")
    async with session_maker() as session:
        for evt in PADDLE_EVENTS:
            raw = json.dumps(evt).encode("utf-8")
            sig_header, ts = sign_paddle(raw, paddle_secret)
            sig_ok = verify_paddle_signature(raw, sig_header, paddle_secret)
            marker = "OK " if sig_ok else "FAIL"
            print(f"  [{marker}] signature: {evt['event_type']}")
            if not sig_ok:
                failures += 1
                continue

            # Tamper check — flip the last byte so the tampered body is
            # guaranteed different regardless of payload content
            tampered = raw[:-1] + bytes([raw[-1] ^ 0x01])
            sig_tampered = verify_paddle_signature(tampered, sig_header, paddle_secret)
            if sig_tampered:
                print(f"  [FAIL] tampered-body accepted for {evt['event_type']}")
                failures += 1
            else:
                print(f"  [OK ] tamper-detection: {evt['event_type']}")

            try:
                result = await _handle_paddle_event(
                    session, evt["event_type"], evt["data"]
                )
                await session.commit()
                print(f"  [OK ] dispatch: {evt['event_type']} -> {result}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] dispatch: {evt['event_type']} -> {exc}")
                failures += 1

    print("\n==== Lemon Squeezy offline verification ====")
    async with session_maker() as session:
        for evt in LEMON_SQUEEZY_EVENTS:
            raw = json.dumps(evt).encode("utf-8")
            sig_header = sign_lemon_squeezy(raw, ls_secret)
            sig_ok = verify_lemon_squeezy_signature(raw, sig_header, ls_secret)
            marker = "OK " if sig_ok else "FAIL"
            event_name = evt["meta"]["event_name"]
            print(f"  [{marker}] signature: {event_name}")
            if not sig_ok:
                failures += 1
                continue

            tampered = raw[:-1] + bytes([raw[-1] ^ 0x01])
            sig_tampered = verify_lemon_squeezy_signature(tampered, sig_header, ls_secret)
            if sig_tampered:
                print(f"  [FAIL] tampered-body accepted for {event_name}")
                failures += 1
            else:
                print(f"  [OK ] tamper-detection: {event_name}")

            try:
                result = await _handle_lemon_squeezy_event(session, event_name, evt)
                await session.commit()
                print(f"  [OK ] dispatch: {event_name} -> {result}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] dispatch: {event_name} -> {exc}")
                failures += 1

    # ------------------------------------------------------------------
    # DB assertions
    # ------------------------------------------------------------------
    print("\n==== DB persistence ====")
    async with session_maker() as session:
        customers = (await session.execute(select(Customer))).scalars().all()
        orders = (await session.execute(select(Order))).scalars().all()
        subscriptions = (await session.execute(select(Subscription))).scalars().all()

    def _check(label: str, expected: int, got: int) -> bool:
        ok = expected <= got  # handlers may legitimately create multiples
        marker = "OK " if ok else "FAIL"
        print(f"  [{marker}] {label}: got {got}, expected ≥ {expected}")
        return ok

    # Expected: 2 customers (one Paddle, one LS), 2 orders, 1 paddle sub +
    # 1 LS sub (subscription_updated/cancelled events update the same row)
    if not _check("customers", 2, len(customers)):
        failures += 1
    if not _check("orders", 2, len(orders)):
        failures += 1
    if not _check("subscriptions", 2, len(subscriptions)):
        failures += 1

    print("\n  Customer details:")
    for c in customers:
        print(f"    - id={c.id} provider={c.provider} email={c.email}")
    print("  Order details:")
    for o in orders:
        print(f"    - id={o.id} provider={o.provider} amount={o.amount} {o.currency} status={o.status}")
    print("  Subscription details:")
    for s in subscriptions:
        print(f"    - id={s.id} provider={s.provider} status={s.status} plan={s.plan_sku}")

    # Spot-check the LS sub ended as cancelled (we sent 3 events for the same id)
    ls_sub = next((s for s in subscriptions if s.provider == "lemon_squeezy"), None)
    if ls_sub is None:
        print("  [FAIL] LS subscription missing")
        failures += 1
    elif ls_sub.status != "canceled":
        print(f"  [FAIL] LS subscription final status = {ls_sub.status} (expected 'canceled')")
        failures += 1
    else:
        print("  [OK ] LS subscription final status = canceled (after 3 events)")

    paddle_sub = next((s for s in subscriptions if s.provider == "paddle"), None)
    if paddle_sub is None:
        print("  [FAIL] Paddle subscription missing")
        failures += 1
    elif paddle_sub.status != "canceled":
        print(f"  [FAIL] Paddle sub final status = {paddle_sub.status}")
        failures += 1
    else:
        print("  [OK ] Paddle subscription final status = canceled")

    await engine.dispose()

    print("")
    if failures:
        print(f"FAILED: {failures} check(s) did not pass.")
        return 1
    print("PASSED: Paddle + Lemon Squeezy webhook verification OK (offline).")
    return 0


# ---------------------------------------------------------------------------
# Live mode — POST against the running service
# ---------------------------------------------------------------------------


def run_live(base_url: str) -> int:
    """Post each event against the running service and print HTTP responses."""
    import httpx

    paddle_secret = os.environ.get("PADDLE_WEBHOOK_SECRET_LOCAL") or os.environ.get("PADDLE_WEBHOOK_SECRET")
    ls_secret = os.environ.get("LEMON_SQUEEZY_WEBHOOK_SECRET_LOCAL") or os.environ.get("LEMON_SQUEEZY_WEBHOOK_SECRET")

    if not paddle_secret or not ls_secret:
        print(
            "ERROR: live mode needs PADDLE_WEBHOOK_SECRET + "
            "LEMON_SQUEEZY_WEBHOOK_SECRET in env (or *_LOCAL overrides).\n"
            "       Those must match what the running service loaded."
        )
        return 2

    failures = 0

    with httpx.Client(timeout=10.0) as client:
        print("\n==== Paddle live verification ====")
        for evt in PADDLE_EVENTS:
            raw = json.dumps(evt).encode("utf-8")
            sig_header, _ = sign_paddle(raw, paddle_secret)
            try:
                resp = client.post(
                    f"{base_url}/api/webhook/paddle",
                    content=raw,
                    headers={
                        "Content-Type": "application/json",
                        "Paddle-Signature": sig_header,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] {evt['event_type']}: http error: {exc}")
                failures += 1
                continue
            ok = 200 <= resp.status_code < 300
            marker = "OK " if ok else "FAIL"
            print(f"  [{marker}] {evt['event_type']}: HTTP {resp.status_code} {resp.text[:200]}")
            if not ok:
                failures += 1

        print("\n==== Lemon Squeezy live verification ====")
        for evt in LEMON_SQUEEZY_EVENTS:
            raw = json.dumps(evt).encode("utf-8")
            sig_header = sign_lemon_squeezy(raw, ls_secret)
            try:
                resp = client.post(
                    f"{base_url}/api/webhook/lemon-squeezy",
                    content=raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-Signature": sig_header,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  [FAIL] {evt['meta']['event_name']}: http error: {exc}")
                failures += 1
                continue
            ok = 200 <= resp.status_code < 300
            marker = "OK " if ok else "FAIL"
            print(f"  [{marker}] {evt['meta']['event_name']}: HTTP {resp.status_code} {resp.text[:200]}")
            if not ok:
                failures += 1

    print("")
    if failures:
        print(f"FAILED: {failures} live request(s) did not return 2xx.")
        return 1
    print("PASSED: All live webhook POSTs returned 2xx.")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("offline", "live"), default="offline",
        help="offline (default) drives handlers directly; live POSTs to a running service",
    )
    parser.add_argument(
        "--base-url", default="http://127.0.0.1:9000",
        help="service base URL for --mode=live",
    )
    args = parser.parse_args()

    if args.mode == "offline":
        return asyncio.run(run_offline())
    return run_live(args.base_url)


if __name__ == "__main__":
    sys.exit(main())
