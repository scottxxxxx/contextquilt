"""The Alignment Layer, phase 1 (design e6ee7ae8, 2026-08-23).

The privacy boundary is enforced in code: shared text that names a
person's tendencies is REJECTED. Ids the model names are validated
against what it was handed. Evidence must be in the transcript or the
event never ships. Impact is derived, never authored.
"""
import pathlib

from contextquilt.services.alignment import (
    ALIGNMENT_SYSTEM,
    build_alignment_content,
    derive_impact,
    evidence_in_transcript,
    guard_shared_text,
    parse_alignment_response,
    private_instruction,
    project_record,
    topic_change_count,
)

T = ("[Jaffer] Let's get the ACL thing cleared first, QA is stuck behind it and "
     "Srikanthi's out Monday. Telemetry can wait a week.\n[Scott (you)] Okay.")


# ---------------- the guard ----------------

def test_guard_rejects_tendency_language_and_names_the_term():
    assert guard_shared_text("Jaffer reversed the priority again") in ("reversed the priority again", "again")
    assert guard_shared_text("This is the third time direction changed") == "third time"
    assert guard_shared_text("Decision inconsistency on auth") == "inconsistency"
    assert guard_shared_text("He tends to change his mind") is not None


def test_guard_rejects_character_words_from_the_shared_denylist():
    assert guard_shared_text("The unreliable stakeholder") == "unreliable"


def test_guard_passes_a_project_fact():
    assert guard_shared_text(
        "Sandbox ACL work moves ahead of the telemetry gap; supersedes the Aug 14 order."
    ) is None
    assert guard_shared_text(None) is None and guard_shared_text("") is None


def test_guard_is_word_bounded():
    assert guard_shared_text("the campaign regained momentum") is None   # 'again' inside a word


# ---------------- the prompt ----------------

def test_prompt_embeds_the_raw_json_shape_and_forbids_tendencies():
    """AnthropicLLMClient ignores json_schema on the wire; the shape must
    be in the prompt or the model answers in prose."""
    assert '{"events": [{"new_decision_id"' in ALIGNMENT_SYSTEM
    assert "inconsistent, reversal, again, keeps changing" in ALIGNMENT_SYSTEM
    assert "VERBATIM" in ALIGNMENT_SYSTEM
    assert "—" not in ALIGNMENT_SYSTEM and "–" not in ALIGNMENT_SYSTEM


def test_content_states_a_cut_and_lists_both_sets():
    c = build_alignment_content(
        "2026-08-21", [{"id": "d1", "text": "ACL first", "owner": "Jaffer"}],
        [{"id": "a1", "text": "Telemetry first", "date": "2026-08-14"}],
        "x" * 30000, transcript_cap=100,
    )
    assert "- a1: Telemetry first [2026-08-14]" in c
    assert "- d1: ACL first (owner: Jaffer)" in c
    assert "[...earlier transcript omitted...]" in c


# ---------------- parse ----------------

def _resp(**over):
    e = {
        "new_decision_id": "d1", "supersedes_ids": ["a1"], "topic": "ACL Priority",
        "statement": "Sandbox ACL work moves ahead of the telemetry gap.",
        "rationale": "QA is blocked behind the ACL fix and Srikanthi is out Monday.",
        "decision_owner": "Jaffer", "implementation_owner": None,
        "evidence_quote": "QA is stuck behind it and Srikanthi's out Monday",
        "confidence": "high",
    }
    e.update(over)
    return {"events": [e]}


def test_parse_validates_ids_against_what_the_model_was_handed():
    d = []
    assert parse_alignment_response(_resp(new_decision_id="ghost"), ["d1"], ["a1"], T, d) == []
    assert "unknown_new_id:ghost" in d
    d = []
    assert parse_alignment_response(_resp(supersedes_ids=["nope"]), ["d1"], ["a1"], T, d) == []
    assert "supersedes_nothing" in d


