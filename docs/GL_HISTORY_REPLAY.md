# General ledger: why it starts empty, and the history replay (NEX-52)

Written because the accounting engine shipped switched off and nobody could
switch it on: `accounting_auto_post_enabled` existed on the settings row and
nowhere in the API. It is now a real setting (Settings → Accounting, admin
only). This page records what was decided about the months that were traded
before it, and how to act on that decision.

**Neither step below is routine. Turning auto-posting on, and running the
replay, each need the owner's sign-off first** — the dry-run report is what
they sign.

## Why the general ledger is empty while the inventory ledger has history

They are two different books.

- The **inventory ledger** (`inventory_ledger`) is written by every operation,
  unconditionally. It has recorded every sale, buyback, purchase and
  adjustment since day one.
- The **general ledger** (`gl_journal_entries` / `gl_journal_lines`) is written
  by the posting mappers in [`app/core/gl_postings.py`](../app/core/gl_postings.py),
  and every one of them returns without doing anything while the flag is off.

The flag defaults to off, so the shop traded normally and the GL received
nothing: no journal entries, no accounting periods. That is not data loss —
the source documents (orders, supplier purchases and payments, buybacks, melt
lots, adjustments) are all there. They were just never posted.

## The decision: replay history, not one opening entry

Two ways to give the books a past were considered.

| | Replay (chosen) | Single opening entry |
|---|---|---|
| What it posts | One journal entry per historical document, on the document's own date | One entry with today's balances |
| Audit trail | Every entry points at its order / purchase / payment | None before the opening date |
| Metal dimension | Grams per karat move document by document | Only a closing position |
| Period reporting | Past months have real revenue, COGS and VAT | Past months stay empty |

Replay was chosen because it keeps the per-document audit trail and the metal
dimension, and because the mappers are already idempotent per source document,
so replaying is safe to repeat. The cost: it opens past periods (below), and it
can only post what has a document (see "What it does not cover").

**Do not also call `POST /api/accounting/opening-balances` after a replay.**
That endpoint snapshots today's stock and supplier balances — the same gold the
replay has just moved through purchases and sales would be counted twice.

## What the replay does

[`app/core/gl_replay.py`](../app/core/gl_replay.py) walks the source documents
in chronological order and hands each to the mapper the live path uses. It does
not compute any debit or credit itself.

| Document | Mapper | Booked on |
|---|---|---|
| Sales (every status) | `post_sale` | order date |
| Voided orders | `post_order_refund` (full reversal) | void date |
| Orders refunded whole | `post_order_refund` (full reversal) | **sale date** — the refund date was never stored; the report lists each one |
| Line refunds | `post_order_refund` (per event, from the `ORDER_ITEM_REFUND` ledger rows) | refund date |
| Supplier purchases | `post_supplier_purchase` | purchase date |
| Supplier payments (cash / gold) | `post_supplier_payment` | payment date |
| Walk-in buybacks | `post_buyback` | buyback date |
| Melts | `post_melt` | date the melt lot was created |
| Gold-lot losses | `post_adjustment` | adjustment date |

Things worth knowing:

- **COGS is historical.** It comes from the gold rate / cost stored on the order
  line at checkout, never today's rate. If the cost the mapper computes differs
  from the snapshot stored on the order, the replay stops instead of posting.
- **Line-refunded orders** have had their header totals overwritten with what
  remains. The replay rebuilds the original sale from the lines (the way
  checkout computed it), posts that, then posts each refund.
- **It ignores the flag and never changes it.** It works with auto-posting on
  or off.
- **It is one transaction.** If anything fails, nothing is written: no entries,
  no periods, and both hash chains are exactly where they were.
- **It is idempotent.** A document that already has its GL entry is skipped, so
  a second run posts nothing, and a run after auto-posting is on only fills
  what is missing.
- Every replayed entry is recorded as posted by the admin who ran the tool
  (`--actor-email`), with `occurred_at` = when it ran and `entry_date` = when
  the business event happened. One `GL_HISTORY_REPLAYED` row on the inventory
  ledger marks the run.

## What it does not cover

Settle these with the accountant; the dry run counts them under "NOT replayed".

- **Opening stock and cash.** Stock that was on hand before the first document,
  or was added without one (products created in the catalogue, seeded lots,
  coin / ounce stock adjustments), never passed a mapper. After a replay, Metal
  Inventory can therefore show fewer grams than the shelf holds (even negative),
  and Cash starts from zero, not from the float in the drawer. The "Balances
  after replay" block of the dry run shows the exact figures. The gap is closed
  with a manual journal entry against Opening Balance Equity, dated before the
  first replayed entry — the accountant's call, not this tool's.
