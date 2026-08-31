"""Unit tests for the correction service (contract item 9)."""

from datetime import date

import pytest

from src.contextquilt.services.corrections import (
    CORRECTION_SIMILARITY_FLOOR,
    CORRECTION_SYSTEM,
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
    assert matched == ["aaaa"]
    assert value["text"] == "Ship the release by August 15"
    assert value["deadline_date"] == "2026-08-15"
    assert value["owner"] == "Robin"
    assert value["_new_type"] == FALLBACK_PATCH_TYPE  # unused when matched


def test_hallucinated_patch_id_downgrades_to_unmatched():
    matched, value = parse_correction_response(_resp("zzzz", "Robin owns the rollout now"), IDS)
    assert matched == []
    assert value["text"] == "Robin owns the rollout now"


def test_null_match_uses_declared_type_when_valid():
    matched, value = parse_correction_response(
        _resp(None, "Robin owns the rollout now", patch_type="commitment"), IDS)
    assert matched == []
    assert value["_new_type"] == "commitment"
    matched, value = parse_correction_response(
        _resp(None, "Robin owns the rollout now", patch_type="not_a_type"), IDS)
    assert value["_new_type"] == FALLBACK_PATCH_TYPE


def test_embedded_json_and_garbage():
    raw = 'Sure: {"corrected_patch_id": "bbbb", "corrected_fact": {"text": "Deadline is now end of August"}, "reason": "r"}'
    matched, value = parse_correction_response(raw, IDS)
    assert matched == ["bbbb"]
    assert parse_correction_response("no json", IDS) is None
    assert parse_correction_response(None, IDS) is None
    assert parse_correction_response({"corrected_fact": {"text": "hi"}}, IDS) is None  # too short


def test_bad_deadline_date_dropped_not_fatal():
    matched, value = parse_correction_response(
        _resp("aaaa", "Ship it by the offsite", deadline="by the offsite",
              deadline_date="sometime soon"),
        IDS, meeting_date=TODAY,
    )
    assert matched == ["aaaa"]
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


# --------------------------------------------------------------------
# A false belief is recorded more than once
# --------------------------------------------------------------------

def test_every_contradicted_patch_is_returned_not_just_the_closest():
    """Steven Williams, 2026-08-31, and the case that forced this.

    The user wrote one sentence: he is not an attorney and neither is
    his mother. It contradicted five ACTIVE patches across four types,
    a person description, his mother's own person patch, a goal, a
    commitment, and the derived summary. The single-id contract could
    supersede at most one of them, so the correction landed, the user
    was told it had been applied, and the app went on asserting the
    same thing from the other four the next time he looked.
    """
    matched, value = parse_correction_response(
        {"corrected_patch_ids": ["aaaa", "bbbb", "cccc"],
         "corrected_fact": {"text": "Steven is not an attorney and neither is his mother"}},
        {"aaaa", "bbbb", "cccc", "dddd"},
    )
    assert matched == ["aaaa", "bbbb", "cccc"]


def test_one_hallucinated_id_does_not_discard_the_real_ones():
    # Dropping the whole list because one entry was invented would turn
    # a partly-good answer into an unmatched correction, which is the
    # failure this change exists to remove.
    matched, _ = parse_correction_response(
        {"corrected_patch_ids": ["aaaa", "ghost", "bbbb"],
         "corrected_fact": {"text": "He is not an attorney"}},
        {"aaaa", "bbbb"},
    )
    assert matched == ["aaaa", "bbbb"]


def test_order_is_preserved_because_the_first_match_decides_type_and_scope():
    # Keeping the single-match case byte-identical to the old behaviour
    # depends on this, so it is asserted rather than assumed.
    matched, _ = parse_correction_response(
        {"corrected_patch_ids": ["cccc", "aaaa"],
         "corrected_fact": {"text": "He is not an attorney"}},
        {"aaaa", "cccc"},
    )
    assert matched == ["cccc", "aaaa"]


def test_a_repeated_id_is_counted_once():
    # Archiving the same patch twice would stamp it twice and write two
    # identical `replaces` edges.
    matched, _ = parse_correction_response(
        {"corrected_patch_ids": ["aaaa", "aaaa"],
         "corrected_fact": {"text": "He is not an attorney"}},
        {"aaaa"},
    )
    assert matched == ["aaaa"]


def test_the_old_single_id_spelling_still_parses():
    """A model shown a cached prompt in the old shape must not break.

    The instruction asks for the plural now, but an in-flight or cached
    prompt carrying the singular is a real possibility and the cost of
    accepting both is two lines.
    """
    matched, _ = parse_correction_response(
        {"corrected_patch_id": "aaaa",
         "corrected_fact": {"text": "He is not an attorney"}},
        {"aaaa"},
    )
    assert matched == ["aaaa"]


@pytest.mark.parametrize("raw", [None, [], "", 42, {"a": 1}, [None, 7]])
def test_an_unusable_id_field_is_an_unmatched_correction_not_a_crash(raw):
    # Unmatched corrections still land, so the only wrong outcome here
    # is an exception that loses a user-stated fact entirely.
    matched, value = parse_correction_response(
        {"corrected_patch_ids": raw,
         "corrected_fact": {"text": "He is not an attorney"}},
        {"aaaa"},
    )
    assert matched == []
    assert value["text"] == "He is not an attorney"


def test_the_instruction_asks_for_every_one_rather_than_the_best():
    """The prompt has to say it, or the model returns its single best.

    Doc 19.3: an unstated field is an unemitted field. A list-shaped
    schema with a "which patch does this contradict" instruction gets a
    list of length one.
    """
    lowered = CORRECTION_SYSTEM.lower()
    assert "every one of them" in lowered
    assert "not just the closest" in lowered
    assert "corrected_patch_ids" in CORRECTION_SYSTEM


# --------------------------------------------------------------------
# The candidate set, which is the binding constraint
# --------------------------------------------------------------------

def _handle_correction_body() -> str:
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2] / "src" / "worker.py").read_text()
    body = src[src.index("async def handle_correction"):]
    return body[:body.index("async def handle_completion")]


