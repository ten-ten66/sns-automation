"""Tests for payment webhook receivers (Paddle + Lemon Squeezy).

Covers:
- HMAC signature verification (valid, invalid, malformed, stale)
- Event dispatch (customer.created, transaction.completed, subscription lifecycle)
- Idempotency (duplicate webhooks → no extra rows)
- Cross-provider isolation
"""

from __future__ import annotations

import hmac
import time
from datetime import datetime
from hashlib import sha256

import pytest
from sqlalchemy import select

from src.api.routes.webhooks import (
    _handle_lemon_squeezy_event,
    _handle_paddle_event,
    verify_lemon_squeezy_signature,
    verify_paddle_signature,
)
from src.core.models import Customer, Order, OrderStatus, Subscription, SubscriptionStatus


# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


class TestPaddleSignature:
    SECRET = "pdl_wh_test_secret"

    def _sign(self, body: bytes, ts: int | None = None) -> str:
        if ts is None:
            ts = int(time.time())
        signed = f"{ts}:".encode() + body
        h1 = hmac.new(self.SECRET.encode(), signed, sha256).hexdigest()
        return f"ts={ts};h1={h1}"

    def test_valid_signature(self):
        body = b'{"event_type":"transaction.completed","data":{}}'
        header = self._sign(body)
        assert verify_paddle_signature(body, header, self.SECRET) is True

    def test_invalid_hmac(self):
        body = b'{"event_type":"transaction.completed"}'
        header = self._sign(body)
        assert verify_paddle_signature(body, header, "wrong-secret") is False

    def test_body_tampered_after_signing(self):
        body = b'{"event_type":"transaction.completed"}'
        header = self._sign(body)
        tampered = body + b'{"extra":"bits"}'
        assert verify_paddle_signature(tampered, header, self.SECRET) is False

    def test_stale_timestamp_rejected(self):
        body = b'{"event_type":"transaction.completed"}'
        header = self._sign(body, ts=int(time.time()) - 10_000)  # ~3 hours old
        assert verify_paddle_signature(body, header, self.SECRET) is False

    def test_malformed_header(self):
        body = b'{}'
        assert verify_paddle_signature(body, "not-a-valid-header", self.SECRET) is False
        assert verify_paddle_signature(body, "", self.SECRET) is False
        assert verify_paddle_signature(body, "ts=abc;h1=xyz", self.SECRET) is False

    def test_missing_secret(self):
        body = b'{}'
        header = self._sign(body)
        assert verify_paddle_signature(body, header, "") is False


class TestLemonSqueezySignature:
    SECRET = "ls_wh_test_secret"

    def _sign(self, body: bytes) -> str:
        return hmac.new(self.SECRET.encode(), body, sha256).hexdigest()

    def test_valid_signature(self):
        body = b'{"meta":{"event_name":"order_created"},"data":{}}'
        assert verify_lemon_squeezy_signature(body, self._sign(body), self.SECRET) is True

    def test_invalid_signature(self):
        body = b'{"meta":{"event_name":"order_created"}}'
        assert verify_lemon_squeezy_signature(body, self._sign(body), "wrong") is False

    def test_body_tampered(self):
        body = b'{"meta":{"event_name":"order_created"}}'
        header = self._sign(body)
        assert verify_lemon_squeezy_signature(body + b"x", header, self.SECRET) is False

    def test_missing_secret(self):
        body = b'{}'
        assert verify_lemon_squeezy_signature(body, self._sign(body), "") is False


