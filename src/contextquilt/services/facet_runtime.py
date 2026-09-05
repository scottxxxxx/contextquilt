"""The registry-backed type runtime: manifest facts, resolved at run time.

CQ's read side and lifecycle loops grew up speaking ShoulderSurf's
type-name dialect ("commitment", "blocker", the project-scoped episode
list) while every registered manifest already declares the underlying
facts per type: `is_completable`, `project_scoped`, `facet`,
`permanence`, `default_ttl_days`, all written into `patch_type_registry`
at registration. This module is the single place those facts are read
back, so a new app's types get the behavior their manifest declares
without CQ shipping code (the "two apps vs a platform with two tenants"
gap, doc audit 2026-07-16).

Semantics are UNION-with-fallback, and the fallback is load-bearing:

- The hardcoded SS name tables remain, verbatim, as the floor. For SS
  the registry rows map onto them exactly (verified against manifest
  v9: freshness-tracked == facet in {Attribute, Affinity, Intention,
  Constraint}; deadline-anchored == is_completable), so SS behavior is
  byte-identical by construction, not by luck.
- Registry rows UNION in on top. Doc 18 gives each app its own subject
  space and (in practice) a disjoint type vocabulary, so a name-keyed
  union cannot make one app's flag change another app's behavior.

Failure posture: the registry being unreachable degrades to the
fallback snapshot, never to a crash. A worker loop that dies because a
lookup table was briefly unavailable archives nothing forever (the
gathered-loops lesson); serving SS's floor for one cache window is the
correct failure.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

import structlog

from contextquilt.services.decay_model import (
    DEFAULT_TTLS,
    FRESHNESS_TRACKED_TYPES,
)

logger = structlog.get_logger()

# Facets whose staleness is self-disclosure freshness (anchor on
# last_observed_at). Verified equal to SS's name set for manifest v9.
FRESHNESS_FACETS = frozenset({"Attribute", "Affinity", "Intention", "Constraint"})

# The SS-name floors. These are the exact literals the runtime replaces;
# they stay here so behavior can never regress below what shipped.
FALLBACK_COMPLETABLE_TYPES = ("commitment", "blocker")
FALLBACK_PROJECT_SCOPED_TYPES = frozenset({
    "decision", "commitment", "blocker", "takeaway",
    "goal", "constraint", "event", "deliverable",
})

# SS's full vocabulary, pinned: for these names the SHIPPED floor is
# authoritative and registry flags are ignored. This is not caution for
# its own sake; there is a live discrepancy. SS manifest v9 declares
# `role` project_scoped=true, but the shipped write path scopes role
# only conditionally (project id/origin when a project name resolved,
# never the name column). Honoring the flag would silently change SS
# write output. Pinning keeps SS byte-identical until that gets settled
# with SS deliberately; every NON-pinned (new-app) type is fully
# manifest-driven, which is the whole point of the pass. Registry TTLs
# still apply to pinned types, as they always have.
SS_PINNED_TYPES = frozenset(DEFAULT_TTLS) | {
    "person", "org", "project", "deliverable", "role", "decision",
}

REGISTRY_TYPES_QUERY = """
    SELECT type_key, facet, is_completable, project_scoped, default_ttl_days,
           -- Read through to_jsonb rather than as a bare column so a
           -- database that has not applied migration 38 yet returns NULL
           -- for it instead of failing the whole query. The runtime's
           -- own failure posture is to serve the floor, and losing
           -- project scoping and completability because a NEW column is
           -- missing would be a wildly disproportionate degradation.
           COALESCE(to_jsonb(t)->>'ledger_tracked', 'false') = 'true'
               AS ledger_tracked
    FROM patch_type_registry t
