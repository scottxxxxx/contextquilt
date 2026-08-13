"""The follow-through lens: the arithmetic, the gate, and the write-up.

The whole point of this lens is that the verdict is computed rather than
judged, so these guards are about the computation. A model can be wrong
about a person and we accept that risk for the prose lenses; it must not
be able to be wrong about a COUNT, choose the lens, or state a number
nothing produced.

The boundary tests matter most: arithmetic does not decline, so the
decline has to be ours, and it has to happen before a call is spent.
"""

import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.consolidation import (
    COMPUTED_LENSES,
    PROFILE_SYSTEM,
    parse_profile_response,
    MAX_SOURCE_TEXTS,
    MODEL_CHOSEN_LENSES,
    PROFILE_LENSES,
    READINESS_AVAILABLE,
    READINESS_PENDING_EVIDENCE,
    READINESS_PENDING_PATTERN,
    READINESS_RETIRED,
    READINESS_SUPPRESSED,
    build_insight_readiness,
    build_profile_content,
    person_insight_rule,
    spread_sample,
)
from contextquilt.services.insight_cards import (
    CARD_SHAPE_RULES,
    TARGET_CLAIM_CHARS,
    TARGET_DO_CHARS,
    CLAIM_LENGTH,
    CLAIM_OPENS_WITH_NAME,
    DO_LENGTH,
    MAX_CLAIM_CHARS,
    MAX_DO_CHARS,
    card_defect,
    opens_with_name,
)
from contextquilt.services.follow_through import (
    FOLLOW_THROUGH_LENS,
    FOLLOW_THROUGH_SYSTEM,
    LATE,
    MIN_JUDGED_ITEMS,
    ON_TIME,
    OPEN_PAST_DUE,
    allowed_numbers,
    build_follow_through_content,
    judge_item,
    judge_items,
    parse_follow_through_response,
    summarize_follow_through,
)

WORKER = (ROOT / "src" / "worker.py").read_text()
MAIN = (ROOT / "src" / "main.py").read_text()
DERIVE_BODY = WORKER.split("async def _derive_follow_through")[1].split(
    "async def _synthesize_cluster"
)[0]

TODAY = date(2026, 8, 13)


def _item(patch_id="p1", origin="m1", due="2026-08-01", completed=None,
          overdue=None, status="active", shelved=None, moves=0, text="ship it"):
    return {
        "patch_id": patch_id, "origin_id": origin, "text": text,
        "deadline_date": due, "completed_at": completed,
        "overdue_since": overdue, "status": status, "shelved_at": shelved,
        "deadline_history": moves,
    }


def _record(n=4, **kw):
    """n judged items across n meetings, one per meeting."""
    return [_item(patch_id=f"p{i}", origin=f"m{i}", **kw) for i in range(n)]


# --- one item's verdict ------------------------------------------------

def test_closed_before_the_date_is_on_time():
    assert judge_item(
        _item(due="2026-08-01", completed=date(2026, 7, 30), status="archived"),
        TODAY,
    ) == ON_TIME


def test_closed_after_the_date_is_late():
    assert judge_item(
        _item(due="2026-08-01", completed=date(2026, 8, 5), status="archived"),
        TODAY,
    ) == LATE


def test_a_sweep_stamp_makes_it_late_even_when_the_close_date_does_not():
    """`overdue_since` means the deadline sweep FOUND the item open past
    its date. That survives the item closing later, and it catches the
    case a date comparison cannot: a due date that was moved after the
    slip, which clears nothing already stamped."""
    assert judge_item(
        _item(due="2026-08-20", completed=date(2026, 8, 12),
              overdue="2026-08-02", status="archived"),
        TODAY,
    ) == LATE


def test_open_past_its_date_counts_open_past_due():
    assert judge_item(_item(due="2026-08-01"), TODAY) == OPEN_PAST_DUE


def test_open_and_not_yet_due_has_no_verdict():
    """Nobody has been asked the question yet, so nothing is counted."""
    assert judge_item(_item(due="2026-09-30"), TODAY) is None


