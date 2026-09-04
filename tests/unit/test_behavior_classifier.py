"""The classifier is the semantic half of the behavior fence.

Measured 2026-09-01: a judge shown the manifest's own definitions said
59% of 120 stored behaviors were something else, mostly commitments and
decisions. The prompt already forbids all of it and both models ignore
it. These tests pin what makes a model safe to run here: contrast in
the prompt, a numbered join key, fail-open parsing, drop rather than
reroute, and the one conversion the manifest built an edge for.
"""

import pathlib
import re

from contextquilt.services.behavior_classifier import (
    CONVERTIBLE_TYPES,
    DEFAULT_MODEL,
    KEEP_TYPE,
    apply_classifier_verdicts,
    build_classifier_content,
    build_classifier_system,
    classifier_types,
    model_override,
    parse_classifier_verdicts,
)

WORKER = pathlib.Path("src/worker.py").read_text()
MODULE = pathlib.Path("src/contextquilt/services/behavior_classifier.py").read_text()

MANIFEST = {"patch_types": [
    {"domain_type": "commitment", "description": "A promise with a named owner."},
    {"domain_type": "person", "description": "A named human."},
    {"domain_type": "moment", "description": "One observed instance of conduct."},
    {"domain_type": "preference", "description": "A lean, value, or stylistic choice."},
    {"domain_type": "decision", "description": "A call that was made."},
    {"domain_type": "project", "description": "A workstream."},
]}


def _patch(text, owner="Denby"):
    return {"type": "moment", "value": {"text": text, "owner": owner}}


# --- contrast ------------------------------------------------------------

def test_behavior_is_offered_first_and_entities_are_not_offered():
    assert classifier_types(MANIFEST) == [
        "moment", "commitment", "preference", "decision"]


def test_the_prompt_carries_every_offered_definition_verbatim():
    """A model describes the shape of what it is given (doc 19.8). The
    right answer must be on the page for a wrong one to be chosen over
    it rather than defaulted into."""
    sys_prompt = build_classifier_system(MANIFEST)
    assert "commitment: A promise with a named owner." in sys_prompt
    assert "decision: A call that was made." in sys_prompt
    assert "person:" not in sys_prompt
    assert "normal for every item in a batch to be a behavior" in sys_prompt


def test_the_prompt_states_the_raw_json_shape():
    """The Anthropic client never puts json_schema on the wire, so an
    unstated shape is an unemitted shape (doc 19.3)."""
    sys_prompt = build_classifier_system(MANIFEST)
    assert '{"items": [{"item": 0, "type":' in sys_prompt
    assert "raw JSON" in sys_prompt


def test_no_dash_punctuation_anywhere_the_model_can_copy_it():
    sys_prompt = build_classifier_system(MANIFEST)
    assert not re.search("[–—]", sys_prompt)
    assert not re.search("[–—]", MODULE)


def test_content_numbers_items_and_shows_owner_and_fact():
    content = build_classifier_content([
        _patch("Asked for the numbers"), _patch("Agreed to send the deck", "Vijay")])
    assert "ITEM 0:\nowner: Denby\nfact: Asked for the numbers" in content
    assert "ITEM 1:\nowner: Vijay\nfact: Agreed to send the deck" in content


# --- parsing fails open ------------------------------------------------

ALLOWED = ["moment", "commitment", "preference", "decision"]


def test_parse_happy_path_is_case_insensitive():
    got = parse_classifier_verdicts(
        {"items": [{"item": 0, "type": "Commitment"}, {"item": 1, "type": "moment"}]},
        2, ALLOWED)
    assert got == ["commitment", "moment"]


def test_parse_garbage_means_keep_everything():
    assert parse_classifier_verdicts("not json", 3, ALLOWED) == [None, None, None]
    assert parse_classifier_verdicts({"items": "x"}, 2, ALLOWED) == [None, None]
    assert parse_classifier_verdicts({}, 1, ALLOWED) == [None]


def test_parse_refuses_out_of_range_bool_duplicate_and_unknown_type():
    got = parse_classifier_verdicts({"items": [
        {"item": 5, "type": "commitment"},        # out of range
        {"item": True, "type": "commitment"},     # bool is not an index
        {"item": 0, "type": "commitment"},
        {"item": 0, "type": "decision"},          # duplicate: first wins
        {"item": 1, "type": "observation"},       # not a declared type
        {"item": 2, "type": 7},                   # not a string
    ]}, 3, ALLOWED)
    assert got == ["commitment", None, None]


# --- applying: keep, convert, drop -------------------------------------

