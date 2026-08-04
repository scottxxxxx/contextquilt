"""Unit tests for enforce_owner_edge_agreement.

The mirror of enforce_person_ownership: that one guarantees an owns edge
exists for a named owner, this one guarantees no owns edge exists for
anybody else.

The live case these are modeled on (ABM, meeting of 2026-07-28): the
commitment "Configure IP whitelisting for new non-prod environment;
coordinate with Denby on turnaround time once IP address is provided"
was stored with owner Denby and owns edges from BOTH Denby and
Ellery. Ellery's real role was supplying the IP address the work waits
on, but `owns` is the only person-to-item label the manifest defines, so
the extractor recorded involvement as ownership.
"""

import copy

from src.contextquilt.services.extraction_schema import (
    enforce_owner_edge_agreement,
    enforce_person_ownership,
)


def _commitment(text: str, owner: str | None, ptype: str = "commitment") -> dict:
    return {"type": ptype, "value": {"text": text, "owner": owner}}


def _person(text: str, owns: list | None = None, extra: list | None = None) -> dict:
    edges = [
        {
            "role": "informs",
            "label": "owns",
            "target_type": t,
            "target_text": x,
        }
        for t, x in (owns or [])
    ]
    edges.extend(extra or [])
    return {"type": "person", "value": {"text": text}, "connects_to": edges}


def _owns_targets(patch: dict) -> list:
    return [
        c["target_text"]
        for c in patch.get("connects_to", [])
        if c.get("label") == "owns"
    ]


class TestTheLiveCase:
    """The ABM IP-whitelisting commitment, reduced."""

    def _content(self) -> dict:
        target = "Configure IP whitelisting for new non-prod environment"
        return {
            "patches": [
                _commitment(target, "Denby"),
                _person("Denby", owns=[("commitment", target)]),
                _person("Ellery", owns=[("commitment", target)]),
            ]
        }

    def test_drops_the_non_owner_edge(self):
        content = enforce_owner_edge_agreement(self._content())
        denby, ellery = content["patches"][1], content["patches"][2]
        assert len(_owns_targets(denby)) == 1
        assert _owns_targets(ellery) == []

    def test_reports_what_it_dropped(self):
        content = enforce_owner_edge_agreement(self._content())
        dropped = content["_owner_edge_agreement_enforced"]["dropped"]
        assert len(dropped) == 1
        assert dropped[0]["person"] == "Ellery"

    def test_the_person_patch_itself_survives(self):
        """Ellery was really in the meeting. We drop the claim, not him."""
        content = enforce_owner_edge_agreement(self._content())
        names = [
            (p.get("value") or {}).get("text")
            for p in content["patches"]
            if p.get("type") == "person"
        ]
        assert "Ellery" in names


class TestConservatism:
    """Cases where dropping an edge would lose information."""

    def test_no_stated_owner_leaves_every_edge_alone(self):
        content = {
            "patches": [
                _commitment("Ship the thing", None),
                _person("Ada", owns=[("commitment", "Ship the thing")]),
                _person("Grace", owns=[("commitment", "Ship the thing")]),
            ]
        }
        before = copy.deepcopy(content)
        after = enforce_owner_edge_agreement(content)
        assert after["patches"] == before["patches"]
        assert "_owner_edge_agreement_enforced" not in after

    def test_empty_string_owner_leaves_edges_alone(self):
        content = {
            "patches": [
                _commitment("Ship the thing", "   "),
                _person("Ada", owns=[("commitment", "Ship the thing")]),
            ]
        }
        after = enforce_owner_edge_agreement(content)
        assert _owns_targets(after["patches"][1]) == ["Ship the thing"]

    def test_compound_owner_keeps_every_edge(self):
        """Same split enforce_person_ownership used to create them."""
        content = {
            "patches": [
                _commitment("Draft the deck", "Marlowe/Quill"),
                _person("Marlowe", owns=[("commitment", "Draft the deck")]),
                _person("Quill", owns=[("commitment", "Draft the deck")]),
            ]
        }
        after = enforce_owner_edge_agreement(content)
        assert _owns_targets(after["patches"][1]) == ["Draft the deck"]
        assert _owns_targets(after["patches"][2]) == ["Draft the deck"]

    def test_placeholder_owner_leaves_edges_alone(self):
        """'Speaker 2' is not ground truth to filter anyone against."""
        content = {
            "patches": [
                _commitment("Ship the thing", "Speaker 2"),
                _person("Ada", owns=[("commitment", "Ship the thing")]),
            ]
        }
        after = enforce_owner_edge_agreement(content)
        assert _owns_targets(after["patches"][1]) == ["Ship the thing"]

    def test_owner_is_the_you_speaker_leaves_edges_alone(self):
        content = {
            "patches": [
                _commitment("Ship the thing", "Scott"),
                _person("Ada", owns=[("commitment", "Ship the thing")]),
            ]
        }
        after = enforce_owner_edge_agreement(content, user_label="Scott")
        assert _owns_targets(after["patches"][1]) == ["Ship the thing"]


