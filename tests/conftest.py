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
import asyncio
from datetime import date, datetime

import bcrypt
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.audit_chain import GENESIS_HASH
from app.db.base import Base
# Importing app.models triggers all model class registration on Base.metadata.
import app.models  # noqa: F401
from app.models import AuthAuditChainHead, InventoryLedgerChainHead, GLJournalChainHead, Role, User


# Tests that post to the general ledger seed an open June-2026 period and assert
# against a June cutoff. Domain objects must therefore carry a date inside that
# period — otherwise the posting helpers fall back to date.today(), the lines fall
# outside the trial-balance window, and the assertions fail with a misleading
# KeyError on an account that was seeded but simply has no activity in range.
# Pinned rather than derived from today so these tests are deterministic forever.
BOOK_DATE = date(2026, 6, 15)
BOOK_DATETIME = datetime(2026, 6, 15, 12, 0, 0)


@pytest_asyncio.fixture
async def db():
    """Fresh in-memory DB per test; sessions roll back on teardown.

    Each test gets a clean schema — no cross-test pollution. The
    ledger chain-head row is seeded with GENESIS so any test that calls
    `record()` finds the same initial state the Alembic migration produces
    in real deployments.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        # Mirror both migrations' seed steps so tests see the same initial
        # state production does.
        session.add(
            InventoryLedgerChainHead(id=1, latest_entry_hash=GENESIS_HASH, row_count=0)
        )
        session.add(
            AuthAuditChainHead(id=1, latest_entry_hash=GENESIS_HASH, row_count=0)
        )
        session.add(
            GLJournalChainHead(id=1, latest_entry_hash=GENESIS_HASH, row_count=0)
        )
        await session.commit()
        yield session

    await engine.dispose()


# ── Real-auth API tests ───────────────────────────────────────────────────────
#
# Most API tests override get_current_user and never touch login. The auth
# tests are the opposite: the real app, the real /auth/login and the real
# get_current_user, with only the database swapped. `device` below is the one
# place that wiring lives.

AUTH_PASSWORD = "correct-horse-battery"
OWNER_EMAIL = "owner@example.com"            # id u-owner,      ADMIN
CASHIER_EMAIL = "cashier@example.com"        # id u-cashier,    CASHIER
ACCOUNTANT_EMAIL = "accountant@example.com"  # id u-accountant, ACCOUNTANT


def fast_hash(password: str) -> str:
    """bcrypt at the minimum cost: these tests log in dozens of times."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


async def settle() -> None:
    """Let the fire-and-forget auth-audit writes finish.

    `fire_auth_event` schedules its write and returns. A test that looks at
    the audit log, or simply ends, before that write lands is reading — or
    tearing down — a database another task is still writing to.
    """
    pending = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task()
        and getattr(t.get_coro(), "__name__", "") == "record_auth_event_safe"
    ]
    await asyncio.gather(*pending)


@pytest_asyncio.fixture
async def device(db, monkeypatch):
    """Factory for logged-out HTTP clients on the real app with real auth.

    A "device" is a client with its own cookie jar — the shop till, a phone.
    Every one talks to the same app and the same per-test database, which is
    seeded with an owner, a cashier and an accountant who all log in with
    AUTH_PASSWORD. Each device also gets an address of its own
    (X-Forwarded-For), so the 5/minute per-IP login limit only comes into
    play when a test sets the header itself.

    Teardown waits for the background audit writes, so none outlives the test.
    """
    from app.core.rate_limit import limiter
    from app.deps import get_db
    from app.main import app

    hashed = fast_hash(AUTH_PASSWORD)
    db.add(User(id="u-owner", email=OWNER_EMAIL, name="Owner", password_hash=hashed, role=Role.ADMIN, is_active=True))
    db.add(User(id="u-cashier", email=CASHIER_EMAIL, name="Cashier", password_hash=hashed, role=Role.CASHIER, is_active=True))
    db.add(User(id="u-accountant", email=ACCOUNTANT_EMAIL, name="Accountant", password_hash=hashed, role=Role.ACCOUNTANT, is_active=True))
    await db.commit()

    async def _get_db():
        yield db

    # The background recorder opens its own session on the app's engine; point
    # it at this test's database so audit rows land where the test can see them.
    monkeypatch.setattr(
        "app.core.auth_audit.async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False, class_=AsyncSession),
    )
    limiter.reset()  # the limiter's memory storage outlives a single test
    app.dependency_overrides[get_db] = _get_db

    opened: list[AsyncClient] = []

    def _new() -> AsyncClient:
        client = AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"X-Forwarded-For": f"203.0.113.{len(opened) + 1}"},
        )
        opened.append(client)
        return client

    yield _new

    for client in opened:
        await client.aclose()
    app.dependency_overrides.clear()
    await settle()