"""

# How long a snapshot is trusted before re-reading the registry. Type
# facts change only at manifest registration (rare, already an
# output-changing event); five minutes bounds the drift window between
# the API and worker processes without putting the registry on any hot
# path.
CACHE_TTL_SECONDS = 300


@dataclass(frozen=True)
class TypeRuntime:
    """One immutable snapshot of every type-keyed behavior decision."""

    # Sorted tuples where the value crosses into SQL params or error
    # messages: deterministic order keeps output byte-stable.
    completable_types: tuple
    project_scoped_types: frozenset
    freshness_tracked_types: frozenset
    facet_by_type: Mapping
    # Every type the decay loop must visit: the SS floor plus each
    # registry type carrying a TTL. A registry type absent here decays
    # never (permanent/decade), which is a statement, not an oversight.
    decaying_types: tuple
    # Types the item ledger holds across meetings: objects that can be
    # UNRESOLVED. See `ledger_tracked_types` for why this is not simply
    # the completable set, and doc 16 section 5.9a for why the primitive
    # is not a commitment.
    ledger_declared_types: frozenset
    # Types a NO-PROJECT recall fetches: self-disclosure that applies
    # everywhere. SS floor is {trait, preference}; a non-pinned type
    # joins when its facet is self-disclosure (Attribute/Affinity/
    # Intention/Constraint) AND it is not project-scoped. Built from
    # facets directly, not the freshness set, because a completable
    # Constraint (universal obligation) belongs here even though decay
    # anchors it on its deadline.
    universal_recall_types: tuple
    # Conduct types: declared `origin_scoped: true` and NOT project
    # scoped, the unit the profile pass reads (SS: moment). They rank
    # below the compact person rules in recall, render as a per-person
    # capsule rather than as list rows, and never earn a Woven tile on
    # their own. Read from the registered manifests, not the registry,
    # because the registry never learned the flag; empty when no
    # manifest declares one. Measured 2026-09-05 in the persona test:
    # three moments in a 15-row block displaced the FCR takeaway, Dana's
    # risks-as-owner-and-date preference and the trait, and the full
    # block lost to the block without moments on every question.
    conduct_types: frozenset = frozenset()

    def is_completable(self, patch_type: str) -> bool:
        return patch_type in self.completable_types

    @property
    def deadline_anchored_types(self) -> frozenset:
        # Deadline anchoring IS completability: an item with a due date
        # must never archive before it. One concept, one set.
        return frozenset(self.completable_types)

    @property
    def ledger_tracked_types(self) -> frozenset:
        """Types the item ledger holds as objects that can be unresolved.

        THE ELIGIBILITY ANSWER, and it is deliberately not the
        completable set alone.

        `is_completable` means a type can be CLOSED by the completion
        machinery, and in this codebase it drags two other things with
        it: deadline anchoring (see above, one concept one set) and
        membership of the People `commitments.they_owe` ledger. That
        makes it the wrong gate for the general primitive. A question
        that keeps coming back has no due date and must never appear as
        something a person OWES; declaring its type completable to get
        it into the ledger would do both by side effect.

        So eligibility is its own declaration, and it is a UNION:

        - every completable type, because an object with an open and a
          closed state is exactly what this ledger holds. This is what
          keeps day one coverage identical to what shipped: SS's
          commitment and blocker arrive here through this half, with no
          manifest change and no registry row required.
        - plus any type whose manifest declares `ledger_tracked: true`,
          which is how a question, an unowned concern or a decision that
          keeps getting revisited joins WITHOUT a CQ deploy.

        The two halves are not redundant. The first says "this can be
        finished"; the second says "this can be unfinished", and only
        the second one generalises past commitments.
        """
        return frozenset(self.completable_types) | self.ledger_declared_types


FALLBACK_UNIVERSAL_RECALL_TYPES = ("preference", "trait")


def fallback_type_runtime() -> TypeRuntime:
    """The SS floor alone: what CQ did before the registry was consulted."""
    return TypeRuntime(
        completable_types=tuple(sorted(FALLBACK_COMPLETABLE_TYPES)),
        project_scoped_types=frozenset(FALLBACK_PROJECT_SCOPED_TYPES),
        freshness_tracked_types=frozenset(FRESHNESS_TRACKED_TYPES),
        facet_by_type={},
        # Nothing is ledger-declared in the floor: SS's completables
        # arrive through the completable half of the union, exactly as
        # they did before the ledger existed.
        ledger_declared_types=frozenset(),
        decaying_types=tuple(sorted(DEFAULT_TTLS)),
        universal_recall_types=FALLBACK_UNIVERSAL_RECALL_TYPES,
    )


def _flag(row, key: str) -> bool:
    """One boolean off a registry row, False when the column is absent.

    asyncpg Records raise KeyError rather than returning None for a
    column that is not in the result set, and the ledger flag is newer
    than some of the databases this runs against."""
    try:
        return bool(row[key])
    except (KeyError, IndexError, TypeError):
        return False


# Latest registered manifest per app. Doc 18 keeps vocabularies disjoint
# across apps, so a union over apps names each type once.
MANIFEST_QUERY = """
    SELECT DISTINCT ON (app_id) manifest
      FROM app_schemas
     ORDER BY app_id, version DESC
