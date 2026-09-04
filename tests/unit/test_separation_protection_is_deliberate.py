"""The thing that keeps separated people apart, pinned so a tidy-up breaks it.

TWO RAJS BECAME ONE ENTITY ON 2026-09-04. No merge request, no prompt, no
human: `find_alias_candidate` paired "Raj" with an incoming "Raj Kumar",
found it the only candidate by ITS rules, and renamed the older person's
entity to the newer person's name.

Chasing that, I told ShoulderSurf the ingest resolver ignoring
`entity_separations` was a live defect, and they sharpened it to "the
system overruling the user". Then I tried to demonstrate it against all
14 real separated pairs and COULD NOT. Nothing was added between the
claim and its repetition except confidence, by both of us, which is doc
19's rule 9 with both parties inside it.

WHAT ACTUALLY PROTECTS THOSE PAIRS, and none of it was designed to:

  1. the worker resolves an EXACT NAME before reaching the heuristic
  2. `is_alias_form` refuses identical names, so the exact-name twin is
     invisible as a competing candidate and cannot even raise the count
  3. `is_contested_person_name` refuses a surface matching more than one
     live person

A protection nobody designed is one refactor away from being removed by
somebody who cannot see they are removing it, and the commit that does it
will look like a tidy-up. So this file makes the protection deliberate.
It adds no machinery. It asserts what already holds, so that removing it
is loud.

THE OTHER FINDING, and it is the transferable one. Three guards exist
against this class, written independently: ShoulderSurf's
`ambiguousEntityIds`, `find_alias_candidate`'s `len(matches) == 1`, and
`is_contested_person_name`'s `> 1`. All three count candidates and refuse
when there are several.

I first wrote that all three "wave the single-candidate case through",
which is wrong in a way worth keeping. For the direction that produced
this bug, a LONGER name arriving against a shorter roster entry, the
contested guard sees ZERO candidates, not one. `person_candidates`
matches short surfaces against longer names and does not look the other
way, so it is not consulted rather than being too permissive. A count
threshold cannot save you from a question that was never asked.

Which leaves two functions in one module disagreeing: `person_candidates`
says these are not the same person and `is_alias_form` says they are, and
the one that says yes runs second.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER_SRC = (ROOT / "src" / "worker.py").read_text()
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.entity_aliasing import (  # noqa: E402
    find_alias_candidate,
    is_alias_form,
    is_contested_person_name,
    person_candidates,
)

# The real separated pairs from prod, 2026-09-04, verbatim.
SEPARATED = [
    ("Sam", "Sam Wisco"),
    ("Sam", "Sam Altman"),
    ("Sam", "Sam new"),
    ("Alex Rodriguez", "Alex"),
    ("Amit", "Amit O"),
    ("Srikanth", "Joy Srikanth"),
    ("Pallavi Kandanur", "Pallavi Vijay"),
    ("Sukumar Gurugubelli", "Santosh Kumar"),
    ("Ganesh Akuthota", "Naga Ganesh"),
]

ROSTER = [(f"e{i}", n) for i, n in enumerate(
    sorted({n for pair in SEPARATED for n in pair}))]
BY_NAME = {n: e for e, n in ROSTER}


# ----------------------------------------------------------------------
# Protection 1: identical names are not alias forms
# ----------------------------------------------------------------------

@pytest.mark.parametrize("a,b", SEPARATED)
def test_a_name_is_never_an_alias_form_of_itself(a, b):
    """This is what makes the exact-name twin invisible as a candidate.

    Without it, an incoming "Sam Wisco" would see both the real "Sam
    Wisco" and "Sam" as candidates. Two candidates would actually be
    SAFER, since the count guard would refuse. The rule that saves these
    pairs is the one that hides the twin, which is the opposite of what
    anyone would predict.
    """
    assert is_alias_form(a, a) is False
    assert is_alias_form(b, b) is False


# ----------------------------------------------------------------------
# Protection 2: exact name resolves before the heuristic ever runs
# ----------------------------------------------------------------------

def _store_entities_source() -> str:
    tree = ast.parse(WORKER_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "store_entities":
            return textwrap.dedent(ast.get_source_segment(WORKER_SRC, node) or "")
    raise AssertionError("store_entities not found")


def test_the_alias_table_is_consulted_before_the_heuristic():
    """Ordering IS the protection, and it lives in a different file from
    the thing it protects, which is why nobody would notice losing it."""
    body = _store_entities_source()
    assert "FROM entity_aliases a" in body
    assert "find_alias_candidate(" in body
    assert body.index("FROM entity_aliases a") < body.index("find_alias_candidate("), (
        "the heuristic now runs before the recorded-alias lookup, which "
        "removes a protection nothing else replaces"
    )


def test_the_contested_name_guard_still_runs_before_the_heuristic():
    body = _store_entities_source()
    assert "is_contested_person_name(" in body
    assert body.index("is_contested_person_name(") < body.index("find_alias_candidate(")


# ----------------------------------------------------------------------
# Protection 3: the many-candidate guard, and the hole all three share
# ----------------------------------------------------------------------

@pytest.mark.parametrize("a,b", SEPARATED)
def test_a_form_matching_both_sides_is_refused(a, b):
    """The many-candidate case, which every guard handles correctly."""
    longer = f"{a} {b.split()[-1]}"
    if not (is_alias_form(a, longer) and is_alias_form(b, longer)):
        pytest.skip(f"{longer!r} is not a form of both")
    assert find_alias_candidate(longer, ROSTER) is None


def test_the_contested_guard_cannot_see_this_case_at_all():
    """THE BUG, and it is blinder than "one candidate slipped through".

    I first wrote this test asserting the guard saw exactly ONE candidate
    and waved it past a `> 1` threshold. It sees ZERO. `person_candidates`
    matches a SHORT surface against longer roster names: a bare first
    name, or a first name plus a surname initial. It requires the
    candidate's first token to equal the surface's first token AND, for a
    multi-token surface, a surname initial expansion. "Raj Kumar" against
    a roster holding "Raj" satisfies neither, so it returns nothing.

    Meanwhile `is_alias_form` pairs on token subset in BOTH directions and
    finds the match happily. So the two functions in one module disagree
    about whether these are the same person, and the one that says yes
    runs after the one that cannot see the question.

    That is worse than a threshold being too permissive, and it is the
    correction that matters: a guard reading `> 1` looks like it protects
    the single-candidate case badly, when in this direction it is not
    consulted at all.

    This asserts what the code DOES, not what it should do. Changing the
    rule is a product decision affecting 84 automatic aliases on one
    account alone, and it has been put to Scott rather than shipped. If
    that decision is ever made, this test should fail, and its failure is
    the signal that the decision landed.
    """
    roster = [("e-old-raj", "Raj")]

    assert person_candidates("Raj Kumar", roster) == [], (
        "the contested guard now sees this case; if that is deliberate, "
        "the merge behavior below has probably changed too"
    )
    assert is_contested_person_name("Raj Kumar", roster) is False
    assert is_alias_form("Raj", "Raj Kumar") is True, (
        "the other function in the same module says these ARE the same "
        "person, which is the disagreement that produced the merge"
    )

    match = find_alias_candidate("Raj Kumar", roster)
    assert match is not None
    entity_id, existing, direction = match
    assert entity_id == "e-old-raj"
    assert existing == "Raj"
    assert direction == "name_is_canonical", (
        "this is the branch that RENAMES the existing entity to the "
        "incoming name, which is how the older person took the newer "
        "person's name"
    )
