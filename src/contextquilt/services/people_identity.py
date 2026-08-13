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

from dataclasses import dataclass
from typing import Iterable, List, Sequence, Set, Tuple

from contextquilt.services.extraction_schema import (
    is_placeholder_or_self_person,
    is_user_reference,
)

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
            "This app's registered manifest declares no owed_to connection "
            "label, so CQ has no counterparty on a commitment and cannot "
            "tell who one is owed TO. Register the manifest version that "
            "declares it (docs/architecture/16-people.md 4.2)."
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
    # The 16a lens stack. Served on the DETAIL route only; the list rows
    # carry no insights and never will, because a stack of claims per
    # row is a different product than a directory.
    "insights": {
        "available": False,
        "reason": (
            "This app's registered manifest declares no person-clustered "
            "consolidation rule, so the profile pass never runs for it and "
            "no insight is ever derived. Declare one (cluster: person) to "
            "turn the lens stack on (docs/architecture/16-people.md 5.8)."
        ),
    },
}


OWED_TO_LABEL = "owed_to"


@dataclass(frozen=True)
class PeopleVocabulary:
    """Which of an app's types and labels carry People semantics.

    The People surface was born speaking SS's dialect: patch type
    `person`, entity type `person`, labels `owns` / `works_on` /
    `owed_to`. Nothing in a manifest marked those roles, so a second app
    could never have People no matter what it declared (TR's vocabulary
    shares zero of those names). The optional manifest `people` block
    names them per app; this is its resolved form.

    `counterparty_label` may be None: an app that declares a people
    block WITHOUT a counterparty is stating it does not track "you owe
    them", and the you_owe capability stays off, honestly.
    """

    person_type: str
    person_entity_type: str
    ownership_label: str
    works_on_label: str
    counterparty_label: "str | None"


# The SS floor, and the resolution for every manifest registered before
# the block existed. Absent block = this, so ShoulderSurf and GhostPour
# behave byte-identically without a re-registration.
DEFAULT_PEOPLE_VOCABULARY = PeopleVocabulary(
    person_type="person",
    person_entity_type="person",
    ownership_label="owns",
    works_on_label="works_on",
    counterparty_label=OWED_TO_LABEL,
)


def people_vocabulary(manifest: object) -> PeopleVocabulary:
    """Resolve an app's People vocabulary from its registered manifest.

    Legacy manifests (no `people` block) get the SS-default vocabulary,
    which is the compat floor, not a claim about the app: for an app
    with no `person` patch type the default vocabulary simply matches
    nothing, which is what having no people means. An EXPLICIT block is
    taken at its word, including the absence of `counterparty_label`.
    """
    if not isinstance(manifest, dict):
        return DEFAULT_PEOPLE_VOCABULARY
    block = manifest.get("people")
    if not isinstance(block, dict) or not block:
        # Missing or empty block: the legacy floor.
        return DEFAULT_PEOPLE_VOCABULARY
    person_type = block.get("person_type") or DEFAULT_PEOPLE_VOCABULARY.person_type
    return PeopleVocabulary(
        person_type=person_type,
        # Defaults to the patch type name: most apps use one word for
        # both, and requiring the repetition would invite drift.
        person_entity_type=block.get("person_entity_type") or person_type,
        ownership_label=block.get("ownership_label")
        or DEFAULT_PEOPLE_VOCABULARY.ownership_label,
        works_on_label=block.get("works_on_label")
        or DEFAULT_PEOPLE_VOCABULARY.works_on_label,
        # Deliberately NOT defaulted: an explicit block without a
        # counterparty label is the app saying "not tracked".
        counterparty_label=block.get("counterparty_label"),
    )


def manifest_declares_owed_to(manifest: object) -> bool:
    """True when this app's manifest declares a COUNTERPARTY label.

    The capability is a property of the app's registered schema, not of
    CQ's code. Shipping the read logic does not make a counterparty exist
    for an app whose extraction never emits one, and `you_owe: []` on an
    app that cannot produce the edge is exactly the "you owe her nothing"
    lie the capabilities block was built to avoid.

    Vocabulary-aware since the people block shipped: the label checked
    is the APP'S counterparty label (SS default: `owed_to`), and an
    explicit block without one answers False.
    """
    if not isinstance(manifest, dict):
        return False
    label = people_vocabulary(manifest).counterparty_label
    if label is None:
        return False
    labels = manifest.get("connection_labels")
    if not isinstance(labels, list):
        return False
    return any(
        isinstance(lb, dict) and lb.get("label") == label
        for lb in labels
    )


