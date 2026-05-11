"""Unit tests for drop_placeholder_and_self_person_patches.

Drops diarization placeholders (``"Speaker 5"``) and self-reference
person patches (``"Scott"`` in Scott's quilt) from extraction output.
enforce_person_ownership already guards against creating these via
its safety net (``_is_real_person_owner``); this sanitizer catches
the case where the LLM emitted them directly.
"""

from src.contextquilt.services.extraction_schema import (
    drop_placeholder_and_self_person_patches,
)


def _person(text: str, connects_to: list | None = None) -> dict:
    return {"type": "person", "value": {"text": text}, "connects_to": connects_to or []}


def _action(text: str, owner: str | None = None, connects_to: list | None = None) -> dict:
    return {
        "type": "commitment",
        "value": {"text": text, "owner": owner},
        "connects_to": connects_to or [],
    }


def _names(patches: list[dict]) -> list[str]:
    return [
        p["value"].get("text") for p in patches if isinstance(p.get("value"), dict)
    ]


class TestPlaceholder:
    def test_drops_speaker_n(self):
        content = {"patches": [_person("Speaker 5"), _person("Thorne")]}
        drop_placeholder_and_self_person_patches(content)
        assert _names(content["patches"]) == ["Thorne"]

    def test_drops_speaker_with_paren_real_name(self):
        # "Speaker 2 (Redfern)" is still a placeholder shape — the LLM
        # left the diarization label in. We don't try to rescue the
        # paren name; that's a more complex extraction concern.
        content = {"patches": [_person("Speaker 2 (Redfern)"), _person("Thorne")]}
        drop_placeholder_and_self_person_patches(content)
        assert _names(content["patches"]) == ["Thorne"]

    def test_drops_unknown(self):
        content = {"patches": [_person("Unknown"), _person("Thorne")]}
        drop_placeholder_and_self_person_patches(content)
        assert _names(content["patches"]) == ["Thorne"]

    def test_drops_unidentified(self):
        content = {"patches": [_person("Unidentified caller"), _person("Thorne")]}
        drop_placeholder_and_self_person_patches(content)
        assert _names(content["patches"]) == ["Thorne"]

    def test_case_insensitive_match(self):
        content = {"patches": [_person("SPEAKER 5"), _person("speaker 1")]}
        drop_placeholder_and_self_person_patches(content)
        assert _names(content["patches"]) == []


class TestSelfReference:
    def test_drops_user_label_exact(self):
        content = {"patches": [_person("Scott"), _person("Thorne")]}
        drop_placeholder_and_self_person_patches(content, user_label="Scott")
        assert _names(content["patches"]) == ["Thorne"]

    def test_case_insensitive_match(self):
        content = {"patches": [_person("scott"), _person("SCOTT"), _person("Thorne")]}
        drop_placeholder_and_self_person_patches(content, user_label="Scott")
        assert _names(content["patches"]) == ["Thorne"]

    def test_trims_whitespace_on_both_sides(self):
        content = {"patches": [_person("  Scott  ")]}
        drop_placeholder_and_self_person_patches(content, user_label="  scott  ")
        assert _names(content["patches"]) == []

    def test_no_user_label_no_self_drop(self):
        content = {"patches": [_person("Scott"), _person("Thorne")]}
        drop_placeholder_and_self_person_patches(content, user_label=None)
        # Without user_label, "Scott" passes through. Placeholders still drop.
        assert _names(content["patches"]) == ["Scott", "Thorne"]

    def test_empty_user_label_no_self_drop(self):
        content = {"patches": [_person("Scott"), _person("Thorne")]}
        drop_placeholder_and_self_person_patches(content, user_label="   ")
        assert _names(content["patches"]) == ["Scott", "Thorne"]

    def test_partial_match_does_not_drop(self):
        # "Scott Guida" is NOT the (you) speaker named "Scott" — could
        # be a different person who shares a first name.
        content = {"patches": [_person("Scott Guida")]}
        drop_placeholder_and_self_person_patches(content, user_label="Scott")
        assert _names(content["patches"]) == ["Scott Guida"]


class TestConnectsToCleanup:
    def test_strips_connects_to_referencing_dropped_placeholder(self):
        action = _action(
            "Draft requirements",
            owner="Speaker 5",
            connects_to=[
                {"target_text": "Speaker 5", "target_type": "person", "role": "informs", "label": "owns"},
                {"target_text": "AI project", "target_type": "project", "role": "parent", "label": "belongs_to"},
            ],
        )
        content = {"patches": [_person("Speaker 5"), action]}
        drop_placeholder_and_self_person_patches(content)
        assert _names(content["patches"]) == ["Draft requirements"]
        # The connect_to referencing "Speaker 5" got stripped.
        remaining = content["patches"][0]["connects_to"]
        assert [c["target_text"] for c in remaining] == ["AI project"]

    def test_strips_connects_to_referencing_self_reference(self):
        action = _action(
            "Ship feature",
            owner="Scott",
            connects_to=[
                {"target_text": "Scott", "target_type": "person", "role": "informs", "label": "owns"},
            ],
        )
        content = {"patches": [_person("Scott"), action]}
        drop_placeholder_and_self_person_patches(content, user_label="Scott")
        assert _names(content["patches"]) == ["Ship feature"]
        assert content["patches"][0]["connects_to"] == []

    def test_keeps_connects_to_referencing_other_names(self):
        action = _action(
            "Coordinate",
            owner="Thorne",
            connects_to=[
                {"target_text": "Thorne", "target_type": "person", "role": "informs", "label": "owns"},
            ],
        )
        content = {"patches": [_person("Speaker 5"), _person("Thorne"), action]}
        drop_placeholder_and_self_person_patches(content)
        # Speaker 5 dropped; Thorne and the action kept; the connect_to to Thorne preserved.
        assert _names(content["patches"]) == ["Thorne", "Coordinate"]
        assert content["patches"][1]["connects_to"][0]["target_text"] == "Thorne"


class TestDefensiveEdges:
    def test_empty_content_safe(self):
        drop_placeholder_and_self_person_patches({})
        drop_placeholder_and_self_person_patches({"patches": []})

    def test_non_person_types_untouched(self):
        content = {"patches": [_action("Speaker 5 will draft the doc", owner="Speaker 5")]}
        drop_placeholder_and_self_person_patches(content)
        # Action keeps its owner field unchanged; only person-type drops.
        assert _names(content["patches"]) == ["Speaker 5 will draft the doc"]
        assert content["patches"][0]["value"]["owner"] == "Speaker 5"

    def test_missing_value_safe(self):
        content = {"patches": [{"type": "person", "value": None}]}
        drop_placeholder_and_self_person_patches(content)
        # Missing-value patch stays in place; nothing to compare.
        assert len(content["patches"]) == 1

    def test_non_string_text_safe(self):
        content = {"patches": [{"type": "person", "value": {"text": 42}}]}
        drop_placeholder_and_self_person_patches(content)
        assert len(content["patches"]) == 1

    def test_whitespace_only_text_safe(self):
        content = {"patches": [{"type": "person", "value": {"text": "   "}}]}
        drop_placeholder_and_self_person_patches(content)
        assert len(content["patches"]) == 1

    def test_no_match_no_mutation(self):
        # When nothing matches, content["patches"] should be the exact
        # same list object (not a needless copy).
        original = [_person("Thorne"), _person("Ashby")]
        content = {"patches": original}
        drop_placeholder_and_self_person_patches(content, user_label="Scott")
        assert content["patches"] is original
