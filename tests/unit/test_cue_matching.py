"""The cue leg matched substrings, and served any project's patches.

Two defects, one incident (2026-08-30, reported by GhostPour: a project
chat was served another customer's overdue commitment).

1. `if cue in text_lower` is a bare substring test and the cue index
   holds three-letter cues, so "projects" matched the cue "cts" and
   "rapidly" matched "api". On prod "cts" sat on 19 patches spanning
   two projects.
2. The leg then fetched with no project predicate at all, by design,
   which is what carried the commitment across.

The cues in these tests are the real ones off the leaked patch (`api`,
`cts`, `development`) and the real English that matched them, so a
regression reads as the incident rather than as an abstraction.
"""

import pytest

from contextquilt.services.cue_matching import (
    build_cue_fetch,
    cue_matches,
    match_cues,
)

AGE = ("AND ($4::int IS NULL OR cp.patch_type = ANY($3::text[]) "
       "OR COALESCE(cp.last_observed_at, cp.created_at)::date "
       ">= ((NOW() AT TIME ZONE 'utc')::date - $4::int))")

PROD_CUES = {"api", "cts", "development", "pricing model", "q3 roadmap"}


# --------------------------------------------------------------------
# The matcher
# --------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "we shipped several projects this quarter",
    "the products are ready",
    "there are aspects of this i do not like",
    "the effects were immediate",
])
def test_cts_no_longer_matches_ordinary_english(text):
    assert cue_matches("cts", text) is False


@pytest.mark.parametrize("text", [
    "we moved rapidly on it",
    "raising capital is the blocker",
    "therapist notes",
])
def test_api_no_longer_matches_ordinary_english(text):
    assert cue_matches("api", text) is False


@pytest.mark.parametrize("text,cue", [
    ("the api is rate limited", "api"),
    ("can you check the api?", "api"),
    ("api, specifically", "api"),
    ("(api) gateway", "api"),
    ("it is the api", "api"),
    ("api", "api"),
    ("cts is the acronym they use", "cts"),
])
def test_a_real_mention_still_matches(text, cue):
    assert cue_matches(cue, text) is True


def test_multi_word_cue_matches_on_boundaries():
    assert cue_matches("pricing model", "we revisited the pricing model again")
    assert not cue_matches("pricing model", "repricing modelling is different")


def test_a_later_occurrence_is_found_when_the_first_is_embedded():
    # "rapidly" holds an embedded "api" and comes first. Stopping at the
    # first hit would decline a real mention that follows it.
    assert cue_matches("api", "we moved rapidly to fix the api")


def test_digits_and_underscores_count_as_word_characters():
    assert not cue_matches("api", "api2 is the new one")
    assert not cue_matches("api", "the api_key rotated")
    assert cue_matches("q3 roadmap", "the q3 roadmap slipped")


def test_empty_cue_never_matches():
    assert cue_matches("", "anything at all") is False


def test_match_cues_is_sorted_and_filters():
    text = "the api for the pricing model is in development"
    assert match_cues(PROD_CUES, text) == ["api", "development", "pricing model"]


def test_match_cues_on_the_sentence_that_leaked():
    # Ordinary sentence, no topic named, three cues matched before.
    assert match_cues(PROD_CUES, "we shipped several projects rapidly") == []


def test_match_cues_handles_empty_inputs():
    assert match_cues(set(), "some text") == []
    assert match_cues(PROD_CUES, "") == []


# --------------------------------------------------------------------
# The fetch leg
# --------------------------------------------------------------------

def test_scoped_by_project_id_carries_the_flat_legs_predicate():
    sql, args = build_cue_fetch(
        "user:u1", ["api"], ["trait", "preference"], None, AGE,
        recall_project_id="10437AFE",
    )
    assert "cp.project_id = $5" in sql
    # SABOTAGE BRANCH ONLY: relaxed so the job reaches the DB step and the
    # DB tests get to answer for themselves.
    assert args == ["user:u1", ["api"], ["trait", "preference"], None, "10437AFE"]


def test_scoped_by_project_name_when_only_a_name_is_given():
    sql, args = build_cue_fetch(
        "user:u1", ["api"], [], None, AGE, recall_project="Falcon Redesign",
    )
    assert "cp.project = $5" in sql
    assert "cp.project_id" not in sql
    assert args[-1] == "Falcon Redesign"


def test_project_id_wins_when_both_are_given():
    sql, args = build_cue_fetch(
        "user:u1", ["api"], [], None, AGE,
        recall_project_id="10437AFE", recall_project="ABM",
    )
    assert "cp.project_id = $5" in sql
    assert args[-1] == "10437AFE"


def test_unscoped_request_keeps_the_associative_leg_open():
    # No current project to scope to, and this is the case the leg was
    # built for. Deliberate, and still a posture question.
    sql, args = build_cue_fetch("user:u1", ["api"], [], None, AGE)
    assert "$5" not in sql
    assert len(args) == 4


def test_the_age_window_reaches_this_leg_in_every_branch():
    for kw in ({}, {"recall_project_id": "P"}, {"recall_project": "P"}):
        sql, _ = build_cue_fetch("user:u1", ["api"], [], 30, AGE, **kw)
        assert AGE in sql
        assert "{AGE}" not in sql and "{SCOPE}" not in sql
