#!/usr/bin/env python3
"""Where do extracted patches disappear, and does transcript length explain it?

WHAT THIS FILE REPLACES, because the mistake is worth keeping. The
first version reconstructed the answer from `context_patches`: a
meeting whose only patches are `behavior` was called one where the main
extraction produced nothing, since the behavior lane is a separate call
(doc 19.5). That reconstruction is INVALID. Six patch types are
ORIGIN-NULL BY DESIGN (see the origin_id design ruling): measured over
14 days, person 140/140, insight 108/108, project 83/83, preference
47/47, trait 30/30, org 17/17, so 425 patches carry no origin_id at
all. A meeting that produced a pile of person and project patches looks
silent to an origin-keyed query while having produced plenty.

It gave a confidently wrong headline. One meeting was reported as "17
behavior observations and not one commitment, so `too short` cannot
explain it". Its actual call: 47,949 chars of transcript, 34 patches
parsed, 15 stored. The patches were there and the query could not see
them.

`extraction_metrics` is the authoritative instrument and was there the
whole time. It records, PER CALL: `transcript_chars`,
`patches_before_filters` (literally
`len(response.content["patches"])`, the parsed model response before
any filter), `patches_after_filters`, `patches_extracted` (post-dedup),
`entities_extracted`, `output_tokens` and `origin_id`. The lesson is
the one already in the operator notes: check what a query resolves
THROUGH, and prefer the artifact to a reconstruction of it.

THE STAGE DECOMPOSITION, which is why the raw column matters:

    before = 0             the model returned no patches, or the
                           response did not parse into any (the
                           Anthropic client does not enforce
                           json_schema on the wire, so a prose answer
                           parses to nothing)
    before > 0, after = 0  the sanitizer chain stripped everything
    after > 0, stored = 0  dedup absorbed it all, which is not a defect

Measured 2026-08-30 over 254 calls in 14 days: 119 parsed zero patches,
1 was stripped by sanitizers, 1 absorbed by dedup. So essentially all
of the loss is at the model, none of it downstream.

AND LENGTH LARGELY EXPLAINS IT. Zero-yield calls had a median
transcript of 786 chars against 13,774 for productive ones, and 90% of
transcripts under 2000 chars produced nothing. Scott's tire store
meeting, which started this investigation, was 2,303 chars with the
owner marker present: a couple of minutes of audio, not a
personal-versus-work problem and not the missing project.

THE RESIDUAL IS THE INTERESTING PART. Eleven transcripts over 10,000
chars still parsed to zero. One (22,901 chars) burned 4,163 output
tokens and produced zero patches AND zero entities, which is the
signature of a response that did not parse. The others sit at 600-900
output tokens with 1,300-1,700 reasoning chars, meaning the model
reasoned and then emitted an empty patches array. Those are two
different failures and this script does not separate them.

READ ONLY. No --apply, no write anywhere in this file.

Usage:
    DATABASE_URL=postgres://... python scripts/measure_extraction_coverage.py
    DATABASE_URL=postgres://... python scripts/measure_extraction_coverage.py --days 30
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import os
import statistics
import sys

import asyncpg

BANDS = (2000, 5000, 10000, 20000, 40000)

METRICS_SQL = """
    SELECT origin_id, transcript_chars, output_tokens, reasoning_chars,
           patches_before_filters AS before_f,
           patches_after_filters  AS after_f,
           patches_extracted      AS stored,
           entities_extracted     AS entities,
           owner_marker_present   AS owner_marker
      FROM extraction_metrics
     WHERE created_at > NOW() - ($1::int * INTERVAL '1 day')
"""


def band(n: int) -> str:
    for edge in BANDS:
        if n < edge:
            return f"<{edge}"
    return f">={BANDS[-1]}"


def stage_of(row) -> str:
    before = row["before_f"] or 0
    after = row["after_f"] or 0
    stored = row["stored"] or 0
    if before == 0:
        return "model returned nothing / did not parse"
    if after == 0:
        return "sanitizers stripped everything"
    if stored == 0:
        return "dedup absorbed everything"
    return "stored something"


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=14, help="window (default 14)")
    ap.add_argument("--long-threshold", type=int, default=10000,
                    help="chars above which a zero-yield call is anomalous")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(METRICS_SQL, args.days)
    finally:
        await conn.close()

    if not rows:
        print(f"no extraction_metrics rows in the last {args.days} days")
        return 0

    print(f"extraction calls, last {args.days} days: {len(rows)}")

    stages = collections.Counter(stage_of(r) for r in rows)
    print("\nwhere the patches went:")
    for name, n in stages.most_common():
        print(f"  {name:44} {n:5}  ({n / len(rows):.0%})")

    empty = [r for r in rows if (r["before_f"] or 0) == 0]
    full = [r for r in rows if (r["before_f"] or 0) > 0]
    sized = [r for r in rows if r["transcript_chars"] is not None]

    if sized:
        print("\ntranscript length, the direct test of 'less to extract':")
        for label, group in (("yielded ZERO patches", empty), ("yielded patches", full)):
            group = [r for r in group if r["transcript_chars"] is not None]
            if not group:
                continue
            chars = sorted(r["transcript_chars"] for r in group)
            print(f"  {label:24} n={len(group):4}  "
                  f"median={statistics.median(chars):8.0f}  "
                  f"min={chars[0]:7}  max={chars[-1]:8}")

        print("\nzero-yield rate by length band:")
        per = collections.defaultdict(lambda: [0, 0])
        for r in sized:
            slot = per[band(r["transcript_chars"])]
            slot[1] += 1
            if (r["before_f"] or 0) == 0:
                slot[0] += 1
        for edge in [f"<{e}" for e in BANDS] + [f">={BANDS[-1]}"]:
            if edge in per:
                zero, total = per[edge]
                print(f"  {edge:>9}  {zero:4} / {total:4}   {zero / total:6.0%} nothing")

    anomalies = [r for r in empty
                 if (r["transcript_chars"] or 0) >= args.long_threshold]
    print(f"\nzero-yield on a transcript >= {args.long_threshold} chars: "
          f"{len(anomalies)}   <-- length does NOT explain these")
    if anomalies:
        print(f"  {'origin':10} {'chars':>7} {'out_tok':>8} {'reason_ch':>10} "
              f"{'ent':>4} {'owner':>6}")
        for r in sorted(anomalies, key=lambda r: -(r["transcript_chars"] or 0)):
            print(f"  {str(r['origin_id'] or '-')[:8]:10} "
                  f"{r['transcript_chars']:>7} {r['output_tokens'] or 0:>8} "
                  f"{r['reasoning_chars'] or 0:>10} {r['entities'] or 0:>4} "
                  f"{str(r['owner_marker'])[:5]:>6}")
        print("\n  A high output_tokens with zero entities AND zero patches is the "
              "signature of\n  a response that did not parse. Low output with high "
              "reasoning_chars is the model\n  reasoning and then emitting an empty "
              "patches array. Different failures.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
