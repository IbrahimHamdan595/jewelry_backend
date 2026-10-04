"""Replay historical operations into the general ledger (NEX-52).

Operator tool — never run automatically. Read docs/GL_HISTORY_REPLAY.md first:
both this replay and switching auto-posting on need the owner's sign-off.

DRY RUN (the default) — replays inside a transaction, prints what would be
posted and which periods would be opened, then rolls back. Writes nothing:
    cd jewelry_backend && ./.venv/bin/python -m scripts.replay_gl_history --actor-email owner@example.com

EXECUTE — the same replay, committed as ONE transaction (all or nothing):
    cd jewelry_backend && ./.venv/bin/python -m scripts.replay_gl_history --actor-email owner@example.com --execute

It runs against whatever DATABASE_URL the environment / .env points at and
prints that target first. Safe to re-run: documents already in the GL are
skipped. --actor-email is the active ADMIN recorded as the poster of every
replayed entry.
"""

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.engine import make_url

from app.config import settings
from app.core import gl_replay
from app.db.session import async_session_factory, engine
from app.models import Role, User


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.replay_gl_history",
        description="Post pre-existing sales, purchases, payments, buybacks, melts and "
                    "adjustments to the general ledger at their original dates.",
    )
    parser.add_argument(
        "--actor-email", required=True,
        help="email of the active ADMIN user recorded as the poster of the replayed entries",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="report what would be posted and write nothing (the default)")
    mode.add_argument("--execute", action="store_true",
                      help="post the entries for real, as a single transaction")
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None, *, session_factory=async_session_factory) -> int:
    args = parse_args(argv)
    url = make_url(settings.database_url)
    print(f"Database: {url.host or '(local)'} / {url.database}")
    print("Mode:     " + ("EXECUTE — entries will be committed" if args.execute
                          else "DRY RUN — nothing will be written"))
    print()

    async with session_factory() as db:
        actor = (
            await db.execute(select(User).where(User.email == args.actor_email))
        ).scalar_one_or_none()
        if actor is None or not actor.is_active or actor.role != Role.ADMIN:
            print(f"error: --actor-email must be an active ADMIN user; "
                  f"{args.actor_email!r} is not.", file=sys.stderr)
            return 2
        try:
            report = await gl_replay.run_replay(db, actor_user_id=actor.id, execute=args.execute)
        except gl_replay.ReplayError as exc:
            print(f"REPLAY FAILED — nothing was written.\n{exc}", file=sys.stderr)
            return 1
        except Exception:
            # run_replay has already rolled back; say so before the traceback.
            print("REPLAY FAILED on an unexpected error — nothing was written.", file=sys.stderr)
            raise

    print(gl_replay.format_report(report))
    if not args.execute:
        print("\nNothing was written. After the owner signs off on this report, re-run with "
              "--execute to post these entries.")
    return 0


async def _run() -> int:
    try:
        return await main()
    finally:
        await engine.dispose()  # close the pool inside the loop — no noise at exit


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
