"""
People identity write-back: pure decision logic, no DB access.

Backs the four endpoints that let an app record what the user said about
who is who (docs/architecture/16-people.md section 5): merge, keep
separate, confirm, create. Everything here is deterministic and unit
tested; the endpoints in src/main.py own the SQL and the transaction.

The one rule worth stating up front: a separation is an UNORDERED pair.
Separating (A, B) must block a later merge of (B, A). Rather than
remembering to check both directions at every call site, the pair is
canonicalised to (lo, hi) on the way in and the table's primary key
enforces it. `canonical_pair` is the single place that ordering is
decided.
"""

from __future__ import annotations

from typing import Iterable, List, Sequence, Set, Tuple

from contextquilt.services.extraction_schema import is_placeholder_or_self_person

# Free-form, but these are the values the apps send today. Recorded so a
# later audit can tell a user's own answer apart from a heuristic or a
# backfill without guessing.
KNOWN_IDENTITY_SOURCES = frozenset({
    "user_confirmation",   # the human answered a prompt
    "app",                 # the app asserted it without an explicit prompt
    "voice_match",         # app-side speaker enrollment agreed
    "merge_backfill",      # scripts/backfill_entity_aliases.py
})

DEFAULT_IDENTITY_SOURCE = "user_confirmation"


class IdentityRequestError(ValueError):
    """Caller-fixable problem with an identity write. Endpoints map this
    to a 422 with `code` so clients can branch without string matching."""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


def canonical_pair(a: str, b: str) -> Tuple[str, str]:
    """Order an unordered entity pair so (A, B) and (B, A) collide.

    Ordering is on the UUID's string form, matching the SQL CHECK
    constraint `entity_id_lo < entity_id_hi`. Postgres compares uuid
    values bytewise and asyncpg hands us the canonical lowercase
    hyphenated text form, so Python's string ordering agrees with the
    constraint for any well-formed uuid. Inputs are lowercased first
    because a client may send an uppercased uuid.
    """
    lo, hi = a.strip().lower(), b.strip().lower()
    if not lo or not hi:
        raise IdentityRequestError(
            "EMPTY_ENTITY_ID", "Entity ids must be non-empty"
        )
    if lo == hi:
        raise IdentityRequestError(
            "SELF_PAIR", "An entity cannot be paired with itself", entity_id=lo
        )
    return (lo, hi) if lo < hi else (hi, lo)


def normalise_merge_request(
    canonical_entity_id: str,
    merge_entity_ids: Sequence[str],
) -> Tuple[str, List[str]]:
    """Clean a merge request into (canonical, losers).

    Deduplicates the loser list preserving first-seen order, drops the
    canonical id if the caller included it (harmless intent, merging
    something into itself is a no-op, not an error), and rejects a
    request that has nothing left to merge.
    """
    canonical = (canonical_entity_id or "").strip().lower()
    if not canonical:
        raise IdentityRequestError(
            "EMPTY_ENTITY_ID", "canonical_entity_id is required"
        )

    seen: Set[str] = {canonical}
    losers: List[str] = []
    for raw in merge_entity_ids or ():
        eid = (raw or "").strip().lower()
        if not eid or eid in seen:
            continue
        seen.add(eid)
        losers.append(eid)

    if not losers:
        raise IdentityRequestError(
            "NO_MERGE_TARGETS",
            "merge_entity_ids must contain at least one id that is not the canonical",
        )
    return canonical, losers


def separation_conflicts(
    canonical_entity_id: str,
    loser_ids: Iterable[str],
    separated_pairs: Iterable[Tuple[str, str]],
) -> List[Tuple[str, str]]:
    """Which requested merges the user has already refused.

    `separated_pairs` is whatever the caller read out of
    entity_separations, in any order. Returns the offending (canonical,
    loser) pairs in request order so the error can name them.

    A merge that conflicts is refused rather than silently dropped: the
    user said these are different people, and quietly merging some of a
    batch while reporting success is the kind of thing that turns into a
    "why does CQ think my two Sarahs are one person" investigation.
    """
    blocked = {canonical_pair(lo, hi) for lo, hi in separated_pairs}
    canonical = (canonical_entity_id or "").strip().lower()
    conflicts: List[Tuple[str, str]] = []
    for loser in loser_ids:
        pair = canonical_pair(canonical, loser)
        if pair in blocked:
            conflicts.append((canonical, loser.strip().lower()))
    return conflicts


