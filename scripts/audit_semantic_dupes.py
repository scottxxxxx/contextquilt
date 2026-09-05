"""
Find and merge semantically duplicate patches in existing data.

Background
----------

Write-time dedup was trigram-only (similarity > 0.6) until the semantic
gray-zone judge shipped, so same-meaning rephrasings ("Deploy API by
EOW" vs "Ship API before end of week") accumulated as duplicate
patches. This script applies the same LLM judge the forward path now
uses to historical pairs.

What it does
------------

Finds active same-user, same-type, same-project pairs with trigram
similarity in the judge's gray band (and slightly above, to catch
pre-dedup-era textual near-dupes), judges them in batches with the
live DEDUP_JUDGE prompt/schema via the configured Anthropic client,
and reports verdicts. With --apply, for each same_fact pair:

  1. keeps the OLDER patch (the original observation)
  2. repoints the newer patch's connections onto it (collision-safe on
     the unique (from, to, role) key)
  3. copies deadline/deadline_date forward if the older patch lacks them
  4. bumps the older patch's last_observed_at to the newer created_at
  5. archives the newer patch

Each patch participates in at most one merge per run; re-run to
converge. Read-only by default.

USAGE (inside the prod container — needs DATABASE_URL + LLM env)
-----

    python scripts/audit_semantic_dupes.py [--limit 100]
    python scripts/audit_semantic_dupes.py --apply

    # Self-disclosure pass (2026-09-04): trait/preference/goal/constraint,
    # the lower floor, same-owner pairs only, plus pairs that share a cue.
    python scripts/audit_semantic_dupes.py --self-typed --user <user_id>
    python scripts/audit_semantic_dupes.py --self-typed --user <user_id> --apply

Every merge unions the dropped patch's cues into the survivor and stamps
value.duplicate_of on the archived row, so the fold is traceable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from contextquilt.services.semantic_dedup import (  # noqa: E402
    DEDUP_JUDGE_SCHEMA,
    DEDUP_JUDGE_SYSTEM,
    MAX_JUDGE_PAIRS,
    SELF_DISCLOSURE_TYPES,
    SELF_TYPED_DEDUP_FLOOR,
    SEMANTIC_DEDUP_FLOOR,
    build_dedup_judge_content,
    parse_dedup_verdicts,
)

# Texty types where semantic duplication happens. Entity-ish types
# (person/project/org) are handled by entity aliasing, not this.
DEDUP_TYPES = (
    "decision", "commitment", "blocker", "takeaway",
    "goal", "constraint", "event", "deliverable",
    "trait", "preference",
)

# Include slightly-above-threshold pairs: write-time trigram dedup only
# compares NEW patches against existing ones, so older same-era textual
# near-dupes can coexist above 0.6 too.
PAIR_CEILING = 0.99


PAIR_SELECT = """
            SELECT pa.subject_key,
                   a.patch_id AS a_id, a.value->>'text' AS a_text, a.created_at AS a_created,
                   a.value->>'deadline_date' AS a_dd,
                   b.patch_id AS b_id, b.value->>'text' AS b_text, b.created_at AS b_created,
                   b.value->>'deadline' AS b_deadline, b.value->>'deadline_date' AS b_dd,
                   a.patch_type,
                   SIMILARITY(LOWER(a.value->>'text'), LOWER(b.value->>'text')) AS sim
              FROM context_patches a
              JOIN patch_subjects pa ON pa.patch_id = a.patch_id
              JOIN context_patches b
                ON b.patch_type = a.patch_type
               AND b.patch_id > a.patch_id
               AND b.project_id IS NOT DISTINCT FROM a.project_id
              JOIN patch_subjects pb ON pb.patch_id = b.patch_id AND pb.subject_key = pa.subject_key
             WHERE COALESCE(a.status, 'active') = 'active'
               AND COALESCE(b.status, 'active') = 'active'
               AND a.patch_type = ANY($1::text[])
               AND ($5::text IS NULL OR pa.subject_key = $5)
               {EXTRA}