def test_no_due_date_has_no_verdict():
    assert judge_item(_item(due=None), TODAY) is None
    assert judge_item(_item(due="next Friday"), TODAY) is None


def test_shelved_is_not_a_delivery_fact():
    """"Let it go" is the user releasing the item. Holding a person to
    something the user stopped tracking is not something the record says.
    Same exclusion the ledger applies to its open arrays."""
    assert judge_item(_item(due="2026-08-01", shelved="2026-08-02Z"), TODAY) is None


def test_archived_without_a_completion_is_not_a_verdict():
    """Expiry is CQ forgetting, not anyone failing. `done is a claim,
    expired is not` already governs the completion history; the same rule
    governs the counts."""
    assert judge_item(_item(due="2026-08-01", status="archived"), TODAY) is None


def test_a_completed_item_counts_whatever_state_preceded_it():
    """Mirrors the ledger's history leg: done is done, shelved or not."""
    assert judge_item(
        _item(due="2026-08-01", completed=date(2026, 7, 30),
              status="archived", shelved="2026-07-29Z"),
        TODAY,
    ) == ON_TIME


# --- the counts --------------------------------------------------------

def test_counts_add_up_and_meetings_are_distinct():
    rows = [
        _item("a", "m1", "2026-07-01", completed=date(2026, 6, 30), status="archived"),
        _item("b", "m1", "2026-07-02", completed=date(2026, 7, 9), status="archived"),
        _item("c", "m2", "2026-07-03"),
        _item("d", "m3", "2026-12-01"),  # not due yet, uncounted
    ]
    facts = judge_items(rows, TODAY)["facts"]
    assert facts["judged_items"] == 3
    assert facts["meetings"] == 2
    assert facts["closed_on_time"] == 1
    assert facts["closed_late"] == 1
    assert facts["open_past_due"] == 1
    assert (facts["closed_on_time"] + facts["closed_late"]
            + facts["open_past_due"]) == facts["judged_items"]
    assert facts["closed_total"] == 2


def test_moved_due_dates_are_counted_from_the_only_place_they_survive():
    """`value.deadline_history` is written by the dedup path when a
    re-observation carries a different date. Nothing else in CQ remembers
    that a date changed."""
    rows = [_item("a", "m1", "2026-07-01", moves=2),
            _item("b", "m2", "2026-07-02", moves=1),
            _item("c", "m3", "2026-07-03")]
    facts = judge_items(rows, TODAY)["facts"]
    assert facts["items_with_moved_due_date"] == 2
    assert facts["due_date_moves"] == 3


def test_deadline_history_is_read_as_a_list_or_a_count():
    """SQL hands over a length, a raw value hands over the array."""
    listed = judge_items([_item("a", "m1", "2026-07-01",
                                moves=[{"deadline_date": "2026-06-01"}])], TODAY)
    assert listed["facts"]["due_date_moves"] == 1


def test_items_are_totally_ordered_so_two_runs_render_identically():
    rows = [_item("b", "m1", "2026-07-02"), _item("a", "m2", "2026-07-02"),
            _item("c", "m3", "2026-07-01")]
    ids = [i["patch_id"] for i in judge_items(rows, TODAY)["items"]]
    assert ids == ["c", "a", "b"]


def test_every_counted_item_is_a_receipt_the_user_can_open():
    """An item with no origin_id cannot be tapped through to a meeting,
    so it must not raise the meeting count."""
    rows = _record(3) + [_item("x", None, "2026-07-01")]
    facts = judge_items(rows, TODAY)["facts"]
    assert facts["judged_items"] == 4 and facts["meetings"] == 3


# --- the gate (the boundary that matters) ------------------------------

def test_the_gate_admits_exactly_at_the_threshold():
    summary = summarize_follow_through(_record(MIN_JUDGED_ITEMS), TODAY,
                                       min_items=MIN_JUDGED_ITEMS, min_meetings=3)
    assert summary is not None
    assert summary["facts"]["judged_items"] == MIN_JUDGED_ITEMS


