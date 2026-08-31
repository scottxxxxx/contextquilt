"""The description series: who we thought someone was, over time.

Brian's observation, 2026-08-18: Suresh read as "scrum master" one day
because he had introduced himself that way, and something else the next
meeting. He did not want the flapping fixed, he wanted it KEPT, because
a person's hats changing across a project is organizational health.

Until migration 39 every meeting overwrote the description, so the
series was destroyed as it was created.
"""

import sys

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services import described_as as da

WORKER = (ROOT / "src" / "worker.py").read_text()
MAIN = (ROOT / "src" / "main.py").read_text()

SCRUM = "Scrum master for the Kore delivery team, runs the standups"
PARA = "Scrum master on the Kore delivery team who runs standups"
LEAD = "Meeting lead and coordinator across the Kore workstreams"


def test_a_paraphrase_confirms_rather_than_appending():
    """Descriptions are free LLM prose. Byte equality would append a new
    iteration nearly every meeting and bury the ones that matter."""
    got = da.classify_observation(PARA, {"description": SCRUM})
    assert got["action"] == da.CONFIRM
    assert got["similarity"] >= da.SAME_PERCEPTION_SIMILARITY


def test_a_real_change_appends():
    got = da.classify_observation(LEAD, {"description": SCRUM})
    assert got["action"] == da.APPEND
    assert got["reason"] == "perception_changed"
    assert got["similarity"] < da.SAME_PERCEPTION_SIMILARITY


def test_the_first_observation_appends():
    got = da.classify_observation(SCRUM, None)
    assert got["action"] == da.APPEND and got["reason"] == "first_observation"


def test_the_same_meeting_twice_does_nothing():
    """A re-ingest is the same observation arriving twice (doc 19.4).
    Confirming twice from one meeting would turn a replay into evidence
    of stability."""
    got = da.classify_observation(
        LEAD, {"description": SCRUM, "last_origin_id": "M1"}, origin_id="M1")
    assert got["action"] == da.IGNORE
    assert got["reason"] == "already_observed_in_this_meeting"


def test_a_different_meeting_is_not_ignored():
    got = da.classify_observation(
        LEAD, {"description": SCRUM, "last_origin_id": "M1"}, origin_id="M2")
    assert got["action"] == da.APPEND


def test_fragments_are_not_perceptions():
    for junk in ("VP", "lead", "", "   ", None):
        assert da.classify_observation(junk, None)["action"] == da.IGNORE


def test_similarity_is_symmetric_and_bounded():
    assert da.similarity(SCRUM, SCRUM) == 1.0
    assert da.similarity(SCRUM, LEAD) == da.similarity(LEAD, SCRUM)
    assert 0.0 <= da.similarity(SCRUM, LEAD) < 1.0
    assert da.similarity(None, SCRUM) == 0.0


# ---------------------------------------------------------------
# The served shape.
# ---------------------------------------------------------------

def _rows():
    return [
        {"description": SCRUM, "first_observed_at": "2026-08-17T10:00:00Z",
         "last_observed_at": "2026-08-17T10:00:00Z", "observation_count": 3,
         "first_origin_id": "M1"},
        {"description": LEAD, "first_observed_at": "2026-08-18T10:00:00Z",
         "last_observed_at": "2026-08-18T10:00:00Z", "observation_count": 1,
         "first_origin_id": "M2"},
    ]


def test_current_is_newest_and_changed_from_is_the_one_before():
    got = da.series_payload(_rows())
    assert got["current"]["text"] == LEAD
    assert got["changed_from"]["text"] == SCRUM
    assert got["iterations"] == 2


def test_one_stable_perception_has_nothing_to_indicate():
    """changed_from is the indicator. A single perception must not light
    it up, or every person shows a change badge forever."""
    got = da.series_payload(_rows()[:1])
    assert got["changed_from"] is None
    assert got["iterations"] == 1


def test_no_history_is_honest_rather_than_empty_prose():
    got = da.series_payload([])
    assert got == {"current": None, "changed_from": None, "iterations": 0,
                   "history": [], "truncated": False}


def test_series_counts_before_it_caps():
    rows = [dict(_rows()[0], description=f"Perception number {i} of this person",
                 first_observed_at=f"2026-08-{i+1:02d}T10:00:00Z")
            for i in range(da.MAX_SERIES + 3)]
    got = da.series_payload(rows)
    assert len(got["history"]) == da.MAX_SERIES
    assert got["iterations"] == da.MAX_SERIES + 3
    assert got["truncated"] is True


def test_observation_count_is_a_count_not_a_score():
    """Doc 16 forbids a synthesized confidence float. A count of
    meetings that said this is traceable; a score is not."""
    got = da.series_payload(_rows())
    assert got["current"]["observation_count"] == 1
    assert isinstance(got["current"]["observation_count"], int)


# ---------------------------------------------------------------
# Wiring.
# ---------------------------------------------------------------

def test_the_worker_records_on_every_reobservation():
    body = WORKER.split("async def store_entities")[1]
    assert "_record_description(entity_id)" in body
    assert "async def _record_description" in body


def test_recording_is_people_only_and_never_raises():
    helper = WORKER.split("async def _record_description")[1].split(
        "async def _reobserve")[0]
    assert "if entity_type != person_entity_type:" in helper
    assert "except Exception" in helper
    assert "described_as_skipped" in helper


