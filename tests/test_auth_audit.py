"""Tests for the auth-audit chain and recorder.

Covers three things that are different from the inventory ledger and
therefore explicitly worth pinning down:

  1. The auth-chain hash treats its OWN field set (`_AUTH_CHAINED_FIELDS`)
     correctly: deterministic, tamper-detected at the exact row, NULL
     fields permitted (user_id, claimed_email, detail are all nullable).
  2. `record_auth_event_safe()` is genuinely best-effort: a forced
     exception inside the recorder does NOT propagate. The auth response
     would already be sent in production.
  3. `get_client_ip()` prefers the leftmost X-Forwarded-For entry over
     `request.client.host` (Render proxy scenario).
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from starlette.requests import Request

from app.core.audit_chain import (
    GENESIS_HASH,
    compute_auth_entry_hash,
    verify_auth_chain,
)
from app.core.auth_audit import (
    EVENT_LOGIN_FAILED,
    EVENT_LOGIN_SUCCESS,
    fire_auth_event,
    get_client_ip,
    record_auth_event_safe,
)


# ── Pure helpers ──────────────────────────────────────────────────────────────

def _sample_fields(**overrides):
    base = {
        "event_type": "LOGIN_SUCCESS",
        "occurred_at": datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
        "user_id": "u-1",
        "claimed_email": "owner@example.com",
        "client_ip": "203.0.113.5",
        "user_agent": "Mozilla/5.0 …",
        "detail": None,
    }
    base.update(overrides)
    return base


def test_auth_hash_is_deterministic():
    a = compute_auth_entry_hash(prev_hash=GENESIS_HASH, fields=_sample_fields())
    b = compute_auth_entry_hash(prev_hash=GENESIS_HASH, fields=_sample_fields())
    assert a == b


def test_auth_hash_permits_null_user_id_and_email():
    """Failed-login probes carry no user_id and may carry a garbage email.
    The hash must tolerate None in those slots."""
    fields = _sample_fields(event_type=EVENT_LOGIN_FAILED, user_id=None, claimed_email=None)
    h = compute_auth_entry_hash(prev_hash=GENESIS_HASH, fields=fields)
    assert len(h) == 64


def test_auth_hash_changes_with_event_type():
    base = compute_auth_entry_hash(prev_hash=GENESIS_HASH, fields=_sample_fields())
    f2 = _sample_fields(event_type=EVENT_LOGIN_FAILED)
    assert compute_auth_entry_hash(prev_hash=GENESIS_HASH, fields=f2) != base


def test_auth_hash_changes_with_client_ip():
    base = compute_auth_entry_hash(prev_hash=GENESIS_HASH, fields=_sample_fields())
    f2 = _sample_fields(client_ip="198.51.100.99")
    assert compute_auth_entry_hash(prev_hash=GENESIS_HASH, fields=f2) != base


def _build_chain(fields_list):
    prev = GENESIS_HASH
    rows = []
    for i, f in enumerate(fields_list):
        eh = compute_auth_entry_hash(prev_hash=prev, fields=f)
        rows.append({"id": f"row-{i}", "prev_hash": prev, "entry_hash": eh, **f})
        prev = eh
    return rows


def test_verify_auth_chain_intact():
    rows = _build_chain([_sample_fields() for _ in range(3)])
    assert verify_auth_chain(rows) == {
        "status": "intact", "total_rows": 3, "first_break": None,
    }


def test_verify_auth_chain_detects_tampering_at_exact_row():
    rows = _build_chain([_sample_fields(claimed_email=f"u{i}@e") for i in range(4)])
    rows[2]["claimed_email"] = "attacker@e"
    result = verify_auth_chain(rows)
    assert result["status"] == "broken"
    assert result["first_break"]["id"] == "row-2"


# ── get_client_ip ─────────────────────────────────────────────────────────────

def _request_with_headers(headers: dict, peer: tuple[str, int] | None = ("127.0.0.1", 0)) -> Request:
    """Build a minimal Starlette Request for unit testing IP extraction."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw_headers,
        "query_string": b"",
        "client": peer,
    }
    return Request(scope)


