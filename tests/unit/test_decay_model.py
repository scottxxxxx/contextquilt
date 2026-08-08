"""The shared decay model: bands derived from the live parameters.

Condition 1 of the 2026-08-07 turn-4 reply to ShoulderSurf: `decay_state`
is computed from the SAME TTLs, anchors, salience multipliers and
access-exemption window the worker's decay loop archives with, never from
a hardcoded threshold. Condition 2: the value is bucketed to the UTC day.

Half of these tests exercise the pure band function; the other half are
source-level guards that keep the worker consuming the shared module, so
the "one concept, two implementations" drift (hit three times in one day
on 2026-08-07) cannot quietly come back.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contextquilt.services.decay_model import (
    AGING_REMAINING_FRACTION,
    DEFAULT_TTLS,
    STALE_REMAINING_FRACTION,
    archive_after,
    decay_state,
    effective_ttl_days,
)

SRC = Path(__file__).resolve().parents[2] / "src"

# Day-aligned fixtures so remaining-days arithmetic is exact: NOW mid-day,
# TODAY its UTC midnight, anchors set N whole days before TODAY.
NOW = datetime(2026, 8, 8, 15, 30, tzinfo=timezone.utc)
TODAY = datetime(2026, 8, 8, 0, 0, tzinfo=timezone.utc)


def days_ago(n: int) -> datetime:
    return TODAY - timedelta(days=n)


def state(**kw) -> str:
    kw.setdefault("now", NOW)
    return decay_state(kw.pop("patch_type", "commitment"), **kw)


# --------------------------------------------------------------------
# Band boundaries: fractions of the effective TTL, not day counts
# --------------------------------------------------------------------

def test_fresh_commitment_is_live():
    assert state(updated_at=days_ago(5)) == "live"


def test_mid_ttl_commitment_is_aging():
    # 30d TTL, 20 elapsed: 10 remaining, between 0.2 and 0.5 of the TTL.
    assert state(updated_at=days_ago(20)) == "aging"


def test_near_archival_commitment_is_stale():
    assert state(updated_at=days_ago(27)) == "stale"


def test_past_archival_is_stale_not_a_crash():
    """An item the loop will collect on its next pass still reports a
    band; negative remaining days is the far end of stale."""
    assert state(updated_at=days_ago(45)) == "stale"


def test_bands_move_when_the_ttl_moves():
    """The derived-not-hardcoded property, stated as behavior: the same
    patch age lands in different bands under different registry TTLs. A
    fixed day threshold would fail this."""
    twenty_days_old = dict(updated_at=days_ago(20))
    assert state(**twenty_days_old) == "aging"                       # TTL 30
    assert state(**twenty_days_old, registry_ttl_days=90) == "live"  # TTL 90
    assert state(**twenty_days_old, registry_ttl_days=22) == "stale" # TTL 22


# --------------------------------------------------------------------
# Anchors: the loop's, exactly
# --------------------------------------------------------------------

def test_future_deadline_keeps_a_completable_live():
    """Completables anchor on GREATEST(updated_at, deadline_date): never
    archived before their due date, so never stale before it either."""
    assert state(
        updated_at=days_ago(29),
        deadline_date=(TODAY + timedelta(days=2)).strftime("%Y-%m-%d"),
    ) == "live"


def test_unparseable_deadline_falls_back_to_updated_at():
    assert state(updated_at=days_ago(27), deadline_date="next Friday") == "stale"


def test_trait_anchors_on_last_observed_at_not_updated_at():
    # 540d TTL. Admin-touched yesterday but not re-observed in 500 days:
    # updated_at must not rescue it (admin edits do not refresh self-typed
    # freshness, same rule as the decay loop and the recall scorer).
    assert state(
        patch_type="trait",
        updated_at=days_ago(1),
        last_observed_at=days_ago(500),
    ) == "stale"


def test_trait_falls_back_to_created_at():
    assert state(
        patch_type="trait",
        updated_at=days_ago(1),
        created_at=days_ago(10),
        last_observed_at=None,
    ) == "live"


# --------------------------------------------------------------------
# Salience, access exemption, permanence
# --------------------------------------------------------------------

def test_low_salience_shrinks_the_ttl():
    # 30d x0.5 = 15d effective: 10 elapsed leaves 5, under 0.5x15.
    assert state(updated_at=days_ago(10), salience="low") == "aging"
    assert state(updated_at=days_ago(10), salience="high") == "live"


def test_recent_access_moves_stale_back_to_live():
    """decay_state is neglect, not age. The exemption leg (last access +
    UNMODIFIED TTL) mirrors the loop's NOT IN subquery: an item the user
    happened to recall yesterday is not about to be archived."""
    assert state(updated_at=days_ago(28)) == "stale"
    assert state(updated_at=days_ago(28), last_accessed_at=days_ago(1)) == "live"


def test_permanent_override_is_always_live():
    assert state(updated_at=days_ago(400), permanence_override="permanent") == "live"
    assert effective_ttl_days("commitment", permanence_override="decade") is None


def test_week_override_replaces_the_type_ttl():
    assert state(updated_at=days_ago(13), permanence_override="week") == "stale"


def test_type_outside_the_decay_model_is_live():
    assert state(patch_type="decision", updated_at=days_ago(1000)) == "live"


# --------------------------------------------------------------------
# UTC-day bucketing (condition 2: byte-stable within a day)
# --------------------------------------------------------------------

@pytest.mark.parametrize("age_days", [5, 14, 15, 20, 23, 24, 27, 30])
def test_same_utc_day_same_band(age_days):
    """00:01 and 23:59 of the same UTC day must agree for every age,
    including ages that sit exactly on a band boundary. Upstream caching
    depends on served payloads not stepping with the time of day."""
    early = datetime(2026, 8, 8, 0, 1, tzinfo=timezone.utc)
    late = datetime(2026, 8, 8, 23, 59, tzinfo=timezone.utc)
    anchor = days_ago(age_days) + timedelta(hours=7)  # deliberately off-midnight
    assert (
        decay_state("commitment", updated_at=anchor, now=early)
        == decay_state("commitment", updated_at=anchor, now=late)
    )


def test_archive_after_matches_the_loop_predicate_shape():
    until = archive_after("commitment", updated_at=days_ago(10))
    assert until == days_ago(10) + timedelta(days=DEFAULT_TTLS["commitment"])


def test_fraction_ordering_sanity():
    assert 0 < STALE_REMAINING_FRACTION < AGING_REMAINING_FRACTION < 1


# --------------------------------------------------------------------
# Source-level guards: one decay model, two consumers, zero copies
# --------------------------------------------------------------------

def test_worker_holds_no_private_copy_of_the_parameters():
    """The decay loop must IMPORT the model, not restate it. A second
    DEFAULT_TTLS/PERMANENCE_CLASS_DAYS literal in worker.py is the split
    brain this module exists to prevent."""
    src = (SRC / "worker.py").read_text()
    assert "from contextquilt.services.decay_model import" in src
    for name in ("DEFAULT_TTLS", "FRESHNESS_TRACKED_TYPES",
                 "DEADLINE_ANCHORED_TYPES", "PERMANENCE_CLASS_DAYS"):
        assert f"{name} = {{" not in src, (
            f"worker.py defines its own {name}; import it from "
            "services/decay_model.py instead"
        )
    assert "SALIENCE_TTL_SQL" in src
    assert "staleness_anchor_sql(" in src


def test_read_side_uses_the_shared_model():
    """The People surface computes decay_state through the module and
    resolves registry TTLs with the SAME query the loop uses."""
    src = (SRC / "main.py").read_text()
    assert "decay_model.decay_state(" in src
    assert "decay_model.TTL_REGISTRY_QUERY" in src
