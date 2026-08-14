"""Unit tests for inject_ownership_entities and cap_entities.

The hole: `enforce_person_ownership` guarantees a person PATCH for every
named owner, `person_appearances` is written from the ENTITIES array, and
nothing joined the two. A meeting could produce three commitments owned
by two named people, zero entities, and therefore zero presence for
either of them (2026-08-13, origin 866E8E1B).

The rules under test are the ones the enforcer already follows, plus one
that is new and load bearing: the capacity is `ownership`, never
`speaker`. Work gets assigned to people in absentia, and SS's duplicate
veto reads the ownership-only-versus-speaker split to tell label drift
from two humans.
"""

import copy
from pathlib import Path

from src.contextquilt.services.extraction_schema import (
    ENTITY_CAPACITY_KEY,
    cap_entities,
    drop_placeholder_entities,
    enforce_person_ownership,
    inject_ownership_entities,
)
from src.contextquilt.services.person_appearances import observed_capacities

WORKER = (Path(__file__).resolve().parents[2] / "src" / "worker.py").read_text()


def _person(name: str, owns: list | None = None) -> dict:
    return {
        "type": "person",
        "value": {"text": name},
        "connects_to": [
            {
                "role": "informs",
                "label": "owns",
                "target_type": "commitment",
                "target_text": t,
            }
            for t in (owns or [])
        ],
    }


def _commitment(text: str, owner: str | None = None) -> dict:
    return {"type": "commitment", "value": {"text": text, "owner": owner},
            "connects_to": []}


def _entity(name: str, etype: str = "person") -> dict:
    return {"name": name, "type": etype, "description": ""}


def _names(content: dict, etype: str = "person") -> list:
    return [e["name"] for e in content["entities"] if e["type"] == etype]


def _caps(content: dict, name: str) -> list:
    for e in content["entities"]:
        if e["name"].lower() == name.lower():
            return e.get(ENTITY_CAPACITY_KEY) or []
    raise AssertionError(f"no entity named {name}")


class TestTheFieldBug:
    def test_owner_with_no_entities_at_all_gets_one(self):
        """The reported shape: patches are right, entities are empty."""
        content = {
            "patches": [
                _commitment("Ship the pricing deck", "Pallavi"),
                _commitment("Unblock the vendor review", "Joy"),
            ],
            "entities": [],
        }
        enforce_person_ownership(content, user_label="Scott")
        inject_ownership_entities(content, user_label="Scott")
        assert sorted(_names(content)) == ["Joy", "Pallavi"]
        assert _caps(content, "Pallavi") == ["ownership"]
        assert _caps(content, "Joy") == ["ownership"]

    def test_no_entities_key_at_all(self):
        content = {"patches": [_person("Pallavi", owns=["Ship the deck"])]}
        inject_ownership_entities(content)
        assert _names(content) == ["Pallavi"]

    def test_nothing_owned_changes_nothing(self):
        content = {
            "patches": [_person("Pallavi"), _commitment("Ship it", None)],
            "entities": [_entity("Acme", "org")],
        }
        before = copy.deepcopy(content)
        inject_ownership_entities(content)
        assert content == before
        assert "_ownership_entities_injected" not in content


class TestCapacity:
    def test_capacity_is_ownership_never_speaker(self):
        """Owning an action item is not evidence of having spoken."""
        content = {"patches": [_person("Pallavi", owns=["Ship it"])], "entities": []}
        inject_ownership_entities(content)
        assert "speaker" not in _caps(content, "Pallavi")

    def test_forged_speaker_capacity_is_discarded(self):
        """extract() does not enforce the schema on the wire, so an entity
        can arrive carrying anything. Speaker is never honoured here."""
        content = {
            "patches": [_person("Pallavi", owns=["Ship it"])],
            "entities": [
                {"name": "Pallavi", "type": "person",
                 ENTITY_CAPACITY_KEY: ["speaker", "ownership"]},
            ],
        }
        inject_ownership_entities(content)
        assert _caps(content, "Pallavi") == ["ownership"]

    def test_model_listed_owner_keeps_mention_and_gains_ownership(self):
        """The mention WAS observed for a name the model put in the array,
        so both capacities are true and both are stamped."""
        content = {
            "patches": [_person("Pallavi", owns=["Ship it"])],
            "entities": [_entity("Pallavi")],
        }
        inject_ownership_entities(content)
        assert _caps(content, "Pallavi") == ["mention", "ownership"]

    def test_injected_owner_is_not_claimed_as_a_mention(self):
        content = {"patches": [_person("Pallavi", owns=["Ship it"])], "entities": []}
        inject_ownership_entities(content)
        assert _caps(content, "Pallavi") == ["ownership"]