def test_one_item_short_declines_before_a_call_is_spent():
    assert summarize_follow_through(
        _record(MIN_JUDGED_ITEMS - 1), TODAY,
        min_items=MIN_JUDGED_ITEMS, min_meetings=3,
    ) is None


def test_enough_items_in_too_few_meetings_declines():
    """The receipts invariant the other lenses carry: a pattern inside
    one meeting is an anecdote wearing a pattern's clothes."""
    rows = [_item(f"p{i}", "m1", "2026-07-01") for i in range(6)]
    assert summarize_follow_through(rows, TODAY, min_items=4, min_meetings=3) is None


def test_the_gate_is_about_volume_not_about_the_verdict():
    """A flawless record and a dismal one both produce a card. This lens
    declines for thin evidence and for nothing else."""
    flawless = [_item(f"p{i}", f"m{i}", "2026-07-01",
                      completed=date(2026, 6, 30), status="archived")
                for i in range(4)]
    assert summarize_follow_through(flawless, TODAY, min_meetings=3) is not None
    assert summarize_follow_through(_record(4), TODAY, min_meetings=3) is not None


def test_an_app_can_raise_the_meeting_gate():
    assert summarize_follow_through(_record(4), TODAY, min_meetings=5) is None


# --- the prompt and the parse ------------------------------------------

def test_prompt_embeds_the_raw_json_shape_and_is_dash_free():
    assert '{"skip":' in FOLLOW_THROUGH_SYSTEM
    assert "—" not in FOLLOW_THROUGH_SYSTEM and "–" not in FOLLOW_THROUGH_SYSTEM


def test_prompt_bans_character_claims_and_invented_numbers():
    lowered = FOLLOW_THROUGH_SYSTEM.lower()
    assert "never character" in lowered
    assert "never state a number the arithmetic did not produce" in lowered


def test_content_carries_the_counts_and_stays_dash_free():
    summary = summarize_follow_through(
        [_item("a", "m1", "2026-07-01", completed=date(2026, 7, 9), status="archived"),
         _item("b", "m2", "2026-07-02"), _item("c", "m3", "2026-07-03"),
         _item("d", "m4", "2026-07-04")],
        TODAY, min_meetings=3,
    )
    content = build_follow_through_content("Suresh", summary["facts"],
                                           summary["items"])
    assert "Suresh" in content
    assert "4" in content and "closed after the due date" in content
    assert "—" not in content and "–" not in content


def test_a_record_with_no_moved_dates_says_nothing_about_moved_dates():
    """Zero in the prompt invites a sentence about an absence nobody
    asked about."""
    summary = summarize_follow_through(_record(4), TODAY, min_meetings=3)
    content = build_follow_through_content("Suresh", summary["facts"], [])
    assert "moved" not in content


def test_the_lens_is_never_read_from_the_response():
    """This pass owns its lens. A model that does not get to pick the
    verdict does not get to pick the card either."""
    out = parse_follow_through_response({
        "skip": False, "lens": "how_they_decide",
        "text": "Commits to a date and lands most of them a week after it.",
        "do": "Ask for the date he will actually hit, not the one he wants.",
    })
    assert out["lens"] == FOLLOW_THROUGH_LENS


def test_no_do_line_no_card():
    """Guardrail 5: a claim never ships without an action."""
    assert parse_follow_through_response({
        "skip": False,
        "text": "Commits to a date and lands most of them a week after it.",
        "do": "",
    }) is None


def test_skip_and_garbage_decline():
    assert parse_follow_through_response({"skip": True, "text": "x", "do": "y"}) is None
    assert parse_follow_through_response("not json at all") is None
    assert parse_follow_through_response(None) is None


def test_a_number_the_arithmetic_did_not_produce_declines_the_claim():
    """The prompt asks; this enforces. Every integer in the claim has to
    be one of the computed counts."""
    facts = judge_items(_record(4), TODAY)["facts"]
    permitted = allowed_numbers(facts)
    good = {"skip": False,
            "text": "Has 4 items past their due date and none closed.",
            "do": "Ask which of the 4 is actually moving this week."}
    assert parse_follow_through_response(dict(good), permitted) is not None
    bad = dict(good, text="Has 17 items past their due date and none closed.")
    assert parse_follow_through_response(bad, permitted) is None


