"""enforce_owed_to_counterparty: the counterparty half of the ledger.

`owed_to` is the only label that can say the (you) speaker owes a named
person something, which makes a wrong one worse than a missing one: it
renders as an obligation the user does not have. These tests pin the three
edges that must never survive and, just as importantly, the cases the
sanitizer must leave completely alone.
"""

from contextquilt.services.extraction_schema import enforce_owed_to_counterparty


def _item(text, owner=None, targets=(), label="owed_to"):
    return {
        "type": "commitment",
        "value": {"text": text, "owner": owner},
        "connects_to": [
            {
                "target_text": t,
                "target_type": "person",
                "role": "informs",
                "label": label,
            }
            for t in targets
        ],
    }


def _labels(patch):
    return [
        c["target_text"]
        for c in patch["connects_to"]
        if c.get("label") == "owed_to"
    ]


def test_the_case_this_exists_for_survives():
    """User owes a named person: owner null, counterparty named."""
    content = {"patches": [_item("Send the revised routing diagram", None, ["Lockridge Chen"])]}
    enforce_owed_to_counterparty(content, user_label="Scott Guida")
    assert _labels(content["patches"][0]) == ["Lockridge Chen"]
    assert "_owed_to_enforced" not in content


def test_owed_to_its_own_owner_is_dropped():
    """"Denby will send Denby the IP" is the owner restated, not a
    counterparty. Kept, it renders as a person owing themselves."""
    content = {"patches": [_item("Send the IP address", "Denby", ["Denby"])]}
    enforce_owed_to_counterparty(content, user_label="Scott Guida")
    assert _labels(content["patches"][0]) == []
    assert content["_owed_to_enforced"]["dropped"][0]["why"] == "owed_to_own_owner"


def test_owed_to_own_owner_is_case_insensitive():
    content = {"patches": [_item("Send the IP address", "Denby", ["denby"])]}
    enforce_owed_to_counterparty(content, user_label=None)
    assert _labels(content["patches"][0]) == []


def test_compound_owner_filters_against_every_part():
    """The same split enforce_person_ownership used to create the owns
    edges, so a slash-joined owner cannot smuggle itself in as its own
    counterparty."""
    content = {"patches": [_item("Ship the migration", "Marlowe/Quill", ["Quill", "Marcus Webb"])]}
    enforce_owed_to_counterparty(content, user_label=None)
    assert _labels(content["patches"][0]) == ["Marcus Webb"]


def test_owed_to_the_you_speaker_is_dropped():
    """Real relationship, wrong representation. Lockridge owes you is already
    carried by Lockridge's owns edge, and the (you) speaker has no person
    patch, so this edge would dangle and Pass-2 would answer the dangle by
    re-creating the self person patch the self gate exists to prevent."""
    content = {"patches": [_item("Confirm the Q3 headcount", "Lockridge Chen", ["Scott Guida"])]}
    enforce_owed_to_counterparty(content, user_label="Scott Guida")
    assert _labels(content["patches"][0]) == []
    assert content["_owed_to_enforced"]["dropped"][0]["why"] == "self_or_placeholder"


def test_bare_you_token_is_dropped_without_a_user_label():
    """The model writes "(you)" and "you" into target_text even when CQ
    never told it the user's name."""
    for token in ("(you)", "you", "me", "I", "self"):
        content = {"patches": [_item("Confirm the number", "Lockridge Chen", [token])]}
        enforce_owed_to_counterparty(content, user_label=None)
        assert _labels(content["patches"][0]) == [], token


def test_diarization_placeholder_is_dropped():
    content = {"patches": [_item("Send the deck", None, ["Speaker 4"])]}
    enforce_owed_to_counterparty(content, user_label="Scott Guida")
    assert _labels(content["patches"][0]) == []


def test_item_with_no_stated_owner_keeps_its_counterparty():
    """Conservative in the same shape as enforce_owner_edge_agreement: no
    stated owner means nothing to contradict, so the edge stands."""
    content = {"patches": [_item("Introduce her to Marcus", None, ["Lockridge Chen"])]}
    enforce_owed_to_counterparty(content, user_label="Scott Guida")
    assert _labels(content["patches"][0]) == ["Lockridge Chen"]


def test_third_party_to_third_party_survives():
    """Lockridge owes Marcus. Not the user's ledger, but a true edge, and the
    read side is what filters it out, not the sanitizer."""
    content = {"patches": [_item("Sign off on the shortlist", "Lockridge Chen", ["Marcus Webb"])]}
    enforce_owed_to_counterparty(content, user_label="Scott Guida")
    assert _labels(content["patches"][0]) == ["Marcus Webb"]


def test_other_labels_pass_through_untouched():
    """Only owed_to edges are considered. An owns edge pointing at the
    item's own owner is the CORRECT shape and must survive."""
    patch = {
        "type": "person",
        "value": {"text": "Denby"},
        "connects_to": [
            {"target_text": "Send the IP address", "target_type": "commitment",
             "role": "informs", "label": "owns"},
            {"target_text": "Atlas Migration", "target_type": "project",
             "role": "informs", "label": "works_on"},
        ],
    }
    content = {"patches": [patch]}
    enforce_owed_to_counterparty(content, user_label="Scott Guida")
    assert len(patch["connects_to"]) == 2
    assert "_owed_to_enforced" not in content


def test_multiple_counterparties_are_filtered_independently():
    content = {"patches": [
        _item("Circulate the summary", "Denby", ["Denby", "Lockridge Chen", "Speaker 2"])
    ]}
    enforce_owed_to_counterparty(content, user_label="Scott Guida")
    assert _labels(content["patches"][0]) == ["Lockridge Chen"]
    assert len(content["_owed_to_enforced"]["dropped"]) == 2


def test_empty_target_is_dropped():
    content = {"patches": [_item("Send the deck", None, ["   "])]}
    enforce_owed_to_counterparty(content, user_label=None)
    assert _labels(content["patches"][0]) == []
    assert content["_owed_to_enforced"]["dropped"][0]["why"] == "empty"


def test_blockers_are_covered_too():
    """they_owe already spans commitment AND blocker. A you_owe that
    silently covered only half would make "nothing you owe" wrong on
    exactly the blocker cases."""
    patch = _item("Waiting on the IP whitelist", "Ellery", ["Ellery"])
    patch["type"] = "blocker"
    content = {"patches": [patch]}
    enforce_owed_to_counterparty(content, user_label=None)
    assert _labels(patch) == []


def test_malformed_input_is_survivable():
    for content in ({}, {"patches": None}, {"patches": []}, {"patches": [None, "x"]}):
        assert enforce_owed_to_counterparty(dict(content), user_label="Scott") is not None


def test_no_owed_to_edges_leaves_content_byte_identical():
    content = {"patches": [_item("Ship it", "Denby", ["Lockridge"], label="owns")]}
    before = repr(content)
    enforce_owed_to_counterparty(content, user_label="Scott Guida")
    assert repr(content) == before