def is_self_owned(owner: object, user_label: "str | None") -> bool:
    """True when an action item belongs to the submitting user.

    This is the gate on the `you_owe` ledger, and it has to be RIGHT in
    one specific direction: a third party's obligation must never be shown
    to the user as their own. "Lockridge owes Marcus the vendor shortlist" has
    an owed_to edge to Marcus, and it must not surface on Marcus's card as
    something the USER owes him.

    So the predicate is stated as an inclusion, not an exclusion. An owner
    string CQ does not recognise ("Speaker 2", a name it cannot resolve)
    returns False and the item stays out of the user's ledger. Being
    absent from `you_owe` understates; being present overstates, and only
    one of those is a lie a memory product can afford.

    Matches: an empty owner (what the prompt asks for), a self token, the
    user's display name, and the display name's first token on its own
    ("Scott" for "Scott Guida"), which is how the extractor usually writes
    it when it writes it at all.

    The name matching itself is `is_user_reference`, shared with the
    extraction sanitizer on purpose. The two ran on different rules once
    (exact display name there, first token here) and that gap was a live
    bug: an item owned by "Scott Guida" kept an owed_to edge to "Scott",
    which the write path allowed and the read path then counted as the
    user owing themselves.
    """
    if owner is None:
        return True
    if not isinstance(owner, str):
        return False
    if not owner.strip():
        return True
    return is_user_reference(owner, user_label)


def capability_report(
    owed_to_available: bool = False,
    insights_available: bool = False,
) -> dict:
    """The capabilities block echoed on every People read.

    Both flags come from the CALLER'S manifest, so two apps reading the
    same user can honestly get different answers: one whose schema
    declares the counterparty label or the person-clustered
    consolidation rule, one whose does not.
    """
    report = {name: dict(spec) for name, spec in READ_CAPABILITIES.items()}
    if owed_to_available:
        report["you_owe"] = {"available": True, "reason": None}
    if insights_available:
        report["insights"] = {"available": True, "reason": None}
    return report


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


def choose_surviving_person_patch(
    candidates: Sequence[dict],
    canonical_name: str,
) -> Tuple[dict | None, List[dict]]:
    """Pick which duplicate person patch survives a merge, and which fold.

    `candidates` are person patches whose text matches the canonical or
    any folded identity, each a dict with at least `patch_id`, `text` and
    `created_at`. Returns (survivor, losers).

    Preference order:
      1. A patch whose text already IS the canonical name. Keeping it
         means the survivor needs no rewrite, so nothing about the
         surviving fact changes.
      2. Otherwise the OLDEST patch, which carries the longest history:
         it has the most connections hanging off it and the earliest
         created_at, and the caller renames its text to the canonical.

    Oldest rather than newest on purpose. The newest patch is the one the
    extractor most recently guessed at; the oldest is the one the rest of
    the quilt has been pointing at.

    Returns (None, []) for zero or one candidate: there is nothing to
    fold, and a lone patch is already the survivor.
    """
    usable = [c for c in candidates if c and c.get("patch_id")]
    if len(usable) < 2:
        return (None, [])

    target = (canonical_name or "").strip().lower()
    exact = [c for c in usable if (c.get("text") or "").strip().lower() == target]
    if exact:
        survivor = exact[0]
    else:
        # created_at may be None on hand-made rows; sort those last
        # rather than blowing up the comparison.
        survivor = sorted(
            usable, key=lambda c: (c.get("created_at") is None, c.get("created_at"))
        )[0]

    losers = [c for c in usable if c["patch_id"] != survivor["patch_id"]]
    return (survivor, losers)


def build_entity_resolver(entity_rows: Sequence[dict], alias_rows: Sequence[dict]):
    """A callable mapping a person surface form -> canonical entity_id.

    Built once per request from the user's person entities and aliases
    (set-based, no per-item queries), for stamping `owner_entity_id`
    onto quilt action items: the server resolution SS asked for so the
    client does zero entity matching (re-implementing it client-side is
    the doc 16 duplication).

    Resolution: case-insensitive exact name, then recorded alias, then
    None — the same order store_entities and the create endpoint use.
    NO heuristic leg on purpose: a wrong link on a served item is worse
    than a null, and null already means "CQ cannot tell". Merged
    entities resolve forward to their canonical (capped walk, same as
    every other forward resolution).
    """
    forward: dict = {}
    for r in entity_rows:
        forward[str(r["entity_id"])] = (
            str(r["merged_into"]) if r.get("merged_into") else None
        )

    def canonical(eid: str) -> str:
        seen = set()
        while forward.get(eid) and eid not in seen and len(seen) < 8:
            seen.add(eid)
            eid = forward[eid]
        return eid

    by_name: dict = {}
    for r in entity_rows:
        name = (r.get("name") or "").strip().lower()
        if name:
            by_name.setdefault(name, canonical(str(r["entity_id"])))
    for r in alias_rows:
        alias = (r.get("alias") or "").strip().lower()
        if alias:
            by_name.setdefault(alias, canonical(str(r["entity_id"])))

    def resolve(surface_form: "str | None") -> "str | None":
        if not surface_form or not isinstance(surface_form, str):
            return None
        return by_name.get(surface_form.strip().lower())

    return resolve


def resolve_identity_source(source: str | None) -> str:
    """Normalise the caller's `source`, defaulting to user_confirmation.

    Unknown values are passed through rather than rejected: the column is
    free-form on purpose so a new app can record its own provenance
    without a CQ deploy. KNOWN_IDENTITY_SOURCES documents the vocabulary
    in use, it does not gate it.
    """
    cleaned = (source or "").strip()
    return cleaned or DEFAULT_IDENTITY_SOURCE
