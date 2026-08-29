"""A card that keeps failing must stop being retried, and stop being promised.

FOUND BY WATCHING THE FIRST CYCLE after #341 rather than by reading the
code that had just been written. #341 argued the catch-up was bounded,
"about 240 calls once", because a card is only written for a person on a
lens they do not already have. True of SUCCESSES only. A decline logs at
debug and returns; a rejected card writes nothing either; the idempotency
gate counts lens STAMPS and never sees a failure. So the same people were
retried every cycle forever: Ragu on `stated_role_dropped` and Pallavi
Kandanu on `opens_with_name`, identically, across 08-26, 08-27 and 08-28.

ShoulderSurf then asked the question that made it worse than a cost bug:
is a decline visible to them? It is. Such a person is served
`pending_pattern`, which the client renders as "Nothing stands out yet.
This fills in as more comes in about {name}." For these people that
invites an action which cannot work, forever.

Scott ruled both halves: stop retrying, and stop promising.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()
WORKER = (ROOT / "src" / "worker.py").read_text()
MIGRATION = (ROOT / "init-db" / "44_person_lens_attempts.sql").read_text()

from contextquilt.services.consolidation import (  # noqa: E402
    LENS_ATTEMPT_LIMIT,
    LENS_ATTEMPT_PASS_SLOT,
    READINESS_PENDING_EVIDENCE,
    READINESS_PENDING_PATTERN,
    READINESS_RETIRED,
    READINESS_STALLED,
    READINESS_SUPPRESSED,
    READINESS_WAITING_STATES,
    _lens_state,
    chosen_lens,
    parse_profile_response,
)


# ------------------------------------------------------------------
# The state itself
# ------------------------------------------------------------------

def test_stalled_is_a_waiting_state_and_NOT_a_closed_one():
    """`suppressed` and `retired` mean the pass will never produce this
    card again. `stalled` explicitly WILL try again when evidence grows,
    so folding it into the closed set would suppress a card that should
    still be inviting something: the opposite of the fix."""
    assert READINESS_STALLED in READINESS_WAITING_STATES
    assert READINESS_STALLED not in {READINESS_SUPPRESSED, READINESS_RETIRED}


def test_a_stalled_lens_reads_stalled_instead_of_pending_pattern():
    assert _lens_state([], gate_met=True, lens="how_they_decide") == \
        READINESS_PENDING_PATTERN
    assert _lens_state([], gate_met=True, lens="how_they_decide", stalled=True) == \
        READINESS_STALLED


def test_below_the_gate_the_counts_still_win():
    """A person can be stalled AND short of evidence, and 'you need two
    more meetings' is the more useful of the two answers."""
    assert _lens_state([], gate_met=False, lens="how_they_decide", stalled=True) == \
        READINESS_PENDING_EVIDENCE


def test_a_user_no_still_outranks_stalled():
    stamps = [{"status": "archived", "archive_cause": "user_delete"}]
    assert _lens_state(stamps, gate_met=True, lens="how_they_decide", stalled=True) == \
        READINESS_SUPPRESSED


def test_an_existing_card_outranks_stalled():
    assert _lens_state([{"status": "active"}], gate_met=True,
                       lens="how_they_decide", stalled=True) == "available"


# ------------------------------------------------------------------
# What is served, and what is deliberately not
# ------------------------------------------------------------------

def test_the_attempt_count_is_never_served():
    """ShoulderSurf argued this and the argument is right: 'we have
    looked 6 times' turns candour into a report of repeated failure and
    reads as the app being broken. Kept internal. Not merely unused on
    the wire, ABSENT from it, because a value that can be rendered will
    be rendered by whoever is in a hurry."""
    from contextquilt.services.consolidation import build_insight_readiness
    out = build_insight_readiness(
        [], [], today=__import__("datetime").date(2026, 8, 28),
        min_patches=4, min_meetings=3,
        attempt_rows=[{"lens": LENS_ATTEMPT_PASS_SLOT, "attempts": 9,
                       "evidence_at_attempt": 12}],
    )
    for row in out["lenses"]:
        assert "attempts" not in row
        assert "last_defect" not in row
        assert "last_outcome" not in row


