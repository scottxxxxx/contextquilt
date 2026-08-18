"""A name that could mean two colleagues resolves to nobody.

The receipt (2026-08-17): 'Mike' -> Mike DiTroia was a recorded alias,
so an interview candidate saying "Mike" in passing gave a Kore.ai
colleague a meeting he was never in, plus a description reading "VP of
Engineering at IMIT, on day 3 at the company". 17 bare first names on
that roster resolved to one person while other live people shared them.

That failure is worse than an invented person. An invented person is
obviously wrong. A stranger's words on a real colleague's page looks
completely plausible and is only catchable by someone who knows them.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.entity_aliasing import (
    is_contested_person_name,
    person_candidates,
)

WORKER = (ROOT / "src" / "worker.py").read_text()

# Scott's real scenario: two Mikes he distinguishes by initial, plus the
# interview candidate who has no business touching either of them.
ROSTER = [
    ("d", "Mike DiTroia"),
    ("p", "Mike Piotrowski"),
    ("t", "Mike Peterson"),
    ("n", "Naga"),
    ("s", "Suresh Muchakurti"),
]


def names(surface, roster=ROSTER):
    return sorted(n for _, n in person_candidates(surface, roster))


def test_bare_first_name_with_several_mikes_is_contested():
    assert names("Mike") == ["Mike DiTroia", "Mike Peterson", "Mike Piotrowski"]
    assert is_contested_person_name("Mike", ROSTER)


def test_surname_initial_narrows_and_can_still_be_contested():
    """Scott's own rule: "Mike P" resolves only if ONE Mike has a
    surname starting with P. Here two do, so it stays a question."""
    assert names("Mike P") == ["Mike Peterson", "Mike Piotrowski"]
    assert is_contested_person_name("Mike P", ROSTER)


def test_surname_initial_resolves_when_unique():
    assert names("Mike D") == ["Mike DiTroia"]
    assert not is_contested_person_name("Mike D", ROSTER)


def test_full_name_is_decisive_however_many_share_the_first_name():
    """Three Mikes exist and this is still not a question."""
    assert names("Mike DiTroia") == ["Mike DiTroia"]
    assert not is_contested_person_name("Mike DiTroia", ROSTER)


def test_an_unshared_first_name_resolves():
    assert not is_contested_person_name("Naga", ROSTER)
    assert not is_contested_person_name("Suresh", ROSTER)


def test_a_name_nobody_holds_is_not_contested():
    """Zero candidates is a new person, not an ambiguity. The guard must
    not block someone genuinely new from being created."""
    assert person_candidates("Priya", ROSTER) == []
    assert not is_contested_person_name("Priya", ROSTER)


def test_case_and_spacing_do_not_change_the_answer():
    for form in ("mike", "  MIKE  ", "Mike"):
        assert is_contested_person_name(form, ROSTER), form


def test_junk_input_is_never_contested():
    for junk in ("", "   ", None):
        assert person_candidates(junk, ROSTER) == []
        assert not is_contested_person_name(junk, ROSTER)


def test_empty_roster_blocks_nothing():
    """A roster fetch that failed must not stop an ingest. No roster
    means no contest, which is the pre-guard behaviour."""
    assert not is_contested_person_name("Mike", [])


# ---------------------------------------------------------------
# The ingest wiring.
# ---------------------------------------------------------------

def test_guard_runs_only_for_people():
    body = WORKER.split("async def store_entities")[1]
    assert "if entity_type == person_entity_type:" in body
    assert "is_contested_person_name(name, roster)" in body


def test_contested_name_creates_nothing():
    """Not resolving is only half of it. Falling through to insert would
    mint a second "Mike", which is its own corruption."""
    body = WORKER.split("async def store_entities")[1]
    guard = body.split("is_contested_person_name(name, roster)")[1][:400]
    assert "continue" in guard, "a contested name must skip the entity entirely"


def test_guard_sits_after_the_exact_match():
    """An exact full-name hit is decisive and must never reach here."""
    body = WORKER.split("async def store_entities")[1]
    i_exact = body.index("# 1. Exact match, case-insensitive")
    i_guard = body.index("# 1b. CONTESTED NAME GUARD")
    i_alias = body.index("# 2. Recorded alias")
    assert i_exact < i_guard < i_alias


def test_roster_failure_degrades_to_todays_behaviour():
    body = WORKER.split("async def store_entities")[1]
    helper = body.split("async def _live_person_roster")[1].split("\n    for ent in entities:")[0]
    assert "person_roster = []" in helper
    assert "except Exception" in helper


def test_the_drop_is_logged_with_its_candidates():
    """A silent drop is indistinguishable from an extraction that never
    named anyone, and that is the whole class of bug we keep paying for."""
    body = WORKER.split("async def store_entities")[1]
    assert "entity_name_contested" in body
    assert "candidates=" in body