class TestTheEnforcersRules:
    def test_compound_owners_are_split(self):
        content = {
            "patches": [_person("Joy/Marlowe", owns=["Ship it"])],
            "entities": [],
        }
        inject_ownership_entities(content)
        assert sorted(_names(content)) == ["Joy", "Marlowe"]

    def test_self_speaker_is_skipped(self):
        content = {
            "patches": [_person("Scott", owns=["Ship it"]),
                        _person("Pallavi", owns=["Ship it"])],
            "entities": [],
        }
        inject_ownership_entities(content, user_label="Scott")
        assert _names(content) == ["Pallavi"]

    def test_you_token_owner_is_skipped(self):
        content = {"patches": [_person("(you)", owns=["Ship it"])], "entities": []}
        inject_ownership_entities(content)
        assert content["entities"] == []

    def test_diarization_placeholders_are_skipped(self):
        content = {
            "patches": [
                _person("Speaker 3", owns=["Ship it"]),
                _person("Speaker_15", owns=["Ship it"]),
                _person("Unknown", owns=["Ship it"]),
                _person("unidentified", owns=["Ship it"]),
            ],
            "entities": [],
        }
        inject_ownership_entities(content)
        assert content["entities"] == []

    def test_matching_is_case_insensitive(self):
        """An entity the model spelled differently is not duplicated."""
        content = {
            "patches": [_person("Pallavi", owns=["Ship it"])],
            "entities": [_entity("pallavi")],
        }
        inject_ownership_entities(content)
        assert _names(content) == ["pallavi"]
        assert _caps(content, "pallavi") == ["mention", "ownership"]

    def test_non_person_entity_of_the_same_name_does_not_block(self):
        """"Marlowe" the org and "Marlowe" the person are separate rows in
        the sink, so they are separate here."""
        content = {
            "patches": [_person("Marlowe", owns=["Ship it"])],
            "entities": [_entity("Marlowe", "org")],
        }
        inject_ownership_entities(content)
        assert _names(content, "org") == ["Marlowe"]
        assert _names(content, "person") == ["Marlowe"]

    def test_person_without_an_ownership_edge_is_not_injected(self):
        """A bystander is not an owner. enforce_owner_edge_agreement has
        already dropped their owns edge by the time this runs."""
        content = {
            "patches": [
                {"type": "person", "value": {"text": "Ellery"},
                 "connects_to": [{"label": "works_with",
                                  "target_type": "commitment",
                                  "target_text": "Ship it"}]},
            ],
            "entities": [],
        }
        inject_ownership_entities(content)
        assert content["entities"] == []


class TestVocabulary:
    def test_app_vocabulary_is_honoured(self):
        content = {
            "patches": [{
                "type": "participant",
                "value": {"text": "Pallavi"},
                "connects_to": [{"label": "assigned_to",
                                 "target_type": "task",
                                 "target_text": "Ship it"}],
            }],
            "entities": [],
        }
        inject_ownership_entities(
            content,
            person_patch_type="participant",
            person_entity_type="human",
            ownership_label="assigned_to",
        )
        assert _names(content, "human") == ["Pallavi"]

    def test_ss_defaults_do_not_match_another_apps_vocabulary(self):
        content = {
            "patches": [{
                "type": "participant",
                "value": {"text": "Pallavi"},
                "connects_to": [{"label": "assigned_to",
                                 "target_type": "task",
                                 "target_text": "Ship it"}],
            }],
            "entities": [],
        }
        inject_ownership_entities(content)
        assert content["entities"] == []


