"""The typed door: a contested name is a question, not a guess.

Ingest stopped guessing in #283. This is the other door, the one Scott
actually used: open Review, tap a generic speaker label, type "Mike".
That path resolved through a recorded alias with LIMIT 1, so it landed
on Mike DiTroia every time regardless of which Mike was meant.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.people_identity import (
    MAX_NAME_CANDIDATES,
    candidate_payload,
    rank_person_candidates,
)

MAIN = (ROOT / "src" / "main.py").read_text()

DITROIA = {"entity_id": "d", "name": "Mike DiTroia", "meetings": 11,
           "last_met": "2026-08-17", "projects": ["kore"]}
PETERSON = {"entity_id": "p", "name": "Mike Peterson", "meetings": 1,
            "last_met": "2026-08-17", "projects": ["emids"]}
ROGERS = {"entity_id": "r", "name": "Mike Rogers", "meetings": 1,
          "last_met": "2026-06-01", "projects": []}


def names(ranked):
    return [c["name"] for c in ranked]


def test_same_project_wins_over_more_meetings():
    """Labelling a speaker in the EMIDS interview: Peterson has one
    meeting to DiTroia's eleven and is still the likelier answer."""
    got = rank_person_candidates([DITROIA, PETERSON, ROGERS], ["emids"])
    assert names(got)[0] == "Mike Peterson"


def test_without_scope_recency_then_volume_decide():
    got = rank_person_candidates([ROGERS, PETERSON, DITROIA])
    assert names(got) == ["Mike DiTroia", "Mike Peterson", "Mike Rogers"]


def test_ranking_is_stable_and_total_ordered():
    """Two candidates identical on every signal must not reorder between
    two identical calls."""
    a = dict(DITROIA, entity_id="a", name="Mike Alpha")
    b = dict(DITROIA, entity_id="b", name="Mike Bravo")
    assert names(rank_person_candidates([b, a])) == names(rank_person_candidates([a, b]))


def test_missing_signals_never_raise():
    """A person with no appearances has null last_met and no projects."""
    bare = {"entity_id": "x", "name": "Mike Nobody"}
    got = rank_person_candidates([bare, DITROIA], ["kore"])
    assert names(got)[0] == "Mike DiTroia"
    assert "Mike Nobody" in names(got)


def test_payload_counts_before_it_caps():
    """A long tail must be visible as a number, never silently dropped,
    the same rule /v1/quilt follows when it truncates."""
    many = [dict(ROGERS, entity_id=str(i), name=f"Mike {i:02d}")
            for i in range(MAX_NAME_CANDIDATES + 4)]
    got = candidate_payload(many)
    assert len(got["candidates"]) == MAX_NAME_CANDIDATES
    assert got["total"] == MAX_NAME_CANDIDATES + 4
    assert got["truncated"] is True


def test_payload_is_honest_when_it_fits():
    got = candidate_payload([DITROIA, PETERSON])
    assert got["total"] == 2 and got["truncated"] is False


# ---------------------------------------------------------------
# The endpoint wiring.
# ---------------------------------------------------------------

def _resolver():
    return MAIN.split("async def _resolve_or_create_person")[1].split("\n@app.")[0]


def test_the_limit_one_alias_lookup_is_gone():
    """The exact line that put an interview candidate's description on a
    Kore.ai colleague's page."""
    body = _resolver()
    assert "LIMIT 1" not in body.split("_name_candidates")[0].split("entity_aliases")[-1] \
        or "entity_aliases" not in body


def test_contested_name_is_refused_with_ranked_candidates():
    body = _resolver()
    assert "CONTESTED_NAME" in body
    assert "status_code=409" in body
    assert "candidate_payload(candidates, scope_project_ids)" in body


def test_a_single_candidate_still_resolves():
    """'VJ' matching exactly one person must not become a question."""
    assert "if len(candidates) == 1 and not create_new:" in _resolver()


def test_an_unknown_name_still_creates():
    """Zero candidates is a NEW PERSON. The guard must not block someone
    genuinely new from being added."""
    body = _resolver()
    i_gt = body.index("len(candidates) > 1")
    i_eq = body.index("len(candidates) == 1")
    assert i_gt < i_eq, "the zero case must fall through both branches"


def test_create_new_overrides_the_refusal():
    """After the user has SEEN the candidates and chosen someone new."""
    body = _resolver()
    assert "create_new: bool = False" in body
    assert "and not create_new" in body


def test_both_doors_pass_the_flag():
    """POST /v1/people and reassign-speaker share this resolver, and a
    picker on one door without the other leaves the hole open."""
    assert MAIN.count("create_new=bool(req.create_new)") == 2


def test_reassign_speaker_supplies_project_scope():
    """The meetings being relabelled are the strongest ranking signal."""
    assert "scope_project_ids=scope_projects" in MAIN


def test_alias_lookup_failure_degrades_to_asking():
    """entity_aliases can lag on the MCP deployment's Postgres. Losing
    it must err toward asking, never toward guessing."""
    helper = MAIN.split("async def _name_candidates")[1].split("\nasync def ")[0]
    assert "alias_candidates_unavailable" in helper
    assert "except Exception" in helper


def test_the_409_is_not_swallowed_and_rolls_back():
    """The refusal is raised INSIDE the transaction that would have
    created the person, so a contested name never strands a half-made
    one. A broad `except` anywhere between the transaction and the
    resolver would convert the 409 into a 500 and hand GP a status their
    picker cannot branch on."""
    body = MAIN.split("async def reassign_speaker")[1].split("\n@app.")[0]
    i_tx = body.index("async with conn.transaction():")
    i_call = body.index("person = await _resolve_or_create_person(")
    assert i_tx < i_call, "the resolver must run inside the transaction"
    between = body[i_tx:i_call]
    assert "except" not in between, "nothing may catch the CONTESTED_NAME 409"


def test_create_new_is_a_modifier_not_a_target():
    """`to_name` + `create_new` must still count as ONE target. If
    create_new joined the exactly-one gate, the retry after a picker
    would be refused as ambiguous."""
    gate = MAIN.split("targets_given = sum(")[1].split(")")[0]
    assert "create_new" not in gate
    for field in ("to_self", "to_person_id", "to_name"):
        assert field in gate