def test_the_parse_defect_is_never_served():
    """A defect name is a fact about our own prompt, not about the
    colleague, and nothing on the person surface should render it."""
    assert "last_defect" in MIGRATION      # stored
    readiness = MAIN[MAIN.index("async def _person_insight_readiness"):]
    readiness = readiness[:readiness.index("\nasync def ", 10)]
    assert "last_defect" not in readiness  # never selected


def test_a_rejection_is_recorded_against_the_lens_the_model_actually_picked():
    """THE #342 DEFECT, AND IT WAS DEAD CODE. The writer read the lens off
    `profile` inside a branch `profile` can only reach when it is falsy,
    so both arms of the conditional were None and the sentinel was the
    answer for every reachable input. One rejected `how_they_decide`
    therefore marked `what_moves_them` stalled as well, which inverts the
    ruling the line was written to implement, and `_readiness`'s per-lens
    branch was unreachable from its only writer.

    The lens is read off the model's own answer now. That is safe for
    exactly one reason and it is worth stating: `parse_profile_response`
    validates this same key against MODEL_CHOSEN_LENSES BEFORE it appends
    any defect, so a non-empty defect list implies a real lens is in
    there."""
    answer = {"skip": False, "lens": "how_they_decide",
              "text": "x" * 400, "do": "Ask for the numbers first."}
    defects: list = []
    assert parse_profile_response(answer, defects=defects) is None, "card is rejected"
    assert defects, "and the rejection is a defect, not a decline"
    assert chosen_lens(answer) == "how_they_decide"


def test_a_decline_still_lands_on_the_pass_sentinel():
    """A model that produced nothing at all said nothing about any one
    lens, so it stalls every pending lens. That half was always right and
    the fix must not trade one wrong answer for the other."""
    assert chosen_lens({"skip": True}) is None
    assert chosen_lens("I would rather not draw a conclusion here.") is None
    assert chosen_lens({"skip": False, "lens": "how_they_hire"}) is None, (
        "an invented lens is not a lens"
    )


def test_the_writer_reads_the_response_and_not_the_parse_result():
    """Pinned at the source, because the bug was invisible in behaviour:
    the sentinel is a legitimate value, so every row the broken line
    wrote looked exactly like a correct decline."""
    i = WORKER.index("failed_lens = (")
    expr = WORKER[i:WORKER.index("LENS_ATTEMPT_PASS_SLOT", i) + len("LENS_ATTEMPT_PASS_SLOT")]
    assert "chosen_lens(response.content)" in expr
    assert "profile" not in expr, "profile is falsy in this branch, by construction"


def test_the_expression_is_not_constant_over_its_reachable_inputs():
    """The diagnosis was made by EXECUTING the old expression over all
    six reachable input combinations and finding one answer every time.
    A constant conditional is the shape to test for, so the replacement
    is executed the same way rather than read."""
    seen = set()
    for content in ({"skip": False, "lens": "how_they_decide", "text": "t", "do": "d"},
                    {"skip": False, "lens": "what_moves_them", "text": "t", "do": "d"},
                    {"skip": True},
                    "no json at all"):
        for defects in ([], ["claim_too_long"]):
            seen.add((chosen_lens(content) if defects else None) or LENS_ATTEMPT_PASS_SLOT)
    assert seen == {"how_they_decide", "what_moves_them", LENS_ATTEMPT_PASS_SLOT}


def test_a_pass_level_decline_stalls_every_pending_lens():
    """A decline means the model looked at the person and produced
    nothing AT ALL, not nothing for one lens, so it cannot be attributed
    to a single lens and must stall all of them."""
    from contextquilt.services.consolidation import build_insight_readiness
    out = build_insight_readiness(
        [], [], today=__import__("datetime").date(2026, 8, 28),
        min_patches=0, min_meetings=0,
        attempt_rows=[{"lens": LENS_ATTEMPT_PASS_SLOT,
                       "attempts": LENS_ATTEMPT_LIMIT,
                       "evidence_at_attempt": 12}],
    )
    states = {r["lens"]: r["state"] for r in out["lenses"]}
    assert any(v == READINESS_STALLED for v in states.values())
    assert READINESS_PENDING_PATTERN not in states.values()


