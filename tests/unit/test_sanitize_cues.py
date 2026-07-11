"""Unit tests for cue sanitation (associative retrieval index)."""

from src.contextquilt.services.extraction_schema import (
    CUE_MAX_LEN,
    MAX_CUES_PER_PATCH,
    normalize_cue_list,
    sanitize_cues,
)


def _content(patches, entities=None):
    return {"patches": patches, "entities": entities or []}


def _patch(text, cues, ptype="takeaway"):
    return {"type": ptype, "value": {"text": text, "cues": cues}}


# ------------------------------------------------------------------
# normalize_cue_list
# ------------------------------------------------------------------

def test_normalizes_case_and_whitespace():
    assert normalize_cue_list(["  Pricing   Model ", "VISA paperwork"]) == [
        "pricing model", "visa paperwork",
    ]


def test_drops_non_list_and_non_string_items():
    assert normalize_cue_list("pricing model") == []
    assert normalize_cue_list(None) == []
    assert normalize_cue_list([42, None, {"a": 1}, "budget cuts"]) == ["budget cuts"]


def test_drops_generic_medium_words():
    assert normalize_cue_list(["meeting", "update", "follow up", "next steps"]) == []


def test_length_bounds():
    assert normalize_cue_list(["ab"]) == []
    assert normalize_cue_list(["x" * (CUE_MAX_LEN + 1)]) == []
    assert normalize_cue_list(["abc"]) == ["abc"]


def test_dedupes_and_caps():
    cues = ["alpha one", "Alpha One", "beta two", "gamma three", "delta four",
            "epsilon five", "zeta six"]
    out = normalize_cue_list(cues)
    assert out == ["alpha one", "beta two", "gamma three", "delta four", "epsilon five"]
    assert len(out) == MAX_CUES_PER_PATCH


# ------------------------------------------------------------------
# sanitize_cues
# ------------------------------------------------------------------

def test_drops_cues_duplicating_entity_names():
    content = _content(
        [_patch("Ship the hero section", ["hero section", "Falcon Redesign"])],
        entities=[{"name": "Falcon Redesign", "type": "project", "description": ""}],
    )
    sanitize_cues(content)
    assert content["patches"][0]["value"]["cues"] == ["hero section"]


def test_empty_result_removes_the_key():
    content = _content(
        [_patch("Something", ["Falcon Redesign"])],
        entities=[{"name": "Falcon Redesign", "type": "project", "description": ""}],
    )
    sanitize_cues(content)
    assert "cues" not in content["patches"][0]["value"]


def test_missing_cues_key_is_untouched():
    content = _content([{"type": "trait", "value": {"text": "Prefers bluntness"}}])
    sanitize_cues(content)
    assert "cues" not in content["patches"][0]["value"]


def test_tolerates_junk_shapes():
    content = {
        "patches": [
            "not a dict",
            {"type": "takeaway", "value": "string value"},
            _patch("Real one", ["pricing model"]),
        ],
        "entities": ["not a dict either"],
    }
    sanitize_cues(content)
    assert content["patches"][2]["value"]["cues"] == ["pricing model"]


def test_entity_name_matching_is_case_and_space_insensitive():
    content = _content(
        [_patch("x", ["falcon  redesign", "onboarding flow"])],
        entities=[{"name": "Falcon Redesign", "type": "project", "description": ""}],
    )
    sanitize_cues(content)
    assert content["patches"][0]["value"]["cues"] == ["onboarding flow"]
