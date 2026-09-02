"""The profile pass (16a / 12a): person-keyed consolidation.

The same machinery as the cue pass with the cluster key changed, and
three deliberate differences these guards pin: the durable-no
idempotency PER LENS (a user-deleted insight is never re-derived for
the lens it carried, while the person's other lenses stay open), the
self exclusion (lenses are about the counterparty), and the receipts
gate (a claim spans >= min_meetings DISTINCT meetings, checked in SQL
and re-checked against the fetched sources).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.consolidation import (
    DEFAULT_MIN_MEETINGS,
    MODEL_CHOSEN_LENSES,
    PROFILE_LENSES,
    PROFILE_SYSTEM,
    build_profile_content,
    manifest_declares_person_insights,
    parse_consolidation_rules,
    parse_profile_response,
    remaining_lenses,
)
from contextquilt.services.people_identity import capability_report

WORKER = (ROOT / "src" / "worker.py").read_text()
MAIN = (ROOT / "src" / "main.py").read_text()
PERSON_BODY = WORKER.split("async def _consolidate_user_people")[1].split(
    "async def _taken_lenses"
)[0]
TAKEN_BODY = WORKER.split("async def _taken_lenses")[1].split(
    "async def _synthesize_person_cluster"
)[0]
SYNTH_BODY = WORKER.split("async def _synthesize_person_cluster")[1].split(
    "async def _write_person_insight"
)[0]
# The write is shared by every lens pass now, so the provenance guards
# point at the one writer rather than at the model pass that used to
# inline it.
WRITE_BODY = WORKER.split("async def _write_person_insight")[1].split(
    "async def _derive_follow_through"
)[0]
SERVE_BODY = MAIN.split("insights: Optional[list] = []")[1].split(
    "detail = _public_person"
)[0]


def _manifest(rules):
    return {
        "patch_types": [
            {"domain_type": "commitment"}, {"domain_type": "takeaway"},
            {"domain_type": "insight"},
        ],
        "consolidation_rules": rules,
    }


# --- rule parsing ------------------------------------------------------

def test_person_cluster_rule_parses_with_receipts_default():
    rules = parse_consolidation_rules(_manifest([{
        "from_types": ["commitment", "takeaway"], "produce_type": "insight",
        "cluster": "person",
    }]))
    assert rules and rules[0]["cluster"] == "person"
    assert rules[0]["min_meetings"] == DEFAULT_MIN_MEETINGS


def test_legacy_rules_default_to_cue_cluster():
    rules = parse_consolidation_rules(_manifest([{
        "from_types": ["takeaway"], "produce_type": "insight",
    }]))
    assert rules and rules[0]["cluster"] == "cue"


def test_unknown_cluster_key_drops_the_rule():
    assert parse_consolidation_rules(_manifest([{
        "from_types": ["takeaway"], "produce_type": "insight",
        "cluster": "vibes",
    }])) == []


def test_min_meetings_floor_is_enforced():
    rules = parse_consolidation_rules(_manifest([{
        "from_types": ["takeaway"], "produce_type": "insight",
        "cluster": "person", "min_meetings": 1,
    }]))
    assert rules[0]["min_meetings"] == DEFAULT_MIN_MEETINGS


# --- the profile prompt and parse --------------------------------------

def test_prompt_embeds_the_raw_json_shape_and_is_dash_free():
    """The Anthropic client does not enforce json_schema on the wire,
    and served prompt text must never carry em or en dashes (models
    copy the punctuation they see)."""
    assert '{"skip":' in PROFILE_SYSTEM
    assert "—" not in PROFILE_SYSTEM and "–" not in PROFILE_SYSTEM


def test_parse_accepts_a_whitelisted_lens_only():
    good = {"skip": False, "lens": "how_they_decide",
            "text": "Says yes in the room, then reopens scope async.",
            "do": "End with them restating the deliverable."}
    assert parse_profile_response(dict(good))["lens"] == "how_they_decide"
    assert parse_profile_response(dict(good) | {"lens": "charisma"}) is None
    assert parse_profile_response(dict(good) | {"skip": True}) is None


def test_parse_requires_both_claim_and_do():
    """16a renders claim and do together or not at all."""
    base = {"skip": False, "lens": "what_moves_them",
            "text": "Engages when someone leads with a measurement.",
            "do": ""}
    assert parse_profile_response(base) is None


def test_content_carries_dates_because_patterns_are_about_time():
    c = build_profile_content("Suresh", [("2026-06-17", "a"), ("2026-07-29", "b")])
    assert "[2026-06-17]" in c and "[2026-07-29]" in c


# --- the lens stack ----------------------------------------------------

def test_remaining_lenses_offers_the_model_chosen_half_only():
    """The profile call picks between the lenses a model is allowed to
    pick between. A computed lens is derived by its own pass and must
    never be on offer here, in either direction of the subtraction."""
    assert remaining_lenses() == sorted(MODEL_CHOSEN_LENSES)
    assert remaining_lenses([]) == sorted(MODEL_CHOSEN_LENSES)
    assert set(remaining_lenses()) < PROFILE_LENSES


def test_one_taken_lens_leaves_the_others_open():
    """The point of the change: a person with one card is still a
    candidate for the rest of the vocabulary."""
    left = remaining_lenses(["how_they_decide"])
    assert left == ["what_moves_them"]
    assert left  # the vocabulary must not close on the first card


def test_a_full_stack_leaves_nothing_open():
    assert remaining_lenses(MODEL_CHOSEN_LENSES) == []
    assert remaining_lenses(PROFILE_LENSES) == []


def test_a_drifted_stamp_cannot_retire_a_real_lens():
    """An unknown lens value counts for nothing: it must never be able
    to close a lens the vocabulary actually holds."""
    assert remaining_lenses(["charisma", None, ""]) == sorted(MODEL_CHOSEN_LENSES)


def test_prompt_names_the_taken_lenses_and_stays_dash_free():
    c = build_profile_content(
        "Suresh", [("2026-06-17", "a")], taken_lenses={"how_they_decide"},
    )
    assert "how_they_decide" in c
    assert "what_moves_them" in c  # named as still open
    assert "—" not in c and "–" not in c


def test_prompt_is_unchanged_when_nothing_is_taken():
    """No hint, no wasted tokens, and the pre-stack prompt byte for byte."""
    dated = [("2026-06-17", "a"), ("2026-07-29", "b")]
    assert build_profile_content("Suresh", dated, taken_lenses=None) == \
        build_profile_content("Suresh", dated)


# --- worker guards -----------------------------------------------------

def test_idempotency_is_a_durable_no_per_lens():
    """The SQL gate must ignore status: a user-deleted (archived)
    insight is a permanent no for the lens it carried, which is what
    makes hold-to-suppress via the existing DELETE route durable.

    It is now keyed on the LENS COUNT rather than on existence, because
    the model picks the lens after the call. A person leaves the
    candidate set only once the stamps cover the whole vocabulary.
    """
    gate = PERSON_BODY.split("count(DISTINCT d.value->>'lens')")[1].split(
        "GROUP BY"
    )[0]
    assert "source_person" in gate
    assert "status" not in gate
    assert "< $11" in gate
    # The ceiling is the vocabulary itself, never a literal, and it is
    # the MODEL-CHOSEN half: this call cannot produce a computed lens, so
    # counting one would hold the person in the candidate set forever.
    assert "len(MODEL_CHOSEN_LENSES)," in PERSON_BODY
    assert "d.value->>'lens' = ANY($12::text[])" in gate


def test_suppressing_one_lens_does_not_bar_the_others():
    """The old gate was NOT EXISTS on ANY prior insight, which made a
    second card structurally impossible. It must be gone."""
    assert "NOT EXISTS" not in PERSON_BODY


def test_taken_lenses_lookup_ignores_status():
    """The set the post-check reads is the durable no itself, so it must
    see archived rows."""
    sql = TAKEN_BODY.split("SELECT DISTINCT")[1].split('"""')[0]
    assert "d.value->>'lens'" in sql
    assert "origin_mode = 'derived'" in sql
    assert "status" not in sql


