"""who_they_are: the arithmetic in code, the model checked not trusted."""

import json

from src.contextquilt.services import who_they_are as w


ROLES = [
    {"patch_id": "p-role-2", "text": "Suresh is scrum master on ABM project",
     "project": "ABM", "stated_at": "2026-08-17T22:00:00+00:00", "origin_id": "m-0817"},
    {"patch_id": "p-role-1", "text": "Suresh was a developer on Kore",
     "project": "Kore", "stated_at": "2026-06-15T10:00:00+00:00", "origin_id": "m-0615"},
]
PERCS = [
    {"description": "Meeting facilitator and lead", "first_observed_at": "2026-08-21",
     "last_observed_at": "2026-08-21", "observation_count": 1, "first_origin_id": "m4"},
    {"description": "Project lead for AI for Work standup", "first_observed_at": "2026-08-20",
     "last_observed_at": "2026-08-20", "observation_count": 1, "first_origin_id": "m3"},
    {"description": "Team member involved in ABM engagement discussions", "first_observed_at": "2026-08-18",
     "last_observed_at": "2026-08-18", "observation_count": 1, "first_origin_id": "m1"},
]


def _facts(roles=ROLES, percs=PERCS):
    return w.build_facts("Suresh Muchakurti", roles, percs, 133, "2026-05-02", "2026-08-21", ["ABM", "Kore"])


def _ok_response(summary=None, trajectory="First seen as a team member in August 2026, then read as the project lead across the standups since.", sources=("R1", "P1", "P3")):
    summary = summary or ("Scrum master on ABM project by his own account, and the meetings since August 2026 consistently show him running the standup and coordinating across teams. "
                          "The perceptions agree with what he stated rather than contradicting it.")
    return json.dumps({"summary": summary, "trajectory": trajectory, "sources": list(sources), "output_language": "en"})


def test_eligibility_needs_a_role_or_two_perceptions():
    assert w.eligible(ROLES, []) is True
    assert w.eligible([], PERCS[:1]) is False
    assert w.eligible([], PERCS[:2]) is True


def test_facts_have_stable_ids_and_a_fingerprint_that_ignores_meeting_count():
    a = _facts()
    b = w.build_facts("Suresh Muchakurti", ROLES, PERCS, 999, "2026-05-02", "2026-08-22", ["ABM", "Kore"])
    assert [r["id"] for r in a["roles"]] == ["R1", "R2"]
    assert [p["id"] for p in a["perceptions"]] == ["P1", "P2", "P3"]
    assert a["fingerprint"] == b["fingerprint"]
    c = w.build_facts("Suresh Muchakurti", ROLES, PERCS[:2], 133, "2026-05-02", "2026-08-21", ["ABM"])
    assert c["fingerprint"] != a["fingerprint"]


def test_title_phrase_matches_the_served_title_strip():
    assert w.title_phrase("Suresh is scrum master on ABM project", "Suresh Muchakurti") == "scrum master on ABM project"
    assert w.title_phrase("Suresh Muchakurti: scrum master", "Suresh Muchakurti") == "scrum master"


def test_accepts_a_good_answer_and_keeps_only_known_sources():
    out = w.parse_response(_ok_response(sources=("R1", "P1", "ZZ")), _facts())
    assert out is not None
    assert out["sources"] == ["R1", "P1"]
    assert out["trajectory"].startswith("First seen")


def test_rejects_a_summary_that_drops_the_stated_role():
    # The precedence rule, enforced on the model's output: if the
    # person said "scrum master", a synthesis that says "project lead"
    # and never the stated phrase has reversed the rule.
    defects = []
    out = w.parse_response(_ok_response(
        summary="Project lead who runs the AI for Work standup and coordinates across teams, confirmed across several meetings in August 2026 and seen as the facilitator throughout."), _facts(), defects)
    assert out is None
    assert defects == ["stated_role_dropped"]


def test_no_stated_role_means_no_phrase_requirement():
    defects = []
    out = w.parse_response(_ok_response(
        summary="Read as a project lead who runs the AI for Work standup, first seen as a team member in August 2026 and consistently the facilitator since.",
        sources=("P1", "P2")), _facts(roles=[]), defects)
    assert out is not None, defects


