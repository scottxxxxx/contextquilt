"""
Strip literal "(you)" leftovers from existing patches.

Background
----------

The "(you)" marker is a transcript-level identification convention —
it tells the extraction prompt which speaker is the app user, and
should never reach stored patch text or owner fields. PR #43 +
extraction_schema.sanitize_you_marker_from_patches scrub it on the
forward path. PR #43 also shipped a one-off voice-rewrite script
(scripts/backfill_voice_cleanup.py) that LLM-rewrote pre-existing
patches into clean second-person voice.

That backfill cleaned `value.text` but left `value.owner` alone, so
4 active rows on prod (as of 2026-05-11) still carry "(you)" — three
pre-#43 commitments/projects with `owner = "Scott (you)"` and one
2026-05-05 person patch with `value.text = "Scott (you)"`. GP is
about to start a canary on retiring their render-time `(you)`-suffix
regex; we want a clean baseline first.

What it does
------------

For every active patch, run the live `sanitize_you_marker_from_patches`
function. If it changes either field, UPDATE the row to the cleaned
value. Read-only by default; `--apply` writes. One transaction per row.

Reuses the live sanitizer rather than duplicating the substitution
logic — guarantees the backfill stays in sync with any future tweak.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/backfill_strip_you_marker.py
    DATABASE_URL='postgres://...' python scripts/backfill_strip_you_marker.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from contextquilt.services.extraction_schema import (  # noqa: E402
    sanitize_you_marker_from_patches,
)

FIND_SQL = """
SELECT patch_id, value
FROM context_patches
WHERE COALESCE(status, 'active') = 'active'
  AND (value->>'text' LIKE '%(you)%' OR value->>'owner' LIKE '%(you)%')
"""

UPDATE_SQL = """
UPDATE context_patches
   SET value = $1::jsonb,
       updated_at = NOW()
 WHERE patch_id = $2
"""


def _sanitize_value(value: dict) -> dict | None:
    """Return the cleaned value iff the sanitizer would change it,
    otherwise None."""
    content = {"patches": [{"value": value}]}
    sanitize_you_marker_from_patches(content)
    new_value = content["patches"][0]["value"]
    # Compare on the two fields the sanitizer touches.
    if (new_value.get("text") != value.get("text")) or (
        new_value.get("owner") != value.get("owner")
    ):
        return new_value
    return None


async def run(database_url: str, apply: bool) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(FIND_SQL)
        candidates: list[tuple[str, dict, dict]] = []
        for r in rows:
            value = r["value"]
            if isinstance(value, str):
                value = json.loads(value)
            if not isinstance(value, dict):
                continue
            new_value = _sanitize_value(value)
            if new_value is None:
                continue
            candidates.append((str(r["patch_id"]), value, new_value))

        print("=" * 72)
        print("BACKFILL STRIP (you) MARKER — audit report")
        print("=" * 72)
        print(f"Rows containing '(you)': {len(rows)}")
        print(f"Mode:                    {'APPLY' if apply else 'DRY-RUN'}")
        print(f"Candidates to clean:     {len(candidates)}")
        print()

        if not candidates:
            print("Nothing to clean. Exiting.")
            return 0

        for pid, old, new in candidates:
            print(f"  {pid}")
            for field in ("text", "owner"):
                if old.get(field) != new.get(field):
                    print(f"    {field}: {old.get(field)!r}  →  {new.get(field)!r}")

        if not apply:
            print()
            print("Dry-run only. Re-run with --apply to write the cleanup.")
            return 0

        for pid, _old, new in candidates:
            async with conn.transaction():
                await conn.execute(UPDATE_SQL, json.dumps(new), pid)
        print()
        print(f"Updated {len(candidates)} row(s).")
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