"""


def conduct_types_from_manifests(manifests) -> frozenset:
    """Types every manifest declares origin_scoped and not project_scoped."""
    out = set()
    for m in manifests or []:
        # A registry-shaped row ({"manifest": ...} or an asyncpg Record)
        # unwraps to its manifest; a manifest is the dict with patch_types.
        if not isinstance(m, dict) or ("manifest" in m and "patch_types" not in m):
            m = m.get("manifest") if hasattr(m, "get") else None
        if isinstance(m, str):
            try:
                m = json.loads(m)
            except ValueError:
                continue
        if not isinstance(m, dict):
            continue
        for t in m.get("patch_types") or []:
            if not isinstance(t, dict):
                continue
            name = t.get("domain_type")
            if isinstance(name, str) and t.get("origin_scoped") is True and not t.get("project_scoped"):
                out.add(name)
    return frozenset(out)


def build_type_runtime(rows, manifests=None) -> TypeRuntime:
    """Fold registry rows over the fallback floor. Pure; unit-tested.

    A type_key may appear more than once (a global row plus app-scoped
    rows). Flags union permissively — any row saying completable makes
    the type completable — because the flags gate BEHAVIOR OFFERED
    (completion, project scoping), and under doc 18's disjoint
    vocabularies the rows for one name all describe the same app's type
    anyway.
    """
    completable = set(FALLBACK_COMPLETABLE_TYPES)
    project_scoped = set(FALLBACK_PROJECT_SCOPED_TYPES)
    freshness = set(FRESHNESS_TRACKED_TYPES)
    facets: dict = {}
    ledger_declared: set = set()
    decaying = set(DEFAULT_TTLS)
    universal = set(FALLBACK_UNIVERSAL_RECALL_TYPES)

    for r in rows or []:
        t = r["type_key"]
        if not t:
            continue
        facet = r["facet"]
        if facet:
            # First non-null wins; per-name rows describe one app's type.
            facets.setdefault(t, facet)
        if t in SS_PINNED_TYPES:
            # The pin covers the DECAY INVENTORY too, not just the flags.
            # Learned on prod 2026-08-10, the hard way: SS types outside
            # DEFAULT_TTLS (project, deliverable, role, decision) carry
            # registry TTLs from registration, and letting them into the
            # inventory gave them their FIRST-EVER decay pass, which
            # archived 63 project + 14 deliverable patches on a live
            # quilt minutes after deploy (restored, cause-stamped rows
            # made them findable). "Never decayed" was shipped SS
            # behavior, not an SS bug to fix silently. Registry TTL
            # OVERRIDES for types already in DEFAULT_TTLS still apply
            # inside the loop, as they always have.
            continue
        if r["default_ttl_days"] is not None:
            decaying.add(t)
        if r["is_completable"]:
            completable.add(t)
        # A type that declares itself ledger tracked without being
        # completable: the widening path (a recurring question, an
        # unowned concern). Read through a key that may be absent on a
        # row from a database predating migration 38, which reads as
        # not declared, which is today's behavior.
        if _flag(r, "ledger_tracked"):
            ledger_declared.add(t)
        if r["project_scoped"]:
            project_scoped.add(t)
        else:
            if facet in FRESHNESS_FACETS:
                universal.add(t)
        if facet in FRESHNESS_FACETS:
            freshness.add(t)

    # A completable is never freshness-anchored: its deadline anchor
    # (GREATEST(updated_at, deadline_date)) is what guarantees it cannot
    # archive before its due date, and a last_observed_at anchor would
    # silently forfeit that. Matters for types like a completable
    # Constraint (TR's improvement_area), where the facet alone would
    # claim freshness.
    freshness -= completable

    return TypeRuntime(
        completable_types=tuple(sorted(completable)),
        project_scoped_types=frozenset(project_scoped),
        freshness_tracked_types=frozenset(freshness),
        facet_by_type=facets,
        ledger_declared_types=frozenset(ledger_declared),
        decaying_types=tuple(sorted(decaying)),
        universal_recall_types=tuple(sorted(universal)),
        conduct_types=conduct_types_from_manifests(manifests),
    )


_cache_snapshot: Optional[TypeRuntime] = None
_cache_loaded_at: float = 0.0


async def get_type_runtime(fetch: Callable, *, ttl_seconds: int = CACHE_TTL_SECONDS) -> TypeRuntime:
    """The current snapshot, re-read from the registry at most once per TTL.

    `fetch` is any asyncpg-style fetch callable (pool.fetch, conn.fetch,
    worker db.fetch). On any registry failure the previous snapshot (or
    the fallback floor) is served and the error logged once per miss.
    """
    global _cache_snapshot, _cache_loaded_at
    now = time.monotonic()
    if _cache_snapshot is not None and (now - _cache_loaded_at) < ttl_seconds:
        return _cache_snapshot
    try:
        rows = await fetch(REGISTRY_TYPES_QUERY)
        try:
            manifests = [r["manifest"] for r in await fetch(MANIFEST_QUERY)]
        except Exception as exc:
            # A database without app_schemas (the MCP lane) has no
            # manifests and therefore no conduct types; nothing else
            # about the snapshot depends on this read.
            logger.warning("type_runtime_manifests_unavailable", error=str(exc)[:200])
            manifests = []
        _cache_snapshot = build_type_runtime(rows, manifests)
    except Exception as exc:
        logger.warning("type_runtime_registry_unavailable", error=str(exc)[:200])
        if _cache_snapshot is None:
            _cache_snapshot = fallback_type_runtime()
    _cache_loaded_at = now
    return _cache_snapshot


def invalidate_type_runtime() -> None:
    """Drop the snapshot (manifest registration calls this so the same
    process serves the new facts immediately; other processes converge
    within the TTL)."""
    global _cache_snapshot, _cache_loaded_at
    _cache_snapshot = None
    _cache_loaded_at = 0.0
