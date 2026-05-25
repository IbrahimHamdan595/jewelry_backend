"""audit A3b: auth_audit_log + chain head + append-only triggers

Revision ID: b66a1a3f957f
Revises: e0a8bacf7474
Create Date: 2026-05-25 22:50:00.000000

AUDIT RATIONALE
---------------
Closes the most basic audit gap: "who logged in when, from where?". Logins
(success and failure), logouts, and password changes write to a dedicated
`auth_audit_log` table with its own SHA-256 hash chain — independent of the
inventory ledger so high-volume failed-login probes don't contend with
inventory writes or noise up financial reconciliation queries.

Schema notes
------------
  • `user_id` is intentionally NOT a foreign key — failed logins may carry
    a `claimed_email` that doesn't match any user (or is a deliberate
    attack probe). The column captures what was submitted, not a verified
    identity.
  • `retention_until_at` is set client-side on insert to
    `now() + auth_audit_retention_days` (default 540 days = 18 months).
    Indexed so a future pruner can `WHERE retention_until_at < now()`
    efficiently. NO pruner is built in this phase — actually deleting
    audit rows is a deliberate, A2-bypass operation handled separately.
  • UPDATE+DELETE triggers mirror A2: blocked unless the session sets
    `app.ledger_maintenance = 'on'`. UPDATE on the chain-head row is
    permitted (every append advances it).

Dialect guard: triggers are Postgres-only; the migration is a no-op on
the SQLite test fixture.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b66a1a3f957f"
down_revision: Union[str, None] = "e0a8bacf7474"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. auth_audit_log
    op.create_table(
        "auth_audit_log",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("claimed_email", sa.String(), nullable=True),
        sa.Column("client_ip", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("detail", sa.String(), nullable=True),
        sa.Column("retention_until_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prev_hash", sa.String(), nullable=False),
        sa.Column("entry_hash", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("entry_hash", name="uq_auth_audit_log_entry_hash"),
    )
    op.create_index("ix_auth_audit_log_event_type", "auth_audit_log", ["event_type"])
    op.create_index("ix_auth_audit_log_user_id", "auth_audit_log", ["user_id"])
    op.create_index("ix_auth_audit_log_claimed_email", "auth_audit_log", ["claimed_email"])
    op.create_index("ix_auth_audit_log_occurred_at", "auth_audit_log", ["occurred_at"])
    op.create_index(
        "ix_auth_audit_log_retention_until_at",
        "auth_audit_log",
        ["retention_until_at"],
    )

    # 2. auth_audit_chain_head — seeded to GENESIS so the first
    #    record_auth_event_safe call finds a row to lock.
    op.create_table(
        "auth_audit_chain_head",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("latest_entry_hash", sa.String(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO auth_audit_chain_head (id, latest_entry_hash, row_count) "
        "VALUES (1, 'GENESIS', 0)"
    )

    # 3. Append-only triggers (PG only). Reuses the audit_block_mutation()
    #    function created by audit A2 — same maintenance-flag bypass applies.
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE TRIGGER trg_auth_audit_log_block_update
            BEFORE UPDATE ON auth_audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_auth_audit_log_block_delete
            BEFORE DELETE ON auth_audit_log
            FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_auth_audit_chain_head_block_delete
            BEFORE DELETE ON auth_audit_chain_head
            FOR EACH ROW EXECUTE FUNCTION audit_block_mutation();
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_auth_audit_chain_head_block_delete ON auth_audit_chain_head;")
        op.execute("DROP TRIGGER IF EXISTS trg_auth_audit_log_block_delete ON auth_audit_log;")
        op.execute("DROP TRIGGER IF EXISTS trg_auth_audit_log_block_update ON auth_audit_log;")

    op.drop_table("auth_audit_chain_head")
    op.drop_index("ix_auth_audit_log_retention_until_at", table_name="auth_audit_log")
    op.drop_index("ix_auth_audit_log_occurred_at", table_name="auth_audit_log")
    op.drop_index("ix_auth_audit_log_claimed_email", table_name="auth_audit_log")
    op.drop_index("ix_auth_audit_log_user_id", table_name="auth_audit_log")
    op.drop_index("ix_auth_audit_log_event_type", table_name="auth_audit_log")
    op.drop_table("auth_audit_log")
