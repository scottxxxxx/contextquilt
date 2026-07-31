"""Unit tests for the deadline micro-pass pure logic."""

from datetime import date

from src.contextquilt.services.deadline_resolver import (
    apply_resolutions,
    build_calendar_context,
    build_micropass_prompt,
    collect_deadline_items,
    parse_micropass_response,
)

MEETING = date(2026, 8, 4)  # a Tuesday


# ------------------------------------------------------------------
# calendar rendering — the lookup table that replaces arithmetic
# ------------------------------------------------------------------

def test_calendar_starts_monday_of_meeting_week():
    cal = build_calendar_context(MEETING)
    assert "Mon 2026-08-03" in cal
    assert "<- meeting week" in cal
    assert "2026-08-04 (Tuesday)" in cal


def test_calendar_covers_the_relative_range():
    cal = build_calendar_context(MEETING)
    assert "Fri 2026-08-07" in cal          # "by Friday"
    assert "Wed 2026-08-12" in cal          # "next Wednesday"
    assert "Tue 2026-08-18" in cal          # "in two weeks"
    assert "Mon 2026-08-31" in cal          # "end of month"


# ------------------------------------------------------------------
# item collection — re-resolves everything with a spoken deadline
# ------------------------------------------------------------------

def _patch(text, deadline=None, deadline_date=None, ptype="commitment"):
    v = {"text": text}
    if deadline is not None:
        v["deadline"] = deadline
    if deadline_date is not None:
        v["deadline_date"] = deadline_date
    return {"type": ptype, "value": v}


def test_collects_spoken_deadlines_even_when_already_dated():
    patches = [
        _patch("send deck", "by Friday", "2026-08-08"),   # wrong inline date
        _patch("no deadline here"),
        _patch("chase florist", "tomorrow"),
    ]
    items = collect_deadline_items(patches)
    assert [(i, d) for i, _, d in items] == [(0, "by Friday"), (2, "tomorrow")]


def test_tolerates_malformed_values():
    assert collect_deadline_items([{"type": "x", "value": "just a string"}, {}]) == []


# ------------------------------------------------------------------
# prompt shape — embedded output contract (client ignores json_schema)
# ------------------------------------------------------------------

def test_prompt_embeds_shape_and_calendar():
    system, user = build_micropass_prompt(MEETING, [(0, "send deck", "by Friday")])
    assert '"deadline_date": "YYYY-MM-DD" | null' in system
    assert "Calendar" in user
    assert 'index 0: deadline "by Friday"' in user


# ------------------------------------------------------------------
# response parsing
# ------------------------------------------------------------------

def test_parse_handles_fences_and_prose():
    assert parse_micropass_response('```json\n[{"index":0,"deadline_date":"2026-08-07"}]\n```') == [
        {"index": 0, "deadline_date": "2026-08-07"}
    ]
    assert parse_micropass_response("Sure! [ {\"index\": 1, \"deadline_date\": null} ] done") == [
        {"index": 1, "deadline_date": None}
    ]
    assert parse_micropass_response("no array here") is None


# ------------------------------------------------------------------
# applying resolutions — validated, index-gated, overwrite-capable
# ------------------------------------------------------------------

def test_apply_overwrites_wrong_inline_date():
    patches = [_patch("send deck", "by Friday", "2026-08-08")]
    n = apply_resolutions(patches, [{"index": 0, "deadline_date": "2026-08-07"}],
                          MEETING, {0})
    assert n == 1
    assert patches[0]["value"]["deadline_date"] == "2026-08-07"


def test_apply_ignores_hallucinated_index():
    patches = [_patch("send deck", "by Friday")]
    n = apply_resolutions(patches, [{"index": 7, "deadline_date": "2026-08-07"}],
                          MEETING, {0})
    assert n == 0


def test_apply_validates_through_plausibility_gate():
    # wrong-year hallucination from the resolver is nulled, not stored
    patches = [_patch("send deck", "by Friday", None)]
    n = apply_resolutions(patches, [{"index": 0, "deadline_date": "2024-08-07"}],
                          MEETING, {0})
    assert n == 0  # None -> None is no change
    assert patches[0]["value"].get("deadline_date") is None


def test_apply_can_null_an_unresolvable_date():
    patches = [_patch("cert work", "when the cert renews", "2026-08-09")]
    n = apply_resolutions(patches, [{"index": 0, "deadline_date": None}],
                          MEETING, {0})
    assert n == 1
    assert patches[0]["value"]["deadline_date"] is None
