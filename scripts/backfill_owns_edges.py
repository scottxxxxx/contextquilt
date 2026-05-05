"""
Backfill missing person + owns edges on action-item patches.

Context:
PR #84 added enforce_person_ownership — a safety net that ensures every
commitment/blocker/decision/goal patch with a named human owner has a
matching person patch and a person→action `owns` connection. PR #87
fixed a silent-failure ordering bug (the patch cap was applied AFTER
the enforcer, so synthetic person patches got truncated when extractions
emitted ≥12 patches).

Patches written between PR #84 and PR #87 — and any earlier action items
the enforcer never saw — can have value.owner set to a real human name
without a person patch + owns edge in the graph. This script finds and
optionally fixes those.

Mirrors the wire-format logic in
src/contextquilt/services/extraction_schema.py::enforce_person_ownership
and the storage shape in src/worker.py::store_connected_patches:
    connection_role  = 'informs'
    connection_label = 'owns'
    direction        = person → action

USAGE
-----

Audit only (default):
    DATABASE_URL='postgres://...' python scripts/backfill_owns_edges.py

Scope to one user:
    DATABASE_URL='postgres://...' python scripts/backfill_owns_edges.py \\
        --user-id fa4d903c-24c0-45d5-9fdb-b5496e32501b

Run the writes:
    DATABASE_URL='postgres://...' python scripts/backfill_owns_edges.py --apply

Notes:
- Read-only by default. --apply is required for any DB write.
- Writes are idempotent: ON CONFLICT DO NOTHING for the connection row;
  person patch creation is gated by an exact-text exists-check that
  matches the enforcer's lookup key.
- Skips owners that match the user's profiles.display_name (the (you)
  speaker for that user).
- Skips diarization placeholders ("Speaker N", "Unknown") and the
  literal (you) tokens.
- Sets origin_mode='inferred', source_prompt='backfill_owns_edge' on
  any synthetic person patch so it's distinguishable in audit logs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections import defaultdict
from typing import Any

import asyncpg


# Mirrors extraction_schema._OWNER_PLACEHOLDER_PREFIXES + _OWNER_YOU_TOKENS.
# Kept literal here so the script is independent of the app import path.
_PLACEHOLDER_PREFIXES = ("speaker ", "speaker_", "unknown", "unidentified")
_YOU_TOKENS = frozenset({"(you)", "you", "self", "me", "i"})

# Action-item patch types that carry an owner field.
_ACTION_TYPES = ("commitment", "blocker", "decision", "goal")


def _is_real_person_owner(owner: str | None, display_name: str | None) -> bool:
    """Same predicate used by enforce_person_ownership at extraction time."""
    if not owner:
        return False
    s = owner.strip()
    if not s:
        return False
    low = s.lower()
    if low in _YOU_TOKENS:
        return False
    if any(low.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        return False
    if display_name and low == display_name.strip().lower():
        return False
    return True


# Single audit query. Pulls every active action-item patch with a
# non-empty owner, plus the matching subject_key, plus a sentinel for
# whether (a) a person patch already exists for that owner under the
# same subject_key and (b) an owns edge already lands on the action.
# Filtering of placeholders / (you) / display_name match happens in
# Python so the script is the single source of truth on that logic.
AUDIT_SQL = """
SELECT
    ap.patch_id          AS action_patch_id,
    aps.subject_key      AS subject_key,
    ap.patch_type        AS action_type,
    ap.value->>'text'    AS action_text,
    ap.value->>'owner'   AS owner_text,
    ap.project_id        AS project_id,
    ap.origin_id         AS origin_id,
    ap.origin_type       AS origin_type,
    ap.created_at        AS action_created_at,
    pr.display_name      AS profile_display_name,
    (
        SELECT pp.patch_id
        FROM context_patches pp
        JOIN patch_subjects pps ON pp.patch_id = pps.patch_id
        WHERE pps.subject_key = aps.subject_key
          AND pp.patch_type = 'person'
          AND COALESCE(pp.status, 'active') = 'active'
          AND LOWER(TRIM(pp.value->>'text')) = LOWER(TRIM(ap.value->>'owner'))
        ORDER BY pp.created_at ASC
        LIMIT 1
    )                    AS existing_person_id,
    EXISTS (
        SELECT 1
        FROM patch_connections pc
        JOIN context_patches pp ON pc.from_patch_id = pp.patch_id
        WHERE pc.to_patch_id = ap.patch_id
          AND pc.connection_label = 'owns'
          AND pp.patch_type = 'person'
          AND COALESCE(pp.status, 'active') = 'active'
    )                    AS has_owns_edge
