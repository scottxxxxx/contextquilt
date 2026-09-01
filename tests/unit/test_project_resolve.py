"""Resolving a project reference, and refusing to guess.

Written 2026-08-31 after a client-side repair matched projects BY NAME
and pointed one project's record at another's id. Scott opened
"Immigration  Interview App" and saw CBE's work: the app asked CQ for
CBE's project id under the Immigration heading, and CQ filtered exactly
as asked. His ruling: repairs match on ID, never on name.

The collisions below are REAL rows from his account, which is why "just
normalise and match" was never available.
"""

import pytest

from contextquilt.services.project_resolve import (
    AMBIGUOUS,
    BY_ID,
    EXACT,
    NORMALIZED,
    RESOLVED,
    UNKNOWN,
    normalize,
    resolve,
)

# Live data, 2026-08-31. 'CBE' and 'Cbe' are two different projects, and
# the third has a double space in its stored name.
ROWS = [
    {"project_id": "548CC5BB", "name": "CBE", "status": "active",
     "patch_count": 29, "meeting_count": 2},
    {"project_id": "2B3879AE", "name": "Cbe", "status": "active",
     "patch_count": 36, "meeting_count": 3},
    {"project_id": "10FF20F9", "name": "Immigration  Interview App",
     "status": "active", "patch_count": 77, "meeting_count": 3},
]


# --------------------------------------------------------------------
# By id: the question a repair should actually be asking
# --------------------------------------------------------------------

def test_a_known_id_resolves_and_returns_cqs_own_name():
    out = resolve(ROWS, project_id="548CC5BB")
    assert out["status"] == RESOLVED and out["match"] == BY_ID
    assert out["name"] == "CBE"


def test_an_id_cq_does_not_hold_is_the_most_actionable_answer():
    """The exact state that produced the incident.

    A client scoped to an id CQ does not have gets a correct, empty,
    unremarkable response from every other endpoint. Here it gets told.
    """
    out = resolve(ROWS, project_id="DOES-NOT-EXIST")
    assert out["status"] == UNKNOWN
    assert out["project_id"] is None


def test_an_id_never_falls_back_to_a_name_match():
    # A repair asking by id has asserted it holds an id. Quietly
    # answering a different question with the name it also happens to
    # have is how a validation becomes a guess.
    out = resolve(ROWS, project_id="NOPE", name="CBE")
    assert out["status"] == UNKNOWN


# --------------------------------------------------------------------
# By name: three answers, one of which is "I will not say"
# --------------------------------------------------------------------

def test_an_exact_name_wins_outright_even_when_others_normalise_to_it():
    """'CBE' is unambiguous as a string even though 'Cbe' exists.

    Treating it as ambiguous would make the endpoint useless for the
    common case in order to be careful about the rare one.
    """
    out = resolve(ROWS, name="CBE")
    assert out["status"] == RESOLVED and out["match"] == EXACT
    assert out["project_id"] == "548CC5BB"


def test_two_names_that_normalise_alike_are_AMBIGUOUS_not_a_best_guess():
    """The whole point of the module.

    Guessing is what caused the incident, and an endpoint that returned
    a best match would move the guess from the client into CQ without
    removing it.
    """
    out = resolve(ROWS, name="cbe")
    assert out["status"] == AMBIGUOUS
    assert out["project_id"] is None
    assert {c["project_id"] for c in out["candidates"]} == {"548CC5BB", "2B3879AE"}


def test_the_ambiguous_answer_carries_counts_for_a_human_to_choose():
    # And a client that ranks these and takes the largest has reinvented
    # the bug. The counts are for a person, and the docstring says so.
    out = resolve(ROWS, name="cbe")
    for c in out["candidates"]:
        assert "patch_count" in c and "meeting_count" in c and "name" in c


def test_candidate_order_does_not_depend_on_the_order_rows_arrive_in():
    """The property that matters, which is not mere repeatability.

    The first version of this test called `resolve` three times with the
    SAME list and asserted the answers agreed. A sabotage that reversed
    the ordering passed it, correctly: reversal is perfectly repeatable.
    Postgres does not promise row order without an ORDER BY, so the real
    risk is the same two projects arriving in a different sequence
    between calls and a caller keyed on position silently changing its
    mind.
    """
    forward = [c["project_id"] for c in resolve(ROWS, name="cbe")["candidates"]]
    backward = [c["project_id"] for c in resolve(list(reversed(ROWS)), name="cbe")["candidates"]]
    assert forward == backward
    assert forward == sorted(forward, key=lambda pid: {
        "548CC5BB": "CBE", "2B3879AE": "Cbe"}[pid])


def test_a_whitespace_variant_resolves_to_the_stored_name():
    """The double space is real and a client will not reproduce it.

    Only one project normalises to this, so it is a resolution rather
    than a guess, and the response carries CQ's exact stored spelling so
    the caller can correct its own copy.
    """
    out = resolve(ROWS, name="Immigration Interview App")
    assert out["status"] == RESOLVED and out["match"] == NORMALIZED
    assert out["name"] == "Immigration  Interview App"
    assert out["project_id"] == "10FF20F9"


@pytest.mark.parametrize("bad", [None, "", "   ", "no such project"])
def test_an_unresolvable_name_is_unknown_rather_than_an_error(bad):
    out = resolve(ROWS, name=bad)
    assert out["status"] == UNKNOWN and out["project_id"] is None


def test_no_projects_at_all_is_unknown_not_a_crash():
    assert resolve([], name="CBE")["status"] == UNKNOWN
    assert resolve([], project_id="548CC5BB")["status"] == UNKNOWN


def test_normalize_is_only_used_to_find_candidates_never_to_pick():
    # Asserted on behaviour rather than trusted to the comment: two
    # names that normalise alike must never produce a project_id.
    assert normalize("Immigration  Interview App") == "immigration interview app"
    assert resolve(ROWS, name="CBE ")["status"] == AMBIGUOUS


# --------------------------------------------------------------------
# The wire
# --------------------------------------------------------------------

def test_a_resolved_answer_never_ships_candidates_and_the_reverse():
    # One shape per answer, so a client cannot read a candidate list off
    # a resolved response and treat it as a choice.
    assert resolve(ROWS, name="CBE")["candidates"] == []
    assert resolve(ROWS, name="cbe")["project_id"] is None


def test_the_route_echoes_the_query_it_received():
    """So a dropped parameter is visible rather than inferred.

    A middlebox has eaten a query param on this system before, and an
    empty answer to a question that never arrived looks exactly like an
    empty answer to one that did.
    """
    from pathlib import Path
    main = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
    body = main[main.index("async def resolve_project("):]
    body = body[:body.index("@app.post(\"/v1/projects/{user_id}\"")]
    assert '"query": {"name": name, "project_id": project_id}' in body
    assert 'logger.info("project_resolve"' in body


def test_a_candidates_project_status_is_not_called_status():
    """One name must not answer two questions in one document.

    The top level uses `status` for the discriminator. A candidate's own
    lifecycle status nested inside it under the same name is the
    `total_available` collision again: two right answers, one name, and
    a reader cannot tell which question a value answers. Renamed before
    the decoder existed rather than after.
    """
    out = resolve(ROWS, name="cbe")
    assert out["status"] == AMBIGUOUS
    for c in out["candidates"]:
        assert "project_status" in c
        assert "status" not in c, (
            "a candidate carries `status`, which at the top level means "
            "the discriminator"
        )
