"""GET /v1/quilt/{user_id}/insights (design 10c): the follow-up rate.

The one Memory-tab number a client can never reconstruct from sync:
completion history leaves the sync surface, so a fresh install has no
basis for it. These guards pin the honesty rules: decay is excluded
from the claim (unobserved is not unfulfilled), thin history serves
null rather than a coin-flip percentage, self resolves through the ego
link with the same resolver the quilt owner chips use, and the UTC-day
bucket keeps the response stable within a day.
"""

import re
from pathlib import Path

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
BODY = MAIN.split('def quilt_insights')[1].split('@app.delete("/v1/quilt/{user_id}/patches/{patch_id}"')[0]


def test_route_exists_and_is_app_authed():
    assert '@app.get("/v1/quilt/{user_id}/insights"' in MAIN
    assert "Depends(verify_application_access)" in BODY


def test_no_self_entity_means_null_not_zero():
    """Without the ego link there is no honest "your items" population;
    null renders as not-tracked, never as a 0% rate."""
    assert "self_at IS NOT NULL" in BODY
    assert BODY.count('"follow_up": None') == 2


def test_thin_history_serves_null():
    """A percentage over two data points is a coin flip wearing a ring
    chart. The floor is a named constant, not an inline magic number."""
    assert "FOLLOW_UP_MIN_BASIS" in MAIN
    assert "basis < FOLLOW_UP_MIN_BASIS" in BODY


def test_decay_is_excluded_from_the_denominator_but_visible():
    """Archived-without-completion is decay or replacement: unobserved,
    not unfulfilled. It must not drag the rate down, and it must be
    reported so the exclusion is visible rather than silent."""
    assert '"unresolved": unresolved' in BODY
    assert "completed + overdue_open" in BODY


def test_overdue_requires_active_and_past_deadline():
    """Open items still inside their deadline are jury-out: neither
    followed up nor failed. Only active past-deadline items count
    against the rate."""
    assert 'r["status"] == "active"' in BODY
    assert 'r["deadline_date"] < today' in BODY


def test_self_resolution_matches_the_owner_chips():
    """Same edge-first machinery as the quilt action items
    (owns-edge person text first, owner surface form fallback, through
    build_entity_resolver), so this number and the chips can never
    disagree about whose item something is."""
    assert "build_entity_resolver" in BODY
    assert "owner_text_by_item" in BODY
    assert "vocab.ownership_label" in BODY


def test_ownerless_commitments_count_as_self():
    """reassign-speaker's to_self clears the owner field by contract, so
    an ownerless commitment on the user's own quilt is theirs."""
    assert re.search(r"owner_entity is None and not value\.get\(.owner.\)", BODY)


def test_day_bucketed_for_stability():
    assert "datetime.utcnow().date().isoformat()" in BODY
