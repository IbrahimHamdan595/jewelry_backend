"""GL: unique indexes behind the reversal + auto-post guards (NEX-49)

Revision ID: d48c4a87efe4
Revises: e2b3c4d5f6a7
Create Date: 2026-10-04

Two partial unique indexes on gl_journal_entries, mirroring the model
(app.models.GL_UQ_REVERSAL_INDEX / GL_UQ_LIVE_SOURCE_INDEX):

  * uq_gl_entries_reverses_entry_id
        (reverses_entry_id) WHERE reverses_entry_id IS NOT NULL
    An entry is reversed at most once.

  * uq_gl_entries_live_source
        (source_type, source_id)
        WHERE reverses_entry_id IS NULL AND source_type IN (<auto-post sources>)
    An auto-post source has at most one live (non-reversal) entry.

gl.reverse_entry and gl_postings.find_live_entry check first, and gl.post_entry
checks again under the chain-head lock; the indexes are the backstop behind
both, for anything that reaches the table another way.

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
The names, columns, predicates and source list below are a frozen copy of the
model's (tests/test_migration_gl_unique_indexes.py holds the two together);
adding a source later needs a new migration.

Compatibility
-------------
Additive only (two indexes, no column or data change). Old code keeps working
against the new schema, and new code keeps working before this is applied —
the in-code checks do not depend on the indexes.

Existing data — fails fast
--------------------------
If the ledger already holds rows that violate either index, the upgrade stops
BEFORE creating anything and raises, naming every offender. It never records
the revision with an index missing. Such rows cannot be deleted to make room
(the ledger is append-only and hash-chained), and they mean the books are
already wrong: that needs a human decision, not a migration's guess.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "d48c4a87efe4"
down_revision: str = "e2b3c4d5f6a7"
branch_labels = None
depends_on = None

_TABLE = "gl_journal_entries"

_UQ_REVERSAL = "uq_gl_entries_reverses_entry_id"
_REVERSAL_COLUMNS = ["reverses_entry_id"]
_REVERSAL_WHERE = "reverses_entry_id IS NOT NULL"

_UQ_LIVE_SOURCE = "uq_gl_entries_live_source"
_LIVE_SOURCE_COLUMNS = ["source_type", "source_id"]
_LIVE_SOURCE_TYPES = (
    "ORDER", "ORDER_REFUND", "SUPPLIER_PURCHASE", "SUPPLIER_PAYMENT",
    "BUYBACK", "MELT", "ADJUSTMENT",
)
_LIVE_SOURCE_WHERE = (
    "reverses_entry_id IS NULL AND source_type IN ("
    + ", ".join(f"'{s}'" for s in _LIVE_SOURCE_TYPES) + ")"
)

_MAX_LISTED = 50  # offenders named per index before the list is cut short


def _listed(lines: list[str]) -> list[str]:
    if len(lines) <= _MAX_LISTED:
        return lines
    return lines[:_MAX_LISTED] + [f"... and {len(lines) - _MAX_LISTED} more"]


def _violations(bind) -> list[str]:
    """One paragraph per index the existing rows already violate (empty = clean)."""
    problems: list[str] = []

    # 1. Entries reversed more than once.
    rows = bind.execute(sa.text(
        "SELECT r.reverses_entry_id, o.entry_no, r.entry_no "
        "FROM gl_journal_entries r "
        "LEFT JOIN gl_journal_entries o ON o.id = r.reverses_entry_id "
        "WHERE r.reverses_entry_id IN ("
        "  SELECT reverses_entry_id FROM gl_journal_entries "
        f"  WHERE {_REVERSAL_WHERE} GROUP BY reverses_entry_id HAVING COUNT(*) > 1) "
        "ORDER BY 2, 1, 3"
    )).fetchall()
    by_original: dict[str, list[str]] = {}
    for original_id, original_no, reversal_no in rows:
        by_original.setdefault(original_no or f"<missing entry {original_id}>", []).append(reversal_no)
    if by_original:
        problems.append("\n".join(
            [f"  Entries reversed more than once ({_UQ_REVERSAL}):"]
            + _listed([f"    - {orig} is reversed {len(revs)} times, by {', '.join(revs)}"
                       for orig, revs in by_original.items()])
        ))

    # 2. Auto-post sources with more than one live (non-reversal) entry.
    rows = bind.execute(sa.text(
        "SELECT source_type, source_id, entry_no FROM gl_journal_entries "
        f"WHERE {_LIVE_SOURCE_WHERE} AND (source_type, source_id) IN ("
        "  SELECT source_type, source_id FROM gl_journal_entries "
        f"  WHERE {_LIVE_SOURCE_WHERE} AND source_id IS NOT NULL "
        "  GROUP BY source_type, source_id HAVING COUNT(*) > 1) "
        "ORDER BY 1, 2, 3"
    )).fetchall()
    by_source: dict[str, list[str]] = {}
    for source_type, source_id, entry_no in rows:
        by_source.setdefault(f"{source_type} {source_id}", []).append(entry_no)
    if by_source:
        problems.append("\n".join(
            [f"  Auto-post sources posted more than once ({_UQ_LIVE_SOURCE}):"]
            + _listed([f"    - {src} has {len(nos)} live entries: {', '.join(nos)}"
                       for src, nos in by_source.items()])
        ))

    return problems


def _create_indexes() -> None:
    op.create_index(
        _UQ_REVERSAL, _TABLE, _REVERSAL_COLUMNS, unique=True,
        postgresql_where=sa.text(_REVERSAL_WHERE), sqlite_where=sa.text(_REVERSAL_WHERE),
    )
    op.create_index(
        _UQ_LIVE_SOURCE, _TABLE, _LIVE_SOURCE_COLUMNS, unique=True,
        postgresql_where=sa.text(_LIVE_SOURCE_WHERE), sqlite_where=sa.text(_LIVE_SOURCE_WHERE),
    )


def upgrade() -> None:
    # Check BOTH indexes before creating either: all or nothing, on every dialect.
    problems = _violations(op.get_bind())
    if problems:
        raise RuntimeError(
            f"\n\nNEX-49 migration {revision} stopped: nothing was changed.\n\n"
            f"{_TABLE} already holds rows that the new unique indexes forbid:\n\n"
            + "\n\n".join(problems)
            + "\n\nWhat to do:\n"
            "  1. Do NOT delete or edit these rows: the ledger is append-only and hash-chained.\n"
            "  2. Do NOT stamp past this revision: it must never be recorded as applied with\n"
            "     an index missing.\n"
            "  3. Give this list to the accountant. Every extra reversal or duplicate posting\n"
            "     leaves the accounts it touches wrong by its full value, and needs a\n"
            "     correcting manual journal entry.\n"
            "  4. Give this list to engineering. The duplicate rows stay in the ledger, so this\n"
            "     migration has to be revised to exempt exactly these rows before it can run.\n"
            "  Until then the application keeps working: its in-code checks still refuse new\n"
            "  duplicates; only the database-level backstop is missing.\n"
        )
    _create_indexes()


def downgrade() -> None:
    op.drop_index(_UQ_LIVE_SOURCE, table_name=_TABLE)
    op.drop_index(_UQ_REVERSAL, table_name=_TABLE)
