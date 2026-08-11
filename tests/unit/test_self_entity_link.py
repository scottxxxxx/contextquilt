"""The ego link (13b prerequisite): user_id -> their own person entity.

The orbit graph excludes the ego's edges, and nothing else in the schema
can say which node is the ego: capacities describe HOW someone entered a
meeting record, not WHO the app user is (proven on prod, where 80+
entities carry `ownership` and the user does not lead). These guards pin
the marker parse, the write-path stamp with its keep-first rule, the
"(you)" entity-name hardening, and the backfill's refuse-on-ambiguity
contract.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from contextquilt.services.extraction_schema import self_speaker_label
from backfill_self_entities import pick_self_candidate

WORKER = (ROOT / "src" / "worker.py").read_text()
MIGRATION = (ROOT / "init-db" / "35_entity_self_link.sql").read_text()
BACKFILL = (ROOT / "scripts" / "backfill_self_entities.py").read_text()


# --- the marker parse -------------------------------------------------

def test_marker_names_the_self_speaker():
    t = "[Suresh] The numbers.\n[Scott (you)] p95 is 240ms.\n[Pallavi] ok"
    assert self_speaker_label(t) == "scott"


def test_no_marker_means_none():
    assert self_speaker_label("[Suresh] hi\n[Pallavi] hello") is None
    assert self_speaker_label("") is None
    assert self_speaker_label(None) is None


def test_marked_placeholder_never_qualifies():
    """"[Speaker 2 (you)]" is a diarization artifact wearing the marker;
    pinning the ego to it would defeat the placeholder gate."""
    assert self_speaker_label("[Speaker 2 (you)] hello\n[Suresh] hi") is None


def test_marker_is_case_insensitive():
    assert self_speaker_label("[Scott (You)] hi") == "scott"


# --- migration --------------------------------------------------------

def test_migration_adds_the_link_and_uniqueness():
    assert "ADD COLUMN IF NOT EXISTS self_at" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS self_source" in MIGRATION
    assert "uniq_entities_self_per_user" in MIGRATION
    assert "WHERE self_at IS NOT NULL" in MIGRATION


# --- the write-path stamp ---------------------------------------------

def test_stamp_is_keep_first():
    """A second candidate must never move the ego: the UPDATE refuses
    when any OTHER entity already carries the stamp, and the conflict is
    logged for a human rather than resolved."""
    assert "self_at IS NULL" in WORKER
    assert "NOT EXISTS" in WORKER.split("_maybe_stamp_self")[1]
    assert "self_entity_conflict" in WORKER


def test_stamp_never_lands_on_a_suppressed_row():
    """"Not a person" and "is the user" cannot both be true."""
    stamp = WORKER.split("async def _maybe_stamp_self")[1].split("async def _reobserve")[0]
    assert "suppressed_at IS NULL" in stamp


def test_stamp_degrades_where_columns_are_absent():
    """MCP's Postgres lags migrations; entity storage must not start
    failing there because the ego link shipped here."""
    stamp = WORKER.split("async def _maybe_stamp_self")[1].split("async def _reobserve")[0]
    assert "self_entity_stamp_skipped" in stamp


def test_every_resolution_path_can_stamp():
    """Exact/alias/heuristic hits funnel through _reobserve; brand-new
    entities stamp after insert. Both sites must call the stamp."""
    assert WORKER.count("await _maybe_stamp_self(") == 2


def test_extraction_lane_passes_both_identity_signals():
    """Structured metadata wins; the inline (you) marker is the
    fallback. Either way the lane passes a self_label."""
    assert "self_label=owner_speaker_label" in WORKER
    assert "or self_speaker_label(effective_summary)" in WORKER


def test_you_marker_stripped_from_entity_names_at_the_sink():
    """Prod grew a literal "Scott (you)" entity; the patch-lane
    sanitizer never covered entity names. The sink strips the marker so
    the marked form resolves to the canonical row."""
    body = WORKER.split("async def store_entities")[1].split("async def store_relationships")[0]
    assert '"(you)" in name.lower()' in body


# --- the backfill candidate rule --------------------------------------

def test_backfill_stamps_the_dominant_candidate():
    pick = pick_self_candidate({"scott": 157, "suresh": 121}, 157)
    assert pick is not None
    entity_id, cov, margin = pick
    assert entity_id == "scott"
    assert cov == 1.0


def test_backfill_refuses_low_coverage():
    """Appearing in 60% of meetings describes a close collaborator, not
    the phone holder."""
    assert pick_self_candidate({"a": 60, "b": 20}, 100) is None


def test_backfill_refuses_a_close_race():
    """Two near-total candidates means the fold machinery has work to do
    first; guessing between them is how the wrong person becomes you."""
    assert pick_self_candidate({"a": 98, "b": 95}, 100) is None


def test_backfill_handles_empty_and_zero():
    assert pick_self_candidate({}, 100) is None
    assert pick_self_candidate({"a": 5}, 0) is None


def test_backfill_is_dry_run_by_default():
    assert 'action="store_true"' in BACKFILL
    assert "args.apply" in BACKFILL


def test_backfill_keeps_first_and_skips_suppressed():
    assert "already stamped" in BACKFILL
    assert "suppressed_at IS NULL" in BACKFILL
