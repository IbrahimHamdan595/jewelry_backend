"""Startup schema guard (NEX-54): never serve from a database that is behind.

Production migrations are applied by hand; `main` deploys by itself. Without a
guard, merging a release that maps a new column before its migration has run
means every request touching that column fails — for `users.token_version`
that is every authenticated request. With it, the new release refuses to
start, the platform keeps the previous one serving, and the only symptom is a
failed deploy whose message says which command to run.

The guard refuses in exactly one situation — the database is at a revision
this code knows and has migrations beyond. Everything it cannot be sure
about (no alembic_version table, a revision it has never heard of, a database
it cannot reach) is a warning and a normal start: a check must not become an
outage of its own.

Most cases run against throwaway migration histories built in a temp
directory, so they say nothing about this repository's real revision ids.
"""
import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import pytest_asyncio
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core import schema_guard
from app.core.schema_guard import MIGRATIONS_DIR, SchemaBehindError, check_schema_is_current


# ── Helpers ───────────────────────────────────────────────────────────────────

def _history(tmp_path: Path, revisions: dict[str, str | tuple[str, ...] | None]) -> ScriptDirectory:
    """A migration history: {revision: down_revision(s)}, written as real
    Alembic revision files."""
    versions = tmp_path / "versions"
    versions.mkdir()
    for revision, down_revision in revisions.items():
        (versions / f"{revision}.py").write_text(
            f'"""{revision}"""\n'
            f"revision = {revision!r}\n"
            f"down_revision = {down_revision!r}\n"
            "branch_labels = None\n"
            "depends_on = None\n\n\n"
            "def upgrade():\n    pass\n\n\n"
            "def downgrade():\n    pass\n"
        )
    return ScriptDirectory(str(tmp_path))


LINEAR = {"aaa111": None, "bbb222": "aaa111", "ccc333": "bbb222"}                      # head: ccc333
TWO_HEADS = {"aaa111": None, "left22": "aaa111", "right2": "aaa111"}                   # heads: left22, right2
MERGED = {**TWO_HEADS, "merge3": ("left22", "right2")}                                 # head: merge3


@pytest_asyncio.fixture
async def database():
    """Factory for a database stamped at the given revision(s).

    `None` builds one with no alembic_version table at all, like the
    create_all databases the tests and local setups use.
    """
    engines = []

    async def _at(revisions) -> object:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        engines.append(engine)
        if revisions is not None:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                for revision in revisions:
                    await conn.execute(text("INSERT INTO alembic_version VALUES (:r)"), {"r": revision})
        return engine

    yield _at
    for engine in engines:
        await engine.dispose()


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and r.name == schema_guard.__name__]


# ── Behind: refuse ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_database_one_revision_behind_refuses_to_start(tmp_path, database):
    with pytest.raises(SchemaBehindError) as excinfo:
        await check_schema_is_current(await database(["bbb222"]), _history(tmp_path, LINEAR))

    message = str(excinfo.value)
    assert "bbb222" in message                  # where the database is
    assert "ccc333" in message                  # where this code needs it
    assert "alembic upgrade head" in message    # what to do about it


@pytest.mark.asyncio
async def test_database_several_revisions_behind_refuses_to_start(tmp_path, database):
    with pytest.raises(SchemaBehindError, match="aaa111"):
        await check_schema_is_current(await database(["aaa111"]), _history(tmp_path, LINEAR))


def test_refusal_is_a_runtime_error():
    """Nothing in the startup path catches it: the process exits."""
    assert issubclass(SchemaBehindError, RuntimeError)


# ── At head: start ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_database_at_head_starts_quietly(tmp_path, database, caplog):
    with caplog.at_level(logging.WARNING):
        await check_schema_is_current(await database(["ccc333"]), _history(tmp_path, LINEAR))
    assert _warnings(caplog) == []


# ── Cannot tell: warn and start ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_database_ahead_of_the_code_starts_with_a_warning(tmp_path, database, caplog):
    """A revision this code has never heard of: the code was rolled back, or
    another branch's migration is already applied. Refusing here would turn
    a rollback into an outage."""
    with caplog.at_level(logging.WARNING):
        await check_schema_is_current(await database(["zzz999"]), _history(tmp_path, LINEAR))
    assert len(_warnings(caplog)) == 1
    assert "zzz999" in _warnings(caplog)[0]


@pytest.mark.asyncio
async def test_database_without_alembic_version_starts_with_a_warning(tmp_path, database, caplog):
    with caplog.at_level(logging.WARNING):
        await check_schema_is_current(await database(None), _history(tmp_path, LINEAR))
    assert len(_warnings(caplog)) == 1
    assert "alembic_version" in _warnings(caplog)[0]


