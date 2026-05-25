"""Hash-chaining for the inventory audit trail.

AUDIT RATIONALE
---------------
The `InventoryLedger` is append-only by convention (the API exposes no UPDATE
or DELETE endpoints), but that convention is enforced only above the database.
A privileged actor — a DBA, a misconfigured backup-restore, an attacker who
captures the application's DB credentials — could `UPDATE inventory_ledger
SET payload = '{}' WHERE id = '…'` and silently rewrite history.

The hash chain raises the bar: each ledger row stores a SHA-256 over its own
contents PLUS the previous row's `entry_hash`. Editing or deleting any
historical row breaks the chain at exactly that point; the breakage is
detectable by walking the chain from genesis.

The hash is stored next to the data it protects, so a sufficiently determined
attacker with full DB access can still rewrite the chain end-to-end. The
practical defense against that is (a) DB-level revoke of UPDATE/DELETE on the
ledger table (covered separately in audit phase A2) and (b) periodic
out-of-band capture of the head hash (e.g. a daily summary email) so any
end-to-end rewrite would have to also retro-fit the historical observers'
copies, which they cannot reach.

DESIGN
------
- `compute_ledger_entry_hash(prev_hash, fields)` is pure: same inputs always
  produce the same digest. No DB, no clock. Sortable canonical JSON exactly
  mirrors the strategy used for zakat snapshot integrity hashing in
  `app/core/zakat.py` (`compute_integrity_hash`).
- The set of `_CHAINED_FIELDS` is fixed and ordered. Adding a field to the
  hash later would break every existing chain; if that is ever necessary,
  bump a hash-version prefix and migrate all stored hashes in a single shot.
- The genesis sentinel is the literal string "GENESIS". The first real ledger
  row chains to it. Any row whose `prev_hash` equals "GENESIS" is by
  construction the first row in the chain.

The fields that participate in the hash are the *user-visible facts* of the
event: what happened, who did it, when, what it referenced, and the payload.
The DB-assigned `id` and `created_at` are deliberately NOT in the hash — the
chain is about the semantic event, not the storage row's identity. (Including
`id` would make the hash unforgeable but also fragile against future row-id
schema changes; payload + meta is enough.)
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

# Sentinel for the row before the first real entry. Any ledger row whose
# `prev_hash` equals GENESIS_HASH is the first link in the chain.
GENESIS_HASH = "GENESIS"

# Ordered list of fields included in the entry hash. Order is part of the
# protocol — DO NOT reorder without writing a migration that re-hashes
# everything.
_CHAINED_FIELDS: tuple[str, ...] = (
    "event_type",
    "actor_user_id",
    "occurred_at",
    "ref_type",
    "ref_id",
    "payload",
)


def _canonical(value: Any) -> Any:
    """Deterministic serialization for hashing.

    Mirrors `app/core/zakat.py::_canonical`:
      • dicts → sorted by key recursively
      • lists/tuples → element-wise canonical
      • datetimes/dates → ISO 8601 string
      • Decimal/anything-with-isoformat → str
      • everything else → passthrough (json.dumps handles primitives)
    """
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _canonical(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    # Decimals, UUIDs, etc. get str-ified so json doesn't choke and the
    # representation is stable.
    if hasattr(value, "__class__") and value.__class__.__name__ in ("Decimal",):
        return str(value)
    return value


def compute_ledger_entry_hash(*, prev_hash: str, fields: dict[str, Any]) -> str:
    """Compute SHA-256 over the canonical serialization of (prev_hash, fields).

    Pure function. No DB, no clock.

    Parameters
    ----------
    prev_hash : str
        The `entry_hash` of the previous row in the chain, or GENESIS_HASH for
        the first row.
    fields : dict
        Must contain every key in `_CHAINED_FIELDS`. Missing keys raise
        KeyError — silent fall-through here would let an attacker omit a field
        and produce a valid-looking hash over a subset of the event.
    """
    if not isinstance(prev_hash, str) or not prev_hash:
        raise ValueError(
            f"prev_hash must be a non-empty string (use GENESIS_HASH for the "
            f"first row); got {prev_hash!r}"
        )

    payload = {f: _canonical(fields[f]) for f in _CHAINED_FIELDS}
    payload["__prev"] = prev_hash
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def verify_chain(rows: Iterable[dict]) -> dict:
    """Walk a sequence of ledger rows and report the first chain break.

    Pure function — caller is responsible for fetching `rows` from the DB in
    chain order (typically `ORDER BY occurred_at, id` to match the order
    `record()` wrote them).

    Each row must include `id`, `prev_hash`, `entry_hash`, and every field in
    `_CHAINED_FIELDS`.

    Returns
    -------
    dict with shape:
        {
          "status": "intact" | "broken" | "empty",
          "total_rows": int,
          "first_break": {
              "id": str,
              "expected_prev_hash": str,
              "actual_prev_hash": str,
              "expected_entry_hash": str,
              "actual_entry_hash": str,
          } | None,
        }
    """
    expected_prev = GENESIS_HASH
    total = 0
    for row in rows:
        total += 1
        recomputed = compute_ledger_entry_hash(
            prev_hash=row["prev_hash"],
            fields={f: row[f] for f in _CHAINED_FIELDS},
        )
        if row["prev_hash"] != expected_prev or row["entry_hash"] != recomputed:
            return {
                "status": "broken",
                "total_rows": total,
                "first_break": {
                    "id": row["id"],
                    "expected_prev_hash": expected_prev,
                    "actual_prev_hash": row["prev_hash"],
                    "expected_entry_hash": recomputed,
                    "actual_entry_hash": row["entry_hash"],
                },
            }
        expected_prev = row["entry_hash"]

    return {
        "status": "intact" if total > 0 else "empty",
        "total_rows": total,
        "first_break": None,
    }