class TestIdempotency:
    def test_second_pass_is_a_no_op(self):
        content = {
            "patches": [
                _person("Pallavi", owns=["Ship it"]),
                _person("Joy/Marlowe", owns=["Unblock it"]),
            ],
            "entities": [_entity("Joy"), _entity("Acme", "org")],
        }
        inject_ownership_entities(content, user_label="Scott")
        first = copy.deepcopy(content["entities"])
        inject_ownership_entities(content, user_label="Scott")
        assert content["entities"] == first

    def test_second_pass_does_not_invent_a_mention(self):
        """The regression this test exists for: re-running must not turn an
        ownership-only entity into one that also claims a mention."""
        content = {"patches": [_person("Pallavi", owns=["Ship it"])], "entities": []}
        inject_ownership_entities(content)
        inject_ownership_entities(content)
        assert _caps(content, "Pallavi") == ["ownership"]

    def test_duplicate_ownership_edges_inject_once(self):
        content = {
            "patches": [_person("Pallavi", owns=["Ship it", "Unblock it"])],
            "entities": [],
        }
        inject_ownership_entities(content)
        assert _names(content) == ["Pallavi"]


class TestSanitizerOrder:
    def test_placeholder_pass_still_cleans_up_after_this(self):
        """Ordering guard. A placeholder that reaches the entities array
        must still be dropped, so this has to run BEFORE the placeholder
        pass, not after it."""
        content = {
            "patches": [_person("Pallavi", owns=["Ship it"])],
            "entities": [_entity("Speaker 4")],
        }
        inject_ownership_entities(content)
        drop_placeholder_entities(content)
        assert _names(content) == ["Pallavi"]

    def test_the_worker_calls_it_in_that_position(self):
        """Source-text guard, the same shape 6.2a uses on main.py. The
        chain order is the whole reason the placeholder rules still apply
        to what this injects, and it is one careless reorder from being
        wrong with no test failing."""
        chain = [
            "strip_prose_from_person_names(",
            "drop_placeholder_and_self_person_patches(",
            "inject_ownership_entities(",
            "drop_placeholder_entities(",
        ]
        positions = [WORKER.index(step) for step in chain]
        assert positions == sorted(positions)

    def test_the_entity_cap_runs_through_the_exempting_form(self):
        """A plain slice here would delete the injection, which is the
        patch backstop's own bug in a second place."""
        assert "cap_entities(entities, MAX_ENTITIES_PER_MEETING)" in WORKER
        assert "entities[:MAX_ENTITIES_PER_MEETING]" not in WORKER


class TestObservedCapacities:
    """The sink's half: what one ingest may stamp on an appearance."""

    def test_nothing_declared_means_mention(self):
        assert observed_capacities(None, spoke=False) == ["mention"]
        assert observed_capacities([], spoke=False) == ["mention"]

    def test_ownership_is_honoured_alone(self):
        assert observed_capacities(["ownership"], spoke=False) == ["ownership"]

    def test_both_accumulate(self):
        assert observed_capacities(["mention", "ownership"], spoke=False) == [
            "mention", "ownership",
        ]

    def test_speaker_is_never_accepted_from_the_entity(self):
        assert observed_capacities(["speaker"], spoke=False) == ["mention"]
        assert observed_capacities(["ownership", "speaker"], spoke=False) == [
            "ownership",
        ]

    def test_speaker_comes_from_the_transcript_and_only_there(self):
        assert observed_capacities(["ownership"], spoke=True) == [
            "ownership", "speaker",
        ]

    def test_junk_is_discarded(self):
        assert observed_capacities(["attendee", 7, None], spoke=False) == ["mention"]


class TestCapEntities:
    def test_under_the_cap_is_untouched(self):
        ents = [_entity(f"e{i}") for i in range(5)]
        kept, dropped = cap_entities(ents, 15)
        assert kept is ents and dropped == 0

    def test_model_noise_is_capped(self):
        ents = [_entity(f"e{i}") for i in range(20)]
        kept, dropped = cap_entities(ents, 15)
        assert len(kept) == 15 and dropped == 5

    def test_ownership_entities_are_exempt(self):
        """The patch-backstop lesson: a cap sized for LLM noise must not
        delete a structural injection, because that deletes a person's
        presence in the meeting."""
        owned = dict(_entity("Pallavi"), **{ENTITY_CAPACITY_KEY: ["ownership"]})
        ents = [_entity(f"e{i}") for i in range(20)] + [owned]
        kept, dropped = cap_entities(ents, 15)
        assert len(kept) == 16
        assert dropped == 5
        assert owned in kept

    def test_order_is_preserved(self):
        ents = [_entity(f"e{i}") for i in range(20)]
        kept, _ = cap_entities(ents, 15)
        assert [e["name"] for e in kept] == [f"e{i}" for i in range(15)]

    def test_non_list_input(self):
        assert cap_entities(None, 15) == ([], 0)
