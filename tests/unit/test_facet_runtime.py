"""The facet runtime: manifest facts resolved at run time, SS floor intact.

The whole point of the pass is that a new app's types get the behavior
their manifest declares WITHOUT CQ shipping code, while SS's behavior
stays byte-identical because the union can only widen the floor. These
tests pin both directions, plus the failure posture (registry down =
floor, never a crash) and the lifecycle wiring.
"""

from pathlib import Path

import pytest

from contextquilt.services import facet_runtime
from contextquilt.services.decay_model import (
    DEFAULT_TTLS,
    FRESHNESS_TRACKED_TYPES,
    DEADLINE_ANCHORED_TYPES,
    staleness_anchor_sql,
    DEADLINE_ANCHOR_SQL,
)
from contextquilt.services.facet_runtime import (
    FRESHNESS_FACETS,
    TypeRuntime,
    build_type_runtime,
    fallback_type_runtime,
)

SRC = Path(__file__).resolve().parents[2] / "src"


def row(type_key, facet=None, is_completable=False, project_scoped=False, ttl=None):
    return {
        "type_key": type_key, "facet": facet,
        "is_completable": is_completable, "project_scoped": project_scoped,
        "default_ttl_days": ttl,
    }


# The live SS manifest v9's registry rows, abbreviated to the fields the
# runtime reads. If SS's rows produce anything beyond the floor, SS
# behavior would change; this fixture is the byte-identity proof.
SS_ROWS = [
    row("trait", "Attribute", ttl=365),
    row("preference", "Affinity", ttl=365),
    row("goal", "Intention", project_scoped=True, ttl=365),
    row("constraint", "Constraint", project_scoped=True, ttl=365),
    row("person", "Connection"),
    row("org", "Connection"),
    row("project", "Connection", ttl=90),
    row("deliverable", "Connection", project_scoped=True, ttl=90),
    row("role", "Connection", project_scoped=True, ttl=365),
    row("decision", "Episode", project_scoped=True, ttl=365),
    row("commitment", "Episode", is_completable=True, project_scoped=True, ttl=30),
    row("blocker", "Episode", is_completable=True, project_scoped=True, ttl=14),
    row("takeaway", "Episode", project_scoped=True, ttl=14),
    row("event", "Episode", project_scoped=True, ttl=90),
]

TR_ROWS = [
    row("session", "Episode", ttl=90),
    row("skill_rating", "Attribute", ttl=365),
    row("improvement_area", "Constraint", is_completable=True, ttl=365),
    row("strength", "Attribute", ttl=365),
]


def test_ss_rows_change_nothing_beyond_the_floor():
    """Byte-identity for SS, by construction: SS's vocabulary is PINNED,
    so its registry flags cannot move the shipped floor at all. Only
    registry TTLs still apply to pinned types (they always have)."""
    rt = build_type_runtime(SS_ROWS)
    fb = fallback_type_runtime()
    assert rt.completable_types == fb.completable_types == ("blocker", "commitment")
    assert rt.project_scoped_types == fb.project_scoped_types
    assert rt.freshness_tracked_types == fb.freshness_tracked_types


def test_pinning_holds_the_live_role_discrepancy():
    """SS manifest v9 declares role project_scoped=true; the shipped
    write path scopes role only conditionally. Honoring the flag would
    silently change SS write output, so the pin eats it. This test IS
    the record of that open question: when the discrepancy is settled
    with SS, unpin deliberately and delete this."""
    rt = build_type_runtime(SS_ROWS)
    assert "role" not in rt.project_scoped_types


def test_completable_wins_over_freshness_facet():
    """A completable Constraint (TR's improvement_area) must anchor on
    its deadline, never on last_observed_at: freshness anchoring would
    forfeit the never-archived-before-due-date guarantee."""
    rt = build_type_runtime(SS_ROWS + TR_ROWS)
    assert "improvement_area" in rt.completable_types
    assert "improvement_area" not in rt.freshness_tracked_types


def test_tr_types_gain_their_declared_behavior():
    rt = build_type_runtime(SS_ROWS + TR_ROWS)
    assert "improvement_area" in rt.completable_types      # completable: true
    assert "improvement_area" in rt.deadline_anchored_types
    assert "skill_rating" in rt.freshness_tracked_types    # facet Attribute
    assert "strength" in rt.freshness_tracked_types
    assert "session" not in rt.freshness_tracked_types     # Episode
    # THE decay gap this slice closes: TR types enter the loop inventory.
    for t in ("session", "skill_rating", "improvement_area", "strength"):
        assert t in rt.decaying_types
    # And the SS floor is still intact underneath.
    assert "commitment" in rt.completable_types


