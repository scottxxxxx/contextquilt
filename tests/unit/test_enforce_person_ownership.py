"""Unit tests for enforce_person_ownership.

Covers the safety net that compensates for unreliable LLM compliance with
the prompt rule that every named action-item owner must have a person
patch + owns connection.
"""

import copy

import pytest

from src.contextquilt.services.extraction_schema import (
    enforce_person_ownership,
    _is_real_person_owner,
    _split_compound_owner,
)


def _commitment(text: str, owner: str | None) -> dict:
    return {
        "type": "commitment",
        "value": {"text": text, "owner": owner},
        "connects_to": [],
    }


def _person(text: str, **kwargs) -> dict:
    p = {"type": "person", "value": {"text": text}, "connects_to": []}
    p.update(kwargs)
    return p


# ============================================================
# _is_real_person_owner — owner-text classifier
# ============================================================

class TestRealPersonOwnerClassifier:
    def test_real_name_passes(self):
        assert _is_real_person_owner("Brian", user_label=None)
        assert _is_real_person_owner("Reshmi", user_label="Scott")

    def test_empty_or_none_rejected(self):
        assert not _is_real_person_owner(None, user_label=None)
        assert not _is_real_person_owner("", user_label=None)
        assert not _is_real_person_owner("   ", user_label=None)

    def test_speaker_placeholders_rejected(self):
        assert not _is_real_person_owner("Speaker 4", user_label=None)
        assert not _is_real_person_owner("Speaker_15", user_label=None)
        assert not _is_real_person_owner("speaker 7", user_label=None)
        assert not _is_real_person_owner("Unknown", user_label=None)
        assert not _is_real_person_owner("unidentified", user_label=None)

    def test_you_tokens_rejected(self):
        # The (you) speaker's ownership is implicit via patch ownership.
        assert not _is_real_person_owner("(you)", user_label=None)
        assert not _is_real_person_owner("you", user_label=None)
        assert not _is_real_person_owner("Self", user_label=None)
        assert not _is_real_person_owner("me", user_label=None)
        assert not _is_real_person_owner("I", user_label=None)

    def test_user_label_match_rejected(self):
        # If the owner_text matches the (you) speaker's name, skip — same reason.
        assert not _is_real_person_owner("Scott", user_label="Scott")
        assert not _is_real_person_owner("scott", user_label="Scott")
        assert not _is_real_person_owner("  Scott  ", user_label="Scott")

    def test_user_label_does_not_block_other_names(self):
        assert _is_real_person_owner("Brian", user_label="Scott")


# ============================================================
# enforce_person_ownership — safety-net behavior
# ============================================================

