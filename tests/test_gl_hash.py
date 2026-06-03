from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.core.audit_chain import compute_gl_entry_hash, verify_gl_chain, GENESIS_HASH


def _header(**over):
    h = {
        "entry_no": "JE-20260603-001",
        "entry_date": date(2026, 6, 3),
        "memo": "test",
        "source_type": "MANUAL",
        "source_id": None,
        "reverses_entry_id": None,
        "actor_user_id": "u1",
        "occurred_at": datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc),
    }
    h.update(over)
    return h


def _line(**over):
    ln = {
        "account_id": "a1", "money_debit": Decimal("100.00"), "money_credit": Decimal("0"),
        "currency": "USD", "fx_rate": Decimal("1"), "base_debit": Decimal("100.00"),
        "base_credit": Decimal("0"), "metal_debit_grams": Decimal("0"),
        "metal_credit_grams": Decimal("0"), "karat": None, "memo": "",
    }
    ln.update(over)
    return ln


def test_hash_is_deterministic():
    h1 = compute_gl_entry_hash(prev_hash=GENESIS_HASH, header=_header(), lines=[_line()])
    h2 = compute_gl_entry_hash(prev_hash=GENESIS_HASH, header=_header(), lines=[_line()])
    assert h1 == h2 and len(h1) == 64


def test_hash_invariant_to_header_key_order():
    forward = _header()
    reordered = {k: forward[k] for k in reversed(list(forward.keys()))}
    assert compute_gl_entry_hash(prev_hash=GENESIS_HASH, header=forward, lines=[_line()]) == \
           compute_gl_entry_hash(prev_hash=GENESIS_HASH, header=reordered, lines=[_line()])


def test_hash_detects_amount_tamper():
    clean = compute_gl_entry_hash(prev_hash=GENESIS_HASH, header=_header(), lines=[_line()])
    tampered = compute_gl_entry_hash(
        prev_hash=GENESIS_HASH, header=_header(), lines=[_line(base_debit=Decimal("999.00"))]
    )
    assert clean != tampered


def test_hash_depends_on_prev():
    a = compute_gl_entry_hash(prev_hash=GENESIS_HASH, header=_header(), lines=[_line()])
    b = compute_gl_entry_hash(prev_hash="deadbeef", header=_header(), lines=[_line()])
    assert a != b


def test_verify_gl_chain_intact_and_broken():
    h1 = compute_gl_entry_hash(prev_hash=GENESIS_HASH, header=_header(entry_no="JE-1"), lines=[_line()])
    row1 = {"id": "1", "prev_hash": GENESIS_HASH, "entry_hash": h1, **_header(entry_no="JE-1"), "lines": [_line()]}
    h2 = compute_gl_entry_hash(prev_hash=h1, header=_header(entry_no="JE-2"), lines=[_line()])
    row2 = {"id": "2", "prev_hash": h1, "entry_hash": h2, **_header(entry_no="JE-2"), "lines": [_line()]}
    assert verify_gl_chain([row1, row2])["status"] == "intact"

    row2_bad = dict(row2, lines=[_line(base_debit=Decimal("1.00"))])
    res = verify_gl_chain([row1, row2_bad])
    assert res["status"] == "broken" and res["first_break"]["id"] == "2"
