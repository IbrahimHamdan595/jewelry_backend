"""Pre-flight for audit B2: chain contiguity with N>1 record() calls per tx.

WHY THIS TEST EXISTS, AND WHY IT'S A PRE-FLIGHT
-----------------------------------------------
Audit B2's stock-take approval will, inside a single transaction, call
`record()` two or three times (MANUAL_ADJUSTMENT + STOCK_TAKE_LINE_APPROVED
+ optionally STOCK_TAKE_CLOSED). Each call does its own
`SELECT FOR UPDATE` on the chain-head row and advances it.

This pattern has NEVER been exercised before. Every prior caller in the
codebase does at most one chained write per transaction. If `record()`
has a subtle stale-head re-read bug — e.g. SQLAlchemy returning the
identity-mapped head entity from the first call instead of re-reading
its mutated state on the second — it would surface here, AS A CHAIN
BREAK, and we'd see it as either:
  • two siblings with the same prev_hash (chain fork), OR
  • the second one's prev_hash not matching the first's entry_hash, OR
  • the head row's latest_entry_hash drifting from the actual newest row.

Catching this in a focused 50-line test is cheaper than catching it
through the whole stock-take state machine. So this lands BEFORE any
B2 schema, refactor, or endpoint exists.

The test is intentionally minimal: it does not depend on stock-take
tables, on the adjustments router, on any new code. Just the existing
record() helper, called three times.
"""
from sqlalchemy import select

import pytest

from app.core.audit_chain import verify_chain
from app.core.ledger import record
from app.models import (
    InventoryLedger,
    InventoryLedgerChainHead,
    Role,
    User,
)


@pytest.mark.asyncio
async def test_three_chained_writes_in_one_transaction_remain_contiguous(db):
    """Call record() three times without an intervening commit. Then:

      1. Each row's prev_hash points to the previous row's entry_hash
         (i.e. they're contiguous in the chain).
      2. No two rows share an entry_hash.
      3. The chain-head's latest_entry_hash equals the THIRD row's
         entry_hash and row_count == 3.
      4. A full verify_chain walk reports `intact` over all three.

    Any stale-head re-read bug in record() — e.g. the second call
    reading the head's pre-mutation state from SQLAlchemy's identity
    map — would break at least one of these four assertions, with high
    fidelity to the actual failure mode.
    """
    user = User(
        id="u-1", email="u@u.u", name="u", password_hash="x",
        role=Role.ADMIN, is_active=True,
    )
    db.add(user)
    await db.flush()

    # Three record() calls. Note: NO db.commit() between them — they
    # must all share the same in-flight transaction, which is the
    # scenario stock-take approval will exercise.
    e1 = await record(
        db, event_type="LOT_CREATED", actor_user_id="u-1",
        ref_type="r", ref_id="r-1", payload={"i": 1},
    )
    e2 = await record(
        db, event_type="LOT_CREATED", actor_user_id="u-1",
        ref_type="r", ref_id="r-2", payload={"i": 2},
    )
    e3 = await record(
        db, event_type="LOT_CREATED", actor_user_id="u-1",
        ref_type="r", ref_id="r-3", payload={"i": 3},
    )

    # 1. Contiguous chain.
    assert e2.prev_hash == e1.entry_hash, (
        "Second write must chain to first. If they share a prev_hash, "
        "record() re-read a stale head from the identity map."
    )
    assert e3.prev_hash == e2.entry_hash, (
        "Third write must chain to second."
    )

    # 2. All three entry hashes are distinct.
    hashes = {e1.entry_hash, e2.entry_hash, e3.entry_hash}
    assert len(hashes) == 3

    # 3. Head reflects the third (latest) write.
    head = (
        await db.execute(
            select(InventoryLedgerChainHead).where(InventoryLedgerChainHead.id == 1)
        )
    ).scalar_one()
    assert head.latest_entry_hash == e3.entry_hash, (
        "Head pointer must advance with each record() call within a tx. "
        "If it equals e1 or e2, the head wasn't being re-fetched/locked "
        "correctly across calls."
    )
    assert head.row_count == 3

    # 4. Full chain walk is intact across all three.
    rows = (
        await db.execute(
            select(InventoryLedger).order_by(InventoryLedger.occurred_at, InventoryLedger.id)
        )
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
    assert result == {"status": "intact", "total_rows": 3, "first_break": None}


@pytest.mark.asyncio
async def test_five_chained_writes_in_one_transaction_all_distinct(db):
    """Stress the same pattern at N=5 to surface any off-by-one in the
    head-advance logic that a 3-row test might miss."""
    user = User(
        id="u-1", email="u@u.u", name="u", password_hash="x",
        role=Role.ADMIN, is_active=True,
    )
    db.add(user)
    await db.flush()

    events = []
    for i in range(5):
        events.append(await record(
            db, event_type="LOT_CREATED", actor_user_id="u-1",
            ref_type="r", ref_id=f"r-{i}", payload={"i": i},
        ))

    # Each event's prev_hash chains to the previous; entry hashes all
    # distinct; head matches the last.
    for i in range(1, 5):
        assert events[i].prev_hash == events[i - 1].entry_hash

    assert len({e.entry_hash for e in events}) == 5

    head = (
        await db.execute(
            select(InventoryLedgerChainHead).where(InventoryLedgerChainHead.id == 1)
        )
    ).scalar_one()
    assert head.latest_entry_hash == events[-1].entry_hash
    assert head.row_count == 5