def test_a_character_verdict_declines_the_claim():
    """"Slips twice before landing" is citable. "Unreliable" is a verdict
    about a human being and is not a fact about anything."""
    assert parse_follow_through_response({
        "skip": False, "text": "He is unreliable on dates he agrees to.",
        "do": "Pad every date he gives you.",
    }) is None
    assert parse_follow_through_response({
        "skip": False,
        "text": "Agrees to a date, then slips twice before landing it.",
        "do": "Pad every date he gives you.",
    }) is not None


# --- the vocabulary ----------------------------------------------------

def test_the_computed_lens_is_in_the_vocabulary_but_not_on_offer():
    assert FOLLOW_THROUGH_LENS in PROFILE_LENSES
    assert FOLLOW_THROUGH_LENS in COMPUTED_LENSES
    assert FOLLOW_THROUGH_LENS not in MODEL_CHOSEN_LENSES


def test_the_vocabulary_is_the_union_and_the_halves_are_disjoint():
    assert PROFILE_LENSES == MODEL_CHOSEN_LENSES | COMPUTED_LENSES
    assert not (MODEL_CHOSEN_LENSES & COMPUTED_LENSES)


# --- sampling (the defect this pass also fixes) ------------------------

def test_a_spread_keeps_both_ends_of_the_window():
    """The pass used to send items[:10]: for a person with 39 sources
    that is the OLDEST 10 and nothing from the last two months."""
    picked = spread_sample(list(range(39)), 10)
    assert len(picked) == 10
    assert picked[0] == 0 and picked[-1] == 38
    assert picked == sorted(picked)
    assert max(picked) - min(picked) == 38


def test_a_short_record_is_sent_whole_and_in_order():
    assert spread_sample([1, 2, 3], 10) == [1, 2, 3]
    assert spread_sample([], 5) == []


def test_sampling_is_deterministic():
    seq = list(range(37))
    assert spread_sample(seq, 6) == spread_sample(seq, 6)


def test_the_profile_prompt_now_reaches_recent_behavior():
    dated = [(f"2026-0{1 + i // 10}-{1 + i % 10:02d}", f"obs {i}") for i in range(30)]
    content = build_profile_content("Suresh", dated)
    assert "obs 0" in content and "obs 29" in content
    assert content.count("\n- ") == MAX_SOURCE_TEXTS


# --- readiness ---------------------------------------------------------

def _readiness(rows=(), stamps=(), min_patches=4, min_meetings=3):
    return {
        e["lens"]: e
        for e in build_insight_readiness(
            rows, stamps, today=TODAY,
            min_patches=min_patches, min_meetings=min_meetings,
        )["lenses"]
    }


def test_a_person_with_nothing_is_pending_evidence_with_real_numbers():
    """Scott's motivating case: one meeting in, nothing to show. The
    client can say "three more meetings", not "check back later"."""
    entry = _readiness()[FOLLOW_THROUGH_LENS]
    assert entry["state"] == READINESS_PENDING_EVIDENCE
    assert entry["more_meetings_help"] is True
    assert entry["meetings_observed"] == 0
    assert entry["meetings_required"] == 3
    assert entry["meetings_remaining"] == 3
    assert entry["items_remaining"] == MIN_JUDGED_ITEMS


def test_readiness_covers_every_lens_in_the_vocabulary():
    entries = _readiness()
    assert set(entries) == PROFILE_LENSES
    for entry in entries.values():
        assert set(entry) == {
            "lens", "state", "more_meetings_help", "items_observed",
            "items_required", "items_remaining", "meetings_observed",
            "meetings_required", "meetings_remaining",
        }


