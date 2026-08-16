#!/usr/bin/env python3
"""Withdraw `what_stands_out` cards that the current rules would not write.

A rule tightening does not reach back: the per-lens durable no means a
card already stamped is never re-derived, so a claim written under looser
rules stays on the person's page forever. This re-runs the CURRENT
ranking against each live card's own stored facts and retracts the ones
that no longer qualify.

Retraction is not deletion and not suppression:

- The row is ARCHIVED, so it reaches clients through the normal delta
  `deleted[]` array rather than vanishing (the tombstone lesson).
- It is stamped `archive_cause = 'retracted'`, which the durable-no check
  skips. A card CQ withdrew is not a user's no, and reading it as one
  would ban that person from the lens forever over a claim they never
  saw.

Dry run by default, like every other backfill here. `--apply` writes.

    python scripts/retract_stands_out_cards.py            # report only
    python scripts/retract_stands_out_cards.py --apply    # write
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from contextquilt.services import relationship_lenses as rl  # noqa: E402
from contextquilt.services.relationship_lenses import (  # noqa: E402
    card_still_qualifies,
)

LIVE_CARDS = """
    SELECT patch_id, value
    FROM context_patches
    WHERE origin_mode = 'derived'
      AND value->>'lens' = $1
      AND COALESCE(status, 'active') = 'active'
    ORDER BY created_at
"""

RETRACT = """
    UPDATE context_patches
    SET status = 'archived',
        updated_at = NOW(),
        value = jsonb_set(value, '{archive_cause}', '"retracted"'::jsonb)
    WHERE patch_id = $1::uuid
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="write the retractions (default is a dry run)")
    args = parser.parse_args()

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(LIVE_CARDS, rl.LENS)
        retract = []
        for row in rows:
            value = row["value"]
            if isinstance(value, str):
                value = json.loads(value)
            facts = value.get("facts") or {}
            ok, why = card_still_qualifies(facts)
            who = value.get("about_person", "?")
            mark = "keep " if ok else "RETRACT"
            print(f"  [{mark}] {who}: {value.get('text', '')!r}")
            print(f"            {facts.get('numerator')}/{facts.get('denominator')}"
                  f" {facts.get('direction')} :: {why}")
            if not ok:
                retract.append(row["patch_id"])

        print(f"\n{len(rows)} live, {len(retract)} to retract")
        if not retract:
            return 0
        if not args.apply:
            print("DRY RUN. Re-run with --apply to write.")
            return 0
        async with conn.transaction():
            for patch_id in retract:
                await conn.execute(RETRACT, str(patch_id))
        print(f"retracted {len(retract)}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
