"""Live speaker labels answered on the device, applied at ingest.

Receipt: 2026-08-23, Scott labelled "christina" in the live Copilot view,
no prompt, because live renames never call reassign-speaker; the name
rode inside the capture and ingest attached it to Christina McAlpin via
a merge_backfill alias with no signal. The client now asks from its
cached roster and sends the answer as metadata.speaker_identities; CQ
rewrites the bracketed label to the canonical name before extraction.
"""
import pathlib

from contextquilt.services.speaker_identities import (
    labels_in_transcript,
    parse_speaker_identities,
    rewrite_speaker_labels,
)

T = "[christina] hi all\n[Scott (you)] hey\n[Christina] back again\n[Speaker 2] who am I"


def test_rewrite_is_case_insensitive_and_keeps_the_you_marker():
    out, counts = rewrite_speaker_labels(
        "[christina] a\n[CHRISTINA (you)] b\n[Bob] c", {"christina": "Christina McAlpin"}
    )
    assert out == "[Christina McAlpin] a\n[Christina McAlpin (you)] b\n[Bob] c"
    assert counts == {"christina": 2}


def test_rewrite_does_not_touch_a_label_that_is_a_prefix_of_another():
    out, _ = rewrite_speaker_labels("[Chris] a\n[Christina] b", {"Chris": "Chris Pine"})
    assert out == "[Chris Pine] a\n[Christina] b"


def test_rewrite_reports_zero_for_a_label_the_transcript_does_not_use():
    out, counts = rewrite_speaker_labels(T, {"Nobody": "Nobody Real"})
    assert out == T and counts == {"Nobody": 0}


def test_count_means_found_even_when_label_already_is_the_canonical_name():
    """SS's Someone new renames the live label to the fuller name BEFORE
    sending, so label == canonical is the normal created case. The count
    is the one diagnostic for "the key did not match the brackets", so it
    must say FOUND here, not 0. Text is untouched."""
    out, counts = rewrite_speaker_labels(
        "[Christina Lopez] a\n[Christina Lopez (you)] b", {"Christina Lopez": "Christina Lopez"}
    )
    assert out == "[Christina Lopez] a\n[Christina Lopez (you)] b"
    assert counts["Christina Lopez"] == 2


def test_parse_keeps_only_well_formed_entries_and_first_answer_per_label():
    raw = [
        {"label": "christina", "entity_id": "abc"},
        {"label": "christina", "create_new": True, "name": "Christina Lopez"},   # dup label, dropped
        {"label": "Speaker 2", "create_new": True, "name": "Christina Lopez"},
        {"label": "Speaker 3", "create_new": True},                            # no name
        {"label": "", "entity_id": "x"},                                       # no label
        {"label": "Both", "entity_id": "e1", "create_new": True, "name": "N"},  # entity_id wins
        "junk", None, {"entity_id": "y"},
    ]
    got = parse_speaker_identities(raw)
    assert [g["label"] for g in got] == ["christina", "Speaker 2", "Both"]
    assert got[0] == {"label": "christina", "entity_id": "abc", "name": None, "create_new": False}
    assert got[1] == {"label": "Speaker 2", "entity_id": None, "name": "Christina Lopez", "create_new": True}
    assert got[2]["entity_id"] == "e1" and got[2]["create_new"] is False


def test_parse_never_raises_on_garbage():
    assert parse_speaker_identities(None) == []
    assert parse_speaker_identities("x") == []
    assert parse_speaker_identities({"label": "a"}) == []


def test_labels_in_transcript_strips_you_and_lowercases():
    assert labels_in_transcript(T) == {"christina", "scott", "speaker 2"}


# ------------------------------------------------------------------
# Worker wiring: the rewrite runs AFTER the owner marker is injected and
# BEFORE the model reads the transcript, on the SAME variable the call
# sends. Read from source; a mirror of this order would be the lie.
# ------------------------------------------------------------------

def _worker():
    return (pathlib.Path(__file__).resolve().parents[2] / "src" / "worker.py").read_text()


def test_rewrite_sits_between_owner_marker_and_the_llm_call():
    w = _worker()
    start = w.index("async def handle_meeting_summary(")
    body = w[start:]
    marker = body.index("effective_summary = normalize_owner_in_transcript(summary, owner_speaker_label)")
    rewrite = body.index("effective_summary, _identities_applied = await self._apply_speaker_identities(")
    call = body.index("user_content=meeting_date_line + language_line + user_context + open_commits_block + effective_summary")
    assert marker < rewrite < call


def test_create_half_stamps_keep_separate_against_first_token_sharers():
    w = _worker()
    start = w.index("async def _create_identified_person(")
    end = w.index("async def handle_meeting_summary(", start)
    body = w[start:end]
    assert "INSERT INTO entity_separations" in body
    assert "'speaker_identity'" in body
    assert "tokenize_name(r[\"name\"] or \"\")[:1] == first" in body
    # exact match resolves, never creates a duplicate (UNIQUE on name)
    assert "LOWER(name) = LOWER($3)" in body


def test_an_unresolvable_entry_leaves_the_label_alone():
    w = _worker()
    start = w.index("async def _apply_speaker_identities(")
    end = w.index("async def _create_identified_person(", start)
    body = w[start:end]
    assert body.count("speaker_identity_unresolved") == 2   # unknown entity + exception
    assert "continue" in body
    assert "\n        raise" not in body and "\n            raise" not in body and "\n                raise" not in body
