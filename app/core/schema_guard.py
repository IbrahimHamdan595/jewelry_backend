"""Startup schema guard (NEX-54): refuse to serve from a database that is
behind this code.

WHY
---
Production migrations are applied by hand; `main` deploys by itself. A
release that maps a new column therefore reaches production BEFORE its
migration unless somebody remembers the order, and the ORM then selects a
column that does not exist: for `users.token_version` that is every
authenticated request answering 500.

A failed start is the safe outcome. The platform keeps the previous release
serving, and the deploy log carries the message below — which revision the
database is at, which one this code needs, and the command to run.

WHAT IT COMPARES
----------------
The revision(s) in the database's `alembic_version` table against the head
revision(s) of the migration scripts shipped with this code, read with
Alembic's own `ScriptDirectory`. Nothing is hard-coded: adding a migration
moves the head, and this guard with it.

  database is AT every head            → start.
  database is BEHIND                   → raise SchemaBehindError. "Behind"
      (every revision it is at is one    means exactly this and nothing
      this code knows, and some head     looser.
      is not among them or their
      ancestors)
  anything else                        → log a warning and start:
      • no `alembic_version` table — a database built with create_all
        (tests, a fresh local setup);
      • the table is empty;
      • a revision this code does not know — the database is AHEAD: the code
        was rolled back, or another branch's migration is already applied;
      • the database cannot be reached, or the scripts cannot be read.

The guard refuses only when it is sure. A check that could itself keep the
service down — after a rollback, during a database blip — would be a worse
trade than the failure it prevents.

MORE THAN ONE HEAD
------------------
Two branches merged without re-chaining their migrations leave two heads.
Alembic then keeps one `alembic_version` row per head, so the rule above is
applied to sets: the database must have reached EVERY head, directly or
through a later (merge) revision. One head missing is "behind"; the message
names the missing head(s) and says `alembic upgrade heads`, because
`upgrade head` is an error while there are several.

COST
----
One `SELECT version_num FROM alembic_version` on one connection, once per
process start, plus reading the migration files (about ten milliseconds).
A database that does not answer holds startup for the driver's connect
timeout, after which the guard steps aside. It runs from the app's lifespan
only — not at import, not per request, and not in tests, which drive the
app without its lifespan.
"""
import logging
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger(__name__)

# <repo>/alembic, found from this file — not from the working directory the
# server happened to be started in, and without needing alembic.ini.
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic"


class SchemaBehindError(RuntimeError):
    """The database is at an older revision than this code needs."""


async def _database_revisions(engine: AsyncEngine) -> set[str] | None:
    """What `alembic_version` says — the one query this guard costs.

    None when it cannot be read at all: no such table, or no database to
    ask. Either way the guard has nothing to compare and steps aside.
    """
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(text("SELECT version_num FROM alembic_version"))
            return {row[0] for row in rows}
    except Exception as exc:
        log.warning(
            "Schema check skipped: alembic_version could not be read (%s). "
            "Expected for a database built without Alembic; otherwise check the database.",
            type(exc).__name__,
        )
        return None


def _with_ancestors(parents: dict[str, tuple[str, ...]], revisions: set[str]) -> set[str]:
    """`revisions` plus everything they were built on."""
    reached: set[str] = set()
    pending = list(revisions)
    while pending:
        revision = pending.pop()
        if revision not in reached:
            reached.add(revision)
            pending.extend(parents[revision])
    return reached


def _parents(down_revision: str | tuple[str, ...] | None) -> tuple[str, ...]:
    """A revision's parent(s): none for the base, two or more for a merge."""
    if down_revision is None:
        return ()
    return (down_revision,) if isinstance(down_revision, str) else tuple(down_revision)


async def check_schema_is_current(engine: AsyncEngine, script: ScriptDirectory | None = None) -> None:
    """Raise SchemaBehindError if `engine`'s database is behind the migration
    scripts; warn and return when that cannot be established. See the module
    docstring for the exact rule.

    `script` is for tests; by default the scripts shipped with this code.
    """
    try:
        script = script or ScriptDirectory(str(MIGRATIONS_DIR))
        parents = {rev.revision: _parents(rev.down_revision) for rev in script.walk_revisions()}
        heads = set(script.get_heads())
    except Exception as exc:
        log.warning("Schema check skipped: migration scripts could not be read (%s).", type(exc).__name__)
        return

    database = await _database_revisions(engine)
    if database is None:
        return
    if not database:
        log.warning("Schema check skipped: alembic_version is empty, so the database's revision is unknown.")
        return

    unknown = database - parents.keys()
    if unknown:
        log.warning(
            "Schema check: the database is at revision %s, which this code's migrations do not "
            "contain (they end at %s). The database is ahead of this code — a rollback, or another "
            "branch's migration. Starting anyway.",
            ", ".join(sorted(unknown)),
            ", ".join(sorted(heads)),
        )
        return

    missing = heads - _with_ancestors(parents, database)
    if not missing:
        return

    upgrade = "alembic upgrade head" if len(heads) == 1 else "alembic upgrade heads"
    raise SchemaBehindError(
        f"Database schema is behind this code: the database is at revision "
        f"{', '.join(sorted(database))} and this code needs {', '.join(sorted(missing))}. "
        f"Run `{upgrade}` against this database, then deploy again. "
        "Refusing to start so that the previous release keeps serving."
    )
