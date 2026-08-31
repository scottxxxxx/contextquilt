"""The extraction length gate, and the lane it must not take with it.

Scott ruled a 1200-character floor on 2026-08-31 after seeing the price:
$0.84 saved per 30 days against 4 real extractions and 5 stored patches.
An informed trade on a small number, which is exactly why the gate has
to cost only what it was priced at.

The placement matters more than the number. The behavior lane is a
SEPARATE call with its own 400-char floor, and an early return would
have killed it too across the 400 to 1200 band: measured, 18 behavior
patches over 8 meetings in 30 days. That is 4.6x the ruled cost, paid
silently. So the gated branch runs the behavior call and then returns,
and the test below pins that rather than trusting the comment.
"""

import os
from pathlib import Path

import pytest

from contextquilt.services.extraction_gate import (
    DEFAULT_MIN_TRANSCRIPT_CHARS,
    TOO_SHORT,
    min_transcript_chars,
    why_not_worth_extracting,
    worth_extracting,
)

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture(autouse=True)
def _clean_env():
    prior = os.environ.pop("CQ_EXTRACTION_MIN_CHARS", None)
    yield
    if prior is not None:
        os.environ["CQ_EXTRACTION_MIN_CHARS"] = prior
    else:
        os.environ.pop("CQ_EXTRACTION_MIN_CHARS", None)


def test_the_floor_is_the_ruled_number():
    assert DEFAULT_MIN_TRANSCRIPT_CHARS == 1200
    assert min_transcript_chars() == 1200


def test_below_the_floor_declines_with_a_reason_not_a_bare_no():
    # The reason is the whole point: a gate that reports only that it
    # declined cannot tell you which condition fired (#350).
    assert why_not_worth_extracting("x" * 1199) == TOO_SHORT


def test_at_and_above_the_floor_proceeds():
    assert why_not_worth_extracting("x" * 1200) is None
    assert why_not_worth_extracting("x" * 50000) is None


def test_the_boolean_is_derived_from_the_reason():
    # Two copies of one gate's conditions is the defect #350 fixed, so
    # these must not be able to drift.
    for n in (0, 400, 1199, 1200, 5000):
        text = "x" * n
        assert worth_extracting(text) is (why_not_worth_extracting(text) is None)


def test_an_empty_transcript_is_not_this_gate_s_business():
    # Existing empty handling owns that case; this gate answers exactly
    # one question so its log line means exactly one thing.
    assert why_not_worth_extracting("") is None
    assert why_not_worth_extracting(None) is None


def test_zero_disables_the_gate_entirely():
    # Reversible from config without a deploy, because the trade costs
    # real patches and whoever finds it losing ones that mattered should
    # be able to turn it off.
    os.environ["CQ_EXTRACTION_MIN_CHARS"] = "0"
    assert min_transcript_chars() == 0
    assert why_not_worth_extracting("x") is None


def test_the_floor_is_configurable():
    os.environ["CQ_EXTRACTION_MIN_CHARS"] = "900"
    assert min_transcript_chars() == 900
    assert why_not_worth_extracting("x" * 899) == TOO_SHORT
    assert why_not_worth_extracting("x" * 900) is None


@pytest.mark.parametrize("bad", ["", "   ", "abc", "-5", "1200.5"])
def test_a_bad_config_value_falls_back_rather_than_crashing_the_worker(bad):
    # A NameError or ValueError in any gathered worker loop crash-loops
    # the whole worker, so a typo in an env var must not be able to.
    os.environ["CQ_EXTRACTION_MIN_CHARS"] = bad
    assert min_transcript_chars() == DEFAULT_MIN_TRANSCRIPT_CHARS


def test_the_gated_branch_still_runs_the_behavior_lane():
    """The placement, pinned. This is the 18-patches-across-8-meetings test.

    Read as source because worker.py cannot be imported without asyncpg,
    which is the same constraint every other worker test here works
    under. It asserts the behavior call appears BETWEEN the skip log and
    the return, so an edit that turns the gated branch back into a bare
    return fails here rather than in production.
    """
    src = (SRC / "worker.py").read_text()
    start = src.index('"extraction_skipped"')
    end = src.index("response = await llm.extract(", start)
    branch = src[start:end]
    assert "_extract_behavior_observations" in branch, (
        "the gated branch no longer runs the behavior lane; that silently "
        "costs 4.6x what this gate was priced and ruled on"
    )
    assert branch.index("_extract_behavior_observations") < branch.index("return")


def test_the_gate_logs_its_reason_and_the_length():
    # A gate that declines silently has no instrument: "gated", "model
    # returned nothing" and "never ran" would be one observable.
    src = (SRC / "worker.py").read_text()
    start = src.index('"extraction_skipped"')
    # A window rather than a paren scan: the call contains nested calls
    # of its own, so slicing to the first ")" stops inside len(...) and
    # the test passes or fails for the wrong reason. It failed for that
    # wrong reason on the first run, which is the only thing that made
    # it visible.
    call = src[start:start + 400]
    for field in ("reason=", "chars=", "floor="):
        assert field in call, f"the decline log omits {field}"