FROM context_patches ap
JOIN patch_subjects aps ON ap.patch_id = aps.patch_id
LEFT JOIN profiles pr
       ON aps.subject_key = 'user:' || pr.user_id
WHERE ap.patch_type = ANY($1::text[])
  AND COALESCE(ap.status, 'active') = 'active'
  AND ap.value ? 'owner'
  AND TRIM(COALESCE(ap.value->>'owner', '')) <> ''
"""


async def _run(database_url: str, user_id: str | None, apply: bool) -> int:
    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(AUDIT_SQL, list(_ACTION_TYPES))

        # Filter in Python (predicates match the live enforcer's logic).
        candidates: list[dict[str, Any]] = []
        skipped_self = 0
        skipped_placeholder = 0
        for r in rows:
            row = dict(r)
            owner = row["owner_text"]
            display_name = row["profile_display_name"]
            if user_id and not row["subject_key"].endswith(user_id):
                continue
            if not _is_real_person_owner(owner, display_name):
                # Distinguish self-skip from placeholder-skip for the report.
                if owner and owner.strip().lower() in _YOU_TOKENS:
                    skipped_placeholder += 1
                elif owner and any(
                    owner.strip().lower().startswith(p)
                    for p in _PLACEHOLDER_PREFIXES
                ):
                    skipped_placeholder += 1
                elif display_name and owner and owner.strip().lower() == display_name.strip().lower():
                    skipped_self += 1
                else:
                    skipped_placeholder += 1
                continue
            if row["has_owns_edge"]:
                continue  # Already wired — nothing to do.
            candidates.append(row)

        # Bucket by what work each candidate needs.
        missing_both = [c for c in candidates if c["existing_person_id"] is None]
        missing_edge_only = [c for c in candidates if c["existing_person_id"] is not None]

        # Group by user for the report.
        by_user: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in candidates:
            by_user[c["subject_key"]].append(c)

        print("=" * 72)
        print("BACKFILL OWNS EDGES — audit report")
        print("=" * 72)
        if user_id:
            print(f"Scope:                  user_id={user_id}")
        else:
            print("Scope:                  all users")
        print(f"Mode:                   {'APPLY' if apply else 'DRY-RUN (no writes)'}")
        print()
        print(f"Action patches scanned: {len(rows)}")
        print(f"Skipped (self-owner):   {skipped_self}")
        print(f"Skipped (placeholder):  {skipped_placeholder}")
        print(f"Already wired:          {len(rows) - skipped_self - skipped_placeholder - len(candidates)}")
        print(f"Candidates to backfill: {len(candidates)}")
        print(f"  - missing person + edge: {len(missing_both)}")
        print(f"  - missing edge only:     {len(missing_edge_only)}")
        print()

        if not candidates:
            print("Nothing to backfill. Exiting.")
            return 0

        # Sample listing — first 25 by created_at desc so the most recent
        # affected patches are visible.
        candidates_sorted = sorted(
            candidates, key=lambda r: r["action_created_at"], reverse=True
        )
        print("Sample candidates (up to 25 most recent):")
        print("-" * 72)
        for c in candidates_sorted[:25]:
            tag = "NEW PERSON" if c["existing_person_id"] is None else "EDGE ONLY"
            text = (c["action_text"] or "").replace("\n", " ")[:70]
            print(
                f"  [{tag}] subject={c['subject_key']}"
                f"  type={c['action_type']}  owner={c['owner_text']!r}"
            )
            print(f"           text={text!r}")
            print(f"           action_id={c['action_patch_id']}  created_at={c['action_created_at']}")
        if len(candidates_sorted) > 25:
            print(f"  ... and {len(candidates_sorted) - 25} more")
        print()

        print("Per-user totals:")
        print("-" * 72)
        for sk, items in sorted(by_user.items(), key=lambda kv: -len(kv[1])):
            print(f"  {sk}: {len(items)} candidate(s)")
        print()

        if not apply:
            print("Dry-run only. Re-run with --apply to write the backfill.")
            return 0

        # ----- WRITES -----
        # One transaction per candidate. Idempotent throughout: person
        # patch insert is gated by a fresh exists-check inside the txn
        # (handles concurrent inserts), connection insert uses the existing
        # ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO NOTHING.
        persons_created = 0
        edges_created = 0
        edges_skipped_dup = 0

        for c in candidates_sorted:
            owner = (c["owner_text"] or "").strip()
            subject_key = c["subject_key"]
            action_id = c["action_patch_id"]
            existing_person_id = c["existing_person_id"]

            async with conn.transaction():
                # Resolve person_id under FOR UPDATE-style guard via re-check.
                if existing_person_id is None:
                    fresh = await conn.fetchrow(
                        """
                        SELECT pp.patch_id
                        FROM context_patches pp
                        JOIN patch_subjects pps ON pp.patch_id = pps.patch_id
                        WHERE pps.subject_key = $1
                          AND pp.patch_type = 'person'
                          AND COALESCE(pp.status, 'active') = 'active'
                          AND LOWER(TRIM(pp.value->>'text')) = LOWER($2)
                        ORDER BY pp.created_at ASC
                        LIMIT 1
                        """,
                        subject_key, owner,
                    )
                    if fresh:
                        person_id = str(fresh["patch_id"])
                    else:
                        person_id = str(uuid.uuid4())
                        patch_name = f"backfill_owns_edge_{person_id[:8]}"
                        value_json = json.dumps({"text": owner})
                        await conn.execute(
                            """
                            INSERT INTO context_patches (
                                patch_id, patch_name, patch_type, value,
                                origin_mode, source_prompt, confidence, persistence,
                                status, created_at, updated_at
                            ) VALUES ($1, $2, 'person', $3,
                                      'inferred', 'backfill_owns_edge', 0.8, 'sticky',
                                      'active', NOW(), NOW())
                            """,
                            person_id, patch_name, value_json,
                        )
                        await conn.execute(
                            "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1::uuid, $2)",
                            person_id, subject_key,
                        )
                        await conn.execute(
                            """
                            INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
                            VALUES ($1::uuid, 1, NOW(), 1.0)
                            """,
                            person_id,
                        )
                        persons_created += 1
                else:
                    person_id = str(existing_person_id)

                # Insert the owns edge. ON CONFLICT covers the rare race
                # where another writer landed it between audit and apply.
                result = await conn.execute(
                    """
                    INSERT INTO patch_connections (
                        from_patch_id, to_patch_id,
                        connection_role, connection_label, context
                    )
                    VALUES ($1::uuid, $2::uuid, 'informs', 'owns', 'backfill_owns_edge')
                    ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO NOTHING
                    """,
                    person_id, action_id,
                )
                # asyncpg returns "INSERT 0 1" on insert, "INSERT 0 0" on conflict skip.
                if result.endswith(" 1"):
                    edges_created += 1
                else:
                    edges_skipped_dup += 1

        print("=" * 72)
        print("BACKFILL COMPLETE")
        print("=" * 72)
        print(f"Person patches created:  {persons_created}")
        print(f"Owns edges created:      {edges_created}")
        print(f"Owns edges already-set:  {edges_skipped_dup}  (race / re-run)")
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit and optionally backfill missing person+owns edges on action-item patches.",
    )
    parser.add_argument(
        "--user-id",
        default=None,
        help="Restrict to a single user_id (matches subject_key suffix).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run the writes. Without this flag the script audits only.",
    )
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL env var is required.", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_run(database_url, args.user_id, args.apply))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
