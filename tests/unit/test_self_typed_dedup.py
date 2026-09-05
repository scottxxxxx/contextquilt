"""Self-disclosure near-duplicates: the lower floor, the cue candidate,
the owner guard, and that the write path and the audit script both read
them from one module.

Measured 2026-09-04 on the largest prod account: ~390 active self-typed
rows, 18 pairs in the judged band, 749 between 0.20 and 0.35, and the
two security traits the recall block rendered back to back that night
at similarity 0.30 with three cues in common. Nothing judged them.
"""
from __future__ import annotations

from pathlib import Path

from contextquilt.services.recall_scorer import FRESHNESS_TRACKED_TYPES
from contextquilt.services.semantic_dedup import (
    CUE_CANDIDATE_SQL,
    DEDUP_JUDGE_SYSTEM,
    SELF_DISCLOSURE_TYPES,
    SELF_TRIGRAM_CANDIDATE_SQL,
    SELF_TYPED_DEDUP_FLOOR,
    SEMANTIC_DEDUP_FLOOR,
    TRIGRAM_CANDIDATE_SQL,
    TRIGRAM_DEDUP_THRESHOLD,
    dedup_floor_for,
)

ROOT = Path(__file__).resolve().parents[2]
WORKER = (ROOT / "src" / "worker.py").read_text()
AUDIT = (ROOT / "scripts" / "audit_semantic_dupes.py").read_text()


def test_self_types_are_the_freshness_set_and_get_the_lower_floor():
    assert SELF_DISCLOSURE_TYPES == FRESHNESS_TRACKED_TYPES
    assert 0 < SELF_TYPED_DEDUP_FLOOR < SEMANTIC_DEDUP_FLOOR < TRIGRAM_DEDUP_THRESHOLD
    for t in ("trait", "preference", "goal", "constraint"):
        assert dedup_floor_for(t) == SELF_TYPED_DEDUP_FLOOR
    for t in ("commitment", "decision", "takeaway", "moment"):
        assert dedup_floor_for(t) == SEMANTIC_DEDUP_FLOOR


def test_the_security_pair_is_now_a_candidate_and_was_not_before():
    """similarity 0.30 on prod: below the old floor, above the new one."""
    assert SELF_TYPED_DEDUP_FLOOR < 0.296 < SEMANTIC_DEDUP_FLOOR


def test_self_candidates_carry_the_owner_guard_and_plain_ones_do_not():
    guard = "COALESCE(cp.value->>'owner', '') = COALESCE($5, '')"
    assert guard in SELF_TRIGRAM_CANDIDATE_SQL
    assert guard in CUE_CANDIDATE_SQL
    assert "owner" not in TRIGRAM_CANDIDATE_SQL


def test_the_cue_candidate_ranks_by_shared_cues_then_similarity():
    assert "JOIN patch_cues pc ON pc.patch_id = cp.patch_id" in CUE_CANDIDATE_SQL
    assert "pc.cue = ANY($4::text[])" in CUE_CANDIDATE_SQL
    assert "ORDER BY shared_cues DESC, sim DESC" in CUE_CANDIDATE_SQL
    # Same output shape as a trigram candidate, so the fast path and the
    # judge downstream do not branch on where the candidate came from.
    for col in ("cp.patch_id", "existing_text", "cp.project_id", "AS sim"):
        assert col in CUE_CANDIDATE_SQL


def test_the_judge_prompt_names_self_disclosure_and_carries_no_dash():
    assert "trait, preference, goal or constraint" in DEDUP_JUDGE_SYSTEM
    assert "same disposition, standing rule or aim" in DEDUP_JUDGE_SYSTEM
    for ch in ("—", "–"):
        assert ch not in DEDUP_JUDGE_SYSTEM
    assert " - " not in DEDUP_JUDGE_SYSTEM


# ----------------------------------------------------------------------
# The write path
# ----------------------------------------------------------------------

def _store_path():
    i = WORKER.index("floor = dedup_floor_for(patch_type, FRESHNESS_TRACKED_TYPES)")
    return WORKER[i:i + 900]


def test_the_store_path_uses_the_module_floor_and_both_self_candidates():
    s = _store_path()
    assert "if patch_type in FRESHNESS_TRACKED_TYPES:" in s
    assert "SELF_TRIGRAM_CANDIDATE_SQL, subject_key, patch_type, text, floor, owner" in s
    assert 'if existing is None and patch.get("_cues"):' in s
    assert "CUE_CANDIDATE_SQL, subject_key, patch_type, text," in s
    assert "TRIGRAM_CANDIDATE_SQL, subject_key, patch_type, text, floor," in s


def test_the_store_path_no_longer_inlines_the_candidate_query():
    i = WORKER.index("# Deduplication, two tiers against active same-type patches")
    block = WORKER[i:i + 2500]
    assert "SELECT cp.patch_id, cp.value->>'text' AS existing_text" not in block
    assert "subject_key, patch_type, text, SEMANTIC_DEDUP_FLOOR" not in block


def test_the_owner_is_the_new_patchs_owner_or_empty_for_the_user():
    assert 'owner = str(value.get("owner") or "")' in _store_path()


# ----------------------------------------------------------------------
# The audit script
# ----------------------------------------------------------------------

def test_the_audit_has_the_self_typed_preset_and_user_filter():
    assert '"--self-typed"' in AUDIT and '"--user"' in AUDIT
    assert "types = list(SELF_DISCLOSURE_TYPES) if self_typed else list(DEDUP_TYPES)" in AUDIT
    assert "floor = SELF_TYPED_DEDUP_FLOOR if self_typed else SEMANTIC_DEDUP_FLOOR" in AUDIT


def test_the_audit_adds_cue_pairs_with_the_owner_guard_only_on_the_preset():
    assert "EXISTS (SELECT 1 FROM patch_cues ca JOIN patch_cues cb ON cb.cue = ca.cue" in AUDIT
    assert 'OWNER_GUARD if self_typed else ""' in AUDIT
    assert "if self_typed:\n            cue_sql = PAIR_SELECT.replace" in AUDIT


def test_the_audit_unions_cues_and_stamps_duplicate_of_on_merge():
    assert "INSERT INTO patch_cues (patch_id, cue)\n                    SELECT $1, cue FROM patch_cues WHERE patch_id = $2" in AUDIT
    assert "'{duplicate_of}', to_jsonb($2::text)" in AUDIT
    assert "if False" not in AUDIT