def test_the_stored_text_is_never_rewritten():
    """The row is a receipt. A CONFIRM bumps dates and counts only; a
    rephrasing that overwrote it would destroy what this exists to keep."""
    helper = WORKER.split("async def _record_description")[1].split(
        "async def _reobserve")[0]
    confirm = helper.split("UPDATE entity_descriptions")[1].split('"""')[0]
    assert "description =" not in confirm


def test_the_person_page_serves_the_series_and_degrades():
    assert '"described_as": described_as_series,' in MAIN
    assert "described_as_series_unavailable" in MAIN


# --------------------------------------------------------------------
# The confirm judge: the lexical threshold could not do this job
# --------------------------------------------------------------------

def test_the_lexical_path_is_unchanged_for_callers_that_cannot_judge():
    """Every existing caller keeps today's answer, byte for byte.

    Backfills, scripts and tests that have no LLM must not start
    receiving a fourth action they do not handle, so the new outcome is
    reachable ONLY when the caller says it can judge.
    """
    held = {"description": "Meeting facilitator and lead"}
    out = da.classify_observation("Project lead facilitating standup", held, "m1")
    assert out["action"] == da.APPEND
    assert out["reason"] == "perception_changed"


def test_an_inconclusive_score_asks_the_judge_when_one_is_available():
    """The measurement that forced this.

    Across the six most-described people, 52 consecutive pairs: median
    similarity 0.11, MAXIMUM 0.33, against a 0.6 threshold. Zero of 122
    rows across 43 people had ever been confirmed, so the series
    recorded paraphrase drift rather than perception change and "how
    they are changing" said Suresh changed ten times in thirteen days.

    Not a number that wants tuning: 0.3 confirms 1 pair in 52, and low
    enough to catch these would merge different people's descriptions.
    """
    held = {"description": "Meeting facilitator and lead"}
    out = da.classify_observation("Project lead facilitating standup", held, "m1",
                               judge_available=True)
    assert out["action"] == da.NEEDS_JUDGE
    assert out["similarity"] < 0.6


def test_the_cheap_lexical_confirm_still_short_circuits_the_judge():
    # An obvious rewording must not cost a call.
    held = {"description": "Immigration attorney exploring case intake automation"}
    out = da.classify_observation(
        "Immigration attorney exploring case intake automation.", held, "m1",
        judge_available=True)
    assert out["action"] == da.CONFIRM


def test_a_first_observation_never_calls_the_judge():
    # Nothing to compare against, so a call would be pure cost.
    out = da.classify_observation("Project lead", None, "m1", judge_available=True)
    assert out["action"] == da.APPEND
    assert out["reason"] == "first_observation"


def test_the_same_meeting_arriving_twice_never_calls_the_judge():
    # Doc 19.4. A re-ingest must not confirm and inflate the count into
    # evidence of stability, and must not pay for a call to decide that.
    held = {"description": "Project lead", "last_origin_id": "m1"}
    out = da.classify_observation("Something else entirely", held, "m1",
                               judge_available=True)
    assert out["action"] == da.IGNORE


@pytest.mark.parametrize("content,expected", [
    ({"verdict": "SAME"}, True),
    ({"verdict": "same"}, True),
    ({"verdict": "CHANGED"}, False),
    ('prose then {"verdict": "SAME"} trailing', True),
    ({"verdict": "MAYBE"}, None),
    ({"verdict": 7}, None),
    ({}, None),
    ("no json here", None),
    (None, None),
])
def test_the_verdict_parses_or_declines_cleanly(content, expected):
    assert da.parse_judge_verdict(content) is expected


@pytest.mark.parametrize("same,action", [
    (True, da.CONFIRM),
    (False, da.APPEND),
    (None, da.APPEND),
])
def test_unsure_and_broken_both_resolve_toward_append(same, action):
    """The direction is the whole design.

    A wrong da.CONFIRM destroys a real perception change, which is the only
    thing this series exists to record. A wrong da.APPEND leaves one extra
    row, which is the noise we already had. So unsure appends, a failed
    judge appends, and an unparseable answer appends. The dedup path
    takes the same posture: judge failure inserts rather than losing a
    memory.
    """
    assert da.resolve_judged(same)["action"] == action


def test_the_three_append_causes_stay_distinguishable():
    # "the model said changed", "the model was unusable" and "the judge
    # never ran" are different facts about the system, and collapsing
    # them would hide a judge that had stopped working behind a result
    # identical to it working and disagreeing.
    assert da.resolve_judged(False)["reason"] == "judge_perception_changed"
    assert da.resolve_judged(None)["reason"] == "judge_unusable"
    assert da.classify_observation("a totally different role here",
                                {"description": "Project lead"}, "m1"
                                )["reason"] == "perception_changed"


def test_the_instruction_tells_the_model_which_way_to_fail():
    """Doc 19.3: an unstated rule is a rule the model writes for itself.

    A judge asked "same or changed" with no tie-break guidance splits
    the difference on exactly the ambiguous cases this lane is made of.
    """
    lowered = da.JUDGE_SYSTEM.lower()
    assert "unsure" in lowered and "changed" in lowered
    assert "erases a change" in lowered


def test_the_judge_prompt_carries_no_dash_and_no_dates():
    # No dashes: a model copies the punctuation it is shown. No dates:
    # nothing in this system persists a meeting date, and a model shown
    # an ingest clock reasons about recency instead of content.
    assert "—" not in da.JUDGE_SYSTEM and "–" not in da.JUDGE_SYSTEM
    body = da.build_judge_content("Held one", "New one")
    assert "date" not in body.lower() and "meeting_id" not in body
