"""Unit tests for multilingual extraction support.

Background: a Spanish-language meeting (2026-06-10) produced zero
patches from the Spanish-speaking (you) speaker while the one English
line in the same transcript extracted fine — the all-English prompt
under-extracted non-English content. The LANGUAGE prompt section (in
both the universal prompt and the schema-driven builder) addresses
that, and `metadata.language` lets apps pin the output language.
"""

from src.contextquilt.services.extraction_prompts import MEETING_SUMMARY_SYSTEM
from src.contextquilt.services.extraction_schema import (
    EXTRACTION_SCHEMA,
    strip_ephemeral_fields,
)
from src.contextquilt.services.schema_prompt_builder import (
    build_output_schema,
    build_prompt,
)
from src.contextquilt.services.recall_scorer import _keywords


MINIMAL_MANIFEST = {
    "app_id": "testapp",
    "display_name": "Test App",
    "patch_types": [
        {
            "domain_type": "commitment",
            "facet": "Episode",
            "permanence": "month",
            "display_name": "Commitment",
            "description": "A promise.",
            "value_shape": {"text": "string"},
            "completable": True,
        }
    ],
    "connection_labels": [],
    "entity_types": [],
}


# ============================================================
# Prompt sections
# ============================================================


def test_universal_prompt_has_language_section():
    assert "=== LANGUAGE ===" in MEETING_SUMMARY_SYSTEM
    assert "User language:" in MEETING_SUMMARY_SYSTEM
    # The core instruction: don't skip non-English content
    assert "EQUAL diligence" in MEETING_SUMMARY_SYSTEM


def test_schema_builder_prompt_has_language_section():
    prompt = build_prompt(MINIMAL_MANIFEST)
    assert "=== LANGUAGE ===" in prompt
    assert "User language:" in prompt
    assert "EQUAL diligence" in prompt


def test_language_section_keeps_structural_fields_stable():
    # deadline_date must remain ISO regardless of output language
    assert "deadline_date" in MEETING_SUMMARY_SYSTEM
    prompt = build_prompt(MINIMAL_MANIFEST)
    assert "YYYY-MM-DD" in prompt


def test_prompt_override_bypasses_language_section():
    # Apps with a verbatim prompt override keep full control.
    manifest = dict(MINIMAL_MANIFEST, extraction_prompt_override="CUSTOM PROMPT")
    assert build_prompt(manifest) == "CUSTOM PROMPT"


# ============================================================
# output_language commitment field
# ============================================================


def test_universal_schema_requires_output_language_before_patches():
    req = EXTRACTION_SCHEMA["required"]
    assert "output_language" in req
    # Language commitment must come before reasoning and patches so it
    # anchors all downstream prose generation.
    assert req.index("output_language") < req.index("_reasoning")
    assert req.index("output_language") < req.index("patches")
    assert "output_language" in EXTRACTION_SCHEMA["properties"]


def test_builder_schema_requires_output_language_before_patches():
    schema = build_output_schema(MINIMAL_MANIFEST)
    req = schema["required"]
    assert "output_language" in req
    assert req.index("output_language") < req.index("patches")
    # Property order drives generation order under strict mode.
    props = list(schema["properties"].keys())
    assert props.index("output_language") < props.index("patches")


def test_strip_ephemeral_fields_removes_output_language():
    content = {"output_language": "es", "_reasoning": "x", "patches": []}
    strip_ephemeral_fields(content)
    assert "output_language" not in content
    assert "_reasoning" not in content
    assert content["patches"] == []


# ============================================================
# Scorer tokenization (accented words must survive whole)
# ============================================================


def test_keywords_keep_accented_words_whole():
    words = _keywords("Corté el césped del jardín")
    assert "césped" in words
    assert "jardín" in words
    assert "corté" in words


def test_keywords_filter_spanish_function_words():
    words = _keywords("pero para que con como entonces también")
    assert words == []


def test_keywords_keep_english_collision_words():
    # "son", "era", "todo" are Spanish function words but English
    # content words — they must NOT be filtered.
    words = _keywords("my son made a todo list about that era")
    assert "son" in words
    assert "todo" in words
    assert "era" in words


def test_keywords_english_behavior_unchanged():
    words = _keywords("Ship the API gateway by Friday, don't slip")
    assert "ship" in words
    assert "api" in words
    assert "gateway" in words
    assert "friday" in words
    assert "don't" in words
    assert "the" not in words
