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
    assert "trajectory_svc.best_change(windows)" in body
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


def test_detail_serves_it_next_to_who_they_are_and_out_of_the_stack():
    i = MAIN.index('if iv.get("lens") == trajectory_svc.LENS:')
    assert "continue" in MAIN[i:i + 500]                      # leaves the capsule stack
    assert '"trajectory": trajectory_card,' in MAIN
    j = MAIN.index('"trajectory": trajectory_card,')
    assert '"who_they_are": who_they_are_card,' in MAIN[j - 300:j]


def test_the_pass_runs_in_the_person_branch_of_consolidation():
    i = WORKER.index("created += await self._derive_who_they_are")
    assert "self._derive_trajectory" in WORKER[i:i + 700]
