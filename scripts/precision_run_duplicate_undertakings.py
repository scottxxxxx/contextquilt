#!/usr/bin/env python3
"""Offline precision run for doc 23, the gate before any code is written.

Scott's answer to doc 23's question 3: "Offline precision run first,
before any code. If it doesn't hold up on the 3,510 pairs, I don't want
the feature."

THE FIRST VERSION OF THIS SCRIPT MEASURED THE WRONG THING, and the
correction is the reason this docstring exists. It showed the model a
whole (owner, project) group at once, up to 51 open commitments, and
asked which PAIRS were the same undertaking: 1,275 combinations in one
call. Three identical Sonnet runs returned 18, 14 and 23 pairs, agreeing
on 11 of the 26 they flagged between them. That instability is a fair
measurement of a task nobody is proposing to ship.

Doc 23's design is DIRECTIONAL and much smaller: each NEW commitment
from a meeting judged against the open set for that owner and project.
This version simulates that. For every meeting in the window, its own
commitments are the arrivals and the same owner's earlier open ones are
the existing set, exactly as the worker would see them.

SELF-AGREEMENT IS MEASURED, NOT ASSUMED, and it is the number most
likely to kill the feature. A merge suggestion that appears on one load
and not the next is unshippable, because the tap closes a commitment the
user still owes. So the whole window is judged TWICE and the overlap
reported. Precision without stability is meaningless: a judge that flags
a different set each time has no precision to measure.

WHAT IT STILL CANNOT DO. It cannot score precision. Only Scott can say
whether a flagged pair is one piece of work. This produces the list he
marks. And "existing" here means open TODAY and created earlier, not
open AT THE TIME, because closure timestamps for historical rows are
approximate; that makes the existing set slightly smaller than the
worker would have seen, never larger, so it can miss a match and cannot
invent one.

FAILING IS AN ALLOWED OUTCOME (doc 23's falsification section).

Writes NOTHING. Read-only against Postgres, plus LLM calls.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg  # noqa: E402

from contextquilt.services.llm_client_anthropic import AnthropicLLMClient  # noqa: E402

KNOWN_POSITIVES = [("db45b39d", "84b99a40"), ("23721690", "b133913f")]
KNOWN_NEGATIVE = "a6f4792c"

OPEN_COMMITMENTS_SQL = """
    SELECT cp.patch_id, cp.value->>'text' AS text,
           COALESCE(cp.value->>'owner', '') AS owner,
           COALESCE(cp.project_id, '') AS project_id,
           cp.origin_id, cp.created_at
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE ps.subject_key = $1
       AND cp.patch_type = 'commitment'
       AND COALESCE(cp.status, 'active') = 'active'
       AND cp.completed_at IS NULL
       AND cp.origin_id IS NOT NULL
     ORDER BY cp.created_at
"""

# The raw shape is embedded because AnthropicLLMClient.extract() accepts
# json_schema for interface parity and does NOT enforce it on the wire.
# A prompt without the shape gets prose back and parses to nothing.
JUDGE_SYSTEM = """A person's new commitments from one meeting are listed under NEW. Their existing open commitments are listed under EXISTING. Decide which NEW items are the SAME UNDERTAKING as an EXISTING one: the same piece of work, described again on a later occasion.

Say SAME when:
- Both refer to the same deliverable or task, even if the scope, the deadline or the wording differ. "Deliver Portuguese language support by end of weekend" and "Complete Portuguese language support implementation and Apple archive submission" are one undertaking described twice.
- The new item restates, narrows or extends the existing work rather than adding new work.

Say NOTHING AT ALL when:
- They are different tasks that share a project, a topic, a system or a noun. Two tasks about one feature are two undertakings.
- One is a step toward the other and each has its own outcome.
- The work is similar but was agreed for different reasons, at different times, with different people.
- You are unsure. A wrong merge costs the user a commitment they still owe; a missed one costs nothing, because they can already see both.

Most new commitments match nothing. An empty answer is the common and correct one.

