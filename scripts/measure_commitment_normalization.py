#!/usr/bin/env python3
"""Does a standard shape make duplicates visible without an LLM judge?

Scott, 2026-09-10, after marking the duplicate gate: would incorporating
a controlled-language standard (he named ASD-STE100) into how action
items are drafted help long term?

THE MEASUREMENT, and it is deliberately two-sided because normalization
cuts both ways. Standardizing raises surface similarity, which is the
point for a true duplicate and the danger for genuinely separate work.

  1. FIDELITY. Fifty original commitments beside their normalized form,
     for Scott to read. A normalized item that misstates what somebody
     committed to is worse than a duplicate, because a commitment is a
     record of what a person said they would do and paraphrase is
     assertion (doc 16 §5.13).

  2. TRIGRAM MOVEMENT on pairs whose answer is already known.
     `semantic_dedup` never even looks below 0.35, so the question is
     whether normalization lifts KNOWN DUPLICATES above that floor while
     leaving KNOWN SEPARATE WORK below it.

       Portuguese  db45b39d / 84b99a40   duplicate (Scott confirmed)
       Steven      23721690 / b133913f   duplicate (Scott confirmed)
       Cigna       9ab83c51 / 3ddab0d8   SEPARATE (Scott confirmed)
                   9ab83c51 / ce9ceed2   SEPARATE
                   3ddab0d8 / ce9ceed2   SEPARATE, and the pair he said
                                         might merge if descriptions
                                         could be combined: open
       Control     23721690 / a6f4792c   SEPARATE, heavy shared words

IF DUPLICATES CROSS 0.35 AND SEPARATE WORK DOES NOT, a large part of the
duplicate feature stops needing a per-meeting LLM call at all, which is
a better outcome than the sixth call doc 23 proposes.

THE RULE THAT MAKES OR BREAKS IT: scope qualifiers are preserved
verbatim. "P1" against "P2 and P3" is what makes the Cigna rows separate
work, and a normalizer that generalizes them into "Cigna demo test
scenarios" would collapse exactly the distinction Scott is least willing
to lose on his own items.

Writes NOTHING. Read-only, plus LLM calls.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from contextquilt.services.llm_client_anthropic import AnthropicLLMClient  # noqa: E402

KNOWN_DUPLICATES = [("db45b39d", "84b99a40"), ("23721690", "b133913f")]
KNOWN_SEPARATE = [("9ab83c51", "3ddab0d8"), ("9ab83c51", "ce9ceed2"),
                  ("23721690", "a6f4792c")]
KNOWN_OPEN = [("3ddab0d8", "ce9ceed2")]
PINNED = sorted({p for pair in KNOWN_DUPLICATES + KNOWN_SEPARATE + KNOWN_OPEN
                 for p in pair} | {"b585a844"})

SAMPLE_SQL = """
    SELECT left(cp.patch_id::text, 8) AS id, cp.value->>'text' AS text,
           COALESCE(cp.value->>'owner', '') AS owner,
           COALESCE(cp.value->>'deadline', '') AS deadline
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE ps.subject_key = $1
       AND cp.patch_type = 'commitment'
       AND COALESCE(cp.status, 'active') = 'active'
       AND cp.completed_at IS NULL
       AND (left(cp.patch_id::text, 8) = ANY($2::text[])
            OR cp.patch_id IN (
                 SELECT cp2.patch_id FROM context_patches cp2
                 JOIN patch_subjects ps2 ON ps2.patch_id = cp2.patch_id
                WHERE ps2.subject_key = $1 AND cp2.patch_type = 'commitment'
                  AND COALESCE(cp2.status,'active') = 'active'
                  AND cp2.completed_at IS NULL
                ORDER BY md5(cp2.patch_id::text) LIMIT $3))
"""

NORMALIZE_SYSTEM = """You rewrite a commitment into a standard shape, so that two records of the SAME work look alike and two records of DIFFERENT work do not.

A commitment often has TWO parts: the ACTION somebody will take, and the PURPOSE it is for. Keep both. Collapsing them into one loses whichever half you did not pick, and two records of one undertaking can then land on opposite halves and stop resembling each other.

Shape: "<Owner> <verb> <object>, <purpose>, by <deadline>."
Keep the purpose introduced the way the original introduces it ("to ...", "for ...").
Leave out any part the original does not state. Never invent one.

Rules:
- The ACTION verb is ONE of: deliver, complete, send, share, review, test, validate, fix, update, create, schedule, hold, contact, confirm, investigate, deploy, document, decide, visit, travel, attend, call.
- CONJUGATE IT. When there is an owner, write the verb in the third person present so it agrees: "Scott delivers", never "Scott deliver". When there is no owner, use the plain imperative: "Deliver the report". The list above is given in the base form; do not copy the base form through.
- The PURPOSE keeps the original's own words. Do not normalise it, do not shorten it, do not swap its verb. It is the half that tells two similar-sounding tasks apart and the half that makes two differently-worded records of one task recognisable.
- PRESERVE VERBATIM any text naming a tier, priority, phase, version, environment, subset or named artifact. Examples: "P1", "P2 and P3", "phase one", "v2.0", "QA", "CTS 717", "Eligibility, Pricing, Order, Status". These separate one piece of work from another and must never be dropped, merged or generalised.
- NEVER SPLIT. One record in, one record out. If the original states two things joined by "and" or a semicolon, keep BOTH in the normalized form. You are rewriting the shape of one record of what somebody said, not deciding how many obligations they took on. Splitting would create an obligation nobody stated separately.
- Never add a fact. Never invent an owner, a deadline, or a detail that is not in the original.
REFUSING IS THE MOST IMPORTANT RULE HERE, and it outranks every rule above. Set "normalized" to null whenever any of these is true:

