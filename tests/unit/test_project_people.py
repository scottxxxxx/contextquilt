"""Explicit project membership: the user SAYING who is on a project.

Scott retired ShoulderSurf's "Contact" project note on 2026-08-31 and
this replaces it. That note stored a person as PROSE inside a project:
no entity, no aliases, no appearances, no insights, no ledger, no
`owed_to`, and no way to correct, merge or rename them. It also never
reached CQ at all. It lived in a CloudKit blob and was flattened into
project-chat prompts as an untyped bullet, so a model saw it many times
and CQ never did.

Read as source, the constraint every route test here works under.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()
MIGRATION = (ROOT / "init-db" / "47_project_people.sql").read_text()

from contextquilt.services.project_roster import merge_roster


def _route(name: str) -> str:
    body = MAIN[MAIN.index(f"async def {name}("):]
    nxt = body.index("\n@app.", 1)
    return body[:nxt]


# --------------------------------------------------------------------
# The mechanism, and why it is not the obvious one
# --------------------------------------------------------------------

def test_membership_is_keyed_on_entity_id_not_a_person_patch():
    """A person holds several patches, one per surface form.

    Which one is "current" changes as the extractor rephrases them, and
    insights keyed on a patch id went unreachable for exactly that
    reason (2026-08-16). The entity is the identity that does not move.
    """
    assert "entity_id     UUID        NOT NULL" in MIGRATION
    assert "PRIMARY KEY (user_id, project_id, entity_id)" in MIGRATION


def test_membership_is_keyed_on_the_stable_project_id():
    # A name is what caused the incident this was designed during: four
    # of this user's projects normalise to "cbe" and one carries a
    # double space.
    assert "project_id    TEXT        NOT NULL" in MIGRATION


def test_the_migration_records_why_works_on_was_rejected():
    """The obvious mechanism, measured and ruled out.

    `works_on` connects a person PATCH to a project PATCH. On live data
    140 of 160 project rows have no project patch at all, 329 project
    patches carry no project_id, and 315 of 357 works_on edges point at
    a project patch that cannot be resolved back to a project row.
    """
    assert "works_on" in MIGRATION
    assert "140 of 160" in MIGRATION


def test_rows_are_retained_rather_than_deleted():
    # "They are not on this after all" is a statement, and it must stay
    # distinguishable from never having been added. Same argument as
    # shelve and description dismissal.
    assert "removed_at" in MIGRATION
    upper = MIGRATION.upper()
    assert "DELETE FROM" not in upper and "DROP TABLE" not in upper
    assert "removed_at = NOW()" in _route("remove_project_person")


def test_the_indexes_match_the_two_real_access_patterns():
    assert "idx_project_people_project" in MIGRATION
    assert "idx_project_people_entity" in MIGRATION
    assert MIGRATION.count("WHERE removed_at IS NULL") >= 2


# --------------------------------------------------------------------
# One identity-authoring path, not two
# --------------------------------------------------------------------

def test_a_name_resolves_through_the_SAME_path_as_creating_a_person():
    """A second identity-authoring path is a second source of truth.

    A name typed here and the same name typed onto a speaker must land
    on one row. `_resolve_or_create_person` is shared by POST /v1/people
    and reassign-speaker's `to_name`, and it writes the entity AND the
    declared person patch so nothing arrives half created.
    """
    body = _route("add_project_person")
    assert "_resolve_or_create_person(" in body
    assert "INSERT INTO entities" not in body, (
        "the route authors identity itself instead of delegating"
    )


def test_an_unknown_project_is_refused_rather_than_stored():
    """Storing a membership against an id CQ does not hold writes a fact
    nothing can ever read, and an unknown project id is precisely the
    state that produced tonight's incident."""
    body = _route("add_project_person")
    assert "no such project for this user" in body
    assert "status_code=404" in body


def test_adding_someone_twice_is_a_no_op_rather_than_an_error():
    # The client cannot always know, and a 409 would make a no-op look
    # like a failure. `membership_created` says which happened.
    body = _route("add_project_person")
    assert "ON CONFLICT (user_id, project_id, entity_id) DO UPDATE" in body
    assert '"membership_created"' in body


def test_re_adding_clears_a_removal_rather_than_writing_a_second_row():
    body = _route("add_project_person")
    assert "removed_at = NULL" in body


