"""Unit tests for the correction service (contract item 9)."""

from datetime import date

from src.contextquilt.services.corrections import (
    FALLBACK_PATCH_TYPE,
    MAX_CANDIDATES,
    build_correction_content,
    parse_correction_response,
)

TODAY = date(2026, 7, 17)
IDS = {"aaaa", "bbbb"}


def _resp(pid, text, **fact):
    return {"corrected_patch_id": pid,
            "corrected_fact": {"text": text, "owner": None, "deadline": None,
                               "deadline_date": None, "patch_type": None, **fact},
            "reason": "r"}


# ------------------------------------------------------------------
# build_correction_content
# ------------------------------------------------------------------

def test_content_carries_ids_scope_and_correction():
    c = build_correction_content(
        "the deadline moved to August",
        [{"patch_id": "aaaa", "patch_type": "commitment", "text": "Ship by July 20"}],
        TODAY.isoformat(), scope_label="Kore",
    )
    assert "patch_id=aaaa" in c
    assert "Project scope: Kore" in c
    assert "the deadline moved to August" in c
    assert "Current date: 2026-07-17" in c


def test_content_caps_candidates_and_handles_empty():
    many = [{"patch_id": str(i), "patch_type": "takeaway", "text": f"t{i}"} for i in range(40)]
    c = build_correction_content("x", many, TODAY.isoformat())
    assert f"{MAX_CANDIDATES}." in c and f"{MAX_CANDIDATES + 1}." not in c
    c2 = build_correction_content("x", [], TODAY.isoformat())
    assert "(none" in c2


# ------------------------------------------------------------------
# parse_correction_response
# ------------------------------------------------------------------

def test_valid_match_with_resolved_deadline():
    matched, value = parse_correction_response(
        _resp("aaaa", "Ship the release by August 15", deadline="August 15",
              deadline_date="2026-08-15", owner="Robin"),
        IDS, meeting_date=TODAY,
    )
    assert matched == "aaaa"
    assert value["text"] == "Ship the release by August 15"
    assert value["deadline_date"] == "2026-08-15"
    assert value["owner"] == "Robin"
    assert value["_new_type"] == FALLBACK_PATCH_TYPE  # unused when matched


def test_hallucinated_patch_id_downgrades_to_unmatched():
    matched, value = parse_correction_response(_resp("zzzz", "Robin owns the rollout now"), IDS)
    assert matched is None
    assert value["text"] == "Robin owns the rollout now"


def test_null_match_uses_declared_type_when_valid():
    matched, value = parse_correction_response(
        _resp(None, "Robin owns the rollout now", patch_type="commitment"), IDS)
    assert matched is None
    assert value["_new_type"] == "commitment"
    matched, value = parse_correction_response(
        _resp(None, "Robin owns the rollout now", patch_type="not_a_type"), IDS)
    assert value["_new_type"] == FALLBACK_PATCH_TYPE


def test_embedded_json_and_garbage():
    raw = 'Sure: {"corrected_patch_id": "bbbb", "corrected_fact": {"text": "Deadline is now end of August"}, "reason": "r"}'
    matched, value = parse_correction_response(raw, IDS)
    assert matched == "bbbb"
    assert parse_correction_response("no json", IDS) is None
    assert parse_correction_response(None, IDS) is None
    assert parse_correction_response({"corrected_fact": {"text": "hi"}}, IDS) is None  # too short


def test_bad_deadline_date_dropped_not_fatal():
    matched, value = parse_correction_response(
        _resp("aaaa", "Ship it by the offsite", deadline="by the offsite",
              deadline_date="sometime soon"),
        IDS, meeting_date=TODAY,
    )
    assert matched == "aaaa"
    assert "deadline_date" not in value
    assert value["deadline"] == "by the offsite"


# ------------------------------------------------------------------
# Completions from chat (contract item 10)
# ------------------------------------------------------------------

from src.contextquilt.services.corrections import (
    build_completion_content,
    parse_completion_response,
)


def test_completion_content_carries_ids_and_statement():
    c = build_completion_content(
        "we closed the vendor escalation yesterday",
        [{"patch_id": "aaaa", "patch_type": "blocker", "text": "Vendor escalation pending"}],
        TODAY.isoformat(), scope_label="Kore",
    )
    assert "patch_id=aaaa" in c and "vendor escalation yesterday" in c
    assert "Open items (candidates):" in c
    assert "(none" in build_completion_content("done", [], TODAY.isoformat())


def test_completion_parse_valid_match_and_evidence_cap():
    out = parse_completion_response(
        {"completed_patch_id": "aaaa", "evidence": "  we closed it  yesterday ", "reason": "r"}, IDS)
    assert out == ("aaaa", "we closed it yesterday")
    long_ev = {"completed_patch_id": "bbbb", "evidence": "x" * 500, "reason": "r"}
    pid, ev = parse_completion_response(long_ev, IDS)
    assert pid == "bbbb" and len(ev) == 300


def test_completion_parse_drops_null_hallucinated_and_garbage():
    assert parse_completion_response({"completed_patch_id": None, "evidence": "e"}, IDS) is None
    assert parse_completion_response({"completed_patch_id": "zzzz", "evidence": "e"}, IDS) is None
    assert parse_completion_response("no json", IDS) is None
    assert parse_completion_response(None, IDS) is None


def test_completion_parse_embedded_json():
    raw = 'ok {"completed_patch_id": "aaaa", "evidence": "user said done", "reason": "r"}'
    assert parse_completion_response(raw, IDS) == ("aaaa", "user said done")
