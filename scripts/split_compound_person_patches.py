"""
Split slash-joined `person` patches that were created before the
extraction enforcer learned to split compound owners (PR #92,
2026-05-05).

Background
----------

The 2026-05-03 `backfill_owns_edges.py` pass saw raw owner strings like
``"Zephyra/Yardley"`` on commitment patches and created one synthetic person
patch with that whole string as the `value.text`, then wired a single
`owns` edge from it to the action. PR #92 added `_split_compound_owner`
to the live enforcer, so any new compound owner ("Marlowe/Quill") is now
split into two person patches with two separate `owns` edges. But the
stale rows from the 2026-05-03 backfill remain as compound persons — in
the dashboard a person card titled "Zephyra/Yardley" reads as one entity.

This script:

    1. Finds person patches whose `value.text` is a slash-joined string
       of single-word capitalized names (the conservative shape that
       _split_compound_owner targets — same name-validation heuristic
       applied here to avoid splitting unrelated strings).
    2. For each compound P:
       - Splits the name into its parts using the same split-on-`/`
         rule used by the enforcer.
       - For each part: finds an existing person patch with the same
         subject_key and matching name (case-insensitive); creates a
         new one otherwise.
       - Copies P's outgoing `owns` edges so each split person has the
         same edges P had (ON CONFLICT keeps it idempotent).
       - Deletes P (its `owns` edges cascade away).

Read-only by default. `--apply` is required for any DB write. One
transaction per compound — partial failures don't half-split a patch.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/split_compound_person_patches.py
    DATABASE_URL='postgres://...' python scripts/split_compound_person_patches.py --apply
    DATABASE_URL='...' python scripts/split_compound_person_patches.py --user-id <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from collections import defaultdict

import asyncpg


# Mirrors src/contextquilt/services/extraction_schema._split_compound_owner —
# inlined to keep the script standalone.
def split_compound_owner(owner_text: str | None) -> list[str]:
    if not owner_text:
        return []
    s = owner_text.strip()
    if not s or "/" not in s:
        return [s] if s else []
    return [p.strip() for p in s.split("/") if p.strip()]


# A compound-person row, for our purposes, is a slash-joined string where
# every part is a single capitalized word (no spaces, no prose). Anything
# else (sentences, URLs, paths) is left alone. The pattern accepts optional
# whitespace around the slash so "Windmere / Corwin" matches as well as
# "Zephyra/Yardley".
_LOOKS_LIKE_COMPOUND_NAME = re.compile(
    r"^[A-Z][a-zA-Z'-]+(?:\s*/\s*[A-Z][a-zA-Z'-]+)+$"
)


def is_compound_person_name(text: str | None) -> bool:
    if not text:
        return False
    return bool(_LOOKS_LIKE_COMPOUND_NAME.match(text.strip()))


FIND_COMPOUND_SQL = """
SELECT
    p.patch_id,
    p.value->>'text'  AS compound_name,
    p.project_id,
    p.project,
    ps.subject_key
FROM context_patches p
JOIN patch_subjects ps ON ps.patch_id = p.patch_id
WHERE p.patch_type = 'person'
  AND COALESCE(p.status, 'active') = 'active'
  AND p.value->>'text' LIKE '%/%'
"""

GET_OUTGOING_OWNS_SQL = """
SELECT to_patch_id, connection_role, connection_label, context
FROM patch_connections
WHERE from_patch_id = $1 AND connection_label = 'owns'
"""

FIND_EXISTING_PERSON_SQL = """
SELECT pp.patch_id
FROM context_patches pp
JOIN patch_subjects pps ON pp.patch_id = pps.patch_id
WHERE pps.subject_key = $1
  AND pp.patch_type = 'person'
  AND COALESCE(pp.status, 'active') = 'active'
  AND LOWER(TRIM(pp.value->>'text')) = LOWER($2)
ORDER BY pp.created_at ASC
LIMIT 1
"""

INSERT_PERSON_SQL = """
INSERT INTO context_patches (
    patch_id, patch_name, patch_type, value,
    origin_mode, source_prompt, confidence, persistence,
    status, project, project_id, created_at, updated_at
) VALUES ($1, $2, 'person', $3,
          'inferred', 'split_compound_person', 0.8, 'sticky',
          'active', $4, $5, NOW(), NOW())
