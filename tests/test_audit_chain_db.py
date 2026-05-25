"""End-to-end test that `record()` produces a verifiable hash chain.

Pure-function semantics are covered in test_audit_chain.py. This test
proves that the chain survives the trip through SQLAlchemy + the DB:
  • first row chains to GENESIS
  • each subsequent row chains to the previous row's entry_hash
  • the chain-head row advances correctly and stays consistent
  • verify_chain accepts a real DB-fetched sequence
"""
from sqlalchemy import select

import pytest

from app.core.audit_chain import GENESIS_HASH, verify_chain
from app.core.ledger import record
from app.models import (
    InventoryLedger,
    InventoryLedgerChainHead,
    Role,
    User,
)


@pytest.mark.asyncio
async def test_record_chains_three_writes_in_order(db):
    """Write three events; assert prev_hash/entry_hash links, head pointer,
    and verify_chain status across the full DB-fetched sequence."""
    # Seed a user — the ledger FKs to users.
    user = User(
        id="u1", email="u@u.u", name="u", password_hash="x",
        role=Role.ADMIN, is_active=True,
    )
    db.add(user)
    await db.flush()

    # Write three events.
    e1 = await record(
        db, event_type="LOT_CREATED", actor_user_id="u1",
        ref_type="gold_lot", ref_id="lot-1",
        payload={"karat": "K22", "weight_grams": "50.000"},
    )
    e2 = await record(
        db, event_type="LOT_CONSUMED", actor_user_id="u1",
        ref_type="gold_lot", ref_id="lot-1",
        payload={"grams": "10.000"},
    )
    e3 = await record(
        db, event_type="MANUAL_ADJUSTMENT", actor_user_id="u1",
        ref_type="gold_lot", ref_id="lot-1",
        payload={"delta": "-1.000", "reason": "LOSS"},
    )

    # Each row's prev_hash points to the previous row's entry_hash.
    assert e1.prev_hash == GENESIS_HASH
    assert e1.entry_hash is not None and len(e1.entry_hash) == 64
    assert e2.prev_hash == e1.entry_hash
    assert e3.prev_hash == e2.entry_hash
    # No two events share an entry_hash.
    assert len({e1.entry_hash, e2.entry_hash, e3.entry_hash}) == 3

    # Chain head reflects the last write.
    head = (
        await db.execute(select(InventoryLedgerChainHead).where(InventoryLedgerChainHead.id == 1))
    ).scalar_one()
    assert head.latest_entry_hash == e3.entry_hash
    assert head.row_count == 3

    # Re-fetch rows from the DB in chain order and run the verifier.
    rows = (
        await db.execute(select(InventoryLedger).order_by(InventoryLedger.occurred_at))
    ).scalars().all()

    row_dicts = [
        {
            "id": r.id,
            "prev_hash": r.prev_hash,
            "entry_hash": r.entry_hash,
            "event_type": r.event_type,
            "actor_user_id": r.actor_user_id,
            "occurred_at": r.occurred_at,
            "ref_type": r.ref_type,
            "ref_id": r.ref_id,
            "payload": r.payload,
        }
        for r in rows
    ]
    result = verify_chain(row_dicts)
    assert result == {"status": "intact", "total_rows": 3, "first_break": None}


@pytest.mark.asyncio
async def test_db_tamper_breaks_chain_at_exact_row(db):
    """Simulate a DBA editing a ledger row: the verifier detects the break at
    exactly that row, even though the chain looks consistent on either side
    of it in isolation."""
    user = User(
        id="u1", email="u@u.u", name="u", password_hash="x",
        role=Role.ADMIN, is_active=True,
    )
    db.add(user)
    await db.flush()

    events = []
    for i in range(5):
        e = await record(
            db, event_type="LOT_CREATED", actor_user_id="u1",
            ref_type="gold_lot", ref_id=f"lot-{i}",
            payload={"i": i},
        )
        events.append(e)

    # Tamper row index 2's payload via the ORM (simulates a malicious UPDATE).
    target = events[2]
    target.payload = {"i": 999}  # was {"i": 2}
    await db.flush()

    rows = (
        await db.execute(select(InventoryLedger).order_by(InventoryLedger.occurred_at))
    ).scalars().all()
    row_dicts = [
        {
            "id": r.id, "prev_hash": r.prev_hash, "entry_hash": r.entry_hash,
            "event_type": r.event_type, "actor_user_id": r.actor_user_id,
            "occurred_at": r.occurred_at, "ref_type": r.ref_type,
            "ref_id": r.ref_id, "payload": r.payload,
        }
        for r in rows
    ]
    result = verify_chain(row_dicts)

    assert result["status"] == "broken"
    assert result["first_break"]["id"] == target.id
    # The stored entry_hash (from the pre-tamper compute) no longer matches
    # the recompute over the tampered payload.
    assert (
        result["first_break"]["expected_entry_hash"]
        != result["first_break"]["actual_entry_hash"]
    )
