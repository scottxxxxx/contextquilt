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


# --------------------------------------------------------------------
# The REAL two-Johns mechanism, found on the third attempt
#
# The two fixes above were both one layer too low. `_resolve_or_create_person`
# does an EXACT name lookup before any candidate logic runs, so labelling a
# speaker "John" when an entity called exactly "John" exists resolved
# immediately and never reached the code either of them touched.
#
# Proved by smoking the deployed build: typing "John" against a roster
# holding "John" returned RESOLVE, and typing "John Kirker" against the
# same roster returned ZERO candidates, so it would have created a new
# person rather than merging. The merge happened at the exact match.
# --------------------------------------------------------------------

def exact_decides(typed, roster_names, create_new=False):
    """Mirrors the exact-match branch of _resolve_or_create_person.

    Returns "ask" when the typed name matches something exactly but is a
    bare first name, "resolve" on a decisive exact match, and "fallthrough"
    when there is no exact hit at all (the candidate logic above then runs).
    """
    hit = any(n.lower() == typed.lower() for n in roster_names)
    if not hit:
        return "fallthrough"
    if len(tokens(typed)) == 1:
        # entities is UNIQUE on name; a bare taken name cannot create.
        return "name_taken" if create_new else "ask"
    return "resolve"


def test_labelling_a_speaker_John_when_a_John_exists_now_ASKS():
    """Scott's bug, at the layer it actually happened."""
    assert exact_decides("John", ["John"]) == "ask"


def test_a_full_name_exact_match_is_still_decisive():
    """"John Kirker" matching "John Kirker" IS the same person, and asking
    there would be noise on every re-label. This is the test that stops
    the rule being widened into uselessness."""
    assert exact_decides("John Kirker", ["John Kirker"]) == "resolve"


def test_create_new_on_a_taken_bare_name_asks_for_more_name():
    """Scott's ruling B (2026-08-23). entities is UNIQUE on
    (user_id, name, entity_type) and ingest upserts on it, so a second
    "John" cannot exist; the first cut of #315 said "create" here and a
    prod smoke hit the constraint. The escape is a surname."""
    assert exact_decides("John", ["John"], create_new=True) == "name_taken"


def test_no_exact_hit_falls_through_to_the_candidate_logic():
    """So the directional rule from the previous fix still governs the
    non-exact path, and the two do not overlap."""
    assert exact_decides("John Kirker", ["John"]) == "fallthrough"


def first_token_matches(surface, roster):
    """Mirrors _name_candidates(all_sharing_first_token=True)."""
    first = tokens(surface)[:1]
    return [n for n in roster if tokens(n)[:1] == first and first]


def test_the_ask_carries_EVERY_John_not_just_the_exact_one():
    """"Which John" is unanswerable from a list of one when a second
    exists. `person_candidates` short-circuits on the exact hit and would
    have shown only "John", which is the right answer to "who is named
    exactly this" and the wrong answer to "who could this mean"."""
    assert first_token_matches("John", ["John", "John Kirker", "Priya Raman"]) \
        == ["John", "John Kirker"]


def test_the_first_token_widening_does_not_leak_into_other_names():
    assert first_token_matches("John", ["Johnny Vance", "Jon Marsh"]) == []


# --------------------------------------------------------------------
# The escape must escape (2026-08-23, found checking CQ's half against
# SS's picker mechanism).
#
# `exact_decides` above returned "create" for create_new on a bare
# exact hit, and the real function did NOT: the flag skipped the ask and
# then fell into the `if row is not None:` resolve, attaching "Someone
# new" to the existing John. The mirror cannot catch a mirror, so this
# reads the source: the flag must null the exact hit for the single
# token shape, BEFORE the ask, and only for that shape.
# --------------------------------------------------------------------

def _resolver_source():
    import inspect, pathlib, re
    src = pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py"
    text = src.read_text()
    start = text.index("async def _resolve_or_create_person(")
    end = text.index("\n@app.post(", start)
    return text[start:end]


def test_create_new_on_a_bare_exact_hit_raises_NAME_TAKEN_before_the_ask():
    body = _resolver_source()
    taken = body.index("if row is not None and create_new")
    ask = body.index("if row is not None and not create_new")
    resolve = body.index("if row is not None:\n        resolved = await _load_active_person")
    assert taken < ask < resolve
    block = body[taken:ask]
    assert "len(tokenize_name(name)) == 1" in block
    assert '"code": "NAME_TAKEN"' in block
    assert "all_sharing_first_token=True" in block      # same list the ask showed
    assert "row = None" not in block                     # the first cut, now gone


def test_NAME_TAKEN_is_scoped_to_the_shape_that_asks():
    """A two-token exact match never 409s, so create_new there must not
    be refused either. Both branches share one predicate."""
    body = _resolver_source()
    taken = body.index("if row is not None and create_new")
    ask = body.index("if row is not None and not create_new")
    assert body[taken:ask].count("len(tokenize_name(name)) == 1") == 1
    assert "len(tokenize_name(name)) == 1" in body[ask:ask + 200]


def test_someone_new_is_a_keep_separate_against_every_offered_candidate():
    """Stamped in the CREATE branch, gated on create_new, against the
    first-token set (what both 409s carried), and echoed because SS's
    veto is local and reads no CQ surface. Covers the surname retry
    after NAME_TAKEN as well as the structural cases."""
    body = _resolver_source()
    create = body.index("created = True")
    patch = body.index("# The person patch.")
    block = body[create:patch]
    assert "if create_new:" in block
    assert "all_sharing_first_token=True" in block
    assert "separated_from = [" in block
    assert "INSERT INTO entity_separations" in block
    assert "for other in separated_from" in block
    assert '"separated_from": separated_from' in body


def test_separated_from_has_a_carrier_on_both_doors():
    """A contract with one carrier disappears silently (19.2); here the
    fact must reach the client on whichever door it used."""
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
    assert '"separated_from": person["separated_from"]' in text      # reassign-speaker
    assert '"separated_from": resolved["separated_from"]' in text    # POST /v1/people
