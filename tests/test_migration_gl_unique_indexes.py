"""NEX-49 migration (…_gl_unique_reversal_and_live_source.py), run for real.

The test schema is built from the model metadata (Base.metadata.create_all);
production's is built by this migration. They must be the same schema, so the
migration is executed here against SQLite and rendered for Postgres, and both
are compared with what the model declares. Its refusal to run over a ledger
that already violates the indexes is exercised too.
"""
import importlib.util
import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.core import gl
from app.core import gl_postings as glp
from app.models import (
    GL_UNIQUE_LIVE_SOURCE_TYPES, GL_UQ_LIVE_SOURCE_INDEX, GL_UQ_REVERSAL_INDEX,
    AccountType, Denomination, GLAccount, GLPeriod, NormalBalance, PeriodStatus,
)

D = Decimal


def _load_migration():
    # By slug, not revision id: revisions get re-chained when branches integrate.
    (path,) = (Path(__file__).parent.parent / "alembic" / "versions").glob(
        "*_gl_unique_reversal_and_live_source.py")
    spec = importlib.util.spec_from_file_location("nex49_gl_unique_indexes", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load_migration()


async def _run(db, fn):
    """Run a migration function against the test session's connection."""
    def _go(sync_session):
        ctx = MigrationContext.configure(sync_session.connection())
        with Operations.context(ctx):
            fn()
    await db.run_sync(_go)


async def _index_sql(db) -> dict[str, str]:
    rows = (await db.execute(text(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'index' AND name LIKE 'uq_gl_entries%' ORDER BY name"))).all()
    return {name: sql for name, sql in rows}


async def _ledger(db):
    period = GLPeriod(year=2026, period_no=6, status=PeriodStatus.OPEN)
    cash = GLAccount(code="1000", name="Cash", type=AccountType.ASSET,
                     denomination=Denomination.MONEY, normal_balance=NormalBalance.DEBIT,
                     currency="USD", system_key="CASH")
    rev = GLAccount(code="4000", name="Sales", type=AccountType.INCOME,
                    denomination=Denomination.MONEY, normal_balance=NormalBalance.CREDIT,
                    currency="USD", system_key="SALES_REVENUE")
    db.add_all([period, cash, rev])
    await db.flush()

    async def post(**kw):
        kw.setdefault("source_type", gl.SOURCE_MANUAL)
        kw.setdefault("source_id", None)
        return await gl.post_entry(
            db, entry_date=date(2026, 6, 3), memo="t", actor_user_id="u1",
            lines=[
                gl.GLLine(account_id=cash.id, denomination="MONEY", base_debit=D("100"), money_debit=D("100")),
                gl.GLLine(account_id=rev.id, denomination="MONEY", base_credit=D("100"), money_credit=D("100")),
            ], **kw)

    return period, post


async def _raw_entry(db, period, entry_no, *, source_type, source_id=None, reverses_entry_id=None):
    """A row written straight into the table, as the pre-NEX-49 code could."""
    uid = uuid4().hex
    await db.execute(text(
        "INSERT INTO gl_journal_entries (id, entry_no, entry_date, period_id, memo, source_type, "
        "source_id, reverses_entry_id, actor_user_id, occurred_at, prev_hash, entry_hash) "
        "VALUES (:id, :no, '2026-06-05', :p, 'raw', :st, :sid, :rev, 'u1', '2026-06-05 12:00:00', 'x', :h)"),
        {"id": uid, "no": entry_no, "p": period.id, "st": source_type, "sid": source_id,
         "rev": reverses_entry_id, "h": f"raw-{uid}"})
    return uid


# ── The migration and the model describe the same two indexes ─────────────────

def test_migration_frozen_literals_match_the_model():
    assert mig._LIVE_SOURCE_TYPES == GL_UNIQUE_LIVE_SOURCE_TYPES
    for name, columns, where, index in (
        (mig._UQ_REVERSAL, mig._REVERSAL_COLUMNS, mig._REVERSAL_WHERE, GL_UQ_REVERSAL_INDEX),
        (mig._UQ_LIVE_SOURCE, mig._LIVE_SOURCE_COLUMNS, mig._LIVE_SOURCE_WHERE, GL_UQ_LIVE_SOURCE_INDEX),
    ):
        assert name == index.name
        assert index.table.name == mig._TABLE and index.unique
        assert columns == [c.name for c in index.columns]
        # One predicate, word for word, on both dialects.
        assert where == str(index.dialect_options["postgresql"]["where"])
        assert where == str(index.dialect_options["sqlite"]["where"])


def test_migration_emits_the_same_postgres_ddl_as_the_model():
    """What production gets (the migration, rendered for Postgres) against what
    the metadata declares (rendered for Postgres) — statement for statement."""
    def _norm(sql):
        return " ".join(sql.split())

    buf = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buf})
    with Operations.context(ctx):
        mig._create_indexes()
    emitted = [_norm(stmt) for stmt in buf.getvalue().split(";") if stmt.strip()]

    declared = [
        _norm(str(CreateIndex(ix).compile(dialect=postgresql.dialect())))
        for ix in (GL_UQ_REVERSAL_INDEX, GL_UQ_LIVE_SOURCE_INDEX)
    ]
    assert emitted == declared
    assert all("WHERE" in stmt for stmt in emitted)


@pytest.mark.asyncio
async def test_migration_builds_exactly_what_the_model_declares(db):
    """Executed on SQLite: downgrade removes both indexes, upgrade puts back the
    very DDL create_all produced — over a ledger full of legitimate repeats."""
    declared = await _index_sql(db)
    assert set(declared) == {mig._UQ_REVERSAL, mig._UQ_LIVE_SOURCE}

    _, post = await _ledger(db)
    orig = await post()
    await gl.reverse_entry(db, original_entry_id=orig.id, actor_user_id="u1", entry_date=date(2026, 6, 4))
    await post(source_type=gl.SOURCE_MANUAL, source_id="ref-1")
    await post(source_type=gl.SOURCE_MANUAL, source_id="ref-1")
    await post(source_type="AR_INVOICE")
    await post(source_type="AR_INVOICE")
    await post(source_type=glp.SOURCE_ORDER, source_id="order-1")
    await post(source_type=glp.SOURCE_ORDER_REFUND, source_id="item-1:1")
    await post(source_type=glp.SOURCE_ORDER_REFUND, source_id="item-1:2")

    await _run(db, mig.downgrade)
    assert await _index_sql(db) == {}

    await _run(db, mig.upgrade)
    assert await _index_sql(db) == declared


# ── Fail fast: never record the revision with an index missing ────────────────

@pytest.mark.asyncio
async def test_migration_refuses_a_ledger_with_an_entry_reversed_twice(db):
    period, post = await _ledger(db)
    orig = await post()
    fine = await post()
    await gl.reverse_entry(db, original_entry_id=fine.id, actor_user_id="u1", entry_date=date(2026, 6, 4))
    await _run(db, mig.downgrade)  # the pre-NEX-49 schema
    await _raw_entry(db, period, "JE-RAW-R1", source_type=gl.SOURCE_REVERSAL,
                     source_id=orig.id, reverses_entry_id=orig.id)
    await _raw_entry(db, period, "JE-RAW-R2", source_type=gl.SOURCE_REVERSAL,
                     source_id=orig.id, reverses_entry_id=orig.id)

    with pytest.raises(RuntimeError) as exc:
        await _run(db, mig.upgrade)
    msg = str(exc.value)
    # Names the offenders ...
    assert mig._UQ_REVERSAL in msg
    assert orig.entry_no in msg and "JE-RAW-R1" in msg and "JE-RAW-R2" in msg
    assert fine.entry_no not in msg  # ... and only the offenders
    # ... says what to do ...
    assert "nothing was changed" in msg.lower() and "What to do" in msg
    # ... and leaves no index behind.
    assert await _index_sql(db) == {}


@pytest.mark.asyncio
async def test_migration_refuses_a_ledger_with_a_double_posted_auto_post_source(db):
    period, post = await _ledger(db)
    await post(source_type=gl.SOURCE_MANUAL, source_id="ref-1")
    await post(source_type=gl.SOURCE_MANUAL, source_id="ref-1")  # legitimate repeat
    await _run(db, mig.downgrade)
    await _raw_entry(db, period, "JE-RAW-S1", source_type=glp.SOURCE_ORDER, source_id="order-9")
    await _raw_entry(db, period, "JE-RAW-S2", source_type=glp.SOURCE_ORDER, source_id="order-9")

    with pytest.raises(RuntimeError) as exc:
        await _run(db, mig.upgrade)
    msg = str(exc.value)
    assert mig._UQ_LIVE_SOURCE in msg
    assert "ORDER order-9" in msg and "JE-RAW-S1" in msg and "JE-RAW-S2" in msg
    assert "ref-1" not in msg
    assert await _index_sql(db) == {}


@pytest.mark.asyncio
async def test_migration_reports_every_violation_at_once(db):
    period, post = await _ledger(db)
    orig = await post()
    await _run(db, mig.downgrade)
    for n in (1, 2):
        await _raw_entry(db, period, f"JE-RAW-R{n}", source_type=gl.SOURCE_REVERSAL,
                         source_id=orig.id, reverses_entry_id=orig.id)
        await _raw_entry(db, period, f"JE-RAW-S{n}", source_type=glp.SOURCE_MELT, source_id="lot-7")

    with pytest.raises(RuntimeError) as exc:
        await _run(db, mig.upgrade)
    msg = str(exc.value)
    assert orig.entry_no in msg and "MELT lot-7" in msg
    assert await _index_sql(db) == {}
