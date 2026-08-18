"""The description series: who we thought someone was, over time.

Brian's observation, 2026-08-18: Suresh read as "scrum master" one day
because he had introduced himself that way, and something else the next
meeting. He did not want the flapping fixed, he wanted it KEPT, because
a person's hats changing across a project is organizational health.

Until migration 39 every meeting overwrote the description, so the
series was destroyed as it was created.
"""

import sys
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
