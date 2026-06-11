"""Unit tests for the semantic dedup judge plumbing (pure parts).

The DB flow in worker.store_connected_patches follows the repo's
post-deploy-smoke convention; these pin the prompt/schema/parsing
contract the worker and the audit script both depend on.
"""

from src.contextquilt.services.semantic_dedup import (
    DEDUP_JUDGE_SCHEMA,
    DEDUP_JUDGE_SYSTEM,
    MAX_JUDGE_PAIRS,
    SEMANTIC_DEDUP_FLOOR,
    TRIGRAM_DEDUP_THRESHOLD,
    build_dedup_judge_content,
    parse_dedup_verdicts,
)


def test_band_is_sane():
    assert 0 < SEMANTIC_DEDUP_FLOOR < TRIGRAM_DEDUP_THRESHOLD < 1
    assert MAX_JUDGE_PAIRS >= 12  # extraction caps at 12 patches/meeting


def test_system_prompt_defaults_to_not_merging():
    # The asymmetry matters: a missed merge is recoverable, a wrong
    # merge silently loses a memory.
    assert "FALSE whenever you are unsure" in DEDUP_JUDGE_SYSTEM


def test_system_prompt_demands_raw_json_shape():
    # The Anthropic client does NOT enforce json_schema on the wire —
    # the system prompt itself must demand the exact JSON shape, or the
    # model answers in prose and every verdict silently defaults False
    # (exactly what the first prod dry-run produced: 0 of 50 merged).
    assert '{"verdicts":' in DEDUP_JUDGE_SYSTEM
    assert "ONLY a JSON object" in DEDUP_JUDGE_SYSTEM


def test_schema_is_strict():
    assert DEDUP_JUDGE_SCHEMA["additionalProperties"] is False
    item = DEDUP_JUDGE_SCHEMA["properties"]["verdicts"]["items"]
    assert set(item["required"]) == {"pair", "same_fact"}
    assert item["additionalProperties"] is False


def test_content_numbers_pairs_and_truncates():
    pairs = [("Deploy API by EOW", "Ship API before end of week"),
             ("x" * 500, "line\nbreak")]
    content = build_dedup_judge_content(pairs)
    assert "PAIR 0:" in content and "PAIR 1:" in content
    assert "Deploy API by EOW" in content
    assert "x" * 300 in content and "x" * 301 not in content  # 300-char cap
    assert "line break" in content  # newlines flattened


def test_parse_happy_path():
    content = {"verdicts": [{"pair": 0, "same_fact": True}, {"pair": 1, "same_fact": False}]}
    assert parse_dedup_verdicts(content, 2) == [True, False]


def test_parse_defaults_false_on_garbage():
    assert parse_dedup_verdicts(None, 2) == [False, False]
    assert parse_dedup_verdicts("not a dict", 2) == [False, False]
    assert parse_dedup_verdicts({}, 2) == [False, False]
    assert parse_dedup_verdicts({"verdicts": "nope"}, 1) == [False]


def test_parse_ignores_out_of_range_and_nonbool():
    content = {"verdicts": [
        {"pair": 5, "same_fact": True},      # out of range
        {"pair": -1, "same_fact": True},     # out of range
        {"pair": 0, "same_fact": "yes"},     # non-bool → ignored
        {"pair": 1, "same_fact": True},      # valid
        "not a dict",
    ]}
    assert parse_dedup_verdicts(content, 2) == [False, True]


def test_parse_missing_pairs_default_false():
    content = {"verdicts": [{"pair": 2, "same_fact": True}]}
    assert parse_dedup_verdicts(content, 4) == [False, False, True, False]
