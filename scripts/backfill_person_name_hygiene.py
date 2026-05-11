"""
Clean up stale `person` patches whose `value.text` contains prose
(written before the prompt + sanitizer hardening for name-only person
fields).

Background
----------

The original LLM extraction prompt told the model to format person
patches as ``"<Name> — <what they handle>"`` — context-in-the-name.
That produced 7 such rows on prod, all dated 2026-04-17 through
2026-04-22, with shapes like:

    "Christina - customer success point of contact for ..."
    "Santosh is a developer working for Morgan Stanley ..."
    "Speaker 5, AI tool operator and technical interview practice partner"

The prompt has been rewritten (PR follow-up) to make value.text a name
only, and the worker now runs ``strip_prose_from_person_names`` as a
belt-and-suspenders sanitizer. This script applies the same sanitizer
rule to existing rows.

What it does
------------

Reuses the live ``strip_prose_from_person_names`` function on every
active person patch. If the function would change value.text, the row
is updated to the cleaned name. Read-only by default; ``--apply``
required for writes. One transaction per row.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/backfill_person_name_hygiene.py
    DATABASE_URL='postgres://...' python scripts/backfill_person_name_hygiene.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

# Make src/ importable so we can reuse the live sanitizer exactly as
# the worker runs it. Avoids duplicating the separator list here and
# guarantees the backfill stays in sync with future sanitizer tweaks.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from contextquilt.services.extraction_schema import (  # noqa: E402
    strip_prose_from_person_names,
)

FIND_SQL = """
SELECT patch_id, value
FROM context_patches
WHERE patch_type = 'person'
  AND COALESCE(status, 'active') = 'active'
"""

UPDATE_SQL = """
UPDATE context_patches
   SET value = $1::jsonb,
       updated_at = NOW()
 WHERE patch_id = $2
"""


def _cleaned_text(text: str) -> str | None:
    """Return the cleaned name iff the sanitizer would change it,
    otherwise None."""
    content = {"patches": [{"type": "person", "value": {"text": text}}]}
    strip_prose_from_person_names(content)
    new_text = content["patches"][0]["value"]["text"]
    return new_text if new_text != text else None


async def run(database_url: str, apply: bool) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(FIND_SQL)
        candidates: list[tuple[str, str, str, dict]] = []
        for r in rows:
            value = r["value"]
            if isinstance(value, str):
                value = json.loads(value)
            text = value.get("text") if isinstance(value, dict) else None
            if not isinstance(text, str):
                continue
            cleaned = _cleaned_text(text)
            if cleaned is None:
                continue
            candidates.append((str(r["patch_id"]), text, cleaned, value))

        print("=" * 72)
        print("BACKFILL PERSON NAME HYGIENE — audit report")
        print("=" * 72)
        print(f"Active person patches scanned: {len(rows)}")
        print(f"Mode:                          {'APPLY' if apply else 'DRY-RUN'}")
        print(f"Candidates to clean:           {len(candidates)}")
        print()

        if not candidates:
            print("Nothing to clean. Exiting.")
            return 0

        for pid, old, new, _value in candidates:
            print(f"  {pid}")
            print(f"    old: {old!r}")
            print(f"    new: {new!r}")

        if not apply:
            print()
            print("Dry-run only. Re-run with --apply to write the cleanup.")
            return 0

        for pid, _old, new, value in candidates:
            new_value = {**value, "text": new}
            async with conn.transaction():
                await conn.execute(UPDATE_SQL, json.dumps(new_value), pid)
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
