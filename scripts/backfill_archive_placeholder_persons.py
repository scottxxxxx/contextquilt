"""
Archive placeholder + self-reference person patches that were created
before the worker sanitizer (PR introducing drop_placeholder_and_self_person_patches)
existed.

Background
----------

Two shapes of person patch should never have been written but were:

  - **Placeholders**: ``"Speaker 5"``, ``"Unknown"``, ``"Unidentified
    caller"``. The LLM emitted them when a transcript only carried
    diarized labels with no real names attached. They show up in the
    dashboard as person cards titled "Speaker 5" — visually noisy,
    useless for recall.

  - **Self-reference**: a ``person`` patch named the same as the
    (you) speaker (e.g. ``"Scott"`` in Scott's quilt). The user's
    own attribution is implicit via ``subject_key``; a third-party-
    style person patch about themselves is a duplicate.

``enforce_person_ownership`` already skips both shapes when synthesizing
person patches (via ``_is_real_person_owner``), and the new
``drop_placeholder_and_self_person_patches`` sanitizer catches them on
the forward path when the LLM emits them directly. This script
backfills the existing rows.

What it does
------------

Archives (sets ``status = 'archived'``) every active ``person`` patch
that:

  - matches a placeholder prefix (``"speaker "``, ``"speaker_"``,
    ``"unknown"``, ``"unidentified"``), OR
  - has ``value.text`` (case-insensitive, trimmed) equal to its
    subject user's ``profiles.display_name``.

Archiving (not deletion) keeps outgoing edges in ``patch_connections``
intact; the patch is just hidden from recall and from
``active``-filtered queries. Easily reversible by setting
``status='active'`` again.

Read-only by default; ``--apply`` writes. One transaction per row.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/backfill_archive_placeholder_persons.py
    DATABASE_URL='postgres://...' python scripts/backfill_archive_placeholder_persons.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

# Same prefixes as src/contextquilt/services/extraction_schema._OWNER_PLACEHOLDER_PREFIXES.
# Kept locally to avoid pulling in the whole worker import graph.
_PLACEHOLDER_PREFIXES = ("speaker ", "speaker_", "unknown", "unidentified")


FIND_SQL = """
SELECT
    cp.patch_id,
    cp.value->>'text' AS name,
    ps.subject_key,
    p.display_name
FROM context_patches cp
JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
LEFT JOIN profiles p ON p.user_id = SUBSTRING(ps.subject_key FROM 'user:(.+)')
WHERE cp.patch_type = 'person'
  AND COALESCE(cp.status, 'active') = 'active'
"""

ARCHIVE_SQL = """
UPDATE context_patches
   SET status = 'archived',
       updated_at = NOW()
 WHERE patch_id = $1
"""


def _classify(name: str | None, display_name: str | None) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return None
    low = name.strip().lower()
    if any(low.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        return "placeholder"
    if isinstance(display_name, str) and display_name.strip():
        if low == display_name.strip().lower():
            return "self_reference"
    return None


async def run(database_url: str, apply: bool) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(FIND_SQL)
        candidates: list[tuple[str, str, str, str]] = []
        for r in rows:
            category = _classify(r["name"], r["display_name"])
            if category is None:
                continue
            candidates.append(
                (str(r["patch_id"]), r["name"], r["subject_key"], category)
            )

        print("=" * 72)
        print("ARCHIVE PLACEHOLDER + SELF-REFERENCE PERSON PATCHES — audit")
        print("=" * 72)
        print(f"Active person patches scanned: {len(rows)}")
        print(f"Mode:                          {'APPLY' if apply else 'DRY-RUN'}")
        print(f"Candidates to archive:         {len(candidates)}")
        print()

        if not candidates:
            print("Nothing to archive. Exiting.")
            return 0

        for pid, name, subject_key, category in sorted(
            candidates, key=lambda c: (c[3], c[1].lower())
        ):
            print(f"  [{category:14}] {name!r}  subject={subject_key}  id={pid}")

        if not apply:
            print()
            print("Dry-run only. Re-run with --apply to archive.")
            return 0

        for pid, _name, _sk, _cat in candidates:
            async with conn.transaction():
                await conn.execute(ARCHIVE_SQL, pid)
        print()
        print(f"Archived {len(candidates)} row(s).")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
    return asyncio.run(run(database_url, args.apply))


if __name__ == "__main__":
    sys.exit(main())
