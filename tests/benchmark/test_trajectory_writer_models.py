"""Which is the CHEAPEST model that can write a trajectory card.

Scott's question, and it deserves a measurement rather than an opinion,
because the repo's own history points both ways and the distinction
between the two halves of it IS the answer.

Against Haiku: the 2026-08-13 person-analyzer experiment and the
who_they_are lens both found Haiku failing INVISIBLY, missing a
cross-meeting pattern and fabricating a receipt, which is why
CQ_WHO_THEY_ARE_MODEL is pinned to Sonnet.

For Haiku: in both of those Haiku was asked to FIND the pattern. Here
it cannot. Every number is computed in SQL, ranked in code, and handed
over; the model is left with the writing. That is a different job and
the prior does not transfer.

What could still go wrong is CONSTRAINT COMPLIANCE, and the insight_cards
docstring records the measured shape of it: at a pinned temperature,
asked for "at most 62 characters", the live model returned 65 EVERY TIME
on five identical calls. A pinned temperature means a formatting failure
is not a lottery a retry could win, it is a person who never gets a card.
So a weaker writer does not produce worse cards here. It produces FEWER
cards, silently, and coverage is the thing to measure.

Which makes this eval almost entirely MECHANICAL. No judge model, no
rubric, no position effects (the 4.6-vs-5 judging on 08-20 turned out to
be a position effect, which is exactly the trap a subjective eval walks
into). The parse already encodes every rule; run each model against
identical inputs and count what the parse throws away and why.

Run inside the prod container, where the key lives:
  docker cp ... contextquilt:/app/
  docker exec contextquilt python /app/test_trajectory_writer_models.py
"""

import asyncio
import json
import os
import sys
from collections import Counter

sys.path[:0] = ["/app", "/app/src", "src"]

from contextquilt.services.llm_client_anthropic import AnthropicLLMClient
from contextquilt.services.trajectory import (
    TRAJECTORY_SYSTEM,
    Window,
    allowed_numbers,
    build_trajectory_content,
    change_for_measure,
    parse_trajectory_response,
    retry_note,
    served_trajectory,
)

MODELS = ["claude-haiku-4-5-20251001", "claude-sonnet-4-6"]

# REPS EXIST BECAUSE THE FIRST RUN COULD NOT SEPARATE THE MODELS.
# The client sends temperature=0.1 for models that accept sampling, so
# these calls are not deterministic, and two back-to-back runs of the
# identical 10 cases moved Sonnet from 6 accepted to 2. That swing is
# larger than any gap between the models, which means a 10-call run
# measures the sampler and reports it as a model difference. This is the
# same shape as the 2026-08-20 finding that the 4.6-vs-5 judging was a
# position effect: the honest reading of an eval that cannot separate its
# arms is "unmeasured", never "equal" and never a winner.
REPS = 3


def w(num, den, prefix, meetings):
    return Window(num, den, [f"{prefix}{i}" for i in range(meetings)])


# Fact sets chosen to span the axes a writer can fail on: both valences,
# both directions, a large gap and a gap barely over the floor, a small
# denominator and a large one, and the two neutral measures whose whole
# risk is being graded. Every one clears the gates, so a rejection here
# is the WRITER failing and never the measurement.
CASES = [
    ("Suresh Muchakurti", "closed_late", w(2, 12, "a", 8), w(8, 11, "b", 8),
     [{"text": "Provide update on dynamic category updates for KB Retrieval"},
      {"text": "Integrate endpoint with 'cold'"},
      {"text": "Confirm the migration window with the platform team"}]),
    ("Vijay Ramanathan", "closed_late", w(9, 12, "a", 7), w(1, 11, "b", 6),
     [{"text": "Send the revised staffing plan"},
      {"text": "Close out the vendor review"}]),
    ("Priya Nair", "closed_late", w(3, 10, "a", 5), w(6, 10, "b", 5),
     [{"text": "Sign off the Q3 dashboard spec"}]),
    ("Sukumar Iyer", "closed_late", w(1, 30, "a", 12), w(11, 24, "b", 11),
     [{"text": "Hand over the runbook"}, {"text": "Reconcile the billing export"}]),
    ("Suresh Muchakurti", "speaking_turns", w(214, 8, "a", 8), w(96, 8, "b", 8), []),
    ("Dana Okoro", "speaking_turns", w(60, 9, "a", 9), w(190, 8, "b", 8), []),
    ("Marco Alvarez", "questions_to_you", w(31, 10, "a", 10), w(6, 9, "b", 9), []),
    ("Lena Fischer", "questions_to_you", w(4, 8, "a", 8), w(22, 7, "b", 7), []),
    ("Tomas Berg", "closed_late", w(2, 9, "a", 6), w(7, 9, "b", 6),
     [{"text": "Draft the incident summary"}]),
    ("Aiko Tanaka", "speaking_turns", w(150, 10, "a", 10), w(70, 9, "b", 9), []),
]