# ---------------------------------------------------------------------------
# Paddle event handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestPaddleEventHandling:
    async def test_customer_created_inserts_row(self, db_session):
        data = {
            "id": "ctm_01h7y",
            "email": "starlight@example.com",
            "name": "Starlight",
            "locale": "en-US",
        }
        result = await _handle_paddle_event(db_session, "customer.created", data)
        await db_session.commit()
        assert result["handled"] == "customer.created"

        row = (await db_session.execute(select(Customer))).scalar_one()
        assert row.email == "starlight@example.com"
        assert row.provider == "paddle"
        assert row.external_id == "ctm_01h7y"
        assert row.display_name == "Starlight"

    async def test_transaction_completed_creates_customer_and_order(self, db_session):
        data = {
            "id": "txn_01h8aaa",
            "status": "completed",
            "customer_id": "ctm_01h7y",
            "customer": {"email": "starlight@example.com"},
            "billed_at": "2026-04-11T10:20:30Z",
            "items": [
                {
                    "price": {
                        "id": "pri_tarot1",
                        "product_id": "pro_tarot",
                    }
                }
            ],
            "details": {"totals": {"grand_total": "999", "currency_code": "USD"}},
        }
        result = await _handle_paddle_event(db_session, "transaction.completed", data)
        await db_session.commit()
        assert result == {"handled": "transaction.completed", "inserted": True}

        orders = (await db_session.execute(select(Order))).scalars().all()
        assert len(orders) == 1
        o = orders[0]
        assert o.external_id == "txn_01h8aaa"
        assert o.provider == "paddle"
        assert float(o.amount) == 9.99
        assert o.currency == "USD"
        assert o.status == OrderStatus.PAID
        assert o.product_sku in ("pro_tarot", "pri_tarot1")
        assert o.paid_at == datetime(2026, 4, 11, 10, 20, 30)

    async def test_transaction_completed_is_idempotent(self, db_session):
        data = {
            "id": "txn_dupe",
            "status": "completed",
            "customer_id": "ctm_xxx",
            "customer": {"email": "dupe@example.com"},
            "items": [{"price": {"id": "pri_x", "product_id": "pro_x"}}],
            "details": {"totals": {"grand_total": "500", "currency_code": "USD"}},
        }
        r1 = await _handle_paddle_event(db_session, "transaction.completed", data)
        await db_session.commit()
        r2 = await _handle_paddle_event(db_session, "transaction.completed", data)
        await db_session.commit()
        assert r1["inserted"] is True
        assert r2["inserted"] is False
        orders = (await db_session.execute(select(Order))).scalars().all()
        assert len(orders) == 1

    async def test_subscription_created_inserts_sub(self, db_session):
        data = {
            "id": "sub_01h9bbb",
            "status": "active",
            "customer_id": "ctm_sub1",
            "customer": {"email": "sub@example.com"},
            "items": [
                {
                    "price": {
                        "id": "pri_monthly",
                        "product_id": "pro_premium",
                        "unit_price": {"amount": "1999", "currency_code": "USD"},
                        "billing_cycle": {"interval": "month", "frequency": 1},
                    }
                }
            ],
            "current_billing_period": {
                "starts_at": "2026-04-11T00:00:00Z",
                "ends_at": "2026-05-11T00:00:00Z",
            },
        }
        result = await _handle_paddle_event(db_session, "subscription.created", data)
        await db_session.commit()
        assert result["inserted"] is True

        subs = (await db_session.execute(select(Subscription))).scalars().all()
        assert len(subs) == 1
        s = subs[0]
        assert s.external_id == "sub_01h9bbb"
        assert s.status == SubscriptionStatus.ACTIVE
        assert float(s.amount) == 19.99
        assert s.interval == "month"
        assert s.current_period_end == datetime(2026, 5, 11, 0, 0, 0)

    async def test_subscription_canceled_updates_existing(self, db_session):
        # First create
        create_data = {
            "id": "sub_cancel_me",
            "status": "active",
            "customer_id": "ctm_c",
            "customer": {"email": "c@example.com"},
            "items": [{"price": {"id": "pri_m", "product_id": "pro_m"}}],
        }
        await _handle_paddle_event(db_session, "subscription.created", create_data)
        await db_session.commit()

        # Then cancel
        cancel_data = {**create_data, "status": "canceled", "canceled_at": "2026-04-15T00:00:00Z"}
        result = await _handle_paddle_event(db_session, "subscription.canceled", cancel_data)
        await db_session.commit()
        assert result["inserted"] is False

        subs = (await db_session.execute(select(Subscription))).scalars().all()
        assert len(subs) == 1
        assert subs[0].status == SubscriptionStatus.CANCELED
        assert subs[0].canceled_at == datetime(2026, 4, 15, 0, 0, 0)

    async def test_unknown_event_returns_reason(self, db_session):
        result = await _handle_paddle_event(db_session, "foo.bar", {})
        assert result["handled"] is False
        assert "unknown event" in result["reason"]