def test_behavior_and_silence_both_keep():
    a, b = _patch("Asked for the numbers"), _patch("Read the clause out")
    split = apply_classifier_verdicts([a, b], ["moment", None])
    assert split["kept"] == [a, b]
    assert split["retyped"] == [] and split["dropped"] == []


def test_a_commitment_verdict_drops_with_an_audible_receipt():
    """Rerouting would mint a commitment with no deadline resolution, no
    owed_to and no project scope, into somebody's they_owe ledger. The
    main extraction owns that type. Drop, and say so."""
    p = _patch("Agreed to send the deck by Thursday", "Vijay")
    split = apply_classifier_verdicts([p], ["commitment"])
    assert split["kept"] == [] and split["retyped"] == []
    assert split["dropped"] == [{
        "text": "Agreed to send the deck by Thursday", "owner": "Vijay",
        "verdict": "commitment"}]


def test_a_preference_verdict_converts_with_held_by():
    p = _patch("Prefers to test and validate before committing", "Steven")
    split = apply_classifier_verdicts([p], ["preference"])
    assert split["kept"] == [] and split["dropped"] == []
    assert split["retyped"] == [p]
    assert p["type"] == "preference"
    assert p["value"]["owner"] == "Steven"
    assert {"label": "held_by", "target_type": "person",
            "target_text": "Steven"} in p["connects_to"]


def test_the_users_own_preference_loses_its_owner_and_gets_no_edge():
    p = _patch("Prefers async standups", "you")
    apply_classifier_verdicts([p], ["preference"])
    assert p["type"] == "preference"
    assert "owner" not in p["value"]
    assert not p.get("connects_to")


def test_only_preference_is_convertible_and_trait_is_not():
    """trait is self_only: a character claim about a person who never
    consented is refused, never routed."""
    assert CONVERTIBLE_TYPES == frozenset({"preference"})
    assert KEEP_TYPE == "moment"


# --- the wiring --------------------------------------------------------

def _lane():
    return WORKER.split("async def _extract_behavior_observations")[1].split(
        "async def ")[0]


def _helper():
    return WORKER.split("async def _classify_behavior_observations")[1].split(
        "async def ")[0]


def test_the_lane_classifies_after_the_sanitizer_and_before_the_sink():
    """Cheap rules first so the judge never spends tokens on a
    placeholder owner; verdicts before the sink so a dropped row never
    gets an origin, an ACL or a dedup hit."""
    body = _lane()
    i_san = body.index("sanitize_behavior_observations(")
    i_cls = body.index("_classify_behavior_observations(")
    i_sink = body.index("store_connected_patches(")
    assert i_san < i_cls < i_sink


def test_the_helper_fails_open_and_has_a_kill_switch():
    body = _helper()
    assert "behavior_classifier.enabled()" in body
    assert "except Exception" in body
    assert body.count("return list(patches)") == 2
    assert "behavior_classifier_verdicts" in body
    assert "dropped_detail=" in body


def test_the_helper_stores_kept_plus_retyped_and_nothing_dropped():
    """Pinned to the WHOLE line. A substring check passed a sabotage that
    appended the dropped rows to the same expression (2026-09-01)."""
    body = _helper()
    returns = re.findall(r"^\s*return (.+?)\s*$", body, re.M)
    assert 'split["kept"] + split["retyped"]' in returns
    assert not any("dropped" in r for r in returns)


# --- the model ----------------------------------------------------------

def test_the_default_model_is_sonnet_and_the_env_wins(monkeypatch):
    """Measured 2026-09-01 on the same 120 rows and the same prompt:
    Haiku dropped 26 rows Sonnet kept, and the hand-read ones were
    conduct filed as somebody else's commitment. A wrong drop here is
    a memory nobody can re-observe."""
    assert DEFAULT_MODEL == "claude-sonnet-4-6"
    monkeypatch.delenv("CQ_BEHAVIOR_CLASSIFIER_MODEL", raising=False)
    assert model_override() == DEFAULT_MODEL
    monkeypatch.setenv("CQ_BEHAVIOR_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
    assert model_override() == "claude-haiku-4-5-20251001"


def test_the_prompt_carries_the_owner_test_and_the_name_removal_test():
    """The two lines the ratchet added, each for a measured miss: Haiku
    filed delegations as the delegate's commitment; both models kept
    facts about the world that stay true with the owner's name removed."""
    sys_prompt = build_classifier_system(MANIFEST)
    assert "the row is about its OWNER" in sys_prompt
    assert "remove the owner's name" in sys_prompt
    assert "stay behavior" in sys_prompt
