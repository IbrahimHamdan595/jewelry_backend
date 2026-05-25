"""audit A1.2: backfill ledger chain, tighten NOT NULL, UNIQUE on entry_hash

Revision ID: ad3defce8609
Revises: 28b387f25187
Create Date: 2026-05-25 19:30:00.000000

AUDIT RATIONALE
---------------
Phase A1.1 added nullable chain columns so the rollout could land without
breaking existing rows. This migration finishes the rollout:

  1. Rebuild the entire chain from GENESIS across every existing row in
     (occurred_at, id) order — including any row already partially chained
     during A1.1 (no special-casing; uniform recompute).
  2. Reset `inventory_ledger_chain_head` to the recomputed final entry_hash
     and the total row count, in the same transaction as the row rewrites.
     If anything fails mid-rebuild, the whole migration rolls back and the
     ledger stays in its prior (partially-chained) state.
  3. Tighten `prev_hash` and `entry_hash` to NOT NULL — guarantees every
     future read sees populated chain pointers.
  4. Replace the non-unique entry_hash index with a UNIQUE constraint.
     Two rows with the same entry_hash would only happen if (a) someone
     replays an existing event with the same prev_hash, or (b) a sha256
     collision. Both are catastrophic; the DB-level UNIQUE prevents both.

Perf note: the rebuild issues one UPDATE per row. For the current dev DB
size (low hundreds of rows) this is sub-second. For a much larger ledger,
the rebuild can be batched or executed offline with table locks; not
addressed here because the production ledger is still small.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# The pure helper is imported from the application code so the migration
# computes the EXACT same hash that runtime `record()` produces. Importing
# the app from a migration is unusual but safe here because the helper has
# no side effects, no DB access, no Pydantic models, no config loading.
from app.core.audit_chain import GENESIS_HASH, compute_ledger_entry_hash


# revision identifiers, used by Alembic.
revision: str = "ad3defce8609"
down_revision: Union[str, None] = "28b387f25187"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Fetch every existing row in canonical chain order. The same order
    #    record() writes in: occurred_at first (the timestamp the event was
    #    recorded at), id as the deterministic tiebreaker for any rows with
    #    identical occurred_at (e.g. two events recorded inside the same
    #    millisecond — possible at PG resolution).
    rows = conn.execute(
        sa.text(
            "SELECT id, event_type, actor_user_id, occurred_at, ref_type, ref_id, payload "
            "FROM inventory_ledger ORDER BY occurred_at, id"
        )
    ).fetchall()

    # 2. Compute the chain in Python — UNIFORM over every row, no
    #    skip-if-already-chained. Phase A1.1 wrote one or more rows
    #    chained to GENESIS; the backfill is the source of truth and
    #    rebuilds those rows' pointers along with everyone else's.
    prev = GENESIS_HASH
    updates: list[tuple[str, str, str]] = []  # (id, prev_hash, entry_hash)
    for row in rows:
        entry_hash = compute_ledger_entry_hash(
            prev_hash=prev,
            fields={
                "event_type": row.event_type,
                "actor_user_id": row.actor_user_id,
                "occurred_at": row.occurred_at,
                "ref_type": row.ref_type,
                "ref_id": row.ref_id,
                "payload": row.payload,
            },
        )
        updates.append((row.id, prev, entry_hash))
        prev = entry_hash

    # 3. Apply the updates row-by-row. Same transaction as everything below;
    #    a failure here rolls back the entire migration.
    for row_id, ph, eh in updates:
        conn.execute(
            sa.text(
                "UPDATE inventory_ledger SET prev_hash = :ph, entry_hash = :eh WHERE id = :id"
            ),
            {"ph": ph, "eh": eh, "id": row_id},
        )

    # 4. Reset the chain head to the final state. If the table was empty,
    #    leave the head pointing at GENESIS with row_count=0 (Phase A1.1's
    #    seeded state).
    final_hash = prev if rows else GENESIS_HASH
    conn.execute(
        sa.text(
            "UPDATE inventory_ledger_chain_head "
            "SET latest_entry_hash = :lh, row_count = :rc "
            "WHERE id = 1"
        ),
        {"lh": final_hash, "rc": len(rows)},
    )

    # 5. Tighten chain columns to NOT NULL. Any row that escaped the
    #    backfill (shouldn't be possible — the SELECT had no WHERE clause)
    #    would have caused this to fail; the surrounding transaction would
    #    then roll back.
    op.alter_column("inventory_ledger", "prev_hash", nullable=False)
    op.alter_column("inventory_ledger", "entry_hash", nullable=False)

    # 6. Drop the non-unique index from Phase A1.1 and replace with a UNIQUE
    #    constraint. PG implements the constraint with a unique index, so
    #    we don't end up with two indexes on the same column.
    op.drop_index("ix_inventory_ledger_entry_hash", table_name="inventory_ledger")
    op.create_unique_constraint(
        "uq_inventory_ledger_entry_hash",
        "inventory_ledger",
        ["entry_hash"],
    )


def downgrade() -> None:
    # Reverse the schema changes; leave the data populated (NULL-ing it back
    # out would discard the chain history and is a destructive op the
    # downgrade should not perform silently).
    op.drop_constraint(
        "uq_inventory_ledger_entry_hash",
        "inventory_ledger",
        type_="unique",
    )
    op.create_index(
        "ix_inventory_ledger_entry_hash",
        "inventory_ledger",
        ["entry_hash"],
        unique=False,
    )
    op.alter_column("inventory_ledger", "entry_hash", nullable=True)
    op.alter_column("inventory_ledger", "prev_hash", nullable=True)