def validate_person_name(name: str) -> str:
    """Accept a name for a user-created person, or explain the refusal.

    Rejects the diarization placeholders the extraction sanitizers spend
    real effort keeping out of the graph ("Speaker 3", "Unknown"). A
    person created through the API is a human vouching for someone, so
    letting a placeholder in here would be a hole straight through
    drop_placeholder_entities.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise IdentityRequestError("EMPTY_NAME", "name is required")
    if len(cleaned) > 200:
        raise IdentityRequestError(
            "NAME_TOO_LONG", "name must be 200 characters or fewer"
        )
    if is_placeholder_or_self_person(cleaned):
        raise IdentityRequestError(
            "PLACEHOLDER_NAME",
            f"'{cleaned}' is a diarization placeholder, not a person's name",
            name=cleaned,
        )
    return cleaned


# What the People read surface can and cannot answer today, reported on
# every response.
#
# This exists because the alternative is worse. Without `owed_to` (doc 16
# section 4.2) CQ has no counterparty on a commitment, so "what you owe
# this person" is structurally unanswerable. Returning 0 for that would
# render in ShoulderSurf as "you owe her nothing", which is a confident
# lie from a memory product. Returning null plus a stated reason lets the
# client render "not tracked yet" instead.
#
# Flip an entry to available=True in the same PR that makes it true.
READ_CAPABILITIES: dict = {
    "they_owe": {
        "available": True,
        "reason": None,
    },
    "you_owe": {
        "available": False,
        "reason": (
            "Commitments carry a single named owner and no counterparty, "
            "so CQ cannot tell who a commitment is owed TO. Needs the "
            "owed_to connection label (docs/architecture/16-people.md 4.2)."
        ),
    },
    "meeting_counts": {
        "available": True,
        "reason": None,
    },
    "confirmed_mention_split": {
        "available": False,
        "reason": (
            "Nothing in CQ produces a per-meeting confirmation signal for a "
            "third party; voice matching is the app's. Only the "
            "person-level confirmed flag is real."
        ),
    },
}


def capability_report() -> dict:
    """The capabilities block echoed on every People read."""
    return {name: dict(spec) for name, spec in READ_CAPABILITIES.items()}


def owner_keys(name: str, aliases: Iterable[str]) -> Set[str]:
    """Every lowercased surface form a commitment's `owner` might use.

    `value.owner` is free text the extractor copied out of a transcript,
    so matching it to a person means matching the canonical name and
    every recorded alias. Blank forms are dropped so an empty alias row
    cannot swallow every ownerless commitment.
    """
    keys = {(name or "").strip().lower()}
    keys.update((a or "").strip().lower() for a in aliases or ())
    keys.discard("")
    return keys


def merge_project_rollups(
    appearance_rows: Iterable[dict],
    stated_rows: Iterable[dict],
) -> List[dict]:
    """Combine the two ways CQ knows a person is on a project.

    `appearance_rows` are observed: the person and the project co-occur in
    real meetings, carrying a `meeting_count`. `stated_rows` come from a
    `works_on` connection, which is someone SAYING they are on it and may
    have no co-attended meeting at all.

    Both belong in "where she shows up", but they are not the same claim,
    so each result carries `observed` and `stated` flags rather than
    collapsing into one number the client cannot interpret. Ordered by
    meeting_count descending, then name, so the response is deterministic
    (a browse surface must not reshuffle between polls).
    """
    by_key: dict = {}

    def _slot(project_id, project_name):
        key = project_id or f"name:{(project_name or '').strip().lower()}"
        if key not in by_key:
            by_key[key] = {
                "project_id": project_id,
                "project": project_name,
                "meeting_count": 0,
                "observed": False,
                "stated": False,
            }
        slot = by_key[key]
        # A later row may carry the display name the earlier one lacked.
        if not slot["project"] and project_name:
            slot["project"] = project_name
        if not slot["project_id"] and project_id:
            slot["project_id"] = project_id
        return slot

    for row in appearance_rows or ():
        slot = _slot(row.get("project_id"), row.get("project"))
        slot["meeting_count"] += int(row.get("meeting_count") or 0)
        slot["observed"] = True

    for row in stated_rows or ():
        slot = _slot(row.get("project_id"), row.get("project"))
        slot["stated"] = True

    return sorted(
        by_key.values(),
        key=lambda r: (-r["meeting_count"], (r["project"] or "").lower()),
    )


def resolve_identity_source(source: str | None) -> str:
    """Normalise the caller's `source`, defaulting to user_confirmation.

    Unknown values are passed through rather than rejected: the column is
    free-form on purpose so a new app can record its own provenance
    without a CQ deploy. KNOWN_IDENTITY_SOURCES documents the vocabulary
    in use, it does not gate it.
    """
    cleaned = (source or "").strip()
    return cleaned or DEFAULT_IDENTITY_SOURCE