- No verb in the list captures the original's action without changing what the person actually agreed to do. "Measure" is not "validate". "Coordinate" is not "contact". If the closest approved verb changes the act, REFUSE rather than pick it.
- The original carries a qualifier that is neither a purpose nor a deadline and would have to be forced into one of those slots. "before the mid-week publication decision" is a CONSTRAINT, not a deadline. "ongoing for 4-6 weeks" is a DURATION, not a deadline. Forcing either into "by <deadline>" states something the person did not.
- The original is two or more distinct actions that cannot both sit in one shape without one of them being bent or dropped. You may not split, so REFUSE.
- Anything else that would make the normalized form say more, less, or other than the original.

A refusal costs nothing: the original text is kept and used as it is today. A rewrite that misstates a commitment sits next to work somebody still owes and nobody will notice it drifted. When in doubt, REFUSE. Refusing on a third of the input would be a fine outcome.

Respond with ONLY a JSON object, no prose and no markdown fences, in exactly this shape:
{"items": [{"i": 0, "normalized": "Steven Williams visits his mother, to test ShoulderSurf in a real interview call, by this weekend."}]}
One entry per numbered input, using each number exactly once."""


def build_content(batch):
    lines = []
    for i, r in enumerate(batch):
        owner = f"  [owner: {r['owner']}]" if r["owner"] else ""
        dl = f"  [deadline: {r['deadline']}]" if r["deadline"] else ""
        lines.append(f"{i}: {r['text']}{owner}{dl}")
    return "\n".join(lines)


def parse_items(content, n):
    out = {}
    if not isinstance(content, dict):
        return out
    for it in content.get("items") or []:
        if not isinstance(it, dict):
            continue
        i = it.get("i")
        if not isinstance(i, int) or not (0 <= i < n):
            continue
        norm = it.get("normalized")
        # `second` is deliberately not read. Splitting was removed on
        # 2026-09-10 after Scott saw it turning one commitment into two:
        # a record of what somebody said is not ours to divide.
        out[i] = (norm if isinstance(norm, str) and norm.strip() else None, None)
    return out


async def run(dsn, subject_key, sample, model):
    conn = await asyncpg.connect(dsn)
    try:
        rows = [dict(r) for r in await conn.fetch(
            SAMPLE_SQL, subject_key, PINNED, max(sample - len(PINNED), 1))]
        by_id = {r["id"]: r for r in rows}
        missing = [p for p in PINNED if p not in by_id]
        print(f"{len(rows)} commitments sampled "
              f"({len(PINNED)} pinned, {len(missing)} pinned rows missing)")

        llm = AnthropicLLMClient()
        norms, seconds, refused = {}, {}, []
        try:
            for start in range(0, len(rows), 10):
                batch = rows[start:start + 10]
                resp = await llm.extract(NORMALIZE_SYSTEM, build_content(batch),
                                         model=model)
                got = parse_items(resp.content, len(batch))
                for i, r in enumerate(batch):
                    n, s = got.get(i, (None, None))
                    if n is None:
                        refused.append(r["id"])
                    else:
                        norms[r["id"]] = n
                    if s:
                        seconds[r["id"]] = s
        finally:
            await llm.close()

        print(f"{len(norms)} normalized, {len(refused)} refused, "
              f"{len(seconds)} carried a second commitment\n")

        print("=== FIDELITY: read these. does the normalized form say the "
              "same thing? ===\n")
        for r in rows:
            n = norms.get(r["id"])
            print(f"  {r['id']}")
            print(f"    ORIGINAL:   {r['text']}")
            print(f"    NORMALIZED: {n if n else '(REFUSED)'}")
            if r["id"] in seconds:
                print(f"    SPLIT OFF:  {seconds[r['id']]}")
            print(f"    FAITHFUL?  [ ] yes  [ ] no\n")

        async def sim(a, b):
            return await conn.fetchval("SELECT similarity($1, $2)", a, b)

        async def report(pairs, label, expect):
            print(f"=== {label} (expect {expect}) ===")
            for x, y in pairs:
                if x not in by_id or y not in by_id:
                    print(f"  {x} / {y}: one side missing from the sample")
                    continue
                before = await sim(by_id[x]["text"], by_id[y]["text"])
                nx, ny = norms.get(x), norms.get(y)
                after = await sim(nx, ny) if nx and ny else None
                cross = ""
                if after is not None:
                    was, now = before >= 0.35, after >= 0.35
                    if was != now:
                        cross = "  CROSSED 0.35" if now else "  DROPPED BELOW 0.35"
                print(f"  {x} / {y}:  before {before:.3f}  ->  "
                      f"{('%.3f' % after) if after is not None else 'n/a'}{cross}")
            print()

        await report(KNOWN_DUPLICATES, "KNOWN DUPLICATES", "rise above 0.35")
        await report(KNOWN_SEPARATE, "KNOWN SEPARATE WORK", "stay below 0.35")
        await report(KNOWN_OPEN, "OPEN (Scott unsure)", "no expectation")
        print("Nothing was written. Fidelity is Scott's to mark.")
    finally:
        await conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subject-key", required=True)
    ap.add_argument("--sample", type=int, default=50)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    return asyncio.run(run(dsn, args.subject_key, args.sample, args.model))


if __name__ == "__main__":
    raise SystemExit(main())
