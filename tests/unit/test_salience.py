"""Unit tests for salience — judgment-weighted encoding (roadmap #4)."""

import json
from datetime import datetime

from src.contextquilt.services.extraction_schema import sanitize_salience
from src.contextquilt.services.recall_scorer import (
    SALIENCE_HIGH_BOOST,
    SALIENCE_LOW_PENALTY,
    score_patches,
)
from src.contextquilt.services.schema_prompt_builder import build_prompt


# ------------------------------------------------------------------
# sanitize_salience
# ------------------------------------------------------------------

def _content(*salience_values):
    return {
        "patches": [
            {"type": "takeaway", "value": {"text": f"t{i}", "salience": s}}
            for i, s in enumerate(salience_values)
        ]
    }


def test_keeps_only_low_and_high():
    c = _content("high", "low", "normal", None, "URGENT", 3, "  High ")
    sanitize_salience(c)
    got = [p["value"].get("salience") for p in c["patches"]]
    assert got == ["high", "low", None, None, None, None, "high"]
    # dropped levels remove the key entirely (absent == normal)
    assert "salience" not in c["patches"][2]["value"]


def test_tolerates_junk_shapes():
    c = {"patches": ["nope", {"type": "t", "value": "string"}, None]}
    sanitize_salience(c)  # must not raise


# ------------------------------------------------------------------
# scorer weighting
# ------------------------------------------------------------------

def _patch(pid, text, salience=None):
    value = {"text": text}
    if salience:
        value["salience"] = salience
    return {
        "patch_id": pid, "patch_type": "takeaway",
        "value": json.dumps(value), "source_prompt": "x",
        "created_at": datetime.utcnow(), "last_observed_at": None,
    }


def test_high_salience_outranks_equal_normal_patch():
    now = datetime.utcnow()
    a, b = _patch("normal", "fact one"), _patch("hot", "fact two", "high")
    a["created_at"] = b["created_at"] = now
    scored = score_patches([a, b], "unrelated query", [])
    assert scored[0][1]["patch_id"] == "hot"
    assert scored[0][0] - scored[1][0] == SALIENCE_HIGH_BOOST


def test_low_salience_sinks_below_equal_normal_patch():
    now = datetime.utcnow()
    a, b = _patch("normal", "fact one"), _patch("meh", "fact two", "low")
    a["created_at"] = b["created_at"] = now
    scored = score_patches([a, b], "unrelated query", [])
    assert scored[0][1]["patch_id"] == "normal"
    assert scored[1][0] - scored[0][0] == SALIENCE_LOW_PENALTY


def test_salience_never_beats_an_entity_match():
    now = datetime.utcnow()
    ent = _patch("ent", "ProjectX slipped again")
    hot = _patch("hot", "totally unrelated", "high")
    ent["created_at"] = hot["created_at"] = now
    scored = score_patches([ent, hot], "ProjectX status", ["ProjectX"])
    assert scored[0][1]["patch_id"] == "ent"


# ------------------------------------------------------------------
# schema-driven prompt section + manifest hooks
# ------------------------------------------------------------------

_MANIFEST = {
    "app_id": "t", "version": 1, "facet_enum_version": 1,
    "patch_types": [{
        "domain_type": "note", "facet": "Episode", "permanence": "week",
        "display_name": "Note", "description": "d", "value_shape": {"text": "string"},
    }],
    "connection_labels": [{
        "label": "mentions", "role": "informs",
        "from_types": ["note"], "to_types": ["note"], "description": "d",
    }],
}


def test_salience_section_present_by_default():
    assert "=== SALIENCE: how strongly to remember ===" in build_prompt(_MANIFEST)


def test_salience_disabled_and_overridden_via_guidance():
    m = dict(_MANIFEST, extraction_prompt_guidance={"salience_enabled": False})
    assert "SALIENCE" not in build_prompt(m)
    m = dict(_MANIFEST, extraction_prompt_guidance={"salience_guidance": "Flag only career-changing moments."})
    prompt = build_prompt(m)
    assert "Flag only career-changing moments." in prompt
    assert "MOST patches are null" not in prompt