def test_the_post_check_declines_a_taken_lens_without_writing():
    """A prompt hint is never an invariant: the lens is re-read after
    the call and a repeat is declined before any INSERT."""
    pre_insert = SYNTH_BODY.split("INSERT INTO context_patches")[0]
    assert 'if profile["lens"] in taken_lenses:' in pre_insert
    assert pre_insert.index('if profile["lens"] in taken_lenses:') > \
        pre_insert.index("parse_profile_response")


def test_self_is_excluded_and_degrades_without_the_ego_link():
    assert "self_at IS NOT NULL" in PERSON_BODY
    assert "self_name = None" in PERSON_BODY
    assert "<> lower(btrim($9))" in PERSON_BODY


def test_receipts_gate_in_sql_and_recheck_in_code():
    assert "count(DISTINCT cp.origin_id) >= $6" in PERSON_BODY
    assert "len(distinct_origins) < rule[\"min_meetings\"]" in SYNTH_BODY


def test_provenance_matches_the_cue_pass():
    assert "'derived', 'profile_pass'" in WRITE_BODY
    assert "source_patch_ids" in WRITE_BODY
    assert "'informs', 'consolidated_into'" in WRITE_BODY


# --- serving guards ----------------------------------------------------

def test_detail_serves_active_insights_only_with_evidence():
    assert "source_person" in SERVE_BODY
    assert "COALESCE(cp.status, 'active') = 'active'" in SERVE_BODY
    assert '"evidence": evidence' in SERVE_BODY