def test_parse_shippable_only_with_evidence_and_a_clean_guard():
    ok = parse_alignment_response(_resp(), ["d1"], ["a1"], T)[0]
    assert ok["shippable"] and ok["guard_hit"] is None and ok["topic"] == "acl-priority"
    no_ev = parse_alignment_response(_resp(evidence_quote="something never said here at all"), ["d1"], ["a1"], T)[0]
    assert no_ev["evidence_quote"] is None and not no_ev["shippable"]
    bad = parse_alignment_response(_resp(statement="Jaffer changed his mind again on ACL."), ["d1"], ["a1"], T)[0]
    assert bad["guard_hit"] and not bad["shippable"]


def test_parse_never_raises_on_garbage():
    assert parse_alignment_response("not json", ["d1"], ["a1"], T) == []
    assert parse_alignment_response({"events": "x"}, ["d1"], ["a1"], T) == []
    assert parse_alignment_response({"events": [1, None]}, ["d1"], ["a1"], T) == []


def test_evidence_match_folds_case_and_punctuation_but_needs_six_words():
    assert evidence_in_transcript("qa is stuck behind it and srikanthis out monday", T)
    assert not evidence_in_transcript("QA is stuck", T)


# ---------------- impact ----------------

def test_impact_is_facts_about_items_ordered_overdue_first():
    lines = derive_impact([
        {"patch_id": "p1", "patch_type": "commitment", "text": "QA cross-platform fix", "owner": "Joy", "deadline_date": "2026-08-25", "overdue": False},
        {"patch_id": "p2", "patch_type": "blocker", "text": "Telemetry data quality", "owner": None, "deadline_date": None, "overdue": False},
        {"patch_id": "p3", "patch_type": "commitment", "text": "Sandbox ACL rework", "owner": "Jaffer", "deadline_date": "2026-08-20", "overdue": True},
    ], "2026-08-21")
    assert [l["subject"] for l in lines] == ["Sandbox ACL rework", "QA cross-platform fix", "Telemetry data quality"]
    assert lines[0]["effect"] == "Overdue since 2026-08-20" and lines[0]["magnitude"] == 1.0
    assert lines[1]["effect"] == "Due 2026-08-25" and lines[2]["effect"] == "Open, no date"
    assert lines[0]["derived_from"] == ["p3"]
    assert all("dev day" not in l["effect"] for l in lines)   # never an estimate


# ---------------- private half ----------------

def test_private_instruction_counts_in_code_and_never_names_anyone():
    s = private_instruction("authentication-approach", 3)
    assert s.startswith("Direction on authentication approach has changed three times.")
    assert "Capture the decision, owner, scope, and confirmation date" in s


def test_topic_change_count_only_counts_superseding_live_events():
    ev = [
        {"topic": "t", "supersedes": ["x"], "status": "confirmed"},
        {"topic": "t", "supersedes": [], "status": "confirmed"},
        {"topic": "t", "supersedes": ["y"], "status": "expired"},
        {"topic": "u", "supersedes": ["z"], "status": "proposed"},
    ]
    assert topic_change_count(ev, "t") == 1


# ---------------- the record ----------------

def _ev(i, topic, status, at, sup=(), sb=None, conf=None, impact=()):
    return {"event_id": i, "topic": topic, "status": status, "proposed_at": at,
            "supersedes": list(sup), "superseded_by": sb, "confirmed_at": conf,
            "expires_at": None, "impact": list(impact)}


def test_record_current_is_newest_confirmed_unsuperseded_per_topic():
    rec = project_record([
        _ev("e1", "auth", "confirmed", "2026-07-12", conf="x", sb="e3"),
        _ev("e3", "auth", "confirmed", "2026-08-22", sup=["e1"], conf="x",
            impact=[{"subject": "Testing", "effect": "Due 2026-08-30", "magnitude": 0.6, "derived_from": ["p9"]}]),
        _ev("e4", "acl", "proposed", "2026-08-21", sup=["e2"]),
    ])
    assert [e["event_id"] for e in rec["current_directions"]] == ["e3"]
    assert [e["event_id"] for e in rec["awaiting_confirmation"]] == ["e4"]
    assert rec["direction_change_count"] == 1
    assert rec["cumulative_impact"][0]["derived_from"] == ["p9"]
    assert [e["event_id"] for e in rec["history"]] == ["e1", "e4", "e3"]


