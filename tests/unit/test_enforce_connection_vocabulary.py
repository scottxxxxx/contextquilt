"""Unit tests for enforce_connection_vocabulary / classify_connection.

Background: the LLM emits edges with vocabulary labels but off-spec
type combos — reversed direction (blocker --blocked_by--> commitment),
pairs no orientation allows (owns commitment->decision), and labels not
in the vocabulary at all (works_with). SS's client validator drops them
silently, losing real semantic content. The sanitizer flips reversed
edges and drops invalid ones at write time.
"""

from src.contextquilt.services.extraction_schema import (
    build_label_specs,
    classify_connection,
    enforce_connection_vocabulary,
)


# Mirrors the relevant slice of the SS manifest v6 vocabulary
LABELS = [
    {"label": "blocked_by", "role": "depends_on", "from_types": ["commitment"], "to_types": ["blocker"]},
    {"label": "owns", "role": "informs", "from_types": ["person"], "to_types": ["commitment", "blocker", "decision", "goal"]},
    {"label": "belongs_to", "role": "parent", "from_types": ["commitment", "blocker", "decision"], "to_types": ["project", "deliverable"]},
    {"label": "describes", "role": "informs", "from_types": ["role"], "to_types": ["person"]},
]
SPECS = build_label_specs(LABELS)


def _patch(text, ptype, connects_to=None):
    return {"type": ptype, "value": {"text": text}, "connects_to": connects_to or []}


def _edge(target_text, target_type, label, role="informs"):
    return {"target_text": target_text, "target_type": target_type, "role": role, "label": label}


# ============================================================
# classify_connection
# ============================================================


def test_valid_combo():
    assert classify_connection("blocked_by", "commitment", "blocker", SPECS) == ("valid", "depends_on")


def test_reversed_combo():
    assert classify_connection("blocked_by", "blocker", "commitment", SPECS) == ("reversed", "depends_on")


def test_unknown_label():
    assert classify_connection("works_with", "commitment", "person", SPECS) == ("invalid", None)


def test_invalid_pair_for_known_label():
    # owns commitment->decision: neither orientation allowed
    assert classify_connection("owns", "commitment", "decision", SPECS) == ("invalid", None)


def test_symmetric_pair_prefers_valid():
    # belongs_to commitment->project valid as-is, never "reversed"
    assert classify_connection("belongs_to", "commitment", "project", SPECS) == ("valid", "parent")


# ============================================================
# enforce_connection_vocabulary
# ============================================================


def test_valid_edges_kept_and_role_normalized():
    content = {
        "patches": [
            _patch("Ship the API", "commitment", [_edge("Legal review", "blocker", "blocked_by", role="informs")]),
            _patch("Legal review", "blocker"),
        ]
    }
    enforce_connection_vocabulary(content, LABELS)
    edges = content["patches"][0]["connects_to"]
    assert len(edges) == 1
    assert edges[0]["role"] == "depends_on"  # normalized from "informs"


def test_reversed_edge_flipped_onto_target_patch():
    content = {
        "patches": [
            _patch("Legal review", "blocker", [_edge("Ship the API", "commitment", "blocked_by")]),
            _patch("Ship the API", "commitment"),
        ]
    }
    enforce_connection_vocabulary(content, LABELS)
    # Edge removed from the blocker...
    assert content["patches"][0]["connects_to"] == []
    # ...and reappears on the commitment, pointing at the blocker
    edges = content["patches"][1]["connects_to"]
    assert len(edges) == 1
    assert edges[0]["target_text"] == "Legal review"
    assert edges[0]["target_type"] == "blocker"
    assert edges[0]["label"] == "blocked_by"
    assert edges[0]["role"] == "depends_on"
    audit = content["_connection_vocabulary_enforced"]
    assert audit["flipped"] == 1


def test_reversed_edge_dropped_when_target_not_in_output():
    content = {
        "patches": [
            _patch("Legal review", "blocker", [_edge("Ship the API", "commitment", "blocked_by")]),
        ]
    }
    enforce_connection_vocabulary(content, LABELS)
    assert content["patches"][0]["connects_to"] == []
    assert content["_connection_vocabulary_enforced"]["dropped"] == 1


def test_unknown_label_dropped():
    content = {
        "patches": [
            _patch("Ship the API", "commitment", [_edge("Maria", "person", "works_with")]),
            _patch("Maria", "person"),
        ]
    }
    enforce_connection_vocabulary(content, LABELS)
    assert content["patches"][0]["connects_to"] == []
    audit = content["_connection_vocabulary_enforced"]
    assert audit["dropped"] == 1
    assert "works_with" in audit["dropped_detail"][0]


def test_flip_dedupes_against_existing_edge():
    # Commitment already carries the correct edge; the blocker carries
    # the reversed duplicate — flip must not create a second copy.
    content = {
        "patches": [
            _patch("Ship the API", "commitment", [_edge("Legal review", "blocker", "blocked_by", role="depends_on")]),
            _patch("Legal review", "blocker", [_edge("Ship the API", "commitment", "blocked_by")]),
        ]
    }
    enforce_connection_vocabulary(content, LABELS)
    assert len(content["patches"][0]["connects_to"]) == 1
    assert content["patches"][1]["connects_to"] == []


def test_no_vocabulary_is_a_noop():
    content = {
        "patches": [
            _patch("Ship the API", "commitment", [_edge("Maria", "person", "works_with")]),
        ]
    }
    enforce_connection_vocabulary(content, None)
    assert len(content["patches"][0]["connects_to"]) == 1
    enforce_connection_vocabulary(content, [])
    assert len(content["patches"][0]["connects_to"]) == 1


def test_mixed_batch_audit_counts():
    content = {
        "patches": [
            _patch("Ship the API", "commitment", [
                _edge("Legal review", "blocker", "blocked_by", role="depends_on"),  # valid
                _edge("Pick vendor", "decision", "owns"),                            # invalid pair
            ]),
            _patch("Legal review", "blocker", [
                _edge("Ship the API", "commitment", "blocked_by"),                   # reversed, target present
            ]),
            _patch("Pick vendor", "decision"),
        ]
    }
    enforce_connection_vocabulary(content, LABELS)
    audit = content["_connection_vocabulary_enforced"]
    assert audit["kept"] == 1
    assert audit["flipped"] == 1
    assert audit["dropped"] == 1


def test_tolerates_malformed_patches_and_edges():
    content = {
        "patches": [
            "not a dict",
            {"type": "commitment"},  # no value/connects_to
            _patch("Ship the API", "commitment", ["not a dict edge"]),
        ]
    }
    # Must not raise
    enforce_connection_vocabulary(content, LABELS)
    assert content["patches"][2]["connects_to"] == []
