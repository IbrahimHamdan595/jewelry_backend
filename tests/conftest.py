"""Minimal test-DB fixture.

Provides an isolated, per-test async SQLAlchemy session backed by in-memory
SQLite via aiosqlite. Used by tests that need to exercise the actual SELECT
queries — e.g. the zakat filter-correctness test that proves the WHERE clauses
exclude SOLD products, depleted lots, and zero-qty unit types.

We deliberately do NOT set up a global Postgres test DB:
  • the in-memory SQLite engine starts in microseconds
  • the zakat queries only use type-portable constructs (no ::jsonb casts,
    no `RETURNING`, no PG-specific functions)
  • running tests requires zero external services

If a future test needs Postgres-only features (e.g. JSONB ops, advisory
locks, NOTIFY/LISTEN), add a separate pg-backed fixture rather than promoting
this one — the speed/portability win of in-memory SQLite is worth keeping
for the simple cases.
"""
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
# Importing app.models triggers all model class registration on Base.metadata.
import app.models  # noqa: F401


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory DB per test; sessions roll back on teardown.

    Each test gets a clean schema — no cross-test pollution. Tests that need
    seed data set it up explicitly inside the test body.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session

    await engine.dispose()
