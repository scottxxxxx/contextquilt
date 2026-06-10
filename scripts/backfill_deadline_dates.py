"""
Promote ISO-formatted free-text deadlines to structured deadline_date.

Background
----------

The deadline-structuring change has the extraction LLM resolve spoken
deadlines ("tomorrow", "end of week") into an absolute `value.deadline_date`
(YYYY-MM-DD), anchored to the meeting date. That only covers patches
written after the change ships.

Existing patches carry only free-text `value.deadline`. Most of those
("Monday", "before development") cannot be resolved retroactively — the
anchor meeting date wasn't recorded in the value — but a meaningful slice
are already exact ISO dates ("2024-09-11", "2026-06-19"). Those can be
promoted verbatim.

What it does
------------

For every ACTIVE patch of a completable type (commitment, blocker) that
has a `value.deadline` but no `value.deadline_date`, run the deadline
string through the live `validate_deadline_date` acceptance rules (no
meeting-date plausibility window — these are historical rows). If it
validates, write it into `value.deadline_date`.

Reuses the live validator rather than duplicating the format rules —
guarantees the backfill stays in sync with the forward path.

Read-only by default; `--apply` writes. One transaction per row.

USAGE
-----

    DATABASE_URL='postgres://...' python scripts/backfill_deadline_dates.py
    DATABASE_URL='postgres://...' python scripts/backfill_deadline_dates.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from contextquilt.services.extraction_schema import validate_deadline_date  # noqa: E402

COMPLETABLE_TYPES = ("commitment", "blocker")


async def main(apply: bool) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT patch_id, patch_type, value
              FROM context_patches
             WHERE patch_type = ANY($1::text[])
               AND COALESCE(status, 'active') = 'active'
               AND value->>'deadline' IS NOT NULL
               AND value->>'deadline_date' IS NULL
             ORDER BY created_at
            """,
            list(COMPLETABLE_TYPES),
        )
        print(f"{len(rows)} active {'/'.join(COMPLETABLE_TYPES)} rows with free-text deadline and no deadline_date")

        promoted = 0
        skipped = 0
        for row in rows:
            value = row["value"]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
            if not isinstance(value, dict):
                skipped += 1
                continue

            raw = value.get("deadline")
            normalized = validate_deadline_date(raw)
            if normalized is None:
                skipped += 1
                continue

            print(f"  {row['patch_id']} [{row['patch_type']}] deadline={raw!r} -> deadline_date={normalized}")
            if apply:
                value["deadline_date"] = normalized
                await conn.execute(
                    """
                    UPDATE context_patches
                       SET value = $1::jsonb,
                           updated_at = NOW()
                     WHERE patch_id = $2
                    """,
                    json.dumps(value),
                    row["patch_id"],
                )
            promoted += 1

        mode = "APPLIED" if apply else "DRY RUN (use --apply to write)"
        print(f"\n{mode}: {promoted} promoted, {skipped} skipped (not ISO-parseable)")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
