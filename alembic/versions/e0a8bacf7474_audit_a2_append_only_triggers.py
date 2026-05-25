"""audit A2: append-only triggers on inventory_ledger / chain_head / zakat_snapshots

Revision ID: e0a8bacf7474
Revises: ad3defce8609
Create Date: 2026-05-25 21:35:00.000000

AUDIT RATIONALE
---------------
A1 hash-chained the ledger so tampering is *detectable*. A2 makes it
*blocked at the database layer*. Postgres BEFORE-trigger functions raise
on UPDATE/DELETE against the audit tables. The block survives a
misconfigured app role, a runaway script that connects directly to the
DB, or a future code change that accidentally calls `db.delete()` on a
ledger row.

Scope of blocks
---------------
  * `inventory_ledger`: UPDATE and DELETE blocked (only INSERT permitted).
  * `inventory_ledger_chain_head`: DELETE blocked. UPDATE is permitted
    because the single head row is legitimately rewritten on every
    `record()` call (latest_entry_hash + row_count advance).
  * `zakat_snapshots`: UPDATE and DELETE blocked. Snapshots are
    immutable by spec.

Maintenance bypass
------------------
Triggers consult `current_setting('app.ledger_maintenance', true)`. When
that session-scoped flag reads 'on', mutations are allowed. Use the helper
`app.core.audit_maintenance.enable_audit_maintenance(connection)` from
inside an Alembic migration that legitimately needs to fix audit data —
the flag is `SET LOCAL` (transaction-scoped) so it cannot leak.

Honest limitations
------------------
  * A Postgres superuser (i.e. whoever holds the Neon admin role) can
    `DROP TRIGGER … ON inventory_ledger`, edit rows, and recreate the
    trigger. Custody of that credential is an out-of-band control,
    documented in AUDIT_READINESS.md (role-split follow-up).
  * The triggers only execute on Postgres. The SQLite test fixture relies
    on Python-layer enforcement (the application never UPDATEs/DELETEs
    these tables; tests that simulate raw-SQL tampering still run because
    SQLite doesn't have these triggers).

Dialect guard
-------------
The whole upgrade body is wrapped in a Postgres-only check. On any other
dialect (i.e. the SQLite test fixture) the migration is a no-op.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e0a8bacf7474"
down_revision: Union[str, None] = "ad3defce8609"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# All trigger functions consult the same session-scoped flag.
# current_setting(name, missing_ok) returns NULL when the flag was never
# set, which we treat as "block mutation".
_BLOCK_FUNCTION_BODY = """
DECLARE
    flag text := current_setting('app.ledger_maintenance', true);
BEGIN
    IF flag = 'on' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    RAISE EXCEPTION
      'append-only: % on % is not permitted (audit phase A2). '
      'Set app.ledger_maintenance = ''on'' inside a migration if this '
      'is a legitimate schema/data fix.',
      TG_OP, TG_TABLE_NAME
      USING ERRCODE = 'P0001';
END;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        # No-op on SQLite test fixture; triggers are PG-only.
        return

    # 1. Shared trigger function for blocking writes to audit tables.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION audit_block_mutation()
        RETURNS TRIGGER
        LANGUAGE plpgsql
        AS $$
        {_BLOCK_FUNCTION_BODY}
        $$;
        """
    )

    # 2. inventory_ledger: block UPDATE and DELETE.
    op.execute(
        """
        CREATE TRIGGER trg_inventory_ledger_block_update
            BEFORE UPDATE ON inventory_ledger
            FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_inventory_ledger_block_delete
            BEFORE DELETE ON inventory_ledger
            FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();
        """
    )

    # 3. inventory_ledger_chain_head: block DELETE only. UPDATE is the
    #    normal append path (every record() call advances the head).
    op.execute(
        """
        CREATE TRIGGER trg_inventory_ledger_chain_head_block_delete
            BEFORE DELETE ON inventory_ledger_chain_head
            FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();
        """
    )

    # 4. zakat_snapshots: block UPDATE and DELETE — same posture as ledger.
    op.execute(
        """
        CREATE TRIGGER trg_zakat_snapshots_block_update
            BEFORE UPDATE ON zakat_snapshots
            FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_zakat_snapshots_block_delete
            BEFORE DELETE ON zakat_snapshots
            FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("DROP TRIGGER IF EXISTS trg_zakat_snapshots_block_delete ON zakat_snapshots;")
    op.execute("DROP TRIGGER IF EXISTS trg_zakat_snapshots_block_update ON zakat_snapshots;")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_inventory_ledger_chain_head_block_delete "
        "ON inventory_ledger_chain_head;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_inventory_ledger_block_delete ON inventory_ledger;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_inventory_ledger_block_update ON inventory_ledger;"
    )
    op.execute("DROP FUNCTION IF EXISTS audit_block_mutation();")