def test_under_the_limit_is_not_stalled_yet():
    """The in-cycle retry already runs once, so the limit is a third and
    fourth chance rather than a first."""
    from contextquilt.services.consolidation import build_insight_readiness
    out = build_insight_readiness(
        [], [], today=__import__("datetime").date(2026, 8, 28),
        min_patches=0, min_meetings=0,
        attempt_rows=[{"lens": LENS_ATTEMPT_PASS_SLOT,
                       "attempts": LENS_ATTEMPT_LIMIT - 1,
                       "evidence_at_attempt": 12}],
    )
    assert all(r["state"] != READINESS_STALLED for r in out["lenses"])


# ------------------------------------------------------------------
# The retry gate is EVIDENCE, not a clock
# ------------------------------------------------------------------

def test_the_cooldown_compares_evidence_and_never_a_timestamp():
    """A person who fails on a given corpus will fail on it tomorrow, so
    a timer would only decide how often we pay for it."""
    body = WORKER[WORKER.index("async def _lens_in_cooldown"):]
    body = body[:body.index("\n    async def ", 10)]
    assert "evidence_at_attempt" in body
    assert "int(evidence or 0) <= int(row[\"evidence_at_attempt\"] or 0)" in body
    for clock in ("last_attempt_at", "NOW()", "INTERVAL", "timedelta"):
        assert clock not in body, f"the gate must not read a clock: {clock}"


def test_growing_evidence_resets_the_count_rather_than_holding_it():
    """New evidence makes it a fresh question, so an old failure must not
    be held against a corpus that no longer exists."""
    body = WORKER[WORKER.index("async def _record_lens_failure"):]
    body = body[:body.index("\n    async def ", 10)]
    assert "WHEN EXCLUDED.evidence_at_attempt" in body
    assert "THEN 1" in body


def test_the_cooldown_runs_BEFORE_the_call_that_costs_money():
    """Checking inside the call would record the failure more neatly and
    save nothing, which is the entire point of the change."""
    body = WORKER[WORKER.index("merged = merge_person_clusters"):]
    body = body[:body.index("return created")]
    gate = body.index("_lens_in_cooldown")
    call = body.index("await self._synthesize_person_cluster(")
    assert gate < call


def test_a_success_clears_the_persons_failures():
    body = WORKER[WORKER.index("merged = merge_person_clusters"):]
    body = body[:body.index("return created")]
    assert "_clear_lens_failures" in body
    assert body.index("if made:") < body.index("_clear_lens_failures")


def test_recording_a_failure_can_never_break_the_pass():
    body = WORKER[WORKER.index("async def _record_lens_failure"):]
    body = body[:body.index("\n    async def ", 10)]
    assert "except Exception" in body and "lens_attempt_not_recorded" in body


# ------------------------------------------------------------------
# The wiring that a source-reading test nearly missed
# ------------------------------------------------------------------

def test_the_readiness_helper_actually_binds_entity_id():
    """THE BUG THIS CAUGHT, in my own code, an hour after writing about
    exactly this shape. The attempts query used `entity_id` inside
    `_person_insight_readiness`, which did not take that parameter. It
    would have raised NameError on the first real call, and the call
    site swallows exceptions into `readiness = None` — so the whole
    readiness surface would have vanished silently and the client's
    absent-readiness branch would have fired on every person page,
    which is the exact catastrophe ShoulderSurf had asked about an hour
    earlier."""
    import ast
    tree = ast.parse(MAIN)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef)
              and n.name == "_person_insight_readiness")
    params = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    assert "entity_id" in params, "the query reads it, so the signature must bind it"
    # and the caller must actually pass a resolved id, not the raw path value
    call = MAIN[MAIN.index("readiness = await _person_insight_readiness("):]
    assert "entity_id=eid" in call[:400]


def test_a_missing_attempts_table_degrades_to_todays_behaviour():
    """It may lag on the MCP deployment's own Postgres. Every lens then
    reads as pending rather than stalled, which is what it does now."""
    body = MAIN[MAIN.index("async def _person_insight_readiness"):]
    body = body[:body.index("\nasync def ", 10)]
    assert "except Exception" in body
    assert "attempt_rows = []" in body
