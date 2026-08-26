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

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

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
    # Both are a human naming a speaker in one meeting, which is a
    # confirmation like any other; the finer value records WHERE it was
    # answered so provenance stays auditable.
    "speaker_reassign",    # to_name on POST /v1/quilt/{u}/reassign-speaker
    "speaker_map",         # to_name on POST /v1/quilt/{u}/speaker-map
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
    # The closure ledger (open on the list, open plus completed on the
    # detail). Always available: it is computed from state CQ already
    # owns, so nothing about an app's manifest can turn it off. What CAN
    # be empty is the restatement history behind it, because a
    # restatement is only recorded from the moment that write path
    # shipped, and no backfill can invent one. An item that carries no
    # restatements is not a quiet item, it is an item CQ was not
    # watching for this yet.
    "item_ledger": {
        "available": True,
        "reason": None,
    },
    # Per meeting question counts. Available, with per meeting nulls
    # doing the honest work: null means that meeting carried no
    # measurable transcript, predates the metric, or named no speaker as
    # the user. Never "was asked nothing".
    "question_counts": {
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
    # The patch type that carries a STATED role ("Suresh is scrum master
    # on ABM project"), as opposed to the per-meeting description the
    # entity carries. None = this app does not track stated roles and
    # `title` stays null on every person, honestly.
    stated_role_type: "str | None" = "role"


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
        # Explicit null = "we do not track stated roles"; absent = floor.
        stated_role_type=(
            block["stated_role_type"] if "stated_role_type" in block
            else DEFAULT_PEOPLE_VOCABULARY.stated_role_type
        ),
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

    # Collect EVERY entity a surface form could mean, rather than the
    # first one seen. `setdefault` kept the first, and the query feeding
    # this has no ORDER BY, so a contested name resolved to whichever row
    # Postgres happened to return. That is not a policy, it is an
    # accident, and on 2026-08-17 it was deciding which of three
    # Pallavis on one project owned the work.
    claims: dict = {}
    for r in entity_rows:
        name = (r.get("name") or "").strip().lower()
        if name:
            claims.setdefault(name, set()).add(canonical(str(r["entity_id"])))
    for r in alias_rows:
        alias = (r.get("alias") or "").strip().lower()
        if alias:
            claims.setdefault(alias, set()).add(canonical(str(r["entity_id"])))

    def resolve(surface_form: "str | None") -> "str | None":
        # The entity this form means, or None when more than one could.
        #
        # A CONTESTED form returns None. Two humans genuinely share a
        # name on this roster (Mike DiTroia and Mike Rogers; three
        # Pallavis on one project), and picking one is a coin flip that
        # reads as certainty everywhere downstream: their items, their
        # meeting counts, their cards. Null already means "CQ cannot
        # tell", which is a claim we are allowed to make and the only
        # honest one available here.
        #
        # Forms that merge to ONE canonical entity are not contested, so
        # an alias and its canonical name still resolve normally.
        if not surface_form or not isinstance(surface_form, str):
            return None
        candidates = claims.get(surface_form.strip().lower())
        if not candidates or len(candidates) > 1:
            return None
        return next(iter(candidates))

    return resolve


def merge_person_clusters(
    clusters: Sequence[dict],
    resolve,
) -> List[dict]:
    """Collapse per-person-patch clusters into one cluster per HUMAN.

    The profile pass groups its candidates by person patch, and the
    extractor mints one person patch per surface form a transcript used,
    so a single colleague arrives here as several clusters. Measured on
    production 2026-08-16: Sukumar held 117 owned items split across
    `Sukumar` and `Sukumar Gurugubelli`, Vijay 110 across `Vijay` and
    `Vijay Rayudu`. Two consequences, and the second is the worse one:

    - Each form earned its own card, so one human rendered two
      HOW THEY DECIDE chips that were near paraphrases of each other.
    - Each card was derived from roughly HALF that person's record,
      because a cluster only ever saw the items hanging off its own
      form. The duplicates were the visible symptom of a corpus split.

    `resolve` maps a surface form to a canonical entity id (see
    `build_entity_resolver`: exact name, then recorded alias, then
    merged_into, and NO heuristic leg). A form that resolves to nothing
    keeps its own cluster keyed on the patch id, so an unknown name is
    still profiled rather than silently dropped; it simply cannot be
    merged with anything, which is the honest outcome when CQ cannot
    tell who it is.

    The surviving primary is chosen by `choose_surviving_person_patch`,
    so the id that identifies this person elsewhere does not change
    under us. Every form's patch id rides along in
    `person_patch_ids`, because the per-lens durable no is stamped
    against whichever form was current when a card was derived and must
    be read across all of them or a suppressed lens comes back.

    Source patch ids are unioned and de-duplicated with insertion order
    preserved, so the merged cluster is deterministic (a browse-adjacent
    pass must not reshuffle its own inputs between cycles).
    """
    by_key: dict = {}
    for cluster in clusters or ():
        patch_id = str(cluster.get("person_patch_id") or "")
        if not patch_id:
            continue
        name = cluster.get("person_name") or ""
        entity_id = resolve(name) if resolve else None
        # Unresolvable forms must not collapse into one another: key on
        # the patch id so each stays its own cluster.
        key = ("entity", entity_id) if entity_id else ("patch", patch_id)
        slot = by_key.setdefault(key, {
            "entity_id": entity_id,
            "members": [],
            "source_patch_ids": [],
            "meeting_count": 0,
        })
        slot["members"].append({
            "patch_id": patch_id,
            "text": name,
            "created_at": cluster.get("created_at"),
        })
        for pid in cluster.get("patch_ids") or ():
            slot["source_patch_ids"].append(str(pid))
        slot["meeting_count"] = max(
            slot["meeting_count"], int(cluster.get("meeting_count") or 0)
        )

    merged: List[dict] = []
    for slot in by_key.values():
        members = slot["members"]
        # The longest surface form is the best display name available
        # here; the canonical entity name is not fetched by this pass.
        canonical_name = max(
            (m["text"] for m in members if m["text"]),
            key=len, default="",
        )
        survivor, _ = choose_surviving_person_patch(members, canonical_name)
        primary = survivor or members[0]
        merged.append({
            "person_patch_id": primary["patch_id"],
            "person_patch_ids": [m["patch_id"] for m in members],
            "person_name": canonical_name or primary["text"],
            "entity_id": slot["entity_id"],
            "patch_ids": list(dict.fromkeys(slot["source_patch_ids"])),
            "meeting_count": slot["meeting_count"],
        })
    # Richest record first, then a stable tiebreak on the primary id.
    merged.sort(key=lambda c: (-len(c["patch_ids"]), c["person_patch_id"]))
    return merged


def resolve_identity_source(source: str | None) -> str:
    """Normalise the caller's `source`, defaulting to user_confirmation.

    Unknown values are passed through rather than rejected: the column is
    free-form on purpose so a new app can record its own provenance
    without a CQ deploy. KNOWN_IDENTITY_SOURCES documents the vocabulary
    in use, it does not gate it.
    """
    cleaned = (source or "").strip()
    return cleaned or DEFAULT_IDENTITY_SOURCE


# ---------------------------------------------------------------
# Ranking the candidates behind a contested typed name.
#
# SS asked for this explicitly and their reasoning decided it: they do
# not hold project membership, and a client-side guess at "likely" would
# be a second entity-resolution opinion on the device, which is exactly
# what `owner_entity_id` exists to prevent. So the server ranks and the
# client renders in the order it is sent.
# ---------------------------------------------------------------

# How many we hand a picker. Past this a list stops being a choice, and
# "someone new" has to stay reachable without scrolling.
MAX_NAME_CANDIDATES = 6


def rank_person_candidates(candidates, scope_project_ids=()):
    """Most likely answer first, by four observed signals in order.

    Present before mention-only, then same project as the meeting being
    labelled, then most recently met, then most meetings, then name for
    a stable tie break.

    PRESENT FIRST is Scott's ruling of 2026-08-26, after his live "Sam"
    run offered Sam Altman (an April article, never in a meeting) beside
    Sam Wisco (one meeting). Mention-only people STAY in the list, since
    a person you have talked about is a person you may now be meeting,
    but a speaker label is a claim that somebody was in the room, and
    the people who have been in a room before are the likelier answer.
    `present` is served on each candidate (any appearance in a
    presence-grade capacity: speaker, ownership, or the pre-31 unknown)
    so the client can show WHY the order is what it is; a candidate
    without the key sorts as not present.

    Note what is NOT in here: any claim about who the speaker probably
    is. Ordering a picker is a convenience, and being wrong costs a
    scroll. Resolving on the same signals would be a claim about a
    colleague, and Scott found the holes in both of the obvious ones
    (presence is backwards, project history is an argument from
    absence). Ranking may use what resolution must not.

    Implemented as successive stable sorts, least significant first,
    because a single tuple key cannot express "descending string"
    without inverting the value.
    """
    out = list(candidates or [])
    scope = {p for p in (scope_project_ids or ()) if p}

    out.sort(key=lambda c: ((c.get("name") or "").lower()))
    out.sort(key=lambda c: (c.get("meetings") or 0), reverse=True)
    out.sort(key=lambda c: (c.get("last_met") or ""), reverse=True)
    if scope:
        out.sort(key=lambda c: not (scope & set(c.get("projects") or ())))
    out.sort(key=lambda c: not bool(c.get("present")))
    return out


def candidate_payload(candidates, scope_project_ids=(), cap=MAX_NAME_CANDIDATES):
    """The wire shape for a contested name: ranked, capped, and honest
    about the cap.

    `total` is counted BEFORE the cap so a long tail is visible as a
    number rather than silently dropped, the same reason /v1/quilt
    counts before it truncates.
    """
    ranked = rank_person_candidates(candidates, scope_project_ids)
    return {
        "candidates": ranked[:cap],
        "total": len(ranked),
        "truncated": len(ranked) > cap,
    }


# ---------------------------------------------------------------
# Owner strings that name more than one person.
#
# SS asked for a marker so a project view can tell "Pradeep & Suresh"
# from "Steven", and was careful to say they would keep a punctuation
# heuristic if this was expensive. It is not, and CQ can do something
# they cannot: CONFIRM the parts against the roster.
#
# What CQ does NOT have is knowledge of this at extraction time. The
# model writes `value.owner` as free text and never says how many humans
# are in it, so a server-side punctuation guess would be the same guess
# SS can make, just further from the user. The roster is the only real
# advantage, and it is what this uses.
# ---------------------------------------------------------------

# Only the separators that actually mean "and another person". A slash
# is deliberately absent: "QA/dev" is one team, not two colleagues.
_OWNER_SPLIT = re.compile(r"\s*(?:&|,|\band\b|\+)\s*", re.IGNORECASE)


def split_owner_string(owner: Optional[str]) -> List[str]:
    """The candidate person names inside one owner string."""
    if not owner or not isinstance(owner, str):
        return []
    return [p.strip() for p in _OWNER_SPLIT.split(owner) if p.strip()]


def owner_names_multiple(owner: Optional[str], resolve) -> Optional[bool]:
    """Does this owner string name more than one LIVE person?

    Three valued on purpose, matching the null-means-cannot-tell
    convention everywhere else on this surface:

      False  no separator at all. One name, whoever it is.
      True   two or more parts each resolve to a live person. CONFIRMED
             by the roster, not inferred from punctuation.
      None   it looks compound but fewer than two parts resolve. Could be
             "Pradeep & the vendor", could be a name containing a comma.
             CQ cannot tell, and says so rather than guessing.

    The None case is where SS's punctuation heuristic belongs: the server
    confirms what it can prove, the client presents what it cannot. That
    split keeps a name heuristic away from the identity path, which is
    how "Pallavi Vijay" happened.
    """
    parts = split_owner_string(owner)
    if len(parts) < 2:
        return False
    if not callable(resolve):
        return None
    resolved = sum(1 for p in parts if resolve(p))
    if resolved >= 2:
        return True
    return None


def owner_is_placeholder(owner: Optional[str]) -> Optional[bool]:
    """Is this owner string a diarization label rather than a name?

    Measured on prod 2026-08-19: 51 OPEN completables across 10 projects
    carry an owner of "Speaker 3", "Speaker 8", "Unknown" and friends. A
    client cannot tell those from a real name without running a name
    heuristic on the device, which is the one thing this boundary keeps
    off the device, so the server says it.

    Sibling of owner_names_multiple, same three-valued convention and the
    same division of labour: CQ proves what it can, the client presents
    what it cannot.

      None   no owner string at all. Nothing to judge, and NOT the same
             claim as "the owner is a real person".
      True   a diarization placeholder. Somebody owns this and CQ does
             not know who; it is not unowned and it is not nobody.
      False  an ordinary owner string, whoever it turns out to name.

    The predicate itself is `is_placeholder_or_self_person`, shared with
    the ingest sanitizers, because a second copy of "what counts as a
    placeholder" is how the two halves drift apart. The self half is
    deliberately not engaged here: no user_label is passed, so a user's
    own name is a different question answered elsewhere (is_self_owned).
    """
    if not owner or not isinstance(owner, str) or not owner.strip():
        return None
    return is_placeholder_or_self_person(owner)


def owned_by_self_verdict(
    owner_entity: Optional[str],
    self_entity_id: Optional[str],
    owner_text: Optional[str],
    value_owner: object,
) -> Optional[bool]:
    """Whose completable is this: the user's, someone else's, or unknown?

    Lifted out of the quilt route so the rule can be exercised rather
    than read. It was a closure over a request-scoped resolver, so the
    only test that could reach it was one that grepped the source, and a
    source-reading test stays green while the branch it describes goes
    the other way.

    Three values, and the middle one is the whole point:

      True   the owner resolves to the ego entity, or nobody was named
             at all (the extraction contract strips the owner on the
             user's own items, so ownerless on the user's own quilt is
             theirs; same rule reassign-speaker's to_self relies on).
      False  the owner resolves to a live person who is not the user.
      None   CQ cannot tell. A diarization placeholder lands here: it
             names somebody, so the ownerless rule must not claim it for
             the user, and it names nobody CQ can identify, so calling
             it a third party's would be a confident answer to a
             question nobody asked.

    `owner_text` is the edge-or-value text the placeholder check reads;
    `value_owner` is the patch's own owner field, which alone decides the
    ownerless case. They differ when an owns-edge names someone the
    patch text does not.
    """
    if owner_entity is not None:
        return owner_entity == self_entity_id
    if owner_is_placeholder(owner_text):
        return None
    return not value_owner


# ---------------------------------------------------------------
# Stated roles and the title (2026-08-21).
#
# Suresh introduced himself as scrum master on 08-17 and the quilt
# stored it as a `role` patch. Four meetings later his card read
# "Meeting facilitator and lead", because the description under a
# person's name is whatever the LAST meeting's extraction said they
# did, and nothing on the person surface ever consulted the role. A
# statement the person made about themselves must beat an inference
# about one hour of conduct, every time. That is the whole rule here.
# Synthesis across the series (a better title learned over time) is a
# separate, model-bearing step and is deliberately not this.
# ---------------------------------------------------------------

_ROLE_LEADS = (" is ", " was ", " serves as ", " works as ", " acts as ", ": ")


def title_from_stated_role(text: Optional[str], names: Sequence[str]) -> Optional[str]:
    """Strip the person's own name and the copula from a stated-role
    text, so "Suresh is scrum master on ABM project" serves as
    "scrum master on ABM project". Returns the text unchanged when it
    does not open with one of the person's names (the role may be
    phrased without the name), and None for empty input. Never invents
    words: the output is a substring of the input or the input itself."""
    if not text or not text.strip():
        return None
    raw = text.strip()
    low = raw.lower()
    for n in sorted({(n or "").strip().lower() for n in names if n and n.strip()}, key=len, reverse=True):
        if not low.startswith(n):
            continue
        rest = raw[len(n):]
        rest_low = rest.lower()
        for lead in _ROLE_LEADS:
            # Pad so a text that ENDS on the copula ("Suresh is") still
            # matches the lead and resolves to None rather than to the
            # name plus a verb.
            if (rest_low + " ").startswith(lead):
                out = rest[len(lead):].strip()
                return out or None
        # "Suresh, scrum master on ABM" / "Suresh (scrum master)"
        if rest.startswith(",") or rest.startswith(" ("):
            out = rest.lstrip(", (").rstrip(")").strip()
            return out or None
    return raw


def stated_roles_payload(rows: Sequence[Mapping[str, Any]], names: Sequence[str]) -> Dict[str, Any]:
    """{"title", "title_source", "items": [...]} from role rows ordered
    newest first. `title` is the newest stated role, derived by
    title_from_stated_role; `title_source` carries its patch_id and
    origin so a client can open the receipt. Items keep the raw text:
    a served name may assert only what was observed (doc 16 section
    5.10), and the raw text IS the observation."""
    items = []
    for r in rows:
        items.append({
            "patch_id": str(r.get("patch_id")) if r.get("patch_id") else None,
            "text": r.get("text"),
            "project": r.get("project"),
            "project_id": r.get("project_id"),
            "origin_id": r.get("origin_id"),
            "stated_at": r.get("stated_at"),
        })
    title = None
    source = None
    for it in items:
        t = title_from_stated_role(it["text"], names)
        if t:
            title = t
            source = {"patch_id": it["patch_id"], "origin_id": it["origin_id"], "stated_at": it["stated_at"]}
            break
    return {"title": title, "title_source": source, "items": items}
