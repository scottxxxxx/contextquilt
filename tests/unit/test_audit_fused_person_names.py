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


def test_tokens_handles_punctuation_and_junk():
    assert mod.tokens("  Mike   DiTroia ") == ["mike", "ditroia"]
    assert mod.tokens("O'Brien-Smith") == ["o", "brien", "smith"]
    assert mod.tokens(None) == []
    assert mod.tokens("") == []
    assert mod.tokens(42) == []