class TestEnforcePersonOwnership:
    def test_injects_missing_person_and_edge(self):
        """Bare commitment with owner text but no person patch — the
        canonical Haiku-4.5 failure mode."""
        content = {
            "patches": [
                _commitment("Circle back with VJ/DJ on dynamic forms", "Brian"),
            ]
        }
        enforce_person_ownership(content)

        # Person patch was synthesized.
        persons = [p for p in content["patches"] if p["type"] == "person"]
        assert len(persons) == 1
        assert persons[0]["value"]["text"] == "Brian"

        # owns connection points from person to the action item.
        edges = persons[0]["connects_to"]
        assert len(edges) == 1
        assert edges[0]["label"] == "owns"
        assert edges[0]["role"] == "informs"
        assert edges[0]["target_type"] == "commitment"
        assert edges[0]["target_text"] == (
            "Circle back with VJ/DJ on dynamic forms"
        )

        # Audit record present.
        audit = content["_person_ownership_enforced"]
        assert audit["persons_injected"] == ["Brian"]
        assert len(audit["connections_injected"]) == 1

    def test_existing_person_patch_reused(self):
        """If the LLM already emitted a person patch for the owner, don't
        double-add — just append the missing edge."""
        content = {
            "patches": [
                _person("Brian"),
                _commitment("Update spec doc", "Brian"),
            ]
        }
        enforce_person_ownership(content)

        persons = [p for p in content["patches"] if p["type"] == "person"]
        assert len(persons) == 1  # not duplicated

        # Edge appended to the existing person patch.
        edges = persons[0]["connects_to"]
        assert len(edges) == 1
        assert edges[0]["label"] == "owns"
        assert edges[0]["target_text"] == "Update spec doc"

        # No persons injected, just connections.
        audit = content["_person_ownership_enforced"]
        assert audit["persons_injected"] == []
        assert len(audit["connections_injected"]) == 1

    def test_existing_owns_edge_not_duplicated(self):
        """If the LLM correctly emitted both the person patch and the owns
        edge, the enforcer should be a no-op."""
        existing_edge = {
            "role": "informs",
            "label": "owns",
            "target_type": "commitment",
            "target_text": "Update spec doc",
        }
        person = _person("Brian")
        person["connects_to"] = [existing_edge]
        content = {
            "patches": [
                person,
                _commitment("Update spec doc", "Brian"),
            ]
        }
        enforce_person_ownership(content)

        persons = [p for p in content["patches"] if p["type"] == "person"]
        assert len(persons) == 1
        assert len(persons[0]["connects_to"]) == 1  # not duplicated

        # Nothing was injected — no audit record (or empty one).
        audit = content.get("_person_ownership_enforced")
        assert audit is None or (
            not audit.get("persons_injected") and not audit.get("connections_injected")
        )

    def test_idempotent(self):
        """Running twice should produce the same shape as running once."""
        content = {
            "patches": [_commitment("Update spec doc", "Brian")],
        }
        enforce_person_ownership(content)
        snapshot = copy.deepcopy(content["patches"])

        enforce_person_ownership(content)
        assert content["patches"] == snapshot

    def test_skips_you_speaker_owner(self):
        """Owner = the (you) speaker → no synthetic person patch."""
        content = {
            "patches": [
                _commitment("Apply SDK patch by Friday", "Scott"),
            ]
        }
        enforce_person_ownership(content, user_label="Scott")

        persons = [p for p in content["patches"] if p["type"] == "person"]
        assert persons == []  # no synthetic Scott patch

    def test_skips_speaker_placeholder_owner(self):
        """Owner = "Speaker 4" → diarization placeholder, no person patch."""
        content = {
            "patches": [_commitment("Do the thing", "Speaker 4")]
        }
        enforce_person_ownership(content)
        assert all(p["type"] != "person" for p in content["patches"])

    def test_skips_empty_owner(self):
        content = {"patches": [_commitment("Do the thing", None)]}
        enforce_person_ownership(content)
        assert all(p["type"] != "person" for p in content["patches"])

    def test_handles_blocker_decision_goal(self):
        """All four PERSON_OWNED_ACTION_TYPES get the same treatment."""
        content = {
            "patches": [
                {"type": "blocker", "value": {"text": "API down", "owner": "Reshmi"}, "connects_to": []},
                {"type": "decision", "value": {"text": "Use Postgres", "owner": "Vijay"}, "connects_to": []},
                {"type": "goal", "value": {"text": "Ship by Q3", "owner": "Brian"}, "connects_to": []},
            ]
        }
        enforce_person_ownership(content)

        person_names = {
            p["value"]["text"]
            for p in content["patches"]
            if p["type"] == "person"
        }
        assert person_names == {"Reshmi", "Vijay", "Brian"}

    def test_skips_non_action_types(self):
        """trait/preference/event etc. with an owner text are not action
        items — enforce_person_ownership ignores them. (Trait/preference
        are gated separately by enforce_owner_gate; event is project-scope
        but not human-owned.)"""
        content = {
            "patches": [
                {"type": "trait", "value": {"text": "Direct", "owner": "Brian"}, "connects_to": []},
                {"type": "event", "value": {"text": "Demo went well", "owner": "Brian"}, "connects_to": []},
            ]
        }
        enforce_person_ownership(content)
        assert all(p["type"] != "person" for p in content["patches"])

    def test_multiple_action_items_same_owner(self):
        """One person patch, multiple owns edges to different action items."""
        content = {
            "patches": [
                _commitment("Update spec doc", "Brian"),
                _commitment("Circle back with VJ", "Brian"),
                {"type": "blocker", "value": {"text": "Form mapping unclear", "owner": "Brian"}, "connects_to": []},
            ]
        }
        enforce_person_ownership(content)

        persons = [p for p in content["patches"] if p["type"] == "person"]
        assert len(persons) == 1  # single Brian patch
        assert persons[0]["value"]["text"] == "Brian"

        # Three owns edges, one per action item.
        edges = persons[0]["connects_to"]
        assert len(edges) == 3
        target_texts = {e["target_text"] for e in edges}
        assert target_texts == {
            "Update spec doc",
            "Circle back with VJ",
            "Form mapping unclear",
        }

    def test_case_insensitive_person_match(self):
        """LLM emits 'Brian' as a person patch, but action item owner is
        'brian' (different case) — still treated as same person."""
        content = {
            "patches": [
                _person("Brian"),
                _commitment("Do the thing", "brian"),
            ]
        }
        enforce_person_ownership(content)

        persons = [p for p in content["patches"] if p["type"] == "person"]
        assert len(persons) == 1  # not duplicated

    def test_empty_patches_noop(self):
        content = {"patches": []}
        enforce_person_ownership(content)
        assert content["patches"] == []
        assert "_person_ownership_enforced" not in content

    def test_no_audit_record_when_nothing_injected(self):
        """All action items already have their person + edge → no audit."""
        person = _person("Brian")
        person["connects_to"] = [
            {
                "role": "informs",
                "label": "owns",
                "target_type": "commitment",
                "target_text": "Already wired",
            }
        ]
        content = {
            "patches": [
                person,
                _commitment("Already wired", "Brian"),
            ]
        }
        enforce_person_ownership(content)
        # _person_ownership_enforced may be absent OR present-but-empty
        audit = content.get("_person_ownership_enforced")
        if audit is not None:
            assert not audit.get("persons_injected")
            assert not audit.get("connections_injected")


