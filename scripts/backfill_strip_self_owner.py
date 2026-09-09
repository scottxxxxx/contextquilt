#!/usr/bin/env python3
"""Strip a SELF owner off self-typed patches, the way the write path does.

WHY THIS EXISTS. `strip_owner_on_self_typed_patches` removes an owner
naming the user from trait/preference/goal/constraint at ingest, because
ownership is implicit on those types: they are the user's own
self-disclosure by definition. Rows written before that sanitizer, and
rows from lanes that predate it, still carry it. On prod 2026-09-09 the
largest account had 10 such rows (8 preference, 2 goal) with
`value.owner = "Scott"`, against an ego entity named "Scott Guida" whose
alias is "Scott".

WHY IT MATTERS NOW, and it is not tidiness. Recall is about to narrow
the universal-type exemption to rows that are the user's OWN, and the
predicate that decides that is `value.owner` being absent. That is
correct by the write contract and wrong for these ten rows, which would
silently lose their account-wide reach. Normalising them first means the
predicate lands on data that already satisfies the invariant, so nothing
regresses even briefly.

The invariant is the write path's, not a new one. This does not decide
anything; it applies a rule that has been in force at ingest and catches
history up to it.

CONSERVATIVE BY CONSTRUCTION. Only patches whose owner matches the EGO
entity's own name or one of its aliases, case-insensitively, and only
self-typed types. A name that merely looks like the user is left alone:
there is exactly one ego entity per user and it is stamped
(`entities.self_at`), so this never has to guess. If there is no ego
link, it does nothing at all rather than falling back to a heuristic.

`value.owner` is REMOVED rather than set null, matching what the
sanitizer does, so the row becomes byte-identical to one written today.

Dry run by default; --apply writes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

# The types where an absent owner is affirmative rather than unknown,
# because the write path strips a self owner from exactly these.
SELF_TYPED = ("trait", "preference", "goal", "constraint")

EGO_SQL = """
    SELECT e.entity_id, e.name,
           COALESCE(array_agg(a.alias) FILTER (WHERE a.alias IS NOT NULL), '{}') AS aliases
      FROM entities e
      LEFT JOIN entity_aliases a ON a.entity_id = e.entity_id
     WHERE e.user_id = $1 AND e.self_at IS NOT NULL
     GROUP BY e.entity_id, e.name
"""

ROWS_SQL = """
    SELECT cp.patch_id, cp.patch_type, cp.value
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE ps.subject_key = $1
       AND COALESCE(cp.status, 'active') = 'active'
       AND cp.patch_type = ANY($2::text[])
       AND COALESCE(cp.value->>'owner', '') <> ''
     ORDER BY cp.created_at
"""

# `- 'owner'` deletes the key outright. Setting it to null would leave a
# row that is not what the sanitizer produces, and every predicate that
# tests `COALESCE(value->>'owner','') = ''` would still pass, which is
# exactly the kind of near-miss nobody re-reads later.
STRIP_SQL = """
    UPDATE context_patches
       SET value = value - 'owner', updated_at = NOW()
     WHERE patch_id = $1
"""


async def run(dsn: str, user_id: str, apply: bool) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        ego = await conn.fetchrow(EGO_SQL, user_id)
        if ego is None:
            print("no ego entity (entities.self_at) for this user; "
                  "nothing can be resolved, so nothing is done")
            return 0
        names = {(ego["name"] or "").strip().lower()}
        names |= {(a or "").strip().lower() for a in (ego["aliases"] or [])}
        names.discard("")
        print(f"ego: {ego['name']}  names={sorted(names)}")

        rows = await conn.fetch(ROWS_SQL, f"user:{user_id}", list(SELF_TYPED))
        print(f"{len(rows)} self-typed rows carry an owner\n")

        stripped, left = [], []
        for r in rows:
            value = r["value"]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = {}
            owner = (value.get("owner") or "").strip()
            if owner.lower() in names:
                stripped.append((str(r["patch_id"]), r["patch_type"], owner,
                                 (value.get("text") or "")[:58]))
                if apply:
                    await conn.execute(STRIP_SQL, r["patch_id"])
            else:
                left.append((r["patch_type"], owner))

        print(f"would strip (self): {len(stripped)}")
        for pid, ptype, owner, text in stripped:
            print(f"  {pid[:8]}  {ptype:11} owner={owner!r}  {text!r}")
        print(f"\nleft alone (someone else): {len(left)}")
        seen: dict = {}
        for ptype, owner in left:
            seen[(ptype, owner)] = seen.get((ptype, owner), 0) + 1
        for (ptype, owner), n in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"  {ptype:11} {owner!r} x{n}")

        print("\nDRY RUN, nothing written. Re-run with --apply to write."
              if not apply else "\nAPPLIED.")
        return 0
    finally:
        await conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    return asyncio.run(run(dsn, args.user_id, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
