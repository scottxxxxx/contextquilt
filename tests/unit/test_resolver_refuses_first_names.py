"""The resolver stops pairing people on a bare first name.

TWO COLLAPSES IN ONE DAY, 2026-09-04. "Raj" absorbed a different Raj
Kumar; "Chris" absorbed a different Chris Leif. Both took the
`name_is_canonical` direction, so the OLDER person's entity was RENAMED
to the NEWER person's name and months of their history was served under
a stranger's name. No merge request, no prompt, no human.

The ambiguity guard could not see either. With one Raj on the roster
there is exactly one candidate, so nothing is ambiguous by that rule, and
one candidate is precisely the case where a bare first name is most
likely to be somebody else.

Scott ruled it: stop merging first names.

THE TRADE, stated because it is real. "Vijay" arriving while "Vijay
Rayudu" exists no longer pairs, so a genuinely new first-name surface can
create a second entity for one person. That is a DUPLICATE: visible on
the page, fixable with the merge the user already has. What it replaces
is a WRONG MERGE: invisible, plausible to anyone who does not know both
people, and caught today only because Scott happened to look.

This module already made that argument, in `is_contested_person_name`:
"A wrong attribution is a claim about a real colleague that reads as
plausible and is invisible to anyone who does not know them; a missing
one is a gap the next sentence fills." The same sentence decides this.

MEASURED BLAST RADIUS on prod before shipping: 83 heuristic aliases, 38
on person entities, and 36 of those 38 are bare first names that would
now be refused. The two survivors are "Dr. Dietz" and "Dr. West", which
carry two tokens. Existing alias ROWS are untouched and still resolve
through the recorded-alias lookup, which runs before this heuristic, so
this changes what gets created and not what already works.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.entity_aliasing import find_alias_candidate  # noqa: E402

# The two real collapses, verbatim.
COLLAPSES = [("Raj", "Raj Kumar"), ("Chris", "Chris Leif")]


@pytest.mark.parametrize("short,long", COLLAPSES)
def test_the_longer_name_no_longer_renames_the_shorter_entity(short, long):
    """The exact direction that did the damage.

    `name_is_canonical` RENAMES the existing entity, so this is the
    branch that took an older colleague's identity away.
    """
    roster = [("e1", short)]
    assert find_alias_candidate(long, roster, people=True) is None


@pytest.mark.parametrize("short,long", COLLAPSES)
def test_it_still_did_that_before_the_fix(short, long):
    """The old behavior, pinned, so the fix cannot be quietly reverted
    without a test going red. `people=False` is the pre-fix path and is
    still what non-person entities get."""
    roster = [("e1", short)]
    match = find_alias_candidate(long, roster, people=False)
    assert match is not None
    assert match[2] == "name_is_canonical"


@pytest.mark.parametrize("short,long", COLLAPSES)
def test_the_short_form_arriving_is_refused_too(short, long):
    """Both directions, not just the destructive one.

    Attaching an incoming "Raj" onto an existing "Raj Kumar" does not
    rename anybody, but it still claims one person's words for another.
    """
    roster = [("e1", long)]
    assert find_alias_candidate(short, roster, people=True) is None


# ----------------------------------------------------------------------
# What must keep working
# ----------------------------------------------------------------------

def test_two_token_forms_still_pair():
    """A surname carries identifying information a first name does not.

    "Dr. Dietz" and "Dr. West" are the two real person aliases on prod
    that survive this rule, and they should.
    """
    assert find_alias_candidate(
        "Dr. Colt Dietz", [("e1", "Dr. Dietz")], people=True) is not None
    assert find_alias_candidate(
        "Mike DiTroia", [("e1", "Mike D")], people=True) is not None


def test_non_person_entities_are_untouched():
    """"KB Retrieval" folding into a fuller product name is one artifact
    getting a better name, not two artifacts sharing a first word. 45 of
    the 83 heuristic aliases on prod are this kind."""
    assert find_alias_candidate(
        "KB Retrieval dynamic category updates",
        [("e1", "KB Retrieval")]) is not None


def test_ambiguity_still_refuses_regardless():
    """The old guard is not replaced, only supplemented."""
    roster = [("e1", "Raj Kumar"), ("e2", "Raj Kapoor")]
    assert find_alias_candidate("Raj", roster, people=True) is None
    assert find_alias_candidate("Raj", roster, people=False) is None


def test_the_default_is_the_old_behavior():
    """`people` defaults to False so every existing caller is unchanged
    until it opts in. The worker opts in for person entities only."""
    assert find_alias_candidate("Raj Kumar", [("e1", "Raj")]) is not None


# ----------------------------------------------------------------------
# The caller
# ----------------------------------------------------------------------

def test_the_worker_passes_the_flag_for_people_only():
    """The rule is useless if the one caller does not opt in, and wrong
    if it opts in for everything. Source-level because the defect would
    be a MISSING ARGUMENT at a call site, which an executing test of this
    function cannot see."""
    body = (ROOT / "src" / "worker.py").read_text()
    # A window, not a paren split. The first version split on ")" and hit
    # the one inside `[(r["entity_id"], r["name"]) for r in ...]`, so it
    # failed on correct code. An instrument that reports a passing state
    # as broken is as useless as one that does the reverse.
    start = body.index("match = find_alias_candidate(")
    call = body[start:body.index("\n", body.index("candidate_rows", start))
                       + 200]
    assert "people=" in call, "the worker never opts in, so the rule never runs"
    assert "person_entity_type" in call, (
        "the worker opts in unconditionally, which would stop artifacts and "
        "projects acquiring their fuller names"
    )
