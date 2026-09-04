"""Doc 22: material the user HEARD rather than took part in.

Scott's podcast recording produced five patches, all `behavior`, and
nothing from the main extraction, because the main extraction is built
around participation. The worse harm was already in the data: `Leo` had
15 person_appearances at speaker capacity, so CQ asserted the user had
been in a room with a podcast host.
"""

import pathlib
import re

from contextquilt.services import material_kind as mk

WORKER = pathlib.Path("src/worker.py").read_text()
MODULE = pathlib.Path("src/contextquilt/services/material_kind.py").read_text()


# --- the flag ----------------------------------------------------------

def test_absent_or_unknown_is_a_meeting_so_nothing_changes_for_anyone():
    """Absent means today's behavior byte for byte. An UNRECOGNISED kind
    is also a meeting rather than an error: a client sending a kind CQ
    does not know yet must not lose its meeting."""
    assert mk.from_metadata(None) == mk.MEETING
    assert mk.from_metadata({}) == mk.MEETING
    assert mk.from_metadata({"material_kind": None}) == mk.MEETING
    assert mk.from_metadata({"material_kind": "podcast"}) == mk.MEETING
    assert mk.from_metadata({"material_kind": 7}) == mk.MEETING
    assert mk.from_metadata("not a mapping") == mk.MEETING


def test_listening_is_recognised_case_and_space_insensitively():
    for raw in ("listening", "LISTENING", "  Listening  "):
        assert mk.from_metadata({"material_kind": raw}) == mk.LISTENING
        assert mk.is_listening({"material_kind": raw})
    assert not mk.is_listening({"material_kind": "meeting"})


# --- what a listener may keep ------------------------------------------

def test_allowed_types_intersect_the_manifest_rather_than_asserting_three():
    """An app that never declared `artifact` does not start receiving one
    because this module has an opinion."""
    assert mk.allowed_types({"patch_types": [
        {"domain_type": "takeaway"}, {"domain_type": "commitment"}]}) == {"takeaway"}
    assert mk.allowed_types({"patch_types": [
        {"domain_type": "takeaway"}, {"domain_type": "event"},
        {"domain_type": "artifact"}]}) == {"takeaway", "event", "artifact"}
    # No manifest at all is the legacy prompt path: full floor.
    assert mk.allowed_types(None) == set(mk.LISTENING_TYPES)
    assert mk.allowed_types({"patch_types": []}) == set(mk.LISTENING_TYPES)


def test_the_prompt_names_only_the_allowed_types_and_forbids_the_rest():
    p = mk.build_listening_system({"takeaway", "event"})
    assert "`takeaway`" in p and "`event`" in p
    assert "`artifact`" not in p
    for banned in ("commitment", "decision", "blocker", "project", "person"):
        assert banned in p  # named in the exclusion sentence
    assert "NEVER emit a commitment" in p
    assert "not in the room" in p


def test_the_prompt_states_its_json_shape_and_bans_dashes():
    """The Anthropic client accepts json_schema and never puts it on the
    wire, so an unstated field is an unemitted field (doc 19.3)."""
    p = mk.build_listening_system(mk.LISTENING_TYPES)
    assert '{"output_language":' in p and '"patches":' in p
    assert "raw JSON" in p
    assert "NEVER use a dash" in p
    assert not re.search("[–—]", p)
    assert not re.search("[–—]", MODULE)


# --- the sanitizer, because a prompt is not a fence --------------------

def _content():
    return {"patches": [
        {"type": "takeaway", "value": {"text": "Small firms buy on time saved",
                                       "owner": "Jason Snell"},
         "connects_to": [{"label": "held_by", "target_text": "Jason Snell"}]},
        {"type": "commitment", "value": {"text": "Will send the deck"}},
        {"type": "moment", "value": {"text": "Made a joke about the CEO",
                                       "owner": "Andy Ihnatko"}},
        {"type": "event", "value": "Apple announced a leadership change"},
        {"type": "artifact", "value": {"text": ""}},
    ], "entities": [{"name": "Jason Snell", "type": "person"}],
       "relationships": [{"from": "a", "to": "b"}],
       "resolved_commitments": ["abc"]}


def test_only_allowed_types_survive_and_the_owner_and_edges_are_stripped():
    out = mk.sanitize_listening_patches(_content(), {"takeaway", "event", "artifact"})
    kept = out["patches"]
    assert [p["type"] for p in kept] == ["takeaway", "event"]
    assert "owner" not in kept[0]["value"]
    assert "connects_to" not in kept[0]
    # A bare string value is normalised, and an empty text is dropped.
    assert kept[1]["value"]["text"] == "Apple announced a leadership change"
    assert out["_listening_sanitized"]["count"] == 2


def test_no_entity_no_relationship_no_commitment_closure_survives():
    """Presence means the user was in the room and a recording is not a
    room. Emptied inside the sanitizer so the suppression travels with
    the shape rather than depending on a caller remembering it."""
    out = mk.sanitize_listening_patches(_content(), set(mk.LISTENING_TYPES))
    assert out["entities"] == []
    assert out["relationships"] == []
    assert out["resolved_commitments"] == []


def test_garbage_content_yields_an_empty_shape_rather_than_raising():
    out = mk.sanitize_listening_patches("not a dict", set(mk.LISTENING_TYPES))
    assert out["patches"] == [] and out["entities"] == []


# --- the wiring --------------------------------------------------------

def _ingest():
    return WORKER.split("async def handle_meeting_summary")[1].split(
        "\n    async def ")[0]


def test_the_ingest_routes_on_the_flag_and_swaps_the_prompt():
    body = _ingest()
    assert "listening = material_kind.is_listening(metadata)" in body
    assert "resolved_prompt = material_kind.build_listening_system(listening_types)" in body
    assert "open_commits_block = \"\"" in body


def test_listening_suppresses_the_behavior_lane_and_the_role_signals():
    body = _ingest()
    assert "if not listening:\n                await self._extract_behavior_observations(" in body
    assert "semantic_role_signals = None if listening else (" in body


def test_the_sanitizer_runs_before_patches_are_read_from_the_response():
    body = _ingest()
    assert body.index("material_kind.sanitize_listening_patches(") < body.index(
        'patches = response.content.get("patches", [])')


def test_an_app_declaring_none_of_the_listening_types_stores_nothing():
    """Rather than falling through to the participation prompt, which
    would put commitments in a listener's ledger."""
    body = _ingest()
    assert "if not listening_types:" in body
    assert "listening_no_declared_types" in body