def test_freshness_facets_equal_the_ss_name_set():
    """The mapping the whole design rests on: the four freshness facets
    produce EXACTLY the four SS freshness names from SS rows. If a facet
    is added to FRESHNESS_FACETS, this forces a deliberate look."""
    derived = {
        r["type_key"] for r in SS_ROWS if r["facet"] in FRESHNESS_FACETS
    }
    assert derived == set(FRESHNESS_TRACKED_TYPES)


def test_ordering_is_deterministic():
    """Tuples that cross into SQL params or error text must not depend
    on dict/row order (byte-stability discipline)."""
    a = build_type_runtime(SS_ROWS + TR_ROWS)
    b = build_type_runtime(list(reversed(SS_ROWS + TR_ROWS)))
    assert a.completable_types == b.completable_types
    assert a.decaying_types == b.decaying_types
    assert a.completable_types == tuple(sorted(a.completable_types))


def test_registry_only_type_without_ttl_never_decays():
    rt = build_type_runtime([row("keepsake", "Connection", ttl=None)])
    assert "keepsake" not in rt.decaying_types


def test_empty_registry_is_the_floor():
    rt = build_type_runtime([])
    fb = fallback_type_runtime()
    assert rt.completable_types == fb.completable_types
    assert rt.decaying_types == tuple(sorted(DEFAULT_TTLS))


@pytest.mark.asyncio
async def test_registry_failure_degrades_to_floor_not_crash():
    facet_runtime.invalidate_type_runtime()

    async def broken_fetch(_q):
        raise RuntimeError("registry unreachable")

    rt = await facet_runtime.get_type_runtime(broken_fetch)
    assert rt.completable_types == fallback_type_runtime().completable_types
    facet_runtime.invalidate_type_runtime()


@pytest.mark.asyncio
async def test_cache_serves_snapshot_without_refetch():
    facet_runtime.invalidate_type_runtime()
    calls = {"n": 0}

    async def fetch(_q):
        calls["n"] += 1
        return SS_ROWS + TR_ROWS

    a = await facet_runtime.get_type_runtime(fetch)
    b = await facet_runtime.get_type_runtime(fetch)
    assert calls["n"] == 1 and a is b
    facet_runtime.invalidate_type_runtime()


# --------------------------------------------------------------------
# Anchor selection honors the widened sets, defaults stay byte-identical
# --------------------------------------------------------------------

def test_anchor_sql_widens_with_runtime_sets():
    rt = build_type_runtime(SS_ROWS + TR_ROWS)
    assert staleness_anchor_sql(
        "improvement_area",
        freshness_types=rt.freshness_tracked_types,
        deadline_types=rt.deadline_anchored_types,
    ) == DEADLINE_ANCHOR_SQL
    assert staleness_anchor_sql(
        "skill_rating",
        freshness_types=rt.freshness_tracked_types,
        deadline_types=rt.deadline_anchored_types,
    ) == "COALESCE(last_observed_at, created_at)"
    # Defaults untouched: the SS floor behaves exactly as before.
    assert staleness_anchor_sql("commitment") == DEADLINE_ANCHOR_SQL
    assert staleness_anchor_sql("improvement_area") == "updated_at"


# --------------------------------------------------------------------
# Source guards: the literals stay replaced
# --------------------------------------------------------------------

def test_worker_lifecycle_reads_the_runtime():
    src = (SRC / "worker.py").read_text()
    assert "get_type_runtime" in src
    assert "runtime.decaying_types" in src, "decay loop must iterate the runtime inventory"
    assert "IN ('commitment', 'blocker')" not in src, (
        "a worker SQL literal survived; use the runtime's completable set"
    )
    assert '"decision", "commitment", "blocker", "takeaway"' not in src, (
        "the hardcoded project_scoped tuple is back"
    )


def test_main_gates_read_the_runtime():
    src = (SRC / "main.py").read_text()
    assert "async def _completable_types" in src
    # Membership checks and SQL params go through the resolved set; the
    # constant remains only as the aliased fallback + one hot-path
    # recall literal deliberately deferred to the scorer/formatter slice.
    uses = [
        l for l in src.splitlines()
        if "COMPLETABLE_PATCH_TYPES" in l and "FALLBACK_COMPLETABLE_TYPES" not in l
    ]
    assert uses == [], f"unwired COMPLETABLE_PATCH_TYPES uses remain: {uses}"