# ============================================================
# _split_compound_owner — slash-separated owner splitter
# ============================================================

class TestCompoundOwnerSplitter:
    def test_single_name_returns_one_element(self):
        assert _split_compound_owner("Brian") == ["Brian"]

    def test_slash_splits_into_two(self):
        assert _split_compound_owner("Srikanth/Ella") == ["Srikanth", "Ella"]

    def test_slash_three_way(self):
        assert _split_compound_owner("Sai/Santosh/Priya") == ["Sai", "Santosh", "Priya"]

    def test_whitespace_around_parts_trimmed(self):
        assert _split_compound_owner(" Srikanth / Ella ") == ["Srikanth", "Ella"]

    def test_empty_parts_dropped(self):
        # Trailing slash, double slash — drop the empty parts.
        assert _split_compound_owner("Brian/") == ["Brian"]
        assert _split_compound_owner("Brian//Reshmi") == ["Brian", "Reshmi"]

    def test_empty_input_returns_empty(self):
        assert _split_compound_owner(None) == []
        assert _split_compound_owner("") == []
        assert _split_compound_owner("   ") == []

    def test_no_split_on_other_separators(self):
        # Conservative: don't split on ', ', ' & ', ' and '. These can
        # legitimately appear inside single names ("Smith, John", "AT&T",
        # "Arvind and family").
        assert _split_compound_owner("Smith, John") == ["Smith, John"]
        assert _split_compound_owner("AT&T") == ["AT&T"]
        assert _split_compound_owner("Arvind and family") == ["Arvind and family"]


# ============================================================
# Compound-owner end-to-end via enforce_person_ownership
# ============================================================