async def one(client, model, name, key, earlier, recent, examples, retry=True):
    case_id = f"{name}/{key}"
    chosen = change_for_measure(key, earlier, recent)
    if chosen is None:
        return {"case": case_id, "model": model, "outcome": "GATE_REJECTED"}
    facts = served_trajectory(chosen, name)
    permitted = allowed_numbers(facts)
    content = build_trajectory_content(name, facts, examples)
    defects = []
    try:
        resp = await client.extract(
            system_prompt=TRAJECTORY_SYSTEM, user_content=content, model=model,
        )
    except Exception as exc:
        return {"case": case_id, "model": model, "outcome": "CALL_FAILED",
                "detail": str(exc)[:160]}
    # Keep the raw answer on every failure path. A run that records
    # `defect=unparseable` and discards what the model actually said
    # names a verdict and hides the evidence, which is the failure
    # `relationship_lenses.rejected_lengths` exists to prevent and which
    # cost three deploys there. It cost one diagnostic round here: the
    # first run showed FOUR unparseables for both models, identical
    # counts across two different models, which is an instrument
    # fingerprint and not a model result.
    raw = resp.content
    skipped = isinstance(raw, dict) and raw.get("skip") is True
    out = parse_trajectory_response(
        resp.content, permitted, name, defects=defects, facts=facts)
    if out is not None:
        return {"case": case_id, "model": model, "outcome": "ACCEPTED",
                "attempt": 1, "text": out["text"], "narrative": out["narrative"],
                "do": out["do"], "cost": resp.cost_usd,
                "claim_chars": len(out["text"]),
                "narrative_chars": len(out["narrative"]),
                "do_chars": len(out["do"])}
    if out is None and skipped:
        # A SKIP IS NOT A FAILURE. The prompt explicitly permits it
        # ("skip when the two stretches genuinely support nothing worth
        # showing") and a writer declining is the honest outcome the
        # whole lens is built around. Counting it as garbage would
        # understate both models and, worse, would reward a model that
        # never declines.
        return {"case": case_id, "model": model, "outcome": "SKIPPED",
                "reason": raw.get("reason"), "cost": resp.cost_usd}
    defect = defects[0] if defects else "unparseable"
    # ONE bounded retry, exactly as the worker does it. A plain retry at a
    # pinned temperature is the same question asked twice; a retry that
    # names the defect is a different prompt. Coverage has to be measured
    # under the retry the production path actually gives it, or the
    # comparison is against a pipeline nobody runs.
    note = retry_note(defect) if retry else None
    if not note:
        return {"case": case_id, "model": model, "outcome": "REJECTED",
                "defect": defect, "attempt": 1, "cost": resp.cost_usd,
                "raw": json.dumps(raw, default=str)[:600]}
    defects2 = []
    try:
        resp2 = await client.extract(
            system_prompt=TRAJECTORY_SYSTEM,
            user_content=content + "\n\n" + note, model=model,
        )
    except Exception as exc:
        return {"case": case_id, "model": model, "outcome": "CALL_FAILED",
                "detail": str(exc)[:160]}
    out2 = parse_trajectory_response(
        resp2.content, permitted, name, defects=defects2, facts=facts)
    total = (resp.cost_usd or 0) + (resp2.cost_usd or 0)
    if out2 is not None:
        return {"case": case_id, "model": model, "outcome": "ACCEPTED",
                "attempt": 2, "first_defect": defect, "text": out2["text"],
                "narrative": out2["narrative"], "do": out2["do"], "cost": total,
                "claim_chars": len(out2["text"]),
                "narrative_chars": len(out2["narrative"]),
                "do_chars": len(out2["do"])}
    return {"case": case_id, "model": model, "outcome": "REJECTED",
            "defect": defect, "second_defect": defects2[0] if defects2 else None,
            "attempt": 2, "cost": total,
            "raw": json.dumps(raw, default=str)[:600],
            "raw2": json.dumps(resp2.content, default=str)[:600]}


async def main():
    client = AnthropicLLMClient()
    rows = []
    for model in MODELS:
        for rep in range(REPS):
            for name, key, earlier, recent, examples in CASES:
                row = await one(client, model, name, key, earlier, recent, examples)
                row["rep"] = rep
                rows.append(row)
                print(".", end="", flush=True)
    await client.close()
    print()
    report = {}
    for model in MODELS:
        mine = [r for r in rows if r["model"] == model]
        accepted = [r for r in mine if r["outcome"] == "ACCEPTED"]
        report[model] = {
            "n": len(mine),
            "accepted": len(accepted),
            "accepted_first_try": len([r for r in accepted if r.get("attempt") == 1]),
            "rejected": len([r for r in mine if r["outcome"] == "REJECTED"]),
            "declined_to_write": len([r for r in mine if r["outcome"] == "SKIPPED"]),
            "skipped_cases": [r["case"] for r in mine if r["outcome"] == "SKIPPED"],
            "call_failed": len([r for r in mine if r["outcome"] == "CALL_FAILED"]),
            "defects": dict(Counter(
                r.get("defect") for r in mine if r["outcome"] == "REJECTED")),
            "first_try_defects": dict(Counter(
                r.get("first_defect") for r in accepted if r.get("first_defect"))),
            "cost_usd": round(sum(r.get("cost") or 0 for r in mine), 5),
            # Per-rep accept counts. If these disagree with each other by
            # more than the gap between the models, the run has not
            # separated them and must not name a winner.
            "accepted_per_rep": [
                len([r for r in mine
                     if r.get("rep") == k and r["outcome"] == "ACCEPTED"])
                for k in range(REPS)],
            "claim_chars": sorted(r["claim_chars"] for r in accepted),
        }
    print(json.dumps({"report": report, "rows": rows}, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
