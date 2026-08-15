"""The ENTITY TYPES section: the contract the schema used to carry.

Regression cover for the 2026-06-10 entity collapse. The manifest has
always declared `entity_types`; the builder rendered none of it, and the
only thing specifying the entities array was the JSON schema the
OpenAI-compat client sends for constrained decoding. The Anthropic-direct
client accepts that schema and does not send it, so on cutover day entity
yield stepped from 4.37/meeting to 1.24 and zero-entity meetings went
from 44% to 82%. These tests pin the prose, because prose is now the only
carrier on the primary lane.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from contextquilt.services.schema_prompt_builder import build_prompt


def _manifest(**over):
    m = {
        "app_id": "test-app",
        "display_name": "Test App",
        "version": 1,
        "patch_types": [
            {
                "domain_type": "commitment",
                "facet": "Obligation",
                "permanence": "quarter",
                "display_name": "Commitment",
                "description": "Something someone owes.",
                "value_shape": {"text": "string", "owner": "string?"},
            }
        ],
        "entity_types": [
            {
                "entity_type": "person",
                "display_name": "Person",
                "description": "A named individual.",
                "indexed": True,
                "extraction_rules": {"guidance": "Only named individuals."},
            },
            {
                "entity_type": "project",
                "display_name": "Project",
                "description": "A named unit of work.",
                "indexed": True,
            },
        ],
    }
    m.update(over)
    return m


def _section(prompt):
    assert "=== ENTITY TYPES" in prompt
    return prompt.split("=== ENTITY TYPES")[1].split("\n=== ")[0]


class TestTheSectionExists:
    def test_the_entities_array_is_described_at_all(self):
        # The bug, stated as a test: before this, the whole prompt said
        # one line about entities and the model behaved accordingly.
        section = _section(build_prompt(_manifest()))
        assert "recall name index" in section

    def test_the_object_shape_is_stated(self):
        # An unstated field is an unemitted field, the same lesson the
        # cue-starvation finding produced for value shapes.
        section = _section(build_prompt(_manifest()))
        for field in ("name", "type", "description"):
            assert f'"{field}"' in section

    def test_declared_types_and_their_guidance_render(self):
        section = _section(build_prompt(_manifest()))
        assert "person" in section and "project" in section
        assert "Only named individuals." in section
        assert "A named unit of work." in section

    def test_the_output_shape_points_at_the_section(self):
        prompt = build_prompt(_manifest())
        assert "see ENTITY TYPES below" in prompt


class TestTheTwoRestoredRules:
    """Both existed in the universal prompt and were lost on the
    manifest path. Neither is decoration: the first is what makes a
    person who only spoke into a person, the second is what stopped
    Joy and Pallavi from being indexed in the meeting that started
    this investigation, where both were named as patch owners."""

    def test_a_named_speaker_label_is_a_naming(self):
        section = _section(build_prompt(_manifest()))
        assert "speaker label carrying a REAL NAME" in section
        assert "(you)" in section

    def test_an_unnamed_speaker_is_still_excluded(self):
        section = _section(build_prompt(_manifest()))
        assert "Speaker 4" in section
        assert "diarization labels" in section

    def test_an_owner_must_also_be_an_entity(self):
        section = _section(build_prompt(_manifest()))
        assert "owner" in section
        assert "MUST also appear in `entities`" in section


class TestManifestControl:
    def test_it_renders_without_declared_entity_types(self):
        # An app with no declarations still needs the shape and the
        # rules: those, not the enum, are what was missing.
        m = _manifest()
        del m["entity_types"]
        section = _section(build_prompt(m))
        assert "RULES:" in section
        assert '"name"' in section

    def test_guidance_can_replace_the_rules(self):
        m = _manifest(
            extraction_prompt_guidance={"entity_guidance": "Only emit ships."}
        )
        section = _section(build_prompt(m))
        assert "Only emit ships." in section
        assert "diarization labels" not in section

    def test_an_override_prompt_still_wins_entirely(self):
        m = _manifest(extraction_prompt_override="Do exactly this and nothing else.")
        assert build_prompt(m) == "Do exactly this and nothing else."


class TestNoDashes:
    def test_the_section_carries_no_dash_punctuation(self):
        # House rule, and load-bearing: models copy the punctuation they
        # are shown, and this prose ships into served output.
        section = _section(build_prompt(_manifest()))
        assert "—" not in section
        assert "–" not in section