def test_a_gate_that_is_met_with_no_card_says_so_rather_than_counting_down():
    """Telling a user "two more meetings" when the threshold is already
    passed and the pass simply found no pattern is a promise CQ cannot
    keep."""
    entry = _readiness(_record(6))[FOLLOW_THROUGH_LENS]
    assert entry["state"] == READINESS_PENDING_PATTERN
    assert entry["meetings_remaining"] == 0
    assert entry["more_meetings_help"] is True


def test_a_suppressed_lens_never_invites_waiting():
    """The addition the client asked for: a rejected card vanishes from
    `insights`, which on the wire looked exactly like a card that was
    never derived. After a reinstall that greeted the user with "keep
    meeting Priya and this fills in" about the claim they threw away."""
    entry = _readiness(
        _record(6),
        [{"lens": FOLLOW_THROUGH_LENS, "status": "archived",
          "archive_cause": "user_delete"}],
    )[FOLLOW_THROUGH_LENS]
    assert entry["state"] == READINESS_SUPPRESSED
    assert entry["more_meetings_help"] is False


def test_a_system_archived_card_is_retired_not_suppressed():
    """Both are gone for good, because the durable no ignores status.
    They are not the same fact about the user, so they are not the same
    word."""
    entry = _readiness(
        _record(6),
        [{"lens": FOLLOW_THROUGH_LENS, "status": "archived",
          "archive_cause": "decay"}],
    )[FOLLOW_THROUGH_LENS]
    assert entry["state"] == READINESS_RETIRED
    assert entry["more_meetings_help"] is False


def test_an_active_stamp_reads_available():
    entry = _readiness(
        _record(6),
        [{"lens": FOLLOW_THROUGH_LENS, "status": "active", "archive_cause": None}],
    )[FOLLOW_THROUGH_LENS]
    assert entry["state"] == READINESS_AVAILABLE
    assert entry["more_meetings_help"] is False


def test_lenses_are_independent_so_a_person_is_never_capped_at_one():
    """A person carries a card per lens. Suppressing one says nothing
    about the others, and neither does deriving one."""
    entries = _readiness(
        _record(6),
        [{"lens": "how_they_decide", "status": "active", "archive_cause": None},
         {"lens": "what_moves_them", "status": "archived",
          "archive_cause": "user_delete"}],
    )
    assert entries["how_they_decide"]["state"] == READINESS_AVAILABLE
    assert entries["what_moves_them"]["state"] == READINESS_SUPPRESSED
    assert entries[FOLLOW_THROUGH_LENS]["state"] == READINESS_PENDING_PATTERN


def test_the_two_lens_families_count_different_things():
    """The model lenses count what their cluster SQL counts: ACTIVE items
    carrying a meeting. The computed lens counts items whose date has
    come due, which is mostly items that already closed, and closing
    archives the row."""
    rows = [_item(f"p{i}", f"m{i}", "2026-07-01", completed=date(2026, 7, 9),
                  status="archived") for i in range(5)]
    entries = _readiness(rows)
    assert entries["how_they_decide"]["items_observed"] == 0
    assert entries[FOLLOW_THROUGH_LENS]["items_observed"] == 5


def test_readiness_numbers_are_always_finite_ints():
    """GP serializes with allow_nan=False: one non-finite float would 502
    the whole person payload."""
    for entry in _readiness(_record(6)).values():
        for key, value in entry.items():
            if key.endswith(("_observed", "_required", "_remaining")):
                assert isinstance(value, int)


def test_the_gate_reported_is_the_gate_the_pass_runs_on():
    """"Two more meetings" is only honest if it counts toward the number
    that actually gates the derivation, so the rule's thresholds are
    read from the manifest, never hardcoded here."""
    rule = person_insight_rule({
        "patch_types": [{"domain_type": "commitment"}, {"domain_type": "insight"}],
        "consolidation_rules": [{
            "cluster": "person", "from_types": ["commitment"],
            "produce_type": "insight", "min_patches": 7, "min_meetings": 5,
        }],
    })
    entry = _readiness(min_patches=rule["min_patches"],
                       min_meetings=rule["min_meetings"])["how_they_decide"]
    assert entry["items_required"] == 7 and entry["meetings_required"] == 5


