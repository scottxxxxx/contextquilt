"""
No em or en dashes may reach the model.

ShoulderSurf has a hard no-dash rule for anything a user sees, and CQ
patch text renders straight into the app. Models reproduce the
punctuation they are shown, so a dash in the prompt becomes a dash in a
patch becomes a dash on screen, which SS then has to filter downstream.

Reported by the SS team 2026-08-01: 72 dash characters in
extraction_prompts.py and 19 in schema_prompt_builder.py, including the
instruction that governs how the model writes output prose, which
contained two em dashes inside itself.

This test is the guard. It fails on the next one that gets added,
whether hand-written into a CQ prompt string or arriving in an app
manifest.
"""

import json
import pathlib

import pytest

from contextquilt.services import extraction_prompts
from contextquilt.services.schema_prompt_builder import (
    build_prompt,
    normalize_dashes,
)

DASHES = ("—", "–")  # em, en
REPO = pathlib.Path(__file__).resolve().parents[2]
PROMPT_SOURCES = [
    REPO / "src/contextquilt/services/extraction_prompts.py",
    REPO / "src/contextquilt/services/schema_prompt_builder.py",
]


def _offenders(text: str):
    """Every dash with a little context, so a failure is actionable."""
    out = []
    for i, ch in enumerate(text):
        if ch in DASHES:
            out.append(repr(text[max(0, i - 60):i + 60]))
    return out


# ------------------------------------------------- the prompt sources

@pytest.mark.parametrize("path", PROMPT_SOURCES, ids=lambda p: p.name)
def test_prompt_source_files_contain_no_dashes(path):
    # Whole file, comments and docstrings included: a dash in a comment
    # directly above a prompt string is exactly how one gets copied in.
    text = path.read_text(encoding="utf-8")
    assert not _offenders(text), (
        f"{path.name} contains em/en dashes:\n" + "\n".join(_offenders(text))
    )


# ------------------------------------------------ the rendered prompts

def test_static_extraction_prompt_has_no_dashes():
    for name in dir(extraction_prompts):
        if name.startswith("_"):
            continue
        value = getattr(extraction_prompts, name)
        if isinstance(value, str):
            assert not _offenders(value), f"{name} contains a dash"


def test_built_prompt_from_the_live_ss_manifest_has_no_dashes():
    manifest = json.loads(
        (REPO / "init-db/11_shouldersurf_schema.json").read_text(encoding="utf-8")
    )
    prompt = build_prompt(manifest)
    assert not _offenders(prompt), (
        "Built SS prompt contains dashes:\n" + "\n".join(_offenders(prompt))
    )


def test_manifest_supplied_dashes_are_normalized_out():
    # The case that actually shipped: an app registers a description with
    # a dash in it. CQ renders descriptions verbatim, so without the
    # backstop this reaches the model.
    manifest = {
        "app_id": "dashy",
        "patch_types": [{
            "domain_type": "trait",
            "facet": "Attribute",
            "description": "How the user operates — focus, rhythm, style.",
            "value_shape": {"text": "string"},
        }],
        "connection_labels": [{
            "label": "belongs_to",
            "role": "parent",
            "from_types": ["trait"],
            "to_types": ["trait"],
            "description": "Child belongs to a parent – archival cascades.",
        }],
    }
    prompt = build_prompt(manifest)
    assert not _offenders(prompt)
    # The content survives, only the punctuation changes.
    assert "focus, rhythm, style" in prompt
    assert "archival cascades" in prompt


def test_prompt_override_is_normalized_too():
    # An app supplying a whole prompt verbatim is the same exposure.
    prompt = build_prompt(
        {"extraction_prompt_override": "Do the thing — carefully."}
    )
    assert not _offenders(prompt)
    assert "Do the thing: carefully." == prompt


# ------------------------------------------------------- the normalizer

def test_spaced_dash_becomes_a_colon():
    # The dominant shape in a description is "phrase - expansion", where a
    # colon is what the author meant.
    assert normalize_dashes("the user's world — company, team") == (
        "the user's world: company, team"
    )


def test_unspaced_dash_becomes_a_comma():
    assert normalize_dashes("a—b") == "a, b"


def test_normalizer_leaves_hyphens_and_arrows_alone():
    # Hyphens inside genuinely hyphenated words are fine, and the prompt
    # uses -> arrows for connection direction and verb conjugation.
    text = "decision-making style, FROM → TO, is→are"
    assert normalize_dashes(text) == text


def test_normalizer_handles_empty_and_none():
    assert normalize_dashes("") == ""
    assert normalize_dashes(None) is None
