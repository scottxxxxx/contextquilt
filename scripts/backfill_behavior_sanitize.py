"""Archive stored behavior observations the live sanitizer would refuse.

`sanitize_behavior_observations` runs at INGEST, so it protects every
meeting from the day it ships forward and does nothing about what is
already stored. Production held 207 rows owned by a diarization label
and 22 that are a task another stage owns, all written before the rule
existed.

REUSES THE LIVE SANITIZER rather than restating its rules. Each row is
shaped into the content the sanitizer expects and passed through it
one at a time; if it comes back with no patches, the sanitizer refused
it and names the reason. A backfill with its own copy of "what is not
a behavior" is a second source of truth that drifts, and this one would
drift toward deleting rows the extractor is still allowed to write.

ARCHIVES, NEVER DELETES. `status='archived'` plus
`value.archive_cause='cleanup'`, which is the documented vocabulary and
the shape delta sync already carries: the row leaves the client through
the `deleted` array on the next sync rather than vanishing without a
tombstone. A hard delete was the lesson that produced that rule.

`updated_at` IS moved here, unlike the headline backfill. That one was
writing presentation onto a live patch and had to leave the decay clock
alone. This is a lifecycle change: the row is leaving the active set,
and the timestamp is the record of when.

Dry run by default. `--apply` writes.

    python scripts/backfill_behavior_sanitize.py
    python scripts/backfill_behavior_sanitize.py --user <id>
    python scripts/backfill_behavior_sanitize.py --apply
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from contextquilt.services.extraction_schema import (  # noqa: E402
    BEHAVIOR_OBSERVATION_TYPES,
    sanitize_behavior_observations,
)

SELECT = """
    SELECT cp.patch_id::text AS patch_id, cp.patch_type, cp.value
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE COALESCE(cp.status, 'active') = 'active'
       AND cp.patch_type = ANY($1::text[])
"""


def _value(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            out = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return out if isinstance(out, dict) else {}
    return {}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="archive the refused rows (default is a dry run)")
    ap.add_argument("--user", help="restrict to one user_id")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"],
                                     min_size=1, max_size=3)
    sql, params = SELECT, [sorted(BEHAVIOR_OBSERVATION_TYPES)]
    if args.user:
        params.append(f"user:{args.user}")
        sql += f"       AND ps.subject_key = ${len(params)}\n"
    rows = await pool.fetch(sql, *params)
    print(f"{len(rows)} active rows of type "
          f"{sorted(BEHAVIOR_OBSERVATION_TYPES)}")

    refused = []
    retype: list = []
    reasons: Counter = Counter()
    for row in rows:
        value = _value(row["value"])
        # One at a time so the verdict maps back to THIS patch_id. The
        # sanitizer reports the reason but not the id, and matching a
        # batch back by text would collide on duplicates.
        out = sanitize_behavior_observations(
            {"patches": [{"type": row["patch_type"], "value": value}]})
        kept_patches = out.get("patches") or []
        if kept_patches:
            # RETYPED rather than refused. A stated preference has a
            # right home, so the row is converted and the person is
            # attached with `held_by` instead of being archived.
            survivor = kept_patches[0]
            if survivor.get("type") != row["patch_type"]:
                edge = next((e for e in (survivor.get("connects_to") or [])
                             if e.get("label") == "held_by"), None)
                retype.append((row["patch_id"], survivor["type"],
                               (edge or {}).get("target_text"),
                               (value.get("text") or "")[:70]))
            continue
        info = (out.get("_behavior_observations_sanitized") or {})
        dropped = (info.get("dropped") or [{}])[0]
        reason = dropped.get("reason") or "unknown"
        reasons[reason] += 1
        refused.append((row["patch_id"], reason, (value.get("text") or "")[:78],
                        value.get("owner")))

    if args.limit:
        refused = refused[:args.limit]

    print(f"the LIVE sanitizer RETYPES: {len(retype)}")
    for pid, newt, person, text in retype[:10]:
        print(f"  -> {newt}" + (f" held_by {person}" if person else " (self)"))
        print(f"      {text}")
    print(f"the LIVE sanitizer refuses: {len(refused)}")
    print(f"  by reason: {dict(reasons)}")
    print(f"  surviving: {len(rows) - len(refused)}")
    if not refused:
        await pool.close()
        return 0

    print("\nsamples:")
    for pid, reason, text, owner in refused[:12]:
        print(f"  [{reason}] owner={owner!r}")
        print(f"      {text}")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to write.\n")
        await pool.close()
        return 0

    # Retype first: it is the non-destructive half.
    converted = 0
    for pid, newt, person, _text in retype:
        await pool.execute(
            "UPDATE context_patches SET patch_type = $2, updated_at = NOW() "
            " WHERE patch_id = $1 AND COALESCE(status,'active') = 'active'",
            pid, newt)
        if person:
            # Resolve the person patch this preference is held by. Only
            # an EXISTING person patch: inventing one here would create a
            # person from a string, which is the thing the placeholder
            # rules exist to prevent.
            target = await pool.fetchval(
                """SELECT cp.patch_id FROM context_patches cp
                     JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                    WHERE cp.patch_type = 'person'
                      AND COALESCE(cp.status,'active') = 'active'
                      AND lower(cp.value->>'text') = lower($1)
                      AND ps.subject_key = (
                          SELECT subject_key FROM patch_subjects
                           WHERE patch_id = $2 LIMIT 1)
                    LIMIT 1""", person, pid)
            if target:
                await pool.execute(
                    """INSERT INTO patch_connections
                         (from_patch_id, to_patch_id, connection_role,
                          connection_label)
                       VALUES ($1, $2, 'informs', 'held_by')
                       ON CONFLICT DO NOTHING""", pid, target)
        converted += 1
    print(f"RETYPED {converted}")

    done = 0
    for pid, reason, _text, _owner in refused:
        await pool.execute(
            """
            UPDATE context_patches
               SET status = 'archived',
                   updated_at = NOW(),
                   value = jsonb_set(
                       jsonb_set(COALESCE(value, '{}'::jsonb),
                                 '{archive_cause}', '"cleanup"'::jsonb, true),
                       '{archive_detail}', to_jsonb($2::text), true)
             WHERE patch_id = $1
               AND COALESCE(status, 'active') = 'active'
            """,
            pid, reason,
        )
        done += 1
    print(f"\nARCHIVED {done} (archive_cause=cleanup). "
          f"They leave clients through the delta-sync `deleted` array.")
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
