"""Put the role-semantics prompt on the wire once, and grade the answer.

READ ONLY. No database connection, no ingest, no write of any kind. It
builds the content the worker builds, calls the client the worker calls,
and runs the parse the worker runs. Nothing it does can be observed from
outside except one LLM call on the operator's key.

WHY THIS EXISTS. `_extract_semantic_role_signals` only runs at ingest,
so until a real meeting lands the prompt has never been on the wire and
nobody can say what the model answers with. Waiting for a meeting proves
less than this does anyway: a worker log line says the parse returned
something, while this says whether the pointers landed on the people the
transcript actually shows.

WHAT IS BEING GRADED, AND IT IS NOT "did the model reply". Doc 19.1 is
the design: the model returns POINTERS AT TURN NUMBERS, never names and
never counts, and WHO did each thing is read off that turn's own speaker
label. So the failure worth catching is a pointer that lands on the
wrong turn, and the only way to catch it is a transcript whose answer is
designed rather than guessed. The fixture below carries two planted
negatives for exactly that reason.

Usage, on prod, through the documented compose-run path:

    cd /opt/cq-ops && docker compose --project-name contextquilt \
      -f docker-compose.prod.yml --env-file .env.prod \
      run --rm --no-deps --entrypoint python context-quilt \
      scripts/verify_role_semantics_wire.py
"""
import asyncio
import json
import sys

from contextquilt.services import role_semantics
from contextquilt.services.extraction_schema import transcript_turns
from contextquilt.services.llm_client_anthropic import AnthropicLLMClient

# Designed ground truth, not a sample of anything. Dana assigns twice and
# both are taken in the room; Dana steers twice; Marcus steers once and
# defers once on an input outside his control.
#
# TWO NEGATIVES ARE PLANTED, and they are the point of the fixture:
#   turn 14 is Marcus taking something on HIMSELF, which is a commitment
#     the main extraction owns and must NOT count as an assignment;
#   turn 15 is Dana admitting a plain delay with no upstream in it, which
#     must NOT count as a deferral.
# A model that pattern-matches "I'll do X" and "I have not" passes the
# positives and fails these two, which is why a fixture of positives
# alone would have told us nothing.
TRANSCRIPT = """[Scott (you)] Morning both. Where are we on the billing migration?
[Dana Reyes] Let's take the migration first and park the pricing page until the end.
[Marcus Hill] Fine by me.
[Dana Reyes] Marcus, can you get the migration script reviewed before Thursday?
[Marcus Hill] Yes, I'll have it reviewed by Wednesday night.
[Dana Reyes] Scott, can you write up the rollback plan this week?
[Scott (you)] I can do that, yes.
[Marcus Hill] One thing before we move on, I want to cover staffing.
[Dana Reyes] Go ahead.
[Marcus Hill] I can't start the vendor rollout until legal comes back on the contract.
[Scott (you)] Who is chasing legal on that?
[Marcus Hill] I sent it over on Monday and I am still waiting on them.
[Dana Reyes] Next topic. Pricing.
[Marcus Hill] The new tiers are drafted. I'll share the doc after this call.
[Dana Reyes] I have not got to the dashboard yet.
[Scott (you)] That's everything. Thanks both.
"""

# Only the four semantic counts are graded. The directive/responsive
# split is reported and deliberately NOT asserted: the prompt tells the
# model to leave an unclear turn out, so the split is a judgement call
# with a legitimate range, and pinning it here would turn a correct
# abstention into a red run.
EXPECTED = {
    "dana reyes": {
        "follow_ups_assigned": 2, "follow_ups_accepted": 2,
        "agenda_moves": 2, "upstream_deferrals": 0,
    },
    "marcus hill": {
        "follow_ups_assigned": 0, "follow_ups_accepted": 0,
        "agenda_moves": 1, "upstream_deferrals": 1,
    },
}


async def main() -> int:
    turns, self_key = transcript_turns(TRANSCRIPT, None)
    print(f"turns parsed:     {len(turns)}")
    print(f"self_key:         {self_key!r}")
    print(f"transcript chars: {len(TRANSCRIPT)} (floor {role_semantics.MIN_TRANSCRIPT_CHARS})")
    if not role_semantics.worth_a_call(TRANSCRIPT, turns, self_key):
        print("FAILED BEFORE THE MODEL: worth_a_call() refused this transcript.")
        return 1

    client = AnthropicLLMClient()
    print(f"model:            {client.model}\n")
    response = await client.extract(
        system_prompt=role_semantics.ROLE_SEMANTICS_SYSTEM,
        user_content=role_semantics.build_role_semantics_content(turns),
    )
    raw = response.content
    print("--- RAW MODEL ANSWER ---")
    print(raw if isinstance(raw, str) else json.dumps(raw, indent=2))
    print(f"--- cost_usd: {getattr(response, 'cost_usd', None)} ---\n")

    defects: list = []
    signals = role_semantics.parse_role_semantics_response(
        raw, turns, self_key, defects=defects,
    )
    if not signals:
        # This is `role_semantics_none` in the worker log, reproduced
        # where the raw answer above is visible beside it.
        print(f"PARSE RETURNED NOTHING. defect={defects[0] if defects else 'empty'}")
        return 1

    print("--- PARSED (what would reach the columns) ---")
    print(json.dumps(signals, indent=2, sort_keys=True))

    print("\n--- GRADED against the designed answer ---")
    ok = True
    by_label = signals.get("by_label") or {}
    for key, want in EXPECTED.items():
        got = by_label.get(key)
        if got is None:
            print(f"  MISS {key}: absent from by_label")
            ok = False
            continue
        for column, want_value in want.items():
            hit = got[column] == want_value
            ok = ok and hit
            print(f"  {'ok  ' if hit else 'MISS'} {key}.{column}: "
                  f"got {got[column]}, designed {want_value}")
        # Reported, never asserted. Their sum is NOT the turn count.
        print(f"       {key}: directive={got['directive_turns']} "
              f"responsive={got['responsive_turns']} "
              f"(unclassified turns are deliberate)")

    unexpected = sorted(set(by_label) - set(EXPECTED))
    if unexpected:
        # A name here means the transcript parse found a speaker the
        # fixture does not know about, which is a bug in one of the two.
        print(f"  MISS unexpected speakers in by_label: {unexpected}")
        ok = False

    print("\nRESULT:", "PROMPT IS ON THE WIRE AND THE POINTERS LANDED"
          if ok else "AT LEAST ONE MISS, read the raw answer above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