def test_serving_never_fails_the_detail_route():
    assert "except Exception" in SERVE_BODY


def test_evidence_carries_the_source_text_and_its_patch_ids():
    """The design shows a sentence per moment. The source patch's own
    text is CQ state, so serving it does not cross the doc 15 line."""
    assert '"text": e["text"]' in SERVE_BODY
    assert '"patch_ids"' in SERVE_BODY


def test_evidence_is_one_row_per_meeting_with_a_deterministic_pick():
    """Distinct meetings, and a total order for the representative, so
    two identical calls render identically."""
    ev = SERVE_BODY.split("SELECT origin_id,")[1].split('"""')[0]
    assert "GROUP BY origin_id" in ev
    assert "ORDER BY created_at, patch_id" in ev
    assert "ORDER BY met_on ASC, origin_id ASC" in ev


def test_archived_sources_are_not_live_receipts():
    ev = SERVE_BODY.split("SELECT origin_id,")[1].split('"""')[0]
    assert "COALESCE(status, 'active') = 'active'" in ev


def test_the_evidence_date_is_named_for_what_it_is():
    """min(created_at) is the INGEST date. CQ persists no meeting date
    and must not imply one; `date` survives as the legacy alias because
    the served surface is additive only."""
    assert '"ingested_on"' in SERVE_BODY
    assert '"date"' in SERVE_BODY


def test_insight_decay_comes_from_the_shared_model_not_a_confidence_float():
    """decay_state on the insight patch itself, from the same module the
    worker's decay loop reads. A per-source confidence fraction would
    report a threshold the decay loop never acts on."""
    assert "decay_model.decay_state(" in SERVE_BODY
    assert "freshness_types=ins_runtime.freshness_tracked_types" in SERVE_BODY
    served_keys = [ln for ln in SERVE_BODY.splitlines()
                   if ln.strip().startswith('"')]
    assert not any("confidence" in ln for ln in served_keys)


def test_decay_state_is_null_when_the_type_carries_no_ttl():
    """Null means not tracked. Reporting `live` for a type outside the
    decay model would be a band CQ cannot stand behind."""
    assert "decay_model.effective_ttl_days(" in SERVE_BODY
    assert "ins_state = None" in SERVE_BODY


def test_only_a_failed_fetch_is_a_cannot_tell():
    """A patchless person is the thinnest NOT YET, not a cannot-tell: an
    entity accumulates a person patch as it is observed. Serving null
    there rendered nothing for the exact case the not-yet card exists
    for, so [] is the default and null is reserved for a failed fetch."""
    assert "insights: Optional[list] = []" in MAIN
    assert "insights = None" in SERVE_BODY   # the swallowed error only
    assert "insights = []" in SERVE_BODY


# --- the capabilities entry -------------------------------------------

def test_insights_capability_is_off_by_default_with_a_reason():
    caps = capability_report()
    assert caps["insights"]["available"] is False
    assert "consolidation" in caps["insights"]["reason"]


def test_insights_capability_flips_on_a_person_clustered_rule():
    caps = capability_report(insights_available=True)
    assert caps["insights"] == {"available": True, "reason": None}


def test_capability_flags_are_independent():
    assert capability_report(True, False)["insights"]["available"] is False
    assert capability_report(False, True)["you_owe"]["available"] is False


def test_manifest_declares_person_insights_follows_the_cluster_key():
    person_rule = {"from_types": ["takeaway"], "produce_type": "insight",
                   "cluster": "person"}
    assert manifest_declares_person_insights(_manifest([person_rule])) is True
    assert manifest_declares_person_insights(_manifest([
        {"from_types": ["takeaway"], "produce_type": "insight"},
    ])) is False
    assert manifest_declares_person_insights(None) is False


# --- is_self on the People rows (13b ratification answer 2) ------------

def test_people_rows_carry_tri_state_is_self():
    """True on the ego row, false on others when an ego link exists,
    null everywhere when it does not: the graph excludes the ego, and
    excluding nobody must be visible, never silent."""
    assert "e.self_at IS NOT NULL AS _is_self_row" in MAIN
    assert '"is_self": p["_is_self_row"] if has_self else None' in MAIN
