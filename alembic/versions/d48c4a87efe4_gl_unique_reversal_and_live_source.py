"""GL: unique indexes behind the reversal + auto-post guards (NEX-49)

Revision ID: d48c4a87efe4
Revises: e2b3c4d5f6a7
Create Date: 2026-10-04

Two partial unique indexes on gl_journal_entries, mirroring the model
(GLJournalEntry.__table_args__):

  * uq_gl_entries_reverses_entry_id
        (reverses_entry_id) WHERE reverses_entry_id IS NOT NULL
    An entry is reversed at most once. gl.reverse_entry checks first and
    answers 409; the index closes the window where two concurrent reversals
    both pass that check.

  * uq_gl_entries_live_source
        (source_type, source_id)
        WHERE reverses_entry_id IS NULL AND source_type IN (<auto-post sources>)
    gl_postings.find_live_entry checks for an existing auto-posted entry BEFORE
    the chain-head lock, so two simultaneous posts of one source could both
    pass it.

Why the second index is scoped to a list of source types
---------------------------------------------------------
(source_type, source_id) is unique among live entries only for the auto-post
sources that find_live_entry guards — each is posted once, for a freshly
created row (per-item refunds use "<order_item_id>:<cumulative_qty>"). Other
sources legitimately repeat and must never be refused:
  * MANUAL (and any caller-chosen source_type): source_id is a free-text
    reference an accountant may reuse.
  * AR_INVOICE / AR_RECEIPT / VENDOR_BILL / VENDOR_PAYMENT / OPENING /
    YEAR_CLOSE post with source_id NULL (never collides anyway).
The list is a frozen copy of app.models.GL_UNIQUE_LIVE_SOURCE_TYPES; adding a
source later needs a new migration.

Compatibility
-------------
Additive only (two indexes, no column or data change). Old code keeps working
against the new schema, and new code keeps working before this is applied —
the in-code checks do not depend on the indexes.

Existing data
-------------
Ledger rows are append-only and hash-chained, so a duplicate that already
exists cannot be deleted to make room for a unique index — and a migration
that died on it would block every later revision. So each index is created
only if the data already satisfies it; otherwise it is SKIPPED with a WARNING
naming the offending entries, and the upgrade still succeeds. A skipped index
means that backstop is absent (the in-code checks still apply). The listed
entries then need an accounting correction, and the index a follow-up
migration whose predicate exempts those rows.
"""
from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d48c4a87efe4"
down_revision: str = "e2b3c4d5f6a7"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_TABLE = "gl_journal_entries"
_UQ_REVERSAL = "uq_gl_entries_reverses_entry_id"
_UQ_LIVE_SOURCE = "uq_gl_entries_live_source"

_REVERSAL_WHERE = "reverses_entry_id IS NOT NULL"
_LIVE_SOURCE_WHERE = (
    "reverses_entry_id IS NULL AND source_type IN ("
    "'ORDER', 'ORDER_REFUND', 'SUPPLIER_PURCHASE', 'SUPPLIER_PAYMENT', "
    "'BUYBACK', 'MELT', 'ADJUSTMENT')"
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. An entry is reversed at most once.
    dupes = bind.execute(sa.text(
        "SELECT COALESCE(MAX(o.entry_no), r.reverses_entry_id), COUNT(*) "
        "FROM gl_journal_entries r "
        "LEFT JOIN gl_journal_entries o ON o.id = r.reverses_entry_id "
        "WHERE r.reverses_entry_id IS NOT NULL "
        "GROUP BY r.reverses_entry_id HAVING COUNT(*) > 1 ORDER BY 1"
    )).fetchall()
    if dupes:
        log.warning(
            "NEX-49: unique index %s NOT created: %d journal entr(ies) are already reversed "
            "more than once: %s. The accounts they touch are off by the extra reversals. "
            "Ledger rows cannot be deleted, so the index needs a follow-up migration that "
            "exempts these rows. Until then only the in-code check guards reversals.",
            _UQ_REVERSAL, len(dupes), ", ".join(f"{no} (x{n})" for no, n in dupes),
        )
    else:
        op.create_index(
            _UQ_REVERSAL, _TABLE, ["reverses_entry_id"], unique=True,
            postgresql_where=sa.text(_REVERSAL_WHERE), sqlite_where=sa.text(_REVERSAL_WHERE),
            if_not_exists=True,
        )

    # 2. An auto-post source has at most one live (non-reversal) entry.
    dupes = bind.execute(sa.text(
        "SELECT source_type, source_id, COUNT(*) FROM gl_journal_entries "
        f"WHERE {_LIVE_SOURCE_WHERE} AND source_id IS NOT NULL "
        "GROUP BY source_type, source_id HAVING COUNT(*) > 1 ORDER BY 1, 2"
    )).fetchall()
    if dupes:
        log.warning(
            "NEX-49: unique index %s NOT created: %d auto-post source(s) already have more "
            "than one live journal entry: %s. They are double-posted. Ledger rows cannot be "
            "deleted, so the index needs a follow-up migration that exempts these rows. "
            "Until then only the in-code check guards auto-posts.",
            _UQ_LIVE_SOURCE, len(dupes), ", ".join(f"{st} {sid} (x{n})" for st, sid, n in dupes),
        )
    else:
        op.create_index(
            _UQ_LIVE_SOURCE, _TABLE, ["source_type", "source_id"], unique=True,
            postgresql_where=sa.text(_LIVE_SOURCE_WHERE), sqlite_where=sa.text(_LIVE_SOURCE_WHERE),
            if_not_exists=True,
        )


def downgrade() -> None:
    # if_exists: upgrade may have skipped an index (see "Existing data" above).
    op.drop_index(_UQ_LIVE_SOURCE, table_name=_TABLE, if_exists=True)
    op.drop_index(_UQ_REVERSAL, table_name=_TABLE, if_exists=True)