def test_person_insight_rule_is_none_without_a_person_clustered_rule():
    assert person_insight_rule({"patch_types": [], "consolidation_rules": []}) is None
    assert person_insight_rule(None) is None


# --- worker wiring -----------------------------------------------------

def test_the_pass_declines_in_code_before_it_spends_a_call():
    before = DERIVE_BODY.split("self.llm.extract")[0]
    assert "if summary is None:" in before
    assert "summarize_follow_through" in before


def test_the_durable_no_is_status_blind_for_this_lens_too():
    gate = DERIVE_BODY.split("AND NOT EXISTS")[1].split(")")[0]
    assert "d.value->>'lens' = $9" in gate
    assert "status" not in gate


def test_the_receipts_are_the_items_the_arithmetic_counted():
    assert '[i["patch_id"] for i in items]' in DERIVE_BODY


def test_the_post_check_reruns_the_taken_set_after_the_call():
    after = DERIVE_BODY.split("self.llm.extract")[1]
    assert "_taken_lenses" in after
    assert after.index("_taken_lenses") < after.index("_write_person_insight")


def test_the_self_person_is_excluded_like_the_other_pass():
    assert "self_at IS NOT NULL" in DERIVE_BODY
    assert "<> lower(btrim($8))" in DERIVE_BODY


def test_completable_types_come_from_the_facet_runtime_not_a_hardcode():
    """A completable is a completable because a manifest said so."""
    assert "completable_types" in DERIVE_BODY
    assert "commitment" not in DERIVE_BODY and "blocker" not in DERIVE_BODY


# --- the served surface ------------------------------------------------

def test_readiness_is_served_on_the_detail_route():
    assert '"insight_readiness": readiness' in MAIN


def test_readiness_is_null_when_the_app_cannot_produce_insights():
    assert "readiness: Optional[dict] = None" in MAIN
    assert "if insights_available and person_rule:" in MAIN


def test_the_computed_facts_are_served_beside_the_claim():
    assert '"facts": iv.get("facts")' in MAIN


# --- the card the claim has to fit in ---------------------------------

CLAIM = "Lands about half of what he commits to, usually a week late."
DO = "Ask for the date he will actually hit, not the one he wants."


def test_the_shipped_claims_would_all_have_been_rejected():
    """The four live insights ran 97 to 177 characters because nothing
    told the model otherwise, and the collapsed capsule is one line."""
    too_long = "Gates forward movement until verification is in place " * 3
    assert card_defect(too_long, DO) == CLAIM_LENGTH
    assert card_defect(CLAIM, "Do this. " * 20) == DO_LENGTH
    assert card_defect(CLAIM, DO) is None


def test_the_ceilings_are_enforced_at_the_boundary():
    assert card_defect("x" * MAX_CLAIM_CHARS, DO) is None
    assert card_defect("x" * (MAX_CLAIM_CHARS + 1), DO) == CLAIM_LENGTH
    assert card_defect(CLAIM, "y" * MAX_DO_CHARS) is None
    assert card_defect(CLAIM, "y" * (MAX_DO_CHARS + 1)) == DO_LENGTH


def test_a_claim_never_opens_with_the_name_the_page_already_shows():
    """Every shipped claim opened with the person's name, on a page
    titled with that name, spending characters the budget does not have.
    Fixed at the generator: editing served words on the client is the
    pattern this workstream has been retiring."""
    assert opens_with_name("Priya gates forward movement.", "Priya Raman")
    assert opens_with_name("Raman gates forward movement.", "Priya Raman")
    assert not opens_with_name("Gates forward movement, Priya style.", "Priya")
    assert card_defect("Priya gates it until verified.", DO, "Priya") == \
        CLAIM_OPENS_WITH_NAME


