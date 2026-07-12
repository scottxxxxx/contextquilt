"""Unit tests for ingest-mode routing rules (the transformer contract)."""

from src.contextquilt.services.ingest_modes import (
    INGEST_MODE_TYPES,
    is_interaction_allowed,
)


def test_undeclared_mode_allows_everything():
    for t in ("meeting_transcript", "structured_patches", "chat_log", "hydrate", "junk"):
        assert is_interaction_allowed(None, t) is True
        assert is_interaction_allowed("", t) is True


def test_unknown_declared_mode_is_not_enforced():
    # Forward-compat: a mode this build doesn't know must not brick ingestion.
    assert is_interaction_allowed("telepathy", "meeting_transcript") is True


def test_structured_app_rejects_transcript_shapes():
    for t in ("meeting_transcript", "meeting_summary", "analysis", "chat_log", "trace"):
        assert is_interaction_allowed("structured", t) is False
    assert is_interaction_allowed("structured", "structured_patches") is True


def test_extraction_app_rejects_structured_patches():
    assert is_interaction_allowed("extraction", "structured_patches") is False
    for t in INGEST_MODE_TYPES["extraction"]:
        assert is_interaction_allowed("extraction", t) is True


def test_system_and_unknown_types_bypass_the_gate():
    for mode in ("extraction", "structured"):
        for t in ("hydrate", "tool_call", "some_future_type", None):
            assert is_interaction_allowed(mode, t) is True


def test_every_mode_owns_at_least_one_type_and_sets_are_disjoint():
    sets = list(INGEST_MODE_TYPES.values())
    assert all(sets)
    assert not (sets[0] & sets[1])
