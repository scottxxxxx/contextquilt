"""Wiring for the 5.15 hero card: measurement in the worker, serving on
person detail. The services were merged in #307 with 107 tests and
NOTHING imported them (doc 16 5.15 was marked NOT SERVED); Scott looked
at Suresh on his phone on 2026-08-24 and asked where the changes were.
"""
import pathlib

from contextquilt.services import trajectory as trajectory_svc

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
WORKER = (SRC / "worker.py").read_text()
MAIN = (SRC / "main.py").read_text()


def test_the_services_are_finally_imported_by_both_halves():
    assert "from contextquilt.services import trajectory as trajectory_svc" in WORKER
    assert "from contextquilt.services import trajectory as trajectory_svc" in MAIN


def test_served_reassembles_the_515_shape_and_drops_the_fingerprint():
    card = trajectory_svc.served({
        "facts": {"measure_key": "closed_late", "earlier": {"numerator": 2},
                  "recent": {"numerator": 5}, "fingerprint": "abc123"},
        "text": "t", "narrative": "n", "do": "d",
    })
    assert card["lens"] == trajectory_svc.LENS
    assert card["display_order"] == trajectory_svc.DISPLAY_ORDER
    assert card["measure_key"] == "closed_late"
    assert "fingerprint" not in card
    assert (card["text"], card["narrative"], card["do"]) == ("t", "n", "d")
    assert trajectory_svc.served({"facts": {}}) is None
    assert trajectory_svc.served({}) is None


def _pass():
    i = WORKER.index("async def _derive_trajectory")
    return WORKER[i:WORKER.index("async def _person_entity_resolver", i)]


def test_the_pass_wires_only_the_constructible_measures():
    """questions_to_you is in MEASURES and deliberately NOT built:
    migration 37 attributes questions the user receives to the user
    block in aggregate, never to the asker's row. Building it from
    questions_asked would be a different claim wearing this label."""
    body = _pass()
    assert '"speaking_turns": (turn_window' in body
    assert '"closed_late": (closed_window' in body
    code = '"'*3 + ('"'*3).join(body.split('"'*3)[2:])      # everything after the docstring
    assert "questions_to_you" not in code                     # only the docstring may name it
    assert "questions_asked" not in code


def test_the_pass_splits_on_sequence_and_uses_the_service_gates():
    body = _pass()
    assert "trajectory_svc.split_meetings(newest_first)" in body
    assert "trajectory_svc.best_change(windows, held_key=held_key)" in body
    assert "trajectory_svc.allowed_numbers(facts)" in body
    assert "ORDER BY max(pa.last_seen_at) DESC" in body       # sequence proxy, never served
    assert "served_trajectory" in body


def test_owner_resolves_through_the_entity_graph():
    """Owner text alone missed 97 of 110 items in the insight rework;
    the resolver is the identity lesson and the pass must use it."""
    body = _pass()
    assert "resolve_person_entity(it[\"owner\"] or \"\")" in body
    assert "who != eid" in body


def test_durable_no_and_fingerprint_gate_before_any_model_call():
    body = _pass()
    gate = body.index('if any(r["status"] != "active" for r in existing)')
    call = body.index("self.llm.extract")
    assert gate < call
    assert 'if any(r["fp"] == fingerprint for r in existing)' in body


# ------------------------------------------------------------------
# Hysteresis (Scott's ruling 2026-08-26): a live card is judged at the
# hold floors, and comes down as `lapsed` when even those fail, rather
# than standing on numbers that are no longer true or flickering.
# ------------------------------------------------------------------

def test_the_live_card_is_read_before_the_gates_and_names_its_measure():
    body = _pass()
    fetch = body.index("d.value->'facts'->>'measure_key' AS measure_key")
    pick = body.index("trajectory_svc.best_change(windows, held_key=held_key)")
    assert fetch < pick
    assert 'held_key = live[0]["measure_key"] if live else None' in body
    assert body.count("FROM context_patches d") == 1        # one fetch, reused below


def test_a_live_card_that_fails_the_hold_floor_is_archived_as_lapsed():
    body = _pass()
    i = body.index("if not chosen:")
    block = body[i:i + 900]
    assert "'\"lapsed\"'::jsonb" in block
    assert "for r in live:" in block
    assert "trajectory_lapsed" in block
    # lapsed is not a durable no: the existing fetch skips lapsed rows the
    # same way it skips replaced ones, so the person can earn a new card
    assert "NOT IN ('replaced', 'lapsed')" in body


def test_detail_serves_it_next_to_who_they_are_and_out_of_the_stack():
    i = MAIN.index('if iv.get("lens") == trajectory_svc.LENS:')
    assert "continue" in MAIN[i:i + 500]                      # leaves the capsule stack
    assert '"trajectory": trajectory_card,' in MAIN
    j = MAIN.index('"trajectory": trajectory_card,')
    assert '"who_they_are": who_they_are_card,' in MAIN[j - 300:j]


# ------------------------------------------------------------------
# The hero pass has its OWN budget, and says so when it cannot run
# (ruled 2026-08-27). It was last in a fixed order on a shared pool of 3
# and went dark 08-25 to 08-27 while Suresh cleared the entry floor at
# +40.7 percent, logging nothing, because "no budget" and "nobody
# qualified" produced the identical silence.
# ------------------------------------------------------------------

def _consolidate_user_body():
    i = WORKER.index("async def _consolidate_user(")
    return WORKER[i:WORKER.index("async def people_network_loop", i)]