# ---------------------------------------------------------------------------
# Lemon Squeezy event handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLemonSqueezyEventHandling:
    async def test_order_created(self, db_session):
        payload = {
            "meta": {"event_name": "order_created"},
            "data": {
                "type": "orders",
                "id": "9999",
                "attributes": {
                    "store_id": 1,
                    "customer_id": 12345,
                    "user_email": "sparkle@example.com",
                    "user_name": "Sparkle",
                    "currency": "USD",
                    "total": 1299,
                    "status": "paid",
                    "created_at": "2026-04-11T11:00:00Z",
                    "identifier": "order-identifier-abc",
                    "order_number": 42,
                    "first_order_item": {"product_id": 777},
                },
            },
        }
        result = await _handle_lemon_squeezy_event(db_session, "order_created", payload)
        await db_session.commit()
        assert result["inserted"] is True

        cust = (await db_session.execute(select(Customer))).scalar_one()
        assert cust.email == "sparkle@example.com"
        assert cust.provider == "lemon_squeezy"
        assert cust.external_id == "12345"

        order = (await db_session.execute(select(Order))).scalar_one()
        assert float(order.amount) == 12.99
        assert order.status == OrderStatus.PAID
        assert order.provider == "lemon_squeezy"
        assert order.product_sku == "777"

    async def test_order_refunded_updates_order(self, db_session):
        # Seed an order
        await _handle_lemon_squeezy_event(
            db_session,
            "order_created",
            {
                "meta": {"event_name": "order_created"},
                "data": {
                    "id": "refund_target",
                    "attributes": {
                        "customer_id": 7,
                        "user_email": "r@example.com",
                        "currency": "USD",
                        "total": 500,
                        "status": "paid",
                        "created_at": "2026-04-10T00:00:00Z",
                        "first_order_item": {"product_id": 1},
                    },
                },
            },
        )
        await db_session.commit()

        await _handle_lemon_squeezy_event(
            db_session,
            "order_refunded",
            {
                "meta": {"event_name": "order_refunded"},
                "data": {
                    "id": "refund_target",
                    "attributes": {"refunded_at": "2026-04-11T09:00:00Z"},
                },
            },
        )
        await db_session.commit()

        order = (await db_session.execute(select(Order))).scalar_one()
        assert order.status == OrderStatus.REFUNDED
        assert order.refunded_at == datetime(2026, 4, 11, 9, 0, 0)

    async def test_subscription_lifecycle(self, db_session):
        base = {
            "meta": {"event_name": "subscription_created"},
            "data": {
                "id": "sub_ls_1",
                "attributes": {
                    "customer_id": 9,
                    "user_email": "s@example.com",
                    "user_name": "S",
                    "currency": "USD",
                    "total": 1999,
                    "variant_id": 555,
                    "product_id": 888,
                    "status": "active",
                    "created_at": "2026-04-11T00:00:00Z",
                    "renews_at": "2026-05-11T00:00:00Z",
                    "cancelled": False,
                },
            },
        }
        r1 = await _handle_lemon_squeezy_event(db_session, "subscription_created", base)
        await db_session.commit()
        assert r1["inserted"] is True

        # Update to cancelled
        base["meta"]["event_name"] = "subscription_cancelled"
        base["data"]["attributes"]["status"] = "cancelled"
        base["data"]["attributes"]["cancelled"] = True
        base["data"]["attributes"]["ends_at"] = "2026-04-20T00:00:00Z"
        r2 = await _handle_lemon_squeezy_event(db_session, "subscription_cancelled", base)
        await db_session.commit()
        assert r2["inserted"] is False

        sub = (await db_session.execute(select(Subscription))).scalar_one()
        assert sub.status == SubscriptionStatus.CANCELED
        assert sub.cancel_at_period_end is True
        assert sub.canceled_at == datetime(2026, 4, 20, 0, 0, 0)

    async def test_payment_event_logs_only(self, db_session):
        payload = {
            "meta": {"event_name": "subscription_payment_success"},
            "data": {"id": "inv_1", "attributes": {}},
        }
        result = await _handle_lemon_squeezy_event(
            db_session, "subscription_payment_success", payload
        )
        assert result["handled"] == "subscription_payment_success"
        assert result["note"] == "logged only"

    async def test_unknown_event_returns_reason(self, db_session):
        result = await _handle_lemon_squeezy_event(
            db_session, "frobnicated", {"meta": {"event_name": "frobnicated"}, "data": {}}
        )
        assert result["handled"] is False


# ---------------------------------------------------------------------------
# Cross-provider isolation: same external_id under different providers
# should produce independent rows (not a conflict)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_external_id_across_providers_are_independent(db_session):
    paddle_data = {
        "id": "abc123",  # same id
        "status": "completed",
        "customer_id": "p_cust",
        "customer": {"email": "shared@example.com"},
        "items": [{"price": {"id": "p", "product_id": "p"}}],
        "details": {"totals": {"grand_total": "100", "currency_code": "USD"}},
    }
    ls_payload = {
        "meta": {"event_name": "order_created"},
        "data": {
            "id": "abc123",  # same id, different provider
            "attributes": {
                "customer_id": 1,
                "user_email": "shared@example.com",
                "currency": "USD",
                "total": 200,
                "status": "paid",
                "created_at": "2026-04-11T00:00:00Z",
                "first_order_item": {"product_id": 1},
            },
        },
    }
    await _handle_paddle_event(db_session, "transaction.completed", paddle_data)
    await _handle_lemon_squeezy_event(db_session, "order_created", ls_payload)
    await db_session.commit()

    orders = (await db_session.execute(select(Order))).scalars().all()
    assert len(orders) == 2
    providers = {o.provider for o in orders}
    assert providers == {"paddle", "lemon_squeezy"}
