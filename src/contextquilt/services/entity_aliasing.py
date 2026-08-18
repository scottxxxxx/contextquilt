"""
Entity alias detection — pure name-form heuristics, no DB access.

Decides whether one entity name is plausibly an alternate surface form
of another ("Lockridge" / "S. Abrams" → "Lockridge Abrams"; "Axiom" →
"Axiom Industries"). Deliberately conservative: callers must only merge
when `find_alias_candidate` returns exactly one same-type match —
ambiguity ("Lockridge" with both "Lockridge Abrams" and "Lockridge Chen" present)
means no merge, matching today's behavior of separate entities.

Used by the worker's entity write path (resolve-before-insert), the
relationship resolver, and scripts/backfill_entity_aliases.py.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

from contextquilt.services.extraction_schema import is_placeholder_or_self_person

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

# Names carrying the (you) marker are sanitizer leakage from old data,
# never legitimate alias anchors ("Scott" must not merge into
# "Scott (you)" — the marked row is the one that's wrong).
_MARKER_TOKENS = frozenset({"you"})


def tokenize_name(name: str) -> List[str]:
    """Lowercase word tokens. Unicode-aware so accented names hold
    together ('José' stays one token)."""
    if not isinstance(name, str):
        return []
    return [t.lower() for t in _TOKEN_RE.findall(name)]


def _name_eligible(name: str) -> bool:
    """Names that may participate in alias matching at all. Diarization
    placeholders ("Speaker 3", "Unknown") and (you)-marker leakage are
    never alias anchors — a prod dry-run happily proposed
    'Speaker 1' → 'Speaker 10' and 'Scott' → 'Scott (you)' without this."""
    if is_placeholder_or_self_person(name):
        return False
    if _MARKER_TOKENS & set(tokenize_name(name)):
        return False
    return True


def _is_possessive_in(token: str, raw_long: str) -> bool:
    """True when `token` appears possessively in the raw long name
    ("Underhill" in "Underhill's Flatmate") — possession is a relationship to
    the entity, not an alternate name for it."""
    return re.search(
        rf"\b{re.escape(token)}['’]s\b", raw_long, re.IGNORECASE
    ) is not None


def is_alias_form(short: str, long: str) -> bool:
    """True when `short` reads as an abbreviated surface form of `long`.

    Rules (all token-based, case-insensitive):
      - token subset: every token of `short` appears in `long`
        ("Lockridge" ⊂ "Lockridge Abrams", "Axiom" ⊂ "Axiom Industries")
      - initial expansion: a single-LETTER token of `short` ("S" from
        "S. Abrams") matches the first letter of an alphabetic `long`
        token not consumed by another short token. Digits never expand
        ("Speaker 1" is not a form of "Speaker 10").
      - a single-token short form must be the first or last token of
        `long` ("Lockridge"/"Abrams" → "Lockridge Abrams", but "Artemis" is not
        a form of "Ashcombe Agent Platform Artemis Edition"), and must not
        appear possessively ("Underhill" is not "Underhill's Flatmate")
      - `long` must be strictly more informative: more tokens than
        `short`, or same token count with at least one initial expanded
      - identical names (case-insensitive) are NOT alias forms — the
        caller treats those as the same entity directly
      - placeholder / (you)-marker names never participate

    Known residual risk: a unique short brand name still merges into a
    longer same-type compound ("Google" → "Google Cloud Platform" when
    no other Google-prefixed company exists). Type scoping + the
    unique-candidate rule bound the blast radius, and the merge is
    recoverable via the rename endpoint since both forms keep resolving.
    """
    if not _name_eligible(short) or not _name_eligible(long):
        return False
    short_tokens = tokenize_name(short)
    long_tokens = tokenize_name(long)
    if not short_tokens or not long_tokens:
        return False
    if short_tokens == long_tokens:
        return False
    if len(short_tokens) > len(long_tokens):
        return False

    if len(short_tokens) == 1:
        tok = short_tokens[0]
        if len(tok) > 1:  # plain word, not an initial
            if tok != long_tokens[0] and tok != long_tokens[-1]:
                return False
            if _is_possessive_in(tok, long):
                return False

    remaining = list(long_tokens)
    expanded_initial = False

    # Consume exact-token matches first so initials only claim leftovers
    # ("S. Lockridge" against "Lockridge Mayfield" must expand S → Mayfield, not Lockridge).
    unmatched_short: List[str] = []
    for tok in short_tokens:
        if tok in remaining:
            remaining.remove(tok)
        else:
            unmatched_short.append(tok)

    for tok in unmatched_short:
        if len(tok) != 1 or not tok.isalpha():
            return False
        for i, cand in enumerate(remaining):
            if cand.startswith(tok) and cand[:1].isalpha():
                remaining.pop(i)
                expanded_initial = True
                break
        else:
            return False

    if len(short_tokens) == len(long_tokens) and not expanded_initial:
        # Same length, pure subset → identical token lists, already
        # excluded above; anything else here means reordering, which we
        # don't treat as an alias signal.
        return False
    return True


def find_alias_candidate(
    name: str,
    existing: Sequence[Tuple[Any, str]],
) -> Optional[Tuple[Any, str, str]]:
    """Match `name` against existing same-type entities of one user.

    `existing` is a sequence of (entity_id, entity_name) pairs — the
    caller is responsible for filtering to the same entity_type, and
    for excluding exact (case-insensitive) matches, which are handled
    as the same entity before any heuristics.

    Returns (entity_id, entity_name, direction) for a UNIQUE match:
      direction == "name_is_alias"     — `name` is the short form; record
                                          it as an alias of the entity
      direction == "name_is_canonical" — `name` is the longer/fuller
                                          form; rename the entity to
                                          `name` and keep its old name
                                          as an alias

    Returns None when nothing matches or when 2+ entities match
    (ambiguous — never merge on ambiguity).
    """
    matches: List[Tuple[Any, str, str]] = []
    for entity_id, entity_name in existing:
        if is_alias_form(name, entity_name):
            matches.append((entity_id, entity_name, "name_is_alias"))
        elif is_alias_form(entity_name, name):
            matches.append((entity_id, entity_name, "name_is_canonical"))
    if len(matches) == 1:
        return matches[0]
    return None


# ---------------------------------------------------------------
# Contested names.
#
# `find_alias_candidate` above already refuses to act on ambiguity, and
# that guard held. The leak was the RECORDED alias path, which is an
# exact lookup with a LIMIT 1 and no ambiguity check, so once
# 'Mike' -> Mike DiTroia exists it resolves forever, including in an
# interview with a completely different Mike.
#
# Receipt, 2026-08-17: an EMIDS interview candidate said "Mike" in
# passing. The recorded alias attached the transcript to a Kore.ai
# colleague, who acquired a meeting he was never in and a description
# reading "VP of Engineering at IMIT, on day 3 at the company". On that
# roster 17 bare first names resolve to one person while other live
# people share the name.
#
# WHAT WAS TRIED AND REJECTED, so it does not get re-proposed:
#   - "exactly one candidate is a SPEAKER in this meeting" is not just
#     weak, it is backwards. The person in the room gets addressed
#     directly; a third-person "Mike" is usually the Mike who is absent.
#   - "exactly one candidate has appeared in this PROJECT" is an
#     argument from absence (doc 19.10). A person who has never been in
#     a meeting has no way to show up in the data, so their absence is
#     ignorance, not evidence.
# Neither survives contact with two colleagues who share a first name.
# ---------------------------------------------------------------

def _surname_initial(tokens_) -> str:
    """The letter a shorthand like "Mike P" is asking about, or ''."""
    if len(tokens_) == 2 and len(tokens_[1]) == 1:
        return tokens_[1]
    return ""


def person_candidates(surface: str, roster) -> List[Tuple[Any, str]]:
    """Every live person `surface` could plausibly denote.

    `roster` is (entity_id, name) for the user's live people. Returns the
    candidates; the caller decides what more than one means.

    Three forms, and the SAME counting rule covers all of them, which is
    why "Mike P" needs no special case:

      "Mike DiTroia"  a full name. Exact match only, so it is decisive
                      even when six Mikes exist.
      "Mike P"        first name plus a surname INITIAL. Matches every
                      Mike whose surname starts with P. Unique on most
                      rosters, and honestly contested when it is not.
      "Mike"          a bare first name. Matches every Mike there is.
    """
    toks = tokenize_name(surface or "")
    if not toks:
        return []

    exact = [(eid, n) for eid, n in roster
             if tokenize_name(n or "") == toks]
    if exact:
        # A full name that names somebody is never contested, however
        # many people share its first token.
        return exact[:1] if len(toks) > 1 else exact

    initial = _surname_initial(toks)
    out: List[Tuple[Any, str]] = []
    for eid, n in roster:
        cand = tokenize_name(n or "")
        if not cand or cand[0] != toks[0]:
            continue
        if len(toks) == 1:
            out.append((eid, n))            # bare first name
        elif initial and len(cand) > 1 and cand[-1].startswith(initial):
            out.append((eid, n))            # "Mike P" against Piotrowski
    return out


def is_contested_person_name(surface: str, roster) -> bool:
    """True when `surface` could honestly mean more than one live person.

    The ingest path must not resolve these to anybody. A wrong
    attribution is a claim about a real colleague that reads as
    plausible and is invisible to anyone who does not know them; a
    missing one is a gap the next sentence fills.
    """
    return len(person_candidates(surface, roster)) > 1