def test_the_hero_pass_gets_its_own_budget_not_the_remainder():
    body = _consolidate_user_body()
    call = body.index("self._derive_trajectory(")
    args = body[call:call + 220]
    assert "MAX_TRAJECTORY_PER_USER_PER_CYCLE" in args
    # the remainder of the shared pool must not appear in ITS arguments
    assert "MAX_CLUSTERS_PER_USER_PER_CYCLE - created" not in args
    # and it is imported rather than re-declared in the worker
    assert "MAX_TRAJECTORY_PER_USER_PER_CYCLE," in WORKER.split("async def")[0]


def test_hero_cards_are_reported_but_never_charged_to_the_shared_pool():
    """Folding them into `created` would make the separate budget
    cosmetic: the hero cards would push the shared passes out instead."""
    body = _consolidate_user_body()
    assert body.count("hero_created += await self._derive_trajectory(") == 1
    # the ONLY `... += await self._derive_trajectory(` is the hero one, so
    # nothing adds it to the shared counter. Counted rather than asserted
    # absent, because "hero_created" ends in "created" and the naive
    # `not in` check passes on the very line it is meant to forbid.
    assert body.count("created += await self._derive_trajectory(") == 1
    assert "return created + hero_created" in body


def test_the_person_branch_is_reachable_when_the_shared_pool_is_spent():
    """The second door. A cue rule spending the pool used to break the
    rule loop before the person rule was reached, so the hero lens never
    ran at all and its own budget would have been unreachable."""
    body = _consolidate_user_body()
    assert "if created >= MAX_CLUSTERS_PER_USER_PER_CYCLE and not is_person_rule:" in body
    guard = body.index("if created >= MAX_CLUSTERS_PER_USER_PER_CYCLE and not is_person_rule:")
    branch = body.index("if is_person_rule:")
    assert guard < branch


def test_the_shared_passes_are_never_handed_a_negative_budget():
    """The branch is now reachable with the pool already spent, so the
    remainder can go negative without the clamp."""
    body = _consolidate_user_body()
    for pass_name in ("_consolidate_user_people", "_derive_follow_through",
                      "_derive_stands_out", "_derive_who_they_are"):
        i = body.index(f"self.{pass_name}(")
        assert "max(0, MAX_CLUSTERS_PER_USER_PER_CYCLE - created)" in body[i:i + 200], pass_name


def test_starvation_is_logged_at_info_rather_than_being_silent():
    body = _pass()
    assert body.count("trajectory_budget_exhausted") == 2
    assert 'logger.info("trajectory_budget_exhausted"' in body
    assert 'reason="no_budget_on_entry"' in body      # handed nothing
    assert 'reason="budget_reached"' in body          # ran out part way
    # the count is what was observed, so it comes from the loop position
    assert "people_unexamined=len(people) - index" in body
    assert "for index, person in enumerate(people):" in body


def test_the_pass_runs_in_the_person_branch_of_consolidation():
    i = WORKER.index("created += await self._derive_who_they_are")
    assert "self._derive_trajectory" in WORKER[i:i + 700]


# ------------------------------------------------------------------
# The counted-meetings denominator is not the stretch size (2026-08-24,
# Sukumar's live card: "across your first 7 meetings" for 7-of-8).
# ------------------------------------------------------------------

def _rate_facts(e_den=7, e_meet=8, r_den=5, r_meet=8):
    return {"pair_kind": "rate", "measure_key": "speaking_turns", "valence": "neutral",
            "movement": "down", "span_meetings": 16,
            "earlier": {"numerator": 188, "denominator": e_den, "meetings": e_meet},
            "recent": {"numerator": 35, "denominator": r_den, "meetings": r_meet}}


def test_counted_number_called_the_stretch_is_rejected_on_a_rate():
    from contextquilt.services.trajectory import conflates_counted_with_stretch
    f = _rate_facts()
    assert conflates_counted_with_stretch("His turns totaled 188 across your first 7 meetings together", f)
    assert conflates_counted_with_stretch("against 35 across the 5 meetings that followed", f)
    assert not conflates_counted_with_stretch("188 turns, counted in 7 of the 8 earlier meetings", f)


def test_a_fully_counted_stretch_keeps_its_natural_sentence():
    from contextquilt.services.trajectory import conflates_counted_with_stretch
    assert not conflates_counted_with_stretch("across your first 8 meetings", _rate_facts(e_den=8))


def test_proportions_are_never_checked_by_this_gate():
    from contextquilt.services.trajectory import conflates_counted_with_stretch
    f = dict(_rate_facts(), pair_kind="proportion")
    assert not conflates_counted_with_stretch("your first 7 meetings", f)


def test_the_prompt_states_both_numbers_for_a_rate():
    from contextquilt.services.trajectory import build_trajectory_content
    c = build_trajectory_content("Sukumar", _rate_facts())
    assert "EARLIER stretch of 8 meetings: 188 speaking turns in total, counted in 7 of those meetings" in c
    assert "Never call the counted number" in c


def test_parse_rejects_it_and_the_retry_note_names_it():
    from contextquilt.services.trajectory import parse_trajectory_response, retry_note, WINDOW_SIZE_CONFLATED, allowed_numbers
    f = _rate_facts()
    d = []
    out = parse_trajectory_response(
        {"skip": False, "text": "His turns totaled 188 across your first 7 meetings together, against 35 across the 5 meetings that followed.",
         "narrative": "The shift is only visible in aggregate across the 16 meetings; no single meeting shows it, and the totals say what moved.",
         "do": "Check the agenda length against the ground you need to cover."},
        permitted=allowed_numbers(f), person_name="Sukumar", defects=d, facts=f,
    )
    assert out is None and WINDOW_SIZE_CONFLATED in d
    assert "counted in N of those meetings" in retry_note(WINDOW_SIZE_CONFLATED)
