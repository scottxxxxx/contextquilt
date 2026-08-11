"""The profile pass (16a / 12a): person-keyed consolidation.

The same machinery as the cue pass with the cluster key changed, and
three deliberate differences these guards pin: the durable-no
idempotency (a user-deleted insight is never re-derived), the self
exclusion (lenses are about the counterparty), and the receipts gate
(a claim spans >= min_meetings DISTINCT meetings, checked in SQL and
re-checked against the fetched sources).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.consolidation import (
    DEFAULT_MIN_MEETINGS,
    PROFILE_LENSES,
    PROFILE_SYSTEM,
    build_profile_content,
    parse_consolidation_rules,
    parse_profile_response,
)

WORKER = (ROOT / "src" / "worker.py").read_text()
MAIN = (ROOT / "src" / "main.py").read_text()
PERSON_BODY = WORKER.split("async def _consolidate_user_people")[1].split(
    "async def _synthesize_person_cluster"
)[0]
SYNTH_BODY = WORKER.split("async def _synthesize_person_cluster")[1].split(
    "async def _synthesize_cluster"
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
            "do": "End with him restating the deliverable."}
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


# --- worker guards -----------------------------------------------------

def test_idempotency_is_a_durable_no():
    """The NOT EXISTS must ignore status: a user-deleted (archived)
    insight blocks re-derivation forever, which is what makes
    hold-to-suppress via the existing DELETE route a durable no."""
    ne = PERSON_BODY.split("NOT EXISTS")[1].split(")")[0] + ")"
    assert "source_person" in ne
    assert "status" not in ne


def test_self_is_excluded_and_degrades_without_the_ego_link():
    assert "self_at IS NOT NULL" in PERSON_BODY
    assert "self_name = None" in PERSON_BODY
    assert "<> lower(btrim($9))" in PERSON_BODY


def test_receipts_gate_in_sql_and_recheck_in_code():
    assert "count(DISTINCT cp.origin_id) >= $6" in PERSON_BODY
    assert "len(distinct_origins) < rule[\"min_meetings\"]" in SYNTH_BODY


def test_provenance_matches_the_cue_pass():
    assert "'derived', 'profile_pass'" in SYNTH_BODY
    assert "source_patch_ids" in SYNTH_BODY
    assert "'informs', 'consolidated_into'" in SYNTH_BODY


# --- serving guards ----------------------------------------------------

def test_detail_serves_active_insights_only_with_evidence():
    body = MAIN.split("insights: list = []")[1].split("detail = _public_person")[0]
    assert "source_person" in body
    assert "COALESCE(cp.status, 'active') = 'active'" in body
    assert '"evidence": evidence' in body


def test_serving_never_fails_the_detail_route():
    body = MAIN.split("insights: list = []")[1].split("detail = _public_person")[0]
    assert "except Exception" in body