def test_the_response_echoes_what_actually_happened():
    """A 200 says the request was processed, never that it did what the
    caller meant. Both booleans are separate facts: a person can be
    created without the membership being new, and the reverse."""
    body = _route("add_project_person")
    for key in ('"person_created"', '"membership_created"', '"name"',
                '"entity_id"'):
        assert key in body


def test_the_stored_spelling_is_returned_not_the_callers():
    # So a client can correct its own copy, the same reason resolve
    # returns CQ's exact name.
    assert '"name": resolved["name"]' in _route("add_project_person")


# --------------------------------------------------------------------
# The read: declared and observed stay distinguishable
# --------------------------------------------------------------------

def test_declared_and_observed_are_never_flattened_into_one_list():
    """EXECUTED, because a source-reading version of this passed a sabotage.

    Deleting the line that promotes a person to `both` left every
    string-matching test green: they checked that "both" appears in
    main.py, and it also appears in the docstring. A person who is
    declared AND observed would have been reported as merely declared,
    with a wrong observed count, silently.
    """
    out = merge_roster(
        declared=[{"entity_id": "e1", "name": "Marianne", "added_at": None}],
        observed=[{"entity_id": "e1", "name": "Marianne", "meetings": 4},
                  {"entity_id": "e2", "name": "Vijay", "meetings": 9}],
    )
    by = {p["entity_id"]: p for p in out["people"]}
    assert by["e1"]["source"] == "both"
    assert by["e1"]["meetings"] == 4
    assert by["e2"]["source"] == "observed"
    assert out["declared_count"] == 1 and out["observed_count"] == 2


def test_a_declared_person_with_no_meetings_survives_the_merge():
    """The entire point of the endpoint.

    Someone relevant who has never been in a recorded meeting was
    invisible before, and is exactly who a user reaches for the feature
    to add. The observed leg knows nothing about them.
    """
    out = merge_roster(
        declared=[{"entity_id": "e9", "name": "Marianne", "added_at": None}],
        observed=[],
    )
    assert [p["entity_id"] for p in out["people"]] == ["e9"]
    assert out["people"][0]["source"] == "declared"
    assert out["people"][0]["meetings"] == 0


def test_the_meeting_count_comes_from_the_observed_leg():
    # The declared leg has no idea how many meetings anyone attended,
    # so a merge that kept its zero would under-report a real member.
    out = merge_roster(
        declared=[{"entity_id": "e1", "name": "A", "added_at": None}],
        observed=[{"entity_id": "e1", "name": "A", "meetings": 12}])
    assert out["people"][0]["meetings"] == 12


def test_the_roster_order_does_not_depend_on_the_order_rows_arrive_in():
    # Postgres promises no order without an ORDER BY, and a client keyed
    # on position would silently change its mind between calls.
    d = [{"entity_id": "e1", "name": "Zoe", "added_at": None},
         {"entity_id": "e2", "name": "Adam", "added_at": None}]
    o = [{"entity_id": "e3", "name": "Mo", "meetings": 1}]
    forward = [p["name"] for p in merge_roster(d, o)["people"]]
    backward = [p["name"] for p in merge_roster(list(reversed(d)), o)["people"]]
    assert forward == backward == ["Adam", "Mo", "Zoe"]


def test_an_empty_project_is_an_empty_roster_not_a_crash():
    out = merge_roster([], [])
    assert out == {"people": [], "declared_count": 0, "observed_count": 0}


def test_observed_membership_comes_from_assignments_not_patch_stamps():
    # A meeting's assignment is the record of what belongs to a project;
    # a patch's project stamp is set at ingest and is a different claim.
    body = _route("list_project_people")
    assert "origin_project_assignments" in body


def test_merged_people_do_not_appear_twice():
    # Both SQL legs exclude merged entities, or a person merged into
    # another would arrive twice and the merge would show them once with
    # the wrong name.
    body = _route("list_project_people")
    assert body.count("merged_into IS NULL") >= 2


def test_the_route_delegates_the_merge_rather_than_doing_it_itself():
    # The rule the callers follow, since the executable version lives in
    # the service and a second copy here would be a copy no test runs.
    body = _route("list_project_people")
    assert "project_roster.merge_roster" in body
    assert "by_id" not in body