"""

# Pairs that share at least one cue: the wording can sit anywhere below
# the floor and still be one disposition said twice.
CUE_PAIR_EXTRA = """
               AND $2::float >= 0 AND $3::float <= 1
               AND COALESCE(a.value->>'owner', '') = COALESCE(b.value->>'owner', '')
               AND EXISTS (SELECT 1 FROM patch_cues ca JOIN patch_cues cb ON cb.cue = ca.cue
                            WHERE ca.patch_id = a.patch_id AND cb.patch_id = b.patch_id)
             ORDER BY sim DESC
             LIMIT $4
"""

TRIGRAM_PAIR_EXTRA = """
               AND SIMILARITY(LOWER(a.value->>'text'), LOWER(b.value->>'text')) > $2
               AND SIMILARITY(LOWER(a.value->>'text'), LOWER(b.value->>'text')) < $3
               {OWNER}
             ORDER BY sim DESC
             LIMIT $4
"""

OWNER_GUARD = "AND COALESCE(a.value->>'owner', '') = COALESCE(b.value->>'owner', '')"


async def main(apply: bool, limit: int, self_typed: bool = False, user: str | None = None,
               model: str | None = None) -> None:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        sys.exit(1)

    from contextquilt.services.llm_client_anthropic import AnthropicLLMClient
    llm = AnthropicLLMClient()
    # First self-typed dry run (2026-09-05, Haiku, 110 pairs): 40 merges,
    # about 14 of them wrong, including a disposition folded into its
    # opposite. Same lesson as the behavior classifier: Haiku fails a
    # cross-statement semantic call invisibly. Run this pass on Sonnet.
    print(f"judge model: {model or llm.model}")

    types = list(SELF_DISCLOSURE_TYPES) if self_typed else list(DEDUP_TYPES)
    floor = SELF_TYPED_DEDUP_FLOOR if self_typed else SEMANTIC_DEDUP_FLOOR
    subject = f"user:{user}" if user else None

    conn = await asyncpg.connect(dsn)
    try:
        trigram_sql = PAIR_SELECT.replace(
            "{EXTRA}", TRIGRAM_PAIR_EXTRA.replace("{OWNER}", OWNER_GUARD if self_typed else ""))
        pairs = list(await conn.fetch(trigram_sql, types, floor, PAIR_CEILING, limit, subject))
        if self_typed:
            cue_sql = PAIR_SELECT.replace("{EXTRA}", CUE_PAIR_EXTRA)
            seen_ids = {(r["a_id"], r["b_id"]) for r in pairs}
            cue_pairs = [r for r in await conn.fetch(cue_sql, types, floor, PAIR_CEILING, limit, subject)
                         if (r["a_id"], r["b_id"]) not in seen_ids]
            print(f"{len(pairs)} trigram pairs in band {floor}..{PAIR_CEILING}, "
                  f"{len(cue_pairs)} more sharing a cue below it")
            pairs.extend(cue_pairs)
        print(f"{len(pairs)} candidate pairs (band {floor}..{PAIR_CEILING}, capped {limit}, "
              f"types {','.join(types)}{', user ' + user if user else ''})")
        if not pairs:
            return

        # One patch per merge per run
        seen: set = set()
        candidates = []
        for r in pairs:
            if r["a_id"] in seen or r["b_id"] in seen:
                continue
            seen.add(r["a_id"])
            seen.add(r["b_id"])
            candidates.append(r)

        verdicts: list = []
        for i in range(0, len(candidates), MAX_JUDGE_PAIRS):
            batch = candidates[i:i + MAX_JUDGE_PAIRS]
            resp = await llm.extract(
                system_prompt=DEDUP_JUDGE_SYSTEM,
                user_content=build_dedup_judge_content(
                    [(r["a_text"], r["b_text"]) for r in batch]
                ),
                json_schema=DEDUP_JUDGE_SCHEMA,
                model=model,
            )
            verdicts.extend(parse_dedup_verdicts(resp.content, len(batch)))

        merged = 0
        for r, same in zip(candidates, verdicts):
            tag = "MERGE " if same else "keep  "
            print(f"  {tag} [{r['patch_type']}] sim={r['sim']:.2f}")
            print(f"         A: {r['a_text'][:90]}")
            print(f"         B: {r['b_text'][:90]}")
            if not (same and apply):
                merged += 1 if same else 0
                continue

            # Keep the OLDER patch; archive the newer.
            if r["a_created"] <= r["b_created"]:
                keep_id, keep_dd = r["a_id"], r["a_dd"]
                drop_id, drop_deadline, drop_dd = r["b_id"], r["b_deadline"], r["b_dd"]
                newer_created = r["b_created"]
            else:
                keep_id, keep_dd = r["b_id"], r["b_dd"]
                drop_id, drop_deadline, drop_dd = r["a_id"], None, r["a_dd"]
                newer_created = r["a_created"]

            async with conn.transaction():
                for col, other in (("from_patch_id", "to_patch_id"), ("to_patch_id", "from_patch_id")):
                    await conn.execute(
                        f"""
                        UPDATE patch_connections pc SET {col} = $1
                         WHERE pc.{col} = $2
                           AND NOT EXISTS (
                               SELECT 1 FROM patch_connections pc2
                                WHERE pc2.{col} = $1
                                  AND pc2.{other} = pc.{other}
                                  AND pc2.connection_role = pc.connection_role
                           )
                        """,
                        keep_id, drop_id,
                    )
                await conn.execute(
                    "DELETE FROM patch_connections WHERE from_patch_id = $1 OR to_patch_id = $1",
                    drop_id,
                )
                await conn.execute(
                    "DELETE FROM patch_connections WHERE from_patch_id = $1 AND to_patch_id = $1",
                    keep_id,
                )
                if drop_dd and not keep_dd:
                    await conn.execute(
                        """
                        UPDATE context_patches
                           SET value = jsonb_set(
                                   jsonb_set(value, '{deadline_date}', to_jsonb($1::text)),
                                   '{deadline}', to_jsonb($2::text)
                               )
                         WHERE patch_id = $3 AND value->>'deadline_date' IS NULL
                        """,
                        drop_dd, drop_deadline or drop_dd, keep_id,
                    )
                await conn.execute(
                    """
                    UPDATE context_patches
                       SET last_observed_at = GREATEST(last_observed_at, $1),
                           updated_at = NOW()
                     WHERE patch_id = $2
                    """,
                    newer_created, keep_id,
                )
                # The dropped row's cues follow the fact, exactly as the
                # live re-observation path unions them.
                await conn.execute(
                    """
                    INSERT INTO patch_cues (patch_id, cue)
                    SELECT $1, cue FROM patch_cues WHERE patch_id = $2
                    ON CONFLICT DO NOTHING
                    """,
                    keep_id, drop_id,
                )
                await conn.execute(
                    "UPDATE context_patches SET status = 'archived', updated_at = NOW(), "
                    "value = jsonb_set(jsonb_set(value, '{archive_cause}', '\"dedup\"'), "
                    "'{duplicate_of}', to_jsonb($2::text)) WHERE patch_id = $1",
                    drop_id, str(keep_id),
                )
            merged += 1

        mode = "APPLIED" if apply else "DRY RUN (use --apply to write)"
        print(f"\n{mode}: {merged} same_fact of {len(candidates)} judged pairs")
    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write merges (default: dry run)")
    parser.add_argument("--limit", type=int, default=100, help="max candidate pairs per run")
    parser.add_argument("--self-typed", action="store_true",
                        help="trait/preference/goal/constraint only: the lower floor, same-owner "
                             "pairs, plus pairs sharing a cue")
    parser.add_argument("--user", help="restrict to one user_id")
    parser.add_argument("--model", help="judge model override (use Sonnet for the self-typed pass)")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.limit, self_typed=args.self_typed, user=args.user,
                     model=args.model))
