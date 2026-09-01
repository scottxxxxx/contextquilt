"""
Write the tile headline for patches stored before the lane existed.

Background
----------

Woven handoff section 6.3: the `fact` is the record and the `headline` is
the one line a tile can hold. It is WRITTEN rather than truncated,
because cutting a fact at 48 characters puts a visibly broken sentence on
the most prominent text in the product.

The worker lane fills FORWARD only. On the day it ships, every patch
already in the quilt has no headline, so the home screen renders raw fact
text for a user whose entire history predates the lane. That is the whole
gap this closes: 4,625 active patches at the time of writing, priced at
about $0.48 as a one-off.

What it does
------------

Selects active patches with no headline, filters them through the LIVE
`woven_digest.why_not_a_tile`, and asks one batched call per 25 for a
line each. Every answer goes through the LIVE `headlines.why_invalid`,
which REFUSES rather than repairs, because every repair available is a
truncation and that is the exact thing 6.3 forbids.

Reusing both live predicates is the point rather than tidiness. A
backfill with its own copy of "what can tile" or "what is a valid
headline" is a second source of truth that drifts silently, and this one
would drift toward writing headlines nobody ever sees.

Conservative by construction:

  - **Only what can tile.** `why_not_a_tile` decides, not this script. A
    patch it drops is never paid for.
  - **Refusal is free.** A line that breaks a rule is counted and
    discarded. The patch keeps no headline, and null is a real served
    state that the client already falls back from.
  - **Never overwrites.** The query selects rows with no headline, so a
    re-run cannot replace a line that is already there, and an
    interrupted run resumes rather than restarting.
  - **`updated_at` is not moved.** There is no trigger on this table, so
    it moves only if set. A headline is presentation, not a new
    observation, and types that are neither self-typed nor completable
    anchor their decay on `updated_at`: stamping it here would silently
    extend the life of every patch this touched. Bulk-shifting the decay
    clock of the entire quilt is a far larger act than writing a label.

Dry run by default. `--apply` writes.

    python scripts/backfill_headlines.py                  # report only
    python scripts/backfill_headlines.py --limit 200
    python scripts/backfill_headlines.py --apply
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from contextquilt.services import headlines, woven_digest  # noqa: E402
from contextquilt.services.llm_client_anthropic import (  # noqa: E402
    AnthropicLLMClient,
)

BATCH = 25


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write headlines (default is a dry run)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the number of patches considered, 0 = all")
    ap.add_argument("--user", help="restrict to one user_id")
    args = ap.parse_args()

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"],
                                     min_size=1, max_size=3)
    sql, params = headlines.build_pending_fetch(
        subject_key=f"user:{args.user}" if args.user else None)
    rows = await pool.fetch(sql, *params)

    # The live predicate decides, not this script.
    skipped: Counter = Counter()
    candidates = []
    for r in rows:
        patch = dict(r)
        # The WRITER's question: could this earn a tile if it had a
        # line. Asking the reader's question here selects nothing,
        # because every candidate lacks a headline by construction.
        reason = woven_digest.why_not_a_tile(patch, require_headline=False)
        if reason:
            skipped[reason] += 1
            continue
        candidates.append(patch)

    if args.limit:
        candidates = candidates[:args.limit]

    print(f"{len(rows)} patches without a headline")
    print(f"  cannot tile, never paid for: {dict(skipped)}")
    print(f"  eligible: {len(candidates)}"
          + (f" (capped at {args.limit})" if args.limit else ""))
    if not candidates:
        await pool.close()
        return 0

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to write.\n")

    # Free ones first: a fact that is already a valid headline needs no
    # model call and cannot be refused. It also shrinks every batch.
    free, candidates = headlines.partition_by_self_headline(candidates)
    print(f"  already valid as their own headline: {len(free)} "
          f"(no model call)")
    print(f"  needing a written line:              {len(candidates)}\n")

    llm = AnthropicLLMClient()
    written = 0
    recovered_total = 0
    refused: Counter = Counter()
    cost = 0.0
    samples: list = []

    for start in range(0, len(candidates), BATCH):
        batch = candidates[start:start + BATCH]
        try:
            response = await llm.extract(
                system_prompt=headlines.SYSTEM,
                user_content=headlines.build_user_content(batch),
            )
        except Exception as exc:
            # One bad batch must not cost the rest of the run.
            print(f"  batch at {start} failed: {str(exc)[:160]}")
            continue
        cost += float(getattr(response, "cost_usd", 0.0) or 0.0)
        out = headlines.parse(response.content, batch)

        # One second pass over the lines that broke a rule, shown their
        # own attempt and its actual length. Measured on the residue:
        # 16% on a single pass, 52% after this. A REWRITE, not a repair.
        if out["retryable"]:
            try:
                retry = await llm.extract(
                    system_prompt=headlines.RETRY_SYSTEM,
                    user_content=headlines.build_retry_content(
                        out["retryable"], batch),
                )
                cost += float(getattr(retry, "cost_usd", 0.0) or 0.0)
                merged = headlines.apply_retry(
                    out, headlines.parse(retry.content, batch))
                recovered_total += merged["recovered"]
                out["headlines"] = merged["headlines"]
                refused.update(merged["refused"])
            except Exception as exc:
                print(f"  retry at {start} failed: {str(exc)[:120]}")
                refused.update(out["refused"])
        else:
            refused.update(out["refused"])

        by_id = {str(p["patch_id"]): p for p in batch}
        for pid, line in out["headlines"].items():
            if len(samples) < 12:
                fact = woven_digest._text(by_id[pid]) if pid in by_id else ""
                samples.append((line, fact))
            if args.apply:
                await pool.execute(
                    """
                    UPDATE context_patches
                       SET value = jsonb_set(
                             COALESCE(value, '{}'::jsonb),
                             '{headline}', to_jsonb($2::text), true)
                     WHERE patch_id = $1
                    """,
                    pid, line,
                )
            written += 1
        print(f"  {start + len(batch)}/{len(candidates)} considered, "
              f"{written} headlines, {sum(refused.values())} refused")

    if args.apply:
        for pid, line in free.items():
            await pool.execute(
                """
                UPDATE context_patches
                   SET value = jsonb_set(COALESCE(value, '{}'::jsonb),
                         '{headline}', to_jsonb($2::text), true)
                 WHERE patch_id = $1
                """, pid, line)
    written += len(free)
    print(f"\n{'WROTE' if args.apply else 'WOULD WRITE'} {written} headlines "
          f"({len(free)} taken from the fact, no model call)")
    print(f"refused after retry: {dict(refused) or 'none'}")
    print(f"recovered by the second pass: {recovered_total}")
    print(f"cost: ${cost:.2f}")
    print("\nsamples (headline <- fact):")
    for line, fact in samples:
        print(f"  {line!r:52} <- {fact[:70]}")

    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