def test_the_candidate_set_is_not_ranked_by_recency_alone():
    """The defect that survived making the reply plural.

    Scott wrote that Steven is not an attorney and neither is his
    mother. The five patches saying otherwise were four days old. He has
    4,058 ACTIVE patches, and the newest 60 held exactly ONE mentioning
    an attorney: a correction of his own from earlier the same day. So
    the model superseded a CORRECT patch and never saw a wrong one.

    Making the answer plural bought nothing, because the contradicted
    patches were never in the room. A bigger reply cannot fix a
    candidate set chosen on the wrong axis.
    """
    body = _handle_correction_body()
    assert "similarity(COALESCE(cp.value->>'text', ''), $2)" in body
    assert "subject_hits" in body


def test_the_subject_entity_the_route_has_always_sent_is_finally_read():
    """`subject_entity_id` was enqueued by the person card and ignored here.

    The signal that says WHO a correction is about was on the wire from
    the day the card shipped, and the matching stage never opened it.
    """
    body = _handle_correction_body()
    assert 'metadata.get("subject_entity_id")' in body
    assert "entity_aliases" in body, "aliases must count, or a rephrased name misses"


def test_the_ranking_puts_the_screen_first_then_the_subject():
    # What the user was literally looking at outranks everything; the
    # person the correction is about outranks a merely similar sentence;
    # recency is the tail rather than the axis.
    body = _handle_correction_body()
    order = body[body.index("candidates = ("):body.index("by_id = {")]
    assert "in_block + subject_hits + similar + recent" in order


def test_a_failed_subject_lookup_costs_relevance_not_the_correction():
    # A missing alias table or a bad id must not lose a user-stated fact.
    body = _handle_correction_body()
    # Sliced on the indented form: "name_rows = await self.db.fetch"
    # CONTAINS the unindented needle, so the naive slice ended before
    # the handler and this test failed for the wrong reason on its first
    # run. A substring that is a suffix of another name is its own small
    # instrument failure.
    block = body[body.index("subject_names: list = []"):
                 body.index("\n        rows = await self.db.fetch")]
    assert "except Exception" in block
    assert 'logger.warning("correction_subject_names_failed"' in block


def test_the_similarity_floor_is_below_the_dedup_gray_zone():
    """A denial and an assertion share fewer trigrams than two assertions.

    "Steven is not an attorney" against "Steven Williams: immigration
    attorney with domain expertise" is the shape this has to catch, and
    the dedup path's 0.35 would miss it by a mile.
    """
    assert CORRECTION_SIMILARITY_FLOOR < 0.35
    assert 0 < CORRECTION_SIMILARITY_FLOOR <= 0.15


def test_the_candidate_legs_are_logged_with_their_sizes():
    # "Superseded one of five" and "superseded one of one" are the same
    # line unless the legs are counted, and this whole class of bug was
    # invisible for exactly that reason.
    body = _handle_correction_body()
    assert 'logger.info("correction_candidates"' in body
    for field in ("in_block=", "subject=", "similar=", "scanned="):
        assert field in body