Respond with ONLY a JSON object, no prose and no markdown fences, in exactly this shape:
{"matches": [{"new": 0, "existing": 2, "reason": "one short sentence"}]}
Use {"matches": []} when nothing matches."""


def build_content(new_items, existing_items) -> str:
    lines = ["NEW:"]
    for i, it in enumerate(new_items):
        lines.append(f"{i}: {(it['text'] or '').strip()[:300]}")
    lines.append("")
    lines.append("EXISTING:")
    for j, it in enumerate(existing_items):
        lines.append(f"{j}: {(it['text'] or '').strip()[:300]}")
    return "\n".join(lines)


def parse_matches(content, n_new, n_existing):
    """Anything malformed yields nothing. A judge failure must never
    manufacture a merge suggestion."""
    out = []
    if not isinstance(content, dict):
        return out
    for m in content.get("matches") or []:
        if not isinstance(m, dict):
            continue
        a, b, reason = m.get("new"), m.get("existing"), m.get("reason")
        if not (isinstance(a, int) and isinstance(b, int)):
            continue
        if not (0 <= a < n_new and 0 <= b < n_existing):
            continue
        out.append((a, b, (reason or "").strip()[:200]
                    if isinstance(reason, str) else ""))
    return out


def build_calls(rows, days):
    """The worker's view, meeting by meeting: this meeting's commitments
    against the same owner and project's earlier open ones."""
    import datetime
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=days))
    by_meeting = defaultdict(list)
    for r in rows:
        by_meeting[r["origin_id"]].append(dict(r))

    calls = []
    for origin, items in by_meeting.items():
        first_seen = min(i["created_at"] for i in items)
        if first_seen < cutoff:
            continue
        groups = defaultdict(list)
        for it in items:
            groups[(it["owner"].lower(), it["project_id"])].append(it)
        for key, new_items in groups.items():
            existing = [
                r for r in rows
                if (r["owner"].lower(), r["project_id"]) == key
                and r["origin_id"] != origin
                and r["created_at"] < first_seen
            ]
            if existing:
                calls.append((origin, key, new_items,
                              [dict(e) for e in existing]))
    return calls


async def judge_all(llm, calls, model, label):
    flagged, failures = [], 0
    for origin, (owner, project), new_items, existing in calls:
        try:
            resp = await llm.extract(
                JUDGE_SYSTEM, build_content(new_items, existing), model=model)
            matches = parse_matches(resp.content, len(new_items), len(existing))
        except Exception as exc:
            failures += 1
            print(f"  ! {label} {owner or '(none)'} failed: {str(exc)[:100]}")
            continue
        for a, b, reason in matches:
            flagged.append({
                "owner": owner or "(none)", "project": project[:8] or "-",
                "new_id": str(new_items[a]["patch_id"])[:8],
                "old_id": str(existing[b]["patch_id"])[:8],
                "new": new_items[a]["text"], "old": existing[b]["text"],
                "reason": reason,
            })
    return flagged, failures


async def run(dsn, subject_key, days, model) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        rows = [dict(r) for r in await conn.fetch(OPEN_COMMITMENTS_SQL, subject_key)]
    finally:
        await conn.close()

    calls = build_calls(rows, days)
    print(f"{len(rows)} open commitments with an origin")
    print(f"{len(calls)} judge calls in the last {days} days "
          f"(one per meeting per owner/project with prior open work)")
    print(f"running TWICE to measure self-agreement\n")

    llm = AnthropicLLMClient()
    try:
        a, fa = await judge_all(llm, calls, model, "run1")
        b, fb = await judge_all(llm, calls, model, "run2")
    finally:
        await llm.close()

    sa = {(f["new_id"], f["old_id"]) for f in a}
    sb = {(f["new_id"], f["old_id"]) for f in b}
    both, only_a, only_b = sa & sb, sa - sb, sb - sa
    union = sa | sb

    print(f"\n=== run 1: {len(sa)} flagged ({fa} failures) | "
          f"run 2: {len(sb)} flagged ({fb} failures) ===")
    print(f"=== SELF-AGREEMENT: {len(both)} of {len(union)} distinct pairs "
          f"appear in BOTH runs"
          + (f" ({100*len(both)//len(union)}%)" if union else "") + " ===")
    print(f"    only run 1: {len(only_a)}    only run 2: {len(only_b)}\n")

    print("=== pairs flagged in BOTH runs (the stable set, mark these) ===\n")
    seen = set()
    for f in a:
        k = (f["new_id"], f["old_id"])
        if k not in both or k in seen:
            continue
        seen.add(k)
        print(f"  {f['owner']} / {f['project']}   new {f['new_id']} -> old {f['old_id']}")
        print(f"     NEW: {f['new'][:120]}")
        print(f"     OLD: {f['old'][:120]}")
        print(f"     why: {f['reason']}")
        print(f"     SAME UNDERTAKING?  [ ] yes   [ ] no\n")

    if only_a or only_b:
        print("=== flagged in ONLY ONE run (unstable, do not ship on these) ===")
        for f in a + b:
            k = (f["new_id"], f["old_id"])
            if k in both or k in seen:
                continue
            seen.add(k)
            print(f"  {f['new_id']} -> {f['old_id']}  {f['new'][:70]}")
        print()

    print("=== recall against the known set ===")
    for x, y in KNOWN_POSITIVES:
        hit = (x, y) in union or (y, x) in union
        stable = (x, y) in both or (y, x) in both
        print(f"  {x} + {y}: {'FOUND' if hit else 'MISSED'}"
              + (" (stable)" if stable else " (one run only)" if hit else ""))
    neg = [1 for (x, y) in union if KNOWN_NEGATIVE in (x, y)]
    print(f"  known negative {KNOWN_NEGATIVE}: "
          f"{'FLAGGED (bad)' if neg else 'not flagged (good)'}")
    print("\nNothing was written. Precision is Scott's to score on the stable set.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subject-key", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    return asyncio.run(run(dsn, args.subject_key, args.days, args.model))


if __name__ == "__main__":
    raise SystemExit(main())
