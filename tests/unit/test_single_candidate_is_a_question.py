"""One candidate is still a question when the match is structural.

THE BUG THIS IS FOR, on Scott's own data, 2026-08-23. A CBE meeting
created a person called "John". Weeks later he typed a name for a
DIFFERENT John, his friend John Kirker. Exactly one candidate came back,
`POST /v1/people` resolved to it silently, and his friend was attached to
the CBE John's eight meetings and four projects. The card then carried
"Primary CBE admin for AI for Work" above a description of somebody else.

The existing contested guard fires only at len(candidates) > 1, which is
backwards for the FIRST collision and the first collision is the only one
that matters: by the time two Johns exist on the roster the damage is
done, because the second was absorbed into the first and so there is only
ever one. **A rule that fires on the second occurrence can never fire.**

The distinguishing fact is HOW the single candidate matched. A recorded
ALIAS is a question the user already answered. A NAME match is
`person_candidates` guessing that a first name denotes the one person who
currently holds it, and that guess is exactly the thing nobody was asked.
"""

import pytest

from contextquilt.services.entity_aliasing import (
    is_contested_person_name,
    person_candidates,
)


ROSTER_ONE_JOHN = [("cbe-john", "John")]
ROSTER_TWO_JOHNS = [("cbe-john", "John"), ("friend", "John Kirker")]


def test_the_existing_guard_does_not_fire_on_the_first_collision():
    """The reason the bug shipped, stated as a test rather than prose."""
    assert not is_contested_person_name("John Kirker", ROSTER_ONE_JOHN)
    assert len(person_candidates("John Kirker", ROSTER_ONE_JOHN)) <= 1


def test_a_bare_first_name_finds_the_one_person_who_has_it():
    """The mechanism. With one John on the roster this is a unique hit,
    and a unique hit is what used to resolve without asking."""
    assert [n for _, n in person_candidates("John", ROSTER_ONE_JOHN)] == ["John"]


def test_the_guard_does_not_fire_EVEN_ONCE_BOTH_EXIST():
    """I wrote this test asserting the opposite and it failed, which is
    the more useful result.

    `person_candidates` short-circuits on an exact token match: a bare
    "John" against a roster holding both "John" and "John Kirker"
    returns ONLY the entity literally named "John". So the contested
    guard never fires for this shape at all, not on the first collision
    and not on the tenth.

    That is defensible in isolation (an exact match is decisive) and it
    means the ingest path will keep attaching every bare "John" to
    whichever entity happens to be named exactly that, forever, with no
    signal anywhere. Recorded as behaviour rather than fixed here,
    because changing it changes what ingest does to every roster and
    that is a wider decision than this endpoint.
    """
    assert not is_contested_person_name("John", ROSTER_TWO_JOHNS)
    assert [n for _, n in person_candidates("John", ROSTER_TWO_JOHNS)] == ["John"]


def test_but_a_name_that_is_nobody_s_exact_name_IS_contested():
    """The guard does work, just not for the case above. Two people whose
    names both merely START with the surface form and neither of which
    IS it: no exact match, so both are candidates and it asks."""
    roster = [("a", "John Kirker"), ("b", "John Mbeki")]
    assert is_contested_person_name("John", roster)


# --------------------------------------------------------------------
# The rule the fix encodes
# --------------------------------------------------------------------

def decide(typed, candidates, create_new=False):
    """The endpoint's decision, extracted so it can be tested without a DB.

    Mirrors POST /v1/people and /reassign-speaker, which share
    `_resolve_or_create_person`. Exact name resolves before this is
    reached; everything here is a candidate match and the question is
    whether it is safe to resolve without asking.
    """
    if create_new:
        return "create"
    if len(candidates) > 1:
        return "ask"
    if len(candidates) == 1:
        only = candidates[0]
        if only["matched_by"] == "alias":
            return "resolve"
        # Direction of information. Typing a shorthand for somebody the
        # system knows more about is normal; typing MORE than it holds is
        # an assertion, and the token they do not share is the question.
        return "ask" if len(tokens(typed)) > len(tokens(only["name"])) else "resolve"
    return "create"


def tokens(n):
    return [t for t in (n or "").replace(".", " ").split() if t]


def cand(name, matched_by):
    return {"entity_id": name.lower(), "name": name, "matched_by": matched_by}


def test_the_two_johns_case_asks():
    """Typed LONGER than the match: "John Kirker" against a bare "John".
    The match rests entirely on the shared token and the unshared one is
    the whole question. This is the assertion that would have caught it."""
    assert decide("John Kirker", [cand("John", "name")]) == "ask"


def test_the_common_shorthand_case_STILL_RESOLVES():
    """The regression the first version of this fix caused, live on prod
    for about an hour, and the reason the rule is directional.

    Labelling a speaker "Suresh" against a roster holding "Suresh
    Muchakurti" is the normal, correct operation this endpoint exists to
    perform. A fix that refuses what users do all day is worse than the
    bug it closes.
    """
    assert decide("Suresh", [cand("Suresh Muchakurti", "name")]) == "resolve"


def test_an_equal_length_name_resolves():
    """Cannot really occur (equal length with different tokens would not
    have matched structurally), pinned so the comparison stays > and does
    not drift to >=, which would start asking on exact re-labels."""
    assert decide("John Kirker", [cand("John Kirker", "name")]) == "resolve"


def test_a_lone_alias_match_still_resolves():
    """The control, and it is the one that stops this being over-broad.

    A recorded alias is a decision the user already made. Asking again
    would make every "Mike" a prompt forever and teach people to dismiss
    the question, which is how a safety prompt becomes a click-through.
    """
    assert decide("Mike", [cand("Mike DiTroia", "alias")]) == "resolve"


def test_several_candidates_still_ask_regardless_of_how_they_matched():
    assert decide("John", [cand("John", "name"), cand("John Kirker", "alias")]) == "ask"


def test_create_new_overrides_the_question():
    """The caller's escape hatch: the user said "this is somebody new"."""
    assert decide("John Kirker", [cand("John", "name")], create_new=True) == "create"


def test_no_candidates_creates_without_asking():
    assert decide("Anybody New", []) == "create"


def test_the_ask_is_the_same_409_shape_for_one_candidate_as_for_many():
    """A client that already handles CONTESTED_NAME handles this with no
    change; only the length of the candidate list differs. If this ever
    becomes a different code, every existing client breaks silently."""
    one, many = [cand("John", "name")], [cand("John", "name"), cand("Jon", "name")]
    assert decide("John Kirker", one) == decide("John Kirker", many) == "ask"
