"""
Entity alias detection — pure name-form heuristics, no DB access.

Decides whether one entity name is plausibly an alternate surface form
of another ("Sarah" / "S. Abrams" → "Sarah Abrams"; "ABM" →
"ABM Industries"). Deliberately conservative: callers must only merge
when `find_alias_candidate` returns exactly one same-type match —
ambiguity ("Sarah" with both "Sarah Abrams" and "Sarah Chen" present)
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
    ("Annie" in "Annie's Flatmate") — possession is a relationship to
    the entity, not an alternate name for it."""
    return re.search(
        rf"\b{re.escape(token)}['’]s\b", raw_long, re.IGNORECASE
    ) is not None


def is_alias_form(short: str, long: str) -> bool:
    """True when `short` reads as an abbreviated surface form of `long`.

    Rules (all token-based, case-insensitive):
      - token subset: every token of `short` appears in `long`
        ("Sarah" ⊂ "Sarah Abrams", "ABM" ⊂ "ABM Industries")
      - initial expansion: a single-LETTER token of `short` ("S" from
        "S. Abrams") matches the first letter of an alphabetic `long`
        token not consumed by another short token. Digits never expand
        ("Speaker 1" is not a form of "Speaker 10").
      - a single-token short form must be the first or last token of
        `long` ("Sarah"/"Abrams" → "Sarah Abrams", but "Artemis" is not
        a form of "Cory Agent Platform Artemis Edition"), and must not
        appear possessively ("Annie" is not "Annie's Flatmate")
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
    # ("S. Sarah" against "Sarah Smith" must expand S → Smith, not Sarah).
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
