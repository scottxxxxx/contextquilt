"""The decay model, in one place, because it now has two consumers.

The worker's decay loop archives patches; the People read surface reports
`decay_state` (live | aging | stale) on open ledger items. Both MUST agree
on what "close to archival" means, or CQ relocates the split brain it was
asked to remove: reporting `stale` at one threshold while the loop archives
at another. That is the first condition CQ committed to in the 2026-08-07
turn-4 reply to ShoulderSurf: the bands are derived from the decay
parameters, never hardcoded on the read side.

Everything here mirrors the archival predicate in `worker.decay_loop`:

    archived WHEN anchor < NOW() - ttl_days * salience_multiplier
         AND NOT accessed within ttl_days   (patch_usage_metrics)

with the anchor chosen per type (self-typed types anchor on
last_observed_at, deadline-bearing completables on
GREATEST(updated_at, deadline_date), everything else on updated_at) and
permanence_override replacing the type TTL entirely when present.

The SQL fragments the loop interpolates live here too, so a change to the
predicate changes both consumers or neither.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

# --- Parameters (moved verbatim from worker.decay_loop) -------------------

# Fallback TTLs by patch_type when the registry has no entry. The four
# freshness-tracked types are sticky self-disclosure patches; their long
# horizon reflects that people change but not weekly.
DEFAULT_TTLS = {
    "takeaway": 14,
    "blocker": 30,
    "commitment": 30,
    "event": 90,
    "trait": 540,
    "preference": 540,
    "goal": 540,
    "constraint": 540,
}

# Types whose staleness is measured from `last_observed_at` rather than
# `updated_at`. Matches the partial index in init-db/20_preference_freshness.sql.
FRESHNESS_TRACKED_TYPES = {"trait", "preference", "goal", "constraint"}

# Deadline-bearing completables anchor on GREATEST(updated_at, deadline_date)
# so they never archive before their due date.
DEADLINE_ANCHORED_TYPES = {"commitment", "blocker"}

# Maps the 7 permanence classes to days. None = never expires.
PERMANENCE_CLASS_DAYS = {
    "permanent": None,
    "decade": None,
    "year": 365,
    "quarter": 90,
    "month": 30,
    "week": 14,
    "day": 1,
}

# Per-patch salience stretches or shrinks the effective TTL (high x1.5,
# low x0.5, absent = x1.0). The access-exemption window stays at the
# UNMODIFIED TTL: usage refresh is orthogonal to salience.
SALIENCE_TTL_MULTIPLIERS = {"high": 1.5, "low": 0.5}

# --- SQL fragments the decay loop interpolates ----------------------------

SALIENCE_TTL_SQL = (
    "(CASE value->>'salience' "
    "WHEN 'high' THEN 1.5 WHEN 'low' THEN 0.5 ELSE 1.0 END)"
)

# Regex-guarded cast: only sanitizer-valid ISO dates participate.
DEADLINE_ANCHOR_SQL = (
    "GREATEST(updated_at, "
    "CASE WHEN value->>'deadline_date' ~ '^\\d{4}-\\d{2}-\\d{2}$' "
    "THEN (value->>'deadline_date')::date::timestamptz "
    "ELSE updated_at END)"
)

# Registry is keyed (type_key, app_id); prefer a non-NULL app-scoped row
# (smallest TTL wins on multi-app tie, conservative), else the non-NULL
# global row, else the caller falls through to the DEFAULT_TTLS hardcode.
TTL_REGISTRY_QUERY = """
    SELECT default_ttl_days
    FROM patch_type_registry
    WHERE type_key = $1 AND default_ttl_days IS NOT NULL
    ORDER BY (app_id IS NULL) ASC, default_ttl_days ASC
    LIMIT 1
"""


def staleness_anchor_sql(patch_type: str) -> str:
    """The staleness anchor for one type, as the decay loop's SQL uses it."""
    if patch_type in FRESHNESS_TRACKED_TYPES:
        return "COALESCE(last_observed_at, created_at)"
    if patch_type in DEADLINE_ANCHORED_TYPES:
        return DEADLINE_ANCHOR_SQL
    return "updated_at"


# --- decay_state bands ----------------------------------------------------

# Band boundaries are FRACTIONS of the effective TTL remaining, not day
# counts, so they move when the TTL, salience, or anchor moves. `stale`
# means "close to the archival CQ will actually perform", computed from the
# same predicate the loop runs; a fixed day threshold would drift the
# moment a TTL changed.
AGING_REMAINING_FRACTION = 0.5
STALE_REMAINING_FRACTION = 0.2

