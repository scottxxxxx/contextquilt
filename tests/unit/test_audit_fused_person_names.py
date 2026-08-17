"""Unit tests for scripts/audit_fused_person_names.py.

The receipt this was written from: `Pallavi Vijay` is not a person, it is
`Pallavi Kandanu` and `Vijay Rayudu` welded together by the extractor.
The first-token collision audit structurally cannot see that shape, so
these tests pin both halves of the job: it must FIND the fusion, and it
must not cry wolf on an ordinary full name that happens to share a token.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_fused_person_names.py"
_spec = importlib.util.spec_from_file_location("audit_fused", _SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
sys.modules["audit_fused"] = mod
_spec.loader.exec_module(mod)


# The real roster shape, trimmed to what matters.
ROSTER = [
    ("e1", "Pallavi Kandanu"),
    ("e2", "Vijay Rayudu"),
    ("e3", "Pallavi Vijay"),
    ("e4", "Mike Peterson"),
    ("e5", "Mike DiTroia"),
    ("e6", "Pallavi"),
]


# Two candidates that differ only in how much a wrong call would cost.
# `Rao Mehta` needs BOTH halves to head another name, hence Rao Sharma and
# Mehta Gupta: a surname-only match is not a candidate at all.
_TWO_TIER = [
    ("a", "Pallavi Kandanu"),
    ("b", "Vijay Rayudu"),
    ("c", "Pallavi Vijay"),     # 6 beside 87 -> dwarfed, cheap to settle
    ("d", "Rao Sharma"),
    ("e", "Mehta Gupta"),
    ("f", "Rao Mehta"),         # 36 beside 40 -> comparable, expensive
]


def _by_name(candidates):
    return {c["name"]: c for c in candidates}


def test_finds_the_fused_row():
    hits = _by_name(mod.fused_candidates(ROSTER))
    assert "Pallavi Vijay" in hits
    hit = hits["Pallavi Vijay"]
    assert hit["confidence"] == "high"
    assert {n for _, n in hit["head_sources"]} == {"Pallavi Kandanu"}
    assert {n for _, n in hit["tail_sources"]} == {"Vijay Rayudu"}


def test_ordinary_full_name_is_not_a_candidate():
    """`Mike Peterson` shares a first token with `Mike DiTroia` and must
    still be clean: nobody on this roster is named Peterson-first, so the
    tail is not another person's head."""
    hits = _by_name(mod.fused_candidates(ROSTER))
    assert "Mike Peterson" not in hits
    assert "Mike DiTroia" not in hits


def test_bare_single_token_name_is_never_a_candidate():
    """`Pallavi` alone is a fragment for the merge audit to handle, not a
    fusion. A one-token name has no tail to be somebody else's head."""
    assert "Pallavi" not in _by_name(mod.fused_candidates(ROSTER))


def test_real_surname_downgrades_confidence():
    """If the tail is genuinely somebody's surname on this roster, the
    coincidence is ordinary and the hit must not be reported as high."""
    roster = [
        ("a", "Ryan Cole"),
        ("b", "Thomas Nguyen"),
        ("c", "Priya Thomas"),   # Thomas attested as a real surname
        ("d", "Ryan Thomas"),    # head=Ryan, tail=Thomas -> both are heads
    ]
    hit = _by_name(mod.fused_candidates(roster))["Ryan Thomas"]
    assert hit["confidence"] == "low"
    assert [n for _, n in hit["tail_also_a_surname_of"]] == ["Priya Thomas"]


def test_high_confidence_sorts_before_low():
    roster = ROSTER + [
        ("f", "Ryan Cole"),
        ("g", "Thomas Nguyen"),
        ("h", "Priya Thomas"),
        ("i", "Ryan Thomas"),
    ]
    names = [c["name"] for c in mod.fused_candidates(roster)]
    assert names.index("Pallavi Vijay") < names.index("Ryan Thomas")


def test_self_collision_is_not_a_fusion():
    """Both halves must point at two DIFFERENT people. One person whose
    name collides with itself across spellings is a merge case."""
    roster = [("a", "Vijay Rayudu"), ("b", "Vijay Vijay")]
    assert mod.fused_candidates(roster) == []


def test_volume_ratio_measures_cost_of_a_wrong_call():
    """The real shape: a 6-meeting fused row beside an 87-meeting source.
    The ratio is what says a wrong call here is cheap."""
    volumes = {"e1": 87, "e2": 3, "e3": 6}
    hit = _by_name(mod.fused_candidates(ROSTER, volumes=volumes))["Pallavi Vijay"]
    assert hit["meetings"] == 6
    assert hit["biggest_source"] == "Pallavi Kandanu"   # 87 beats 3
    assert hit["volume_ratio"] == round(6 / 87, 3)


def test_ratio_is_null_when_volumes_absent():
    """The structural rule stands without volumes; the ratio must not be
    faked when nothing was measured."""
    hit = _by_name(mod.fused_candidates(ROSTER))["Pallavi Vijay"]
    assert hit["volume_ratio"] is None
    assert hit["meetings"] is None
    assert hit["biggest_source"] is None


def test_zero_volume_source_leaves_ratio_null_not_infinite():
    """A source with no appearances says nothing about cost. Honestly
    null beats an invented infinity, and it must not crash."""
    volumes = {"e1": 0, "e2": 0, "e3": 6}
    hit = _by_name(mod.fused_candidates(ROSTER, volumes=volumes))["Pallavi Vijay"]
    assert hit["volume_ratio"] is None


def test_cheapest_to_settle_sorts_first_within_a_tier():
    volumes = {"a": 87, "b": 3, "c": 6, "d": 40, "e": 38, "f": 36}
    names = [x["name"] for x in mod.fused_candidates(_TWO_TIER, volumes=volumes)]
    assert names.index("Pallavi Vijay") < names.index("Rao Mehta")


def test_measured_ratio_never_sorts_behind_an_unmeasured_one():
    """A candidate we could not measure sorts mid, so it neither jumps the
    queue nor hides behind everything."""
    volumes = {"a": 87, "b": 3, "c": 6}     # Rao Mehta unmeasured
    ranked = mod.fused_candidates(_TWO_TIER, volumes=volumes)
    by_name = {x["name"]: x for x in ranked}
    assert by_name["Rao Mehta"]["volume_ratio"] is None
    names = [x["name"] for x in ranked]
    assert names.index("Pallavi Vijay") < names.index("Rao Mehta")


def test_tokens_handles_punctuation_and_junk():
    assert mod.tokens("  Mike   DiTroia ") == ["mike", "ditroia"]
    assert mod.tokens("O'Brien-Smith") == ["o", "brien", "smith"]
    assert mod.tokens(None) == []
    assert mod.tokens("") == []
    assert mod.tokens(42) == []