class TestScope:
    def test_other_labels_are_untouched(self):
        content = {
            "patches": [
                _commitment("Ship the thing", "Ada"),
                _person(
                    "Grace",
                    owns=[("commitment", "Ship the thing")],
                    extra=[
                        {
                            "role": "informs",
                            "label": "works_on",
                            "target_type": "project",
                            "target_text": "Apollo",
                        }
                    ],
                ),
            ]
        }
        after = enforce_owner_edge_agreement(content)
        grace = after["patches"][1]
        assert _owns_targets(grace) == []
        assert [c["label"] for c in grace["connects_to"]] == ["works_on"]

    def test_edges_into_other_targets_are_untouched(self):
        content = {
            "patches": [
                _commitment("Ship the thing", "Ada"),
                _person(
                    "Grace",
                    owns=[
                        ("commitment", "Ship the thing"),
                        ("commitment", "Write the docs"),
                    ],
                ),
            ]
        }
        after = enforce_owner_edge_agreement(content)
        assert _owns_targets(after["patches"][1]) == ["Write the docs"]

    def test_matching_is_case_and_space_insensitive(self):
        content = {
            "patches": [
                _commitment("Ship the thing", "  ada  "),
                _person("ADA", owns=[("commitment", "  Ship the thing ")]),
            ]
        }
        after = enforce_owner_edge_agreement(content)
        assert len(_owns_targets(after["patches"][1])) == 1

    def test_applies_to_every_person_owned_action_type(self):
        for ptype in ("commitment", "blocker", "decision", "goal"):
            content = {
                "patches": [
                    _commitment("Do the work", "Ada", ptype=ptype),
                    _person("Grace", owns=[(ptype, "Do the work")]),
                ]
            }
            after = enforce_owner_edge_agreement(content)
            assert _owns_targets(after["patches"][1]) == [], ptype


class TestMalformedInput:
    def test_missing_patches_key(self):
        assert enforce_owner_edge_agreement({}) == {}

    def test_patches_not_a_list(self):
        content = {"patches": "nope"}
        assert enforce_owner_edge_agreement(content) == content

    def test_person_without_connects_to(self):
        content = {
            "patches": [
                _commitment("Ship the thing", "Ada"),
                {"type": "person", "value": {"text": "Grace"}},
            ]
        }
        enforce_owner_edge_agreement(content)  # must not raise

    def test_non_dict_entries_are_skipped(self):
        content = {"patches": [None, "junk", _commitment("Ship it", "Ada")]}
        enforce_owner_edge_agreement(content)  # must not raise


class TestComposesWithPersonOwnership:
    """The two sanitizers run back to back in the worker chain."""

    def test_injected_owner_edge_survives_its_mirror(self):
        """enforce_person_ownership adds the edge; this must not undo it."""
        content = {"patches": [_commitment("Ship the thing", "Ada")]}
        enforce_person_ownership(content, user_label=None)
        enforce_owner_edge_agreement(content, user_label=None)
        ada = next(
            p for p in content["patches"]
            if p.get("type") == "person"
            and (p.get("value") or {}).get("text") == "Ada"
        )
        assert _owns_targets(ada) == ["Ship the thing"]

    def test_bystander_dropped_while_owner_injected(self):
        content = {
            "patches": [
                _commitment("Ship the thing", "Ada"),
                _person("Grace", owns=[("commitment", "Ship the thing")]),
            ]
        }
        enforce_person_ownership(content, user_label=None)
        enforce_owner_edge_agreement(content, user_label=None)
        by_name = {
            (p.get("value") or {}).get("text"): p
            for p in content["patches"]
            if p.get("type") == "person"
        }
        assert _owns_targets(by_name["Ada"]) == ["Ship the thing"]
        assert _owns_targets(by_name["Grace"]) == []