def test_record_cumulative_impact_dedupes_by_derived_item():
    line = {"subject": "X", "effect": "Overdue", "magnitude": 1.0, "derived_from": ["p1"]}
    rec = project_record([
        _ev("a", "t", "confirmed", "2026-08-01", sup=["z"], conf="x", impact=[line]),
        _ev("b", "t", "confirmed", "2026-08-09", sup=["a"], conf="x", impact=[dict(line)]),
    ])
    assert len(rec["cumulative_impact"]) == 1


def test_migration_keeps_the_private_column_and_a_shippable_flag():
    sql = (pathlib.Path(__file__).resolve().parents[2] / "init-db" / "40_alignment_events.sql").read_text()
    assert "private_instruction" in sql and "shippable" in sql
    assert "CHECK (status IN ('proposed','confirmed','corrected','expired'))" in sql


# ---------------- the boundary, read from source ----------------

def _main():
    return (pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def _worker():
    return (pathlib.Path(__file__).resolve().parents[2] / "src" / "worker.py").read_text()


def test_no_shared_select_names_the_private_column():
    m = _main()
    start = m.index("# Alignment Layer (design e6ee7ae8")
    end = m.index("# Projects", start)
    block = m[start:end]
    cols = block[block.index("ALIGNMENT_SHARED_COLUMNS = "):block.index('"""', block.index("ALIGNMENT_SHARED_COLUMNS = ") + 30)]
    assert "private_instruction" not in cols
    # the only places the column appears are the correction INSERT (carrying
    # it forward) and the FOR UPDATE row read that feeds it; never a response
    served = block[block.index("def alignment_for_meeting"):block.index("def alignment_confirm")]
    assert "private_instruction" not in served


def test_shared_reads_serve_only_shippable_events():
    m = _main()
    for fn in ("def alignment_for_meeting", "def alignment_record"):
        i = m.index(fn)
        assert "AND shippable" in m[i:i + 1500], fn


def test_correction_is_guarded_and_conflicts_never_merge():
    m = _main()
    i = m.index("def alignment_correct")
    body = m[i:i + 6000]
    assert "alignment_svc.guard_shared_text(statement)" in body
    assert '"code": "SHARED_TEXT_REJECTED"' in body
    assert '"code": "CORRECTION_CONFLICT"' in body
    assert "'corrected'" in body and "superseded_by = $2::uuid" in body


def test_confirm_refuses_anything_but_an_open_proposal():
    m = _main()
    i = m.index("def alignment_confirm")
    body = m[i:i + 4000]
    assert '"code": "NOT_CONFIRMABLE"' in body
    assert "confirmation_on_behalf = $4" in body


def test_worker_guard_regenerates_once_then_drops_and_never_softens():
    w = _worker()
    i = w.index("async def _extract_alignment_events")
    body = w[i:w.index("async def _alignment_referencing_items", i)]
    assert "alignment_guard_rejected" in body
    assert body.count("llm.extract(") == 2                       # one call + one retry, never a loop
    assert 'events = [e for e in events if not e["guard_hit"]]' in body
    assert "shippable" in body and "private_instruction" in body


def test_worker_is_inert_without_a_project_or_a_decision():
    w = _worker()
    i = w.index("async def _extract_alignment_events")
    body = w[i:w.index("async def _alignment_referencing_items", i)]
    assert "if not project_id or not origin_id or not transcript:" in body
    assert "if not todays:" in body
    assert "if not active:" in body


def test_expiry_only_lapses_open_unsuperseded_proposals():
    w = _worker()
    i = w.index("UPDATE alignment_events\n                       SET status = 'expired'")
    body = w[i:i + 500]
    assert "status IN ('proposed', 'corrected')" in body
    assert "confirmed_at IS NULL" in body and "superseded_by IS NULL" in body