def test_rejects_invented_numbers_and_dash_punctuation_and_name_opening():
    f = _facts()
    d = []
    assert w.parse_response(_ok_response(summary="Scrum master on ABM project by his own account, across 47 meetings the standups show him coordinating teams and the picture has not moved."), f, d) is None
    assert d == ["invented_number:47"]
    d = []
    assert w.parse_response(_ok_response(summary="Scrum master on ABM project by his own account — the meetings since August 2026 show him running the standup and coordinating across teams."), f, d) is None
    assert d == ["dash_punctuation"]
    d = []
    assert w.parse_response(_ok_response(summary="Suresh is scrum master on ABM project by his own account, and the meetings since August 2026 show him running the standup and coordinating teams."), f, d) is None
    assert d == ["opens_with_name"]


def test_rejects_length_and_shape_defects():
    f = _facts()
    d = []
    assert w.parse_response("not json at all", f, d) is None and d == ["not_json"]
    d = []
    assert w.parse_response(_ok_response(summary="Scrum master on ABM project."), f, d) is None and d == ["summary_too_short"]
    d = []
    long = "Scrum master on ABM project by his own account. " + ("The standups show him coordinating across teams. " * 12)
    assert w.parse_response(_ok_response(summary=long), f, d) is None and d == ["summary_too_long"]
    d = []
    assert w.parse_response(_ok_response(sources=()), f, d) is None and d == ["no_sources"]


def test_served_shape_carries_receipts_by_cited_id():
    f = _facts()
    value = {"text": "summary text", "trajectory": "moved", "sources": ["R1", "P3"],
             "facts": f, "generated_at": "2026-08-21T23:00:00+00:00", "model": "claude-sonnet-4-6"}
    s = w.served(value)
    assert s["summary"] == "summary text"
    assert [r["kind"] for r in s["receipts"]] == ["stated", "observed"]
    assert s["receipts"][0]["text"] == ROLES[0]["text"]
    assert s["receipts"][0]["origin_id"] == "m-0817"
    assert s["receipts"][1]["times"] == 1
    assert s["inputs_fingerprint"] == f["fingerprint"]


def test_content_lists_roles_before_perceptions_with_ids():
    c = w.build_content(_facts(), used_openings=["Read as"])
    assert c.index("STATED ROLES") < c.index("PERCEPTIONS")
    assert 'R1: "Suresh is scrum master on ABM project" [project: ABM] (stated 2026-08-17)' in c
    assert 'P1: "Meeting facilitator and lead" (seen 2026-08-21, confirmed 1x)' in c
    assert "do not open with any of them" in c


def test_parse_accepts_the_client_s_dict_content():
    # The real LLM clients hand over parsed JSON, not text. The first prod
    # cycle failed on every person because the parse assumed a string.
    f = _facts()
    obj = json.loads(_ok_response())
    out = w.parse_response(obj, f)
    assert out is not None and out["sources"] == ["R1", "P1", "P3"]
    d = []
    assert w.parse_response({"facts": [], "action_items": [], "_parse_error": True}, f, d) is None
    assert d == ["not_json"]


def test_receipt_times_is_null_for_stated_and_int_for_observed():
    f = _facts()
    value = {"text": "s", "trajectory": None, "sources": ["R1", "P2"], "facts": f}
    r = w.served(value)["receipts"]
    assert r[0]["kind"] == "stated" and r[0]["times"] is None
    assert r[1]["kind"] == "observed" and isinstance(r[1]["times"], int)


def test_stated_role_check_ignores_punctuation_and_case():
    # First prod cycle: role text ended in a period, summary had a comma.
    roles = [{"patch_id": "p", "text": "Jared is the new HR manager for the West Coast region.", "stated_at": "2026-08-12", "origin_id": "m"}]
    f = w.build_facts("Jared", roles, [], 2, "2026-08-12", "2026-08-12", [])
    out = w.parse_response(_ok_response(
        summary="Described as the New HR Manager for the West Coast region, a role stated directly in August 2026 with nothing yet to complicate that picture.",
        sources=("R1",)), f)
    assert out is not None


def test_retry_note_names_the_defect_and_only_for_retryable_ones():
    f = _facts()
    assert "characters" in w.retry_note("summary_too_long", f, 712)
    assert "Suresh Muchakurti" in w.retry_note("opens_with_name", f)
    assert "scrum master on ABM project" in w.retry_note("stated_role_dropped", f)
    assert w.retry_note("invented_number:47", f) is None
    assert "invented_number:47" not in w.RETRYABLE