@pytest.mark.asyncio
async def test_empty_alembic_version_starts_with_a_warning(tmp_path, database, caplog):
    with caplog.at_level(logging.WARNING):
        await check_schema_is_current(await database([]), _history(tmp_path, LINEAR))
    assert len(_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_unreachable_database_starts_with_a_warning(tmp_path, caplog):
    """The app has always been able to start while the database is briefly
    away; the guard must not change that."""
    engine = Mock()
    engine.connect = Mock(side_effect=ConnectionRefusedError("simulated outage"))
    with caplog.at_level(logging.WARNING):
        await check_schema_is_current(engine, _history(tmp_path, LINEAR))
    assert len(_warnings(caplog)) == 1


@pytest.mark.asyncio
async def test_missing_migration_scripts_start_with_a_warning(tmp_path, database, caplog):
    """An image built without the alembic/ directory has nothing to compare with."""
    with patch.object(schema_guard, "MIGRATIONS_DIR", tmp_path / "not-there"), caplog.at_level(logging.WARNING):
        await check_schema_is_current(await database(["bbb222"]))
    assert len(_warnings(caplog)) == 1


# ── More than one head ────────────────────────────────────────────────────────
#
# Two branches merged without re-chaining their migrations leave the code with
# two heads. Alembic then records one row per head, and the database is up to
# date only when it has reached EVERY head.

@pytest.mark.asyncio
async def test_two_heads_both_applied_starts(tmp_path, database, caplog):
    with caplog.at_level(logging.WARNING):
        await check_schema_is_current(await database(["left22", "right2"]), _history(tmp_path, TWO_HEADS))
    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_two_heads_one_missing_refuses_and_names_it(tmp_path, database):
    with pytest.raises(SchemaBehindError) as excinfo:
        await check_schema_is_current(await database(["left22"]), _history(tmp_path, TWO_HEADS))
    message = str(excinfo.value)
    assert "right2" in message
    # `alembic upgrade head` is an error when there are two; the plural works.
    assert "alembic upgrade heads" in message


@pytest.mark.asyncio
async def test_two_heads_none_applied_refuses(tmp_path, database):
    with pytest.raises(SchemaBehindError) as excinfo:
        await check_schema_is_current(await database(["aaa111"]), _history(tmp_path, TWO_HEADS))
    assert "left22" in str(excinfo.value) and "right2" in str(excinfo.value)


@pytest.mark.asyncio
async def test_merge_revision_counts_for_both_branches(tmp_path, database, caplog):
    history = _history(tmp_path, MERGED)
    with caplog.at_level(logging.WARNING):
        await check_schema_is_current(await database(["merge3"]), history)
    assert _warnings(caplog) == []

    with pytest.raises(SchemaBehindError, match="merge3"):
        await check_schema_is_current(await database(["left22", "right2"]), history)


# ── Cost ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("stamped", [["ccc333"], ["bbb222"], ["zzz999"]])
async def test_guard_costs_exactly_one_query(tmp_path, database, stamped):
    engine = await database(stamped)
    statements = []
    event.listen(engine.sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2]))

    try:
        await check_schema_is_current(engine, _history(tmp_path, LINEAR))
    except SchemaBehindError:
        pass
    assert statements == ["SELECT version_num FROM alembic_version"]


# ── This repository's own migrations ──────────────────────────────────────────

def _repo_history() -> ScriptDirectory:
    return ScriptDirectory(str(MIGRATIONS_DIR))


def test_repo_migrations_are_found_without_alembic_ini_or_cwd(monkeypatch, tmp_path):
    """Located from the package, not from wherever the server was started."""
    monkeypatch.chdir(tmp_path)
    assert (MIGRATIONS_DIR / "versions").is_dir()
    assert _repo_history().get_heads()


@pytest.mark.asyncio
async def test_repo_head_is_read_from_the_scripts_not_hard_coded(database):
    """Stamped at whatever the scripts say the head is: starts. Stamped one
    step before it: refuses. No revision id appears in this test or in the
    guard, so adding a migration needs no change to either."""
    history = _repo_history()
    heads = history.get_heads()
    await check_schema_is_current(await database(heads))

    parents = history.get_revision(heads[0]).down_revision
    previous = [parents] if isinstance(parents, str) else list(parents)
    with pytest.raises(SchemaBehindError, match=heads[0]):
        await check_schema_is_current(await database(previous + heads[1:]))


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_app_startup_runs_the_guard_and_a_refusal_stops_it():
    """The lifespan runs the guard first; if it refuses, the app never comes
    up and nothing else (the gold-rate poller) is started."""
    from app.main import app

    refusal = SchemaBehindError("database is behind")
    with patch("app.main.check_schema_is_current", AsyncMock(side_effect=refusal)) as guard, \
            patch("app.main.start_gold_rate_poller") as poller:
        with pytest.raises(SchemaBehindError):
            with TestClient(app):
                pass
    guard.assert_awaited_once()
    poller.assert_not_called()


def test_app_startup_continues_when_the_guard_is_satisfied():
    from app.main import app

    with patch("app.main.check_schema_is_current", AsyncMock(return_value=None)) as guard, \
            patch("app.main.start_gold_rate_poller") as poller:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
    guard.assert_awaited_once()
    poller.assert_called_once()


def test_guard_does_not_run_unless_the_lifespan_does():
    """Every other test drives the app without its lifespan, so none of them
    pays for the guard or depends on an alembic_version table."""
    from app.main import app

    with patch("app.main.check_schema_is_current", AsyncMock()) as guard:
        assert TestClient(app).get("/health").status_code == 200
    guard.assert_not_awaited()
