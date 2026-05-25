"""Tests for the pure ledger hash-chain helpers.

Covers `compute_ledger_entry_hash` and `verify_chain` from
`app/core/audit_chain.py`. No DB — these are pure functions and the unit
suite is where the cryptographic semantics get pinned down.

Wiring into the actual `record()` function and end-to-end DB integration
are exercised separately in the integration test (`test_audit_chain_db.py`,
added in A1.1.4).
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.audit_chain import (
    GENESIS_HASH,
    compute_ledger_entry_hash,
    verify_chain,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sample_fields(payload: dict | None = None, *, event_type: str = "LOT_CREATED") -> dict:
    return {
        "event_type": event_type,
        "actor_user_id": "admin-1",
        "occurred_at": datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc),
        "ref_type": "gold_lot",
        "ref_id": "lot-abc",
        "payload": payload or {"karat": "K22", "weight_grams": "50.000"},
    }


# ── compute_ledger_entry_hash — basics ────────────────────────────────────────

def test_hash_is_deterministic_for_same_inputs():
    a = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=_sample_fields())
    b = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=_sample_fields())
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_hash_changes_when_prev_hash_changes():
    """Critical property: chaining works because changing prev_hash changes
    entry_hash. Without this, an attacker could splice one chain into another."""
    f = _sample_fields()
    h1 = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=f)
    h2 = compute_ledger_entry_hash(prev_hash="some-other-hash", fields=f)
    assert h1 != h2


def test_hash_changes_when_any_chained_field_changes():
    base = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=_sample_fields())

    cases = [
        ("event_type", "MELT"),
        ("actor_user_id", "admin-2"),
        ("occurred_at", datetime(2026, 5, 25, 12, 1, tzinfo=timezone.utc)),
        ("ref_type", "product"),
        ("ref_id", "lot-xyz"),
    ]
    for field, new_value in cases:
        fields = _sample_fields()
        fields[field] = new_value
        h = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=fields)
        assert h != base, f"Hash didn't change when {field!r} did"


def test_hash_changes_when_payload_changes():
    base = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=_sample_fields())

    # Add a key
    f1 = _sample_fields(payload={"karat": "K22", "weight_grams": "50.000", "note": "x"})
    assert compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=f1) != base

    # Mutate a value
    f2 = _sample_fields(payload={"karat": "K22", "weight_grams": "50.001"})
    assert compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=f2) != base

    # Drop a key
    f3 = _sample_fields(payload={"karat": "K22"})
    assert compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=f3) != base


def test_hash_ignores_dict_key_order_in_payload():
    """The payload is canonicalized (sorted keys at every level), so the
    same logical payload hashes identically regardless of insertion order."""
    base = compute_ledger_entry_hash(
        prev_hash=GENESIS_HASH,
        fields=_sample_fields(payload={"karat": "K22", "weight_grams": "50.000"}),
    )
    reordered = compute_ledger_entry_hash(
        prev_hash=GENESIS_HASH,
        fields=_sample_fields(payload={"weight_grams": "50.000", "karat": "K22"}),
    )
    assert base == reordered


def test_hash_normalizes_naive_and_utc_datetimes_identically():
    """Critical for DB portability: SQLite strips timezone on round-trip;
    PG keeps it. The hash must treat naive UTC and aware UTC as the same
    instant, or the chain would break on read against SQLite.
    """
    aware = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 5, 25, 12, 0)

    f_aware = _sample_fields()
    f_aware["occurred_at"] = aware
    f_naive = _sample_fields()
    f_naive["occurred_at"] = naive

    h_aware = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=f_aware)
    h_naive = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=f_naive)
    assert h_aware == h_naive


def test_hash_normalizes_other_timezones_to_utc():
    """A tz-aware datetime in another zone normalizes to UTC before hashing."""
    from datetime import timedelta
    plus_three = datetime(2026, 5, 25, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    utc = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)  # same instant

    f1 = _sample_fields()
    f1["occurred_at"] = plus_three
    f2 = _sample_fields()
    f2["occurred_at"] = utc

    h1 = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=f1)
    h2 = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=f2)
    assert h1 == h2


def test_hash_handles_decimal_payload():
    """Decimals should serialize to their str() form, preserving any trailing
    zeros set by quantize()."""
    fields = _sample_fields(payload={"amount": Decimal("100.50")})
    # Sanity: should not raise. The Decimal must survive serialization.
    h = compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=fields)
    assert len(h) == 64


def test_missing_chained_field_raises():
    """Silent fall-through would let an attacker construct a partial event."""
    fields = _sample_fields()
    del fields["payload"]
    with pytest.raises(KeyError):
        compute_ledger_entry_hash(prev_hash=GENESIS_HASH, fields=fields)


def test_empty_prev_hash_rejected():
    """The genesis case uses GENESIS_HASH sentinel, NOT empty string."""
    with pytest.raises(ValueError):
        compute_ledger_entry_hash(prev_hash="", fields=_sample_fields())
    with pytest.raises(ValueError):
        compute_ledger_entry_hash(prev_hash=None, fields=_sample_fields())  # type: ignore[arg-type]


# ── verify_chain — empty / intact / broken ────────────────────────────────────

def _build_chain(fields_list: list[dict]) -> list[dict]:
    """Helper: given a list of field-dicts, produce a list of row-dicts in
    chain order with correct prev_hash and entry_hash."""
    prev = GENESIS_HASH
    rows = []
    for i, f in enumerate(fields_list):
        eh = compute_ledger_entry_hash(prev_hash=prev, fields=f)
        rows.append({"id": f"row-{i}", "prev_hash": prev, "entry_hash": eh, **f})
        prev = eh
    return rows


def test_verify_empty_chain():
    result = verify_chain([])
    assert result["status"] == "empty"
    assert result["total_rows"] == 0
    assert result["first_break"] is None


def test_verify_intact_chain():
    rows = _build_chain([_sample_fields() for _ in range(5)])
    result = verify_chain(rows)
    assert result["status"] == "intact"
    assert result["total_rows"] == 5
    assert result["first_break"] is None


def test_verify_detects_payload_tampering_at_exact_row():
    rows = _build_chain([_sample_fields(payload={"i": i}) for i in range(5)])

    # Tamper with row 2's payload — entry_hash will no longer match a
    # recompute of (prev_hash + tampered_fields).
    rows[2]["payload"] = {"i": 999}

    result = verify_chain(rows)
    assert result["status"] == "broken"
    assert result["total_rows"] == 3  # stopped at row index 2 → counted 3
    assert result["first_break"]["id"] == "row-2"
    # The stored entry_hash is the pre-tamper one; the recompute differs.
    assert result["first_break"]["actual_entry_hash"] != result["first_break"]["expected_entry_hash"]


def test_verify_detects_deleted_row():
    """Simulate someone deleting row 2: the chain still contains row 3
    expecting row 2's entry_hash as its prev_hash, but the verifier walks
    in order and finds row 3 reporting a prev_hash that doesn't match what
    row 1 produced. Break detected at row 3 (the first row whose prev_hash
    no longer matches the walking expectation)."""
    rows = _build_chain([_sample_fields(payload={"i": i}) for i in range(5)])

    deleted = [rows[0], rows[1], rows[3], rows[4]]  # row 2 missing

    result = verify_chain(deleted)
    assert result["status"] == "broken"
    # Walked rows 0,1 OK; row 2 in iteration is original row 3 — break here.
    assert result["first_break"]["id"] == "row-3"
    assert result["first_break"]["actual_prev_hash"] != result["first_break"]["expected_prev_hash"]


def test_verify_detects_entry_hash_only_tampering():
    """If an attacker edits entry_hash directly (without changing fields),
    the recompute won't match the stored entry_hash."""
    rows = _build_chain([_sample_fields(payload={"i": i}) for i in range(3)])
    rows[1]["entry_hash"] = "deadbeef" * 8  # 64 hex chars but wrong

    result = verify_chain(rows)
    assert result["status"] == "broken"
    assert result["first_break"]["id"] == "row-1"


def test_verify_detects_prev_hash_only_tampering():
    """Editing prev_hash to bypass a deletion is detected too: the row's own
    entry_hash was computed against the original prev_hash, so changing
    prev_hash invalidates entry_hash by virtue of the recompute mismatch."""
    rows = _build_chain([_sample_fields(payload={"i": i}) for i in range(3)])
    rows[2]["prev_hash"] = GENESIS_HASH  # pretend row 2 is the first row

    result = verify_chain(rows)
    assert result["status"] == "broken"
    assert result["first_break"]["id"] == "row-2"


def test_verify_first_row_must_chain_to_genesis():
    """If a fabricated 'first row' claims a prev_hash other than GENESIS_HASH,
    the verifier flags it immediately."""
    fields = _sample_fields()
    rogue_prev = "some-attacker-supplied-hash"
    eh = compute_ledger_entry_hash(prev_hash=rogue_prev, fields=fields)
    rogue_rows = [{"id": "row-0", "prev_hash": rogue_prev, "entry_hash": eh, **fields}]

    result = verify_chain(rogue_rows)
    assert result["status"] == "broken"
    assert result["first_break"]["id"] == "row-0"
    assert result["first_break"]["expected_prev_hash"] == GENESIS_HASH
