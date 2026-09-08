#!/usr/bin/env python3
"""Repair or archive `role` patches that never recorded whose role it is.

WHAT THIS IS FOR. Scott opened a project quilt on 2026-09-07 and read a
tile that said "Lead developer" with no name on it. The stored value was
the whole thing: {"text": "Lead developer", "headline": "Lead
developer"}, no owner, no held_by, and exactly one edge, to the PROJECT.
Measured across his account: of 77 active `role` patches, 15 had any
link to a person and 62 had none by any route, 61 of those connecting
only to a project.

`enforce_role_holder` stops new ones (worker chain, 2026-09-07). This is
the repair for the rows already stored. Forward fix and history cleanup
are separate on purpose: the sanitizer must hold even if this script is
never run twice, and this script must not be the thing that defines the
rule.

IT REUSES THE LIVE SANITIZER RATHER THAN REIMPLEMENTING IT. Each row is
rebuilt into the extraction content shape and handed to
`enforce_role_holder`, so the decision here is the same code that runs
at ingest. A second copy of the rule would drift from it, and the copy
that drifts is the one nobody re-reads.

CANDIDATE HOLDERS COME FROM THE SAME MEETING, NOT THE WHOLE ACCOUNT.
This is deliberate and it is the difference between a repair and a
fabrication. Name matching across the account would walk straight into
the same-name fan-out already known on this data (68 people across 29
groups sharing a first name), and attach a role to whichever Alex sorted
first. Restricting candidates to person patches from the role's own
meeting reproduces exactly the context the extraction had.

OUTCOMES, and only these three:
  repaired  a holder was recoverable from the row itself (held_by /
            owner) or from a person named at the start of the role text
            who was present in the same meeting. A `describes` edge is
            written to that person's patch.
  archived  no holder exists anywhere. status='archived',
            value.archive_cause='cleanup'. Never hard-deleted: the
            delta's `deleted[]` is computed FROM archived rows, so a
            hard delete would stop other devices ever learning it went.
  skipped   already has a describes edge, or has no text.

Dry run is the default AND is the measurement. --apply writes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from contextquilt.services.extraction_schema import enforce_role_holder  # noqa: E402

# Active roles, with everything needed to decide, plus whether any edge
# already reaches a person. The person leg is a LEFT JOIN rather than a
# filter so the script reports what it skipped as well as what it fixed.
ROLES_SQL = """
    SELECT cp.patch_id, cp.value, cp.origin_id, cp.origin_type,
           cp.project_id, ps.subject_key,
           EXISTS (
             SELECT 1 FROM patch_connections pc
             JOIN context_patches o ON o.patch_id = CASE
                    WHEN pc.from_patch_id = cp.patch_id THEN pc.to_patch_id
                    ELSE pc.from_patch_id END
             WHERE (pc.from_patch_id = cp.patch_id OR pc.to_patch_id = cp.patch_id)
               AND COALESCE(pc.status, 'active') = 'active'
               AND o.patch_type = 'person'
           ) AS has_person_edge
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE cp.patch_type = 'role'
       AND COALESCE(cp.status, 'active') = 'active'
       AND ($1::text IS NULL OR ps.subject_key = $1)
     ORDER BY cp.created_at
"""

# People the SAME MEETING knew about. Scoped to the subject too, because
# a subject_key is the only thing that makes a patch somebody's.
PEOPLE_IN_MEETING_SQL = """
    SELECT DISTINCT cp.patch_id, cp.value->>'text' AS name
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE cp.patch_type = 'person'
       AND COALESCE(cp.status, 'active') = 'active'
       AND ps.subject_key = $1
       AND cp.origin_id = $2
"""

ARCHIVE_SQL = """
    UPDATE context_patches
       SET status = 'archived', updated_at = NOW(),
           value = jsonb_set(value, '{archive_cause}', '"cleanup"')
     WHERE patch_id = $1 AND COALESCE(status, 'active') = 'active'
"""

# The repair edge. `informs` + `describes` is the manifest's own spec for
# role -> person, so the row lands identical to one the extractor would
# have produced correctly.
CONNECT_SQL = """
    INSERT INTO patch_connections
        (from_patch_id, to_patch_id, connection_role, connection_label, status)
    VALUES ($1, $2, 'informs', 'describes', 'active')
    ON CONFLICT DO NOTHING
"""


async def run(dsn: str, subject_key: str | None, apply: bool) -> int:
    conn = await asyncpg.connect(dsn)
    counts: Counter = Counter()
    repaired: list = []
    archived: list = []
    try:
        rows = await conn.fetch(ROLES_SQL, subject_key)
        print(f"{len(rows)} active role patches"
              + (f" for {subject_key}" if subject_key else " (all subjects)"))

        for r in rows:
            value = r["value"]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = {}
            value = value if isinstance(value, dict) else {}
            text = (value.get("text") or "").strip()

            if r["has_person_edge"]:
                counts["skipped_has_person"] += 1
                continue
            if not text:
                counts["skipped_no_text"] += 1
                continue

            people = []
            if r["origin_id"]:
                people = await conn.fetch(
                    PEOPLE_IN_MEETING_SQL, r["subject_key"], r["origin_id"])
            by_name = {
                (p["name"] or "").strip(): p["patch_id"]
                for p in people if (p["name"] or "").strip()
            }

            # THE LIVE SANITIZER MAKES THE DECISION, not this script.
            content = {
                "patches": [
                    {"type": "role", "value": dict(value)},
                    *({"type": "person", "value": {"text": n}} for n in by_name),
                ]
            }
            enforce_role_holder(content)
            audit = content.get("_role_holder_enforced") or {}

            if audit.get("repaired"):
                holder = audit["repaired"][0]["holder"]
                target = by_name.get(holder)
                if target is None:
                    # A holder named by held_by/owner who was not a person
                    # patch in this meeting. Nothing to point an edge at,
                    # and minting a person here would be a second identity
                    # authoring path. Reported, not guessed at.
                    counts["unresolvable_holder"] += 1
                    continue
                counts["repaired"] += 1
                repaired.append((str(r["patch_id"]), text[:70], holder,
                                 audit["repaired"][0]["source"]))
                if apply:
                    await conn.execute(CONNECT_SQL, r["patch_id"], target)
            elif audit.get("dropped"):
                counts["archived"] += 1
                archived.append((str(r["patch_id"]), text[:70]))
                if apply:
                    await conn.execute(ARCHIVE_SQL, r["patch_id"])
            else:
                counts["skipped_other"] += 1
    finally:
        await conn.close()

    print("\n--- outcome ---")
    for k in ("repaired", "archived", "unresolvable_holder",
              "skipped_has_person", "skipped_no_text", "skipped_other"):
        if counts[k]:
            print(f"{k:22} {counts[k]}")

    if repaired:
        print("\nrepaired (role -> person, via):")
        for pid, text, holder, source in repaired[:40]:
            print(f"  {pid[:8]}  {text!r} -> {holder}  ({source})")
    if archived:
        print("\narchived (no holder exists anywhere):")
        for pid, text in archived[:40]:
            print(f"  {pid[:8]}  {text!r}")
        if len(archived) > 40:
            print(f"  ... and {len(archived) - 40} more")

    print("\nDRY RUN, nothing written. Re-run with --apply to write."
          if not apply else "\nAPPLIED.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subject-key", help="e.g. user:<uuid>; omit for all")
    ap.add_argument("--apply", action="store_true",
                    help="write; default is a dry run")
    args = ap.parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    return asyncio.run(run(dsn, args.subject_key, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