def test_both_lens_families_enforce_the_same_card():
    """One card, one set of limits. A new lens cannot forget one."""
    long_claim = "x" * (MAX_CLAIM_CHARS + 20)
    assert parse_follow_through_response(
        {"skip": False, "text": long_claim, "do": DO}) is None
    assert parse_profile_response(
        {"skip": False, "lens": "how_they_decide", "text": long_claim, "do": DO}
    ) is None
    assert parse_follow_through_response(
        {"skip": False, "text": "Suresh slips twice before landing.", "do": DO},
        None, "Suresh") is None
    assert parse_profile_response(
        {"skip": False, "lens": "how_they_decide",
         "text": "Suresh slips twice before landing.", "do": DO},
        person_name="Suresh") is None


def test_a_rejected_format_is_reported_separately_from_a_decline():
    """A model choosing to skip and a model answering in a shape the card
    cannot hold are different events, and a run of the second would
    silently stall the pass."""
    defects = []
    parse_follow_through_response(
        {"skip": False, "text": "x" * 200, "do": DO}, None, None, defects)
    assert defects == [CLAIM_LENGTH]
    quiet = []
    parse_follow_through_response({"skip": True}, None, None, quiet)
    assert quiet == []


def test_the_computed_lens_can_satisfy_the_ceiling_and_the_numbers_at_once():
    """The hardest brief any lens has: a real count inside 62
    characters. If this cannot be met the ceiling is wrong, so it is
    pinned with a claim that meets it."""
    facts = judge_items(_record(4), TODAY)["facts"]
    numbered = "4 dated items are past due and none have closed."
    assert len(numbered) <= MAX_CLAIM_CHARS
    assert parse_follow_through_response(
        {"skip": False, "text": numbered, "do": DO},
        allowed_numbers(facts), "Suresh",
    ) is not None
    assert len(CLAIM) <= MAX_CLAIM_CHARS


def test_both_prompts_state_the_limits_and_stay_dash_free():
    for prompt in (FOLLOW_THROUGH_SYSTEM, PROFILE_SYSTEM):
        assert CARD_SHAPE_RULES in prompt
        assert str(MAX_CLAIM_CHARS) in prompt and str(MAX_DO_CHARS) in prompt
        assert "hard limit" in prompt
        assert "—" not in prompt and "–" not in prompt


def test_the_closed_total_is_published_because_the_model_reaches_for_it():
    """Measured live: asked to write up 2 on time and 7 late, the model
    writes "9 of 20 due items closed", which is true and is a count over
    these same items. A number the arithmetic can produce but did not
    publish gets published, never forbidden."""
    rows = [_item("a", "m1", "2026-07-01", completed=date(2026, 6, 1), status="archived"),
            _item("b", "m2", "2026-07-02", completed=date(2026, 7, 9), status="archived"),
            _item("c", "m3", "2026-07-03")]
    facts = judge_items(rows, TODAY)["facts"]
    assert facts["closed_total"] == 2
    assert 2 in allowed_numbers(facts)
    content = build_follow_through_content("Suresh", facts, [])
    assert "closed at all" in content


def test_the_prompt_asks_for_less_than_the_parse_allows():
    """Measured against the live model at temperature 0: asked for "at
    most 62 characters" it returned 65 on every one of five identical
    calls. A pinned temperature makes that a person who never gets a
    card, not a retry lottery, so the ASK absorbs the overshoot."""
    assert TARGET_CLAIM_CHARS < MAX_CLAIM_CHARS
    assert TARGET_DO_CHARS < MAX_DO_CHARS
    assert str(TARGET_CLAIM_CHARS) in CARD_SHAPE_RULES
    assert str(TARGET_DO_CHARS) in CARD_SHAPE_RULES


def test_the_number_rule_covers_the_do_line():
    """Also measured live: the do line was where the invented number
    kept appearing ("Ask which 3 items to prioritize")."""
    facts = judge_items(_record(4), TODAY)["facts"]
    assert parse_follow_through_response(
        {"skip": False, "text": "4 dated items are past due.",
         "do": "Ask which 3 items are actually moving."},
        allowed_numbers(facts),
    ) is None
    assert "do line too" in FOLLOW_THROUGH_SYSTEM