def test_get_client_ip_prefers_xff_leftmost():
    """Render scenario: the original client is the leftmost XFF entry; the
    rest are proxy hops; request.client.host is the proxy itself."""
    req = _request_with_headers(
        {"x-forwarded-for": "203.0.113.5, 10.0.0.1, 10.0.0.2"},
        peer=("10.0.0.2", 0),
    )
    assert get_client_ip(req) == "203.0.113.5"


def test_get_client_ip_strips_whitespace():
    req = _request_with_headers(
        {"x-forwarded-for": "  203.0.113.5  , 10.0.0.1"},
        peer=("10.0.0.2", 0),
    )
    assert get_client_ip(req) == "203.0.113.5"


def test_get_client_ip_falls_back_to_peer_when_no_xff():
    """Local dev: no proxy, no XFF, peer is the real client."""
    req = _request_with_headers({}, peer=("127.0.0.1", 0))
    assert get_client_ip(req) == "127.0.0.1"


def test_get_client_ip_returns_none_when_no_peer_and_no_xff():
    req = _request_with_headers({}, peer=None)
    assert get_client_ip(req) is None


def test_get_client_ip_ignores_empty_xff():
    """An XFF header set to empty (some load balancers do this) should
    fall through to peer address."""
    req = _request_with_headers({"x-forwarded-for": ""}, peer=("10.0.0.5", 0))
    assert get_client_ip(req) == "10.0.0.5"


# ── Best-effort behavior ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_auth_event_safe_never_raises_on_db_error():
    """Critical contract: if the recorder fails, the auth path must NOT
    see the exception. A user must be able to log in even if the audit
    table is unreachable."""
    # Force a failure inside the recorder by patching async_session_factory
    # to raise the moment it's invoked.
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated DB outage")

    with patch("app.core.auth_audit.async_session_factory", _boom):
        # If best-effort works, this returns None without propagating.
        result = await record_auth_event_safe(
            event_type=EVENT_LOGIN_SUCCESS,
            user_id="u-1",
            claimed_email="owner@example.com",
            client_ip="203.0.113.5",
            user_agent="curl/8.0",
        )
    assert result is None


@pytest.mark.asyncio
async def test_fire_auth_event_schedules_task_and_completes_after_caller_raises():
    """Regression for the FastAPI BackgroundTasks gotcha:

    BackgroundTasks attached as an endpoint parameter only fire when the
    endpoint returns normally. Raising HTTPException bypasses them, which
    silently drops the most important audit events (failed logins).

    `fire_auth_event` uses asyncio.create_task so the recorder runs
    regardless of whether the caller returns or raises. This test
    simulates the raise-after-fire pattern from the login handler.
    """
    captured_calls = []

    async def fake_recorder(**kwargs):
        captured_calls.append(kwargs)

    with patch("app.core.auth_audit.record_auth_event_safe", fake_recorder):
        async def caller_that_raises():
            fire_auth_event(
                event_type=EVENT_LOGIN_FAILED,
                user_id=None,
                claimed_email="probe@example.com",
                client_ip="203.0.113.5",
                user_agent="curl/8.0",
                detail="invalid credentials",
            )
            raise RuntimeError("simulated HTTPException")

        with pytest.raises(RuntimeError):
            await caller_that_raises()

        # Give the event loop a tick so the scheduled task can run.
        await asyncio.sleep(0)

    assert len(captured_calls) == 1
    assert captured_calls[0]["event_type"] == EVENT_LOGIN_FAILED
    assert captured_calls[0]["claimed_email"] == "probe@example.com"


@pytest.mark.asyncio
async def test_record_auth_event_safe_never_raises_on_chain_compute_error():
    """A second class of failure: the hash compute itself raises. Still
    must not propagate."""
    with patch(
        "app.core.auth_audit.compute_auth_entry_hash",
        side_effect=ValueError("simulated chain error"),
    ):
        result = await record_auth_event_safe(
            event_type=EVENT_LOGIN_FAILED,
            user_id=None,
            claimed_email="probe@example.com",
            client_ip="203.0.113.99",
            user_agent="python-requests/2",
            detail="invalid credentials",
        )
    assert result is None