- **AR invoices not tied to a sale, AR receipts, vendor bills, vendor payments**
  recorded while the flag was off. Their posting logic lives inside the
  functions that create them; there is no mapper to re-run.
- **Stock adjustments other than gold-lot losses** (gains, product / coin /
  ounce). The live path does not post these either.

## Before you run anything

1. **Owner sign-off** for the dry run on production, recorded on the ticket.
2. **Outside shop hours.** The replay holds the ledger chain locks for its whole
   run — a dry run too. Tills wait until it finishes.
3. Chart of accounts seeded: Accounting → Chart of accounts, or
   `POST /api/accounting/seed-coa` (admin, idempotent). The replay never
   creates accounts and refuses to run without them.
4. `GET /api/accounting/ledger/verify` returns `empty` or `intact` with
   `head_matches: true`. The replay refuses to append to a broken chain.

## 1. Dry run (the default — writes nothing)

```bash
cd jewelry_backend
./.venv/bin/python -m scripts.replay_gl_history --actor-email <admin email>
```

It runs against whatever `DATABASE_URL` the environment points at — the `.env`
in the directory you run it from, unless the variable is set in the shell — and
prints the host and database first. **Read that line.** To rehearse on a Neon
branch instead of the live database, set the variable for that one command:

```bash
DATABASE_URL='<branch url>' ./.venv/bin/python -m scripts.replay_gl_history --actor-email <admin email>
```

The dry run performs the real replay inside a transaction and rolls it back,
so the report is exactly what `--execute` would post:

- per document type: how many were found, how many would be posted, how many
  are already in the GL, and the totals (USD debits, grams per karat);
- **the accounting periods it would create**;
- anything not replayed, and any warning (undated refunds, missing sources);
- the trial balance and the account balances the books would end with.

Give that report to the owner and the accountant. Nothing has changed yet.

## 2. Execute

Only after the owner signs off on the dry-run report.

1. Take a Neon branch (or snapshot) of the database. **Posted entries are
   immutable** — there is no undo short of restoring; corrections afterwards
   are reversing entries.
2. Run:

   ```bash
   cd jewelry_backend
   ./.venv/bin/python -m scripts.replay_gl_history --actor-email <admin email> --execute
   ```

3. Check: `GET /api/accounting/ledger/verify` → `intact`, `head_matches: true`;
   Accounting → Trial balance is balanced in money and in metal.

If it stops with `REPLAY FAILED — nothing was written`, that is literally true.
Fix what the message names (usually a CLOSED period in the way) and run again.

## 3. The periods it opens

Posting at original dates needs a period for every month that had activity.
A missing period is **created as OPEN** (that is what `ensure_period` does for
live postings too). So after a replay there are open periods for past months
that nobody has reviewed.

- The dry run lists them ("Accounting periods that would be created").
- After executing, review each one with the accountant and **close it**
  (Accounting → Periods), oldest first.
- Close them only once the replay is complete: a later run that has to post
  into a CLOSED period is refused, and rolls back.

## 4. Turning auto-posting on

Settings → Accounting → "Post sales to the books automatically". Admin only;
the change is written to the audit ledger (`SETTINGS_CHANGED`). The switch
refuses to turn on (409) until the chart of accounts is seeded — with the flag
on and a system account missing, every sale would be rejected.

- **Run the replay first.** Switching the flag on by itself starts the books on
  an arbitrary day with no past: revenue from that day, inventory credited for
  gold the GL never saw arrive.
- If the owner decides *not* to replay, that is a decision to accept the gap —
  write down the start date and agree with the accountant how the opening
  position is booked. Do not let it happen by accident.
- Flag first, replay later still works (already-posted documents are skipped),
  but the older entries then sit after newer ones in the hash chain.
- **Each sale does more work once it is on:** it resolves the system accounts,
  looks up product costs, locks the GL chain head, allocates an entry number
  and writes one journal entry with its lines plus one more audit-ledger row —
  all inside the sale's own transaction. Sales serialise on that lock. This is
  noted, not optimised.
- For the same reason a GL failure now fails the sale: **do not close the
  current month** while the shop is trading.

## Reference

- Core: [`app/core/gl_replay.py`](../app/core/gl_replay.py) —
  `replay_history()` (no commit), `run_replay()` (owns the transaction).
- CLI: [`scripts/replay_gl_history.py`](../scripts/replay_gl_history.py).
- Tests: `tests/test_gl_replay.py`, `tests/test_settings_auto_post_flag.py`.
- Mappers: [`app/core/gl_postings.py`](../app/core/gl_postings.py).