class TestCompoundOwnerInEnforcer:
    def test_two_person_patches_and_two_owns_edges_for_compound_owner(self):
        content = {
            "patches": [
                _commitment("Ship the new SDK", "Srikanth/Ella"),
            ]
        }
        enforce_person_ownership(content)

        person_texts = sorted(
            (p.get("value") or {}).get("text", "")
            for p in content["patches"]
            if p.get("type") == "person"
        )
        assert person_texts == ["Ella", "Srikanth"]

        # Each person patch owns the same commitment.
        for p in content["patches"]:
            if p.get("type") != "person":
                continue
            owns = [
                c for c in p.get("connects_to", [])
                if c.get("label") == "owns" and c.get("target_text") == "Ship the new SDK"
            ]
            assert len(owns) == 1, f"{p['value']['text']} should have exactly one owns edge"

        audit = content.get("_person_ownership_enforced")
        assert audit is not None
        assert sorted(audit["persons_injected"]) == ["Ella", "Srikanth"]
        assert len(audit["connections_injected"]) == 2

    def test_compound_owner_dedups_against_existing_person(self):
        """If 'Srikanth' already has a person patch, only 'Ella' is created."""
        content = {
            "patches": [
                _person("Srikanth"),
                _commitment("Ship the new SDK", "Srikanth/Ella"),
            ]
        }
        enforce_person_ownership(content)

        person_texts = sorted(
            (p.get("value") or {}).get("text", "")
            for p in content["patches"]
            if p.get("type") == "person"
        )
        assert person_texts == ["Ella", "Srikanth"]

        audit = content["_person_ownership_enforced"]
        assert audit["persons_injected"] == ["Ella"]  # Srikanth not re-injected
        assert len(audit["connections_injected"]) == 2  # Both owns edges land

    def test_compound_owner_drops_invalid_part_only(self):
        """'Brian/Speaker 4' → Brian gets a person, 'Speaker 4' does not."""
        content = {
            "patches": [
                _commitment("Ship something", "Brian/Speaker 4"),
            ]
        }
        enforce_person_ownership(content)

        person_texts = [
            (p.get("value") or {}).get("text", "")
            for p in content["patches"]
            if p.get("type") == "person"
        ]
        assert person_texts == ["Brian"]

    def test_compound_owner_skips_user_label_part(self):
        """Compound owner where one part is the (you) speaker — only the other lands."""
        content = {
            "patches": [
                _commitment("Joint commitment", "Scott/Brian"),
            ]
        }
        enforce_person_ownership(content, user_label="Scott")

        person_texts = [
            (p.get("value") or {}).get("text", "")
            for p in content["patches"]
            if p.get("type") == "person"
        ]
        # Scott is the (you) speaker — implicit ownership, no synthetic person.
        assert person_texts == ["Brian"]

    def test_compound_owner_idempotent(self):
        """Running twice on the same content adds nothing new."""
        content = {
            "patches": [
                _commitment("Ship the SDK", "Srikanth/Ella"),
            ]
        }
        enforce_person_ownership(content)
        snapshot = copy.deepcopy(content["patches"])
        enforce_person_ownership(content)
        assert content["patches"] == snapshot

    def test_compound_owner_shared_across_action_items(self):
        """Two action items with same compound owner share person patches."""
        content = {
            "patches": [
                _commitment("First commitment", "Srikanth/Ella"),
                _commitment("Second commitment", "Srikanth/Ella"),
            ]
        }
        enforce_person_ownership(content)

        # Exactly one person patch per name, regardless of how many
        # commitments cited the compound owner.
        person_texts = sorted(
            (p.get("value") or {}).get("text", "")
            for p in content["patches"]
            if p.get("type") == "person"
        )
        assert person_texts == ["Ella", "Srikanth"]

        # Each person should have 2 owns edges (one per commitment).
        for p in content["patches"]:
            if p.get("type") != "person":
                continue
            owns = [c for c in p.get("connects_to", []) if c.get("label") == "owns"]
            assert len(owns) == 2, f"{p['value']['text']} should own 2 commitments"