DECAY_STATE_LIVE = "live"
DECAY_STATE_AGING = "aging"
DECAY_STATE_STALE = "stale"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day_bucket(ts: datetime) -> datetime:
    """UTC midnight of ts's day; naive timestamps are taken as UTC."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


def effective_ttl_days(
    patch_type: str,
    salience: Optional[str] = None,
    permanence_override: Optional[str] = None,
    registry_ttl_days: Optional[int] = None,
) -> Optional[float]:
    """Days of TTL the decay loop actually applies to this patch.

    None means the patch is outside the decay model and never archives
    (permanent/decade override, or a type with no TTL anywhere). An
    override replaces the type TTL entirely and, matching the loop's
    step 1, takes no salience multiplier.
    """
    if permanence_override:
        return PERMANENCE_CLASS_DAYS.get(permanence_override)
    ttl = registry_ttl_days if registry_ttl_days is not None else DEFAULT_TTLS.get(patch_type)
    if ttl is None:
        return None
    return ttl * SALIENCE_TTL_MULTIPLIERS.get(salience or "", 1.0)


def archive_after(
    patch_type: str,
    *,
    updated_at: datetime,
    created_at: Optional[datetime] = None,
    last_observed_at: Optional[datetime] = None,
    deadline_date: Optional[str] = None,
    salience: Optional[str] = None,
    permanence_override: Optional[str] = None,
    registry_ttl_days: Optional[int] = None,
    last_accessed_at: Optional[datetime] = None,
) -> Optional[datetime]:
    """The instant after which the decay loop's predicate holds.

    Mirrors the loop exactly: anchor + effective TTL, extended by the
    access-exemption leg (last access + UNMODIFIED TTL). None = never.
    """
    eff = effective_ttl_days(
        patch_type, salience, permanence_override, registry_ttl_days
    )
    if eff is None:
        return None

    if permanence_override:
        # Step 1 anchors overrides on plain updated_at, whatever the type.
        anchor = updated_at
        base_ttl = eff
    else:
        base_ttl = (
            registry_ttl_days
            if registry_ttl_days is not None
            else DEFAULT_TTLS[patch_type]
        )
        if patch_type in FRESHNESS_TRACKED_TYPES:
            anchor = last_observed_at or created_at or updated_at
        elif patch_type in DEADLINE_ANCHORED_TYPES:
            anchor = updated_at
            if deadline_date and _ISO_DATE_RE.match(deadline_date):
                due = datetime.strptime(deadline_date, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
                if updated_at.tzinfo is None:
                    anchor = max(updated_at.replace(tzinfo=timezone.utc), due)
                else:
                    anchor = max(updated_at.astimezone(timezone.utc), due)
        else:
            anchor = updated_at

    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    survives_until = anchor + timedelta(days=eff)
    if last_accessed_at is not None:
        la = last_accessed_at
        if la.tzinfo is None:
            la = la.replace(tzinfo=timezone.utc)
        survives_until = max(survives_until, la + timedelta(days=base_ttl))
    return survives_until


def decay_state(
    patch_type: str,
    *,
    updated_at: datetime,
    created_at: Optional[datetime] = None,
    last_observed_at: Optional[datetime] = None,
    deadline_date: Optional[str] = None,
    salience: Optional[str] = None,
    permanence_override: Optional[str] = None,
    registry_ttl_days: Optional[int] = None,
    last_accessed_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> str:
    """live | aging | stale for one patch, stable within a UTC day.

    `now` is bucketed to UTC midnight (the second condition committed to
    SS: recall output must stay byte-stable within a UTC day because
    upstream prompt caching depends on it, and a continuously-varying
    payload would thrash every downstream cache). Pass `now` only in tests.

    This is neglect, not age: a recall access bumps
    patch_usage_metrics.last_accessed_at and can move an item from stale
    back to live with the user having done nothing deliberate.
    """
    today = _day_bucket(now or datetime.now(timezone.utc))
    until = archive_after(
        patch_type,
        updated_at=updated_at,
        created_at=created_at,
        last_observed_at=last_observed_at,
        deadline_date=deadline_date,
        salience=salience,
        permanence_override=permanence_override,
        registry_ttl_days=registry_ttl_days,
        last_accessed_at=last_accessed_at,
    )
    if until is None:
        return DECAY_STATE_LIVE
    eff = effective_ttl_days(
        patch_type, salience, permanence_override, registry_ttl_days
    )
    remaining_days = (until - today).total_seconds() / 86400.0
    if remaining_days >= AGING_REMAINING_FRACTION * eff:
        return DECAY_STATE_LIVE
    if remaining_days >= STALE_REMAINING_FRACTION * eff:
        return DECAY_STATE_AGING
    return DECAY_STATE_STALE