"""

INSERT_SUBJECT_SQL = (
    "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1::uuid, $2)"
)

INSERT_USAGE_SQL = """
INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
VALUES ($1::uuid, 1, NOW(), 1.0)
"""

COPY_EDGE_SQL = """
INSERT INTO patch_connections (from_patch_id, to_patch_id, connection_role, connection_label, context)
VALUES ($1::uuid, $2::uuid, $3, 'owns', $4)
ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO NOTHING
"""

DELETE_COMPOUND_SQL = "DELETE FROM context_patches WHERE patch_id = $1"


async def run(database_url: str, user_id: str | None, apply: bool) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(FIND_COMPOUND_SQL)

        candidates: list[dict] = []
        for r in rows:
            name = r["compound_name"]
            if not is_compound_person_name(name):
                continue
            if user_id and not r["subject_key"].endswith(user_id):
                continue
            parts = split_compound_owner(name)
            if len(parts) < 2:
                continue
            candidates.append(
                {
                    "patch_id": r["patch_id"],
                    "compound_name": name,
                    "parts": parts,
                    "subject_key": r["subject_key"],
                    "project_id": r["project_id"],
                    "project": r["project"],
                }
            )

        print("=" * 72)
        print("SPLIT COMPOUND PERSON PATCHES — audit report")
        print("=" * 72)
        print(f"Scope:                  {user_id or 'all users'}")
        print(f"Mode:                   {'APPLY' if apply else 'DRY-RUN'}")
        print(f"Compound persons found: {len(candidates)}")
        print()

        if not candidates:
            print("Nothing to split. Exiting.")
            return 0

        per_user: dict[str, int] = defaultdict(int)
        for c in candidates:
            per_user[c["subject_key"]] += 1
            edges = await conn.fetch(GET_OUTGOING_OWNS_SQL, c["patch_id"])
            c["edges"] = [dict(e) for e in edges]
            print(
                f"  {c['compound_name']!r}  → {c['parts']}"
                f"  subject={c['subject_key']}  edges={len(edges)}"
            )

        print()
        print("Per-user totals:")
        for sk, n in sorted(per_user.items(), key=lambda kv: -kv[1]):
            print(f"  {sk}: {n} compound person(s)")
        print()

        if not apply:
            print("Dry-run only. Re-run with --apply to write the split.")
            return 0

        persons_created = 0
        persons_reused = 0
        edges_created = 0
        compounds_deleted = 0

        for c in candidates:
            compound_id = c["patch_id"]
            subject_key = c["subject_key"]
            edges = c["edges"]

            async with conn.transaction():
                new_person_ids: list[str] = []
                for part in c["parts"]:
                    existing = await conn.fetchrow(
                        FIND_EXISTING_PERSON_SQL, subject_key, part
                    )
                    if existing:
                        new_person_ids.append(str(existing["patch_id"]))
                        persons_reused += 1
                        continue

                    new_id = str(uuid.uuid4())
                    patch_name = f"split_compound_{new_id[:8]}"
                    value_json = json.dumps({"text": part})
                    await conn.execute(
                        INSERT_PERSON_SQL,
                        new_id,
                        patch_name,
                        value_json,
                        c["project"],
                        c["project_id"],
                    )
                    await conn.execute(INSERT_SUBJECT_SQL, new_id, subject_key)
                    await conn.execute(INSERT_USAGE_SQL, new_id)
                    new_person_ids.append(new_id)
                    persons_created += 1

                for new_pid in new_person_ids:
                    for edge in edges:
                        result = await conn.execute(
                            COPY_EDGE_SQL,
                            new_pid,
                            str(edge["to_patch_id"]),
                            edge["connection_role"],
                            edge["context"],
                        )
                        # asyncpg returns "INSERT 0 N" — N=0 means ON CONFLICT skipped.
                        if result.endswith(" 1"):
                            edges_created += 1

                await conn.execute(DELETE_COMPOUND_SQL, compound_id)
                compounds_deleted += 1

            print(
                f"  ✓ split {c['compound_name']!r} → "
                f"{len(c['parts'])} person(s), {len(new_person_ids) * len(edges)} edges targeted"
            )

        print()
        print("-" * 72)
        print("Summary:")
        print(f"  compounds split:    {compounds_deleted}")
        print(f"  new persons:        {persons_created}")
        print(f"  reused persons:     {persons_reused}")
        print(f"  new owns edges:     {edges_created}")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id",
        help="Scope to a single user's subject_key (matches 'user:<id>' suffix).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes. Without this flag the script is read-only.",
    )
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL not set.", file=sys.stderr)
        return 1
    return asyncio.run(run(database_url, args.user_id, args.apply))


if __name__ == "__main__":
    sys.exit(main())
