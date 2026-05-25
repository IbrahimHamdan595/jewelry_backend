"""Maintenance bypass for the append-only audit triggers (audit phase A2).

AUDIT RATIONALE
---------------
A2 installs Postgres BEFORE-UPDATE/DELETE triggers on the audit tables
(`inventory_ledger`, `inventory_ledger_chain_head`, `zakat_snapshots`) that
raise on any mutation other than INSERT. This blocks the application — and
anyone running ad-hoc SQL — from silently editing audit history.

But legitimate maintenance still needs to happen:
  * the A1.2 backfill UPDATEd every row to populate the chain (already ran
    before A2 landed, but a future migration could need to do the same);
  * a recovery scenario might need to delete a corrupted row.

The bypass uses Postgres `SET LOCAL`, which is transaction-scoped. The
trigger checks `current_setting('app.ledger_maintenance', true)`; if it
reads `'on'`, the mutation is allowed. `SET LOCAL` is wiped at COMMIT or
ROLLBACK, so a forgotten flag cannot leak into ordinary application code
running in a subsequent transaction.

USAGE
-----
Inside an Alembic migration that legitimately needs to UPDATE/DELETE rows
in an audit table:

    from app.core.audit_maintenance import enable_audit_maintenance

    def upgrade():
        if op.get_bind().dialect.name == "postgresql":
            enable_audit_maintenance(op.get_bind())
        # ...do the UPDATEs...

Any mutation attempt OUTSIDE such an explicit opt-in will raise:
    P0001  append-only: UPDATE on inventory_ledger is not permitted
           (audit phase A2). Set app.ledger_maintenance = 'on' inside a
           migration if this is a legitimate schema/data fix.

This is by design. If you see that error in production logs, treat it as a
bug — application code must never write to the audit tables outside the
sanctioned paths (`record()` for the ledger, `POST /zakat/snapshots` for
zakat).
"""
from sqlalchemy import text


def enable_audit_maintenance(connection) -> None:
    """Allow UPDATE/DELETE on audit tables for the current transaction only.

    Scoped to the current transaction via Postgres `SET LOCAL`. The flag is
    automatically cleared at COMMIT or ROLLBACK; no need (and no way) to
    "turn it off" inside the same transaction.

    Call once at the top of a migration that touches audit tables.
    No-op on non-Postgres dialects (the triggers don't exist there).
    """
    if connection.dialect.name != "postgresql":
        return
    connection.execute(text("SET LOCAL app.ledger_maintenance = 'on'"))
