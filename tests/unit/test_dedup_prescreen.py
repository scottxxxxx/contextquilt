"""The cheap first pass in front of the dedup judge.

The asymmetry is the whole design and it is what these tests pin: a missed
merge is recoverable, a wrong merge destroys a memory, so the band in the
middle escalates to the judge rather than being guessed either way.
"""
import asyncio
import re
from pathlib import Path

import pytest

from contextquilt.services import jev_client, semantic_dedup

WORKER = Path(__file__).resolve().parents[2] / "src" / "worker.py"


def _source_without_comments(path: Path) -> str:
    return "\n".join(
        re.sub(r"#.*$", "", line) for line in path.read_text().splitlines())


# ---------------------------------------------------------------- the triage
def test_a_confident_same_fact_merges():
    assert semantic_dedup.triage(0.91) is True


def test_a_confident_different_fact_inserts():
    assert semantic_dedup.triage(0.02) is False


@pytest.mark.parametrize("noul", [0.31, 0.5, 0.55, 0.69])
def test_the_middle_escalates_rather_than_guessing(noul):
    """The measured fixture had a true pair stable at 0.50 to 0.55. Deciding
    that by threshold is a coin flip on a destructive operation."""
    assert semantic_dedup.triage(noul) is None


def test_no_answer_escalates():
    assert semantic_dedup.triage(None) is None


def test_the_floors_leave_the_measured_margin():
    """Nearest negative measured 0.15, nearest confident positive 0.71."""
    assert semantic_dedup.merge_floor() == 0.70
    assert semantic_dedup.insert_ceiling() == 0.30
    assert semantic_dedup.insert_ceiling() < semantic_dedup.merge_floor()


def test_the_boundaries_are_inclusive_on_the_safe_side():
    assert semantic_dedup.triage(0.70) is True
    assert semantic_dedup.triage(0.30) is False


# ---------------------------------------------------------------- the run
def _ask_returning(mapping):
    async def ask(state, questions, **kwargs):
        key = (state["statement_a"], state["statement_b"])
        return {"same_fact": {"type": "noul", "noul": mapping[key]}}
    return ask


def test_prescreen_maps_each_pair_in_order(monkeypatch):
    pairs = [("a", "b"), ("c", "d"), ("e", "f")]
    monkeypatch.setattr(jev_client, "api_key", lambda: "k")
    monkeypatch.setattr(jev_client, "ask", _ask_returning(
        {("a", "b"): 0.95, ("c", "d"): 0.03, ("e", "f"): 0.52}))
    assert asyncio.run(semantic_dedup.prescreen(pairs)) == [True, False, None]


def test_no_key_escalates_everything(monkeypatch):
    monkeypatch.setattr(jev_client, "api_key", lambda: "")
    assert asyncio.run(semantic_dedup.prescreen([("a", "b")])) == [None]


def test_the_kill_switch_escalates_everything(monkeypatch):
    monkeypatch.setenv("CQ_JEV_ENABLED", "0")
    monkeypatch.setattr(jev_client, "api_key", lambda: "k")
    assert asyncio.run(semantic_dedup.prescreen([("a", "b")])) == [None]


def test_a_raising_scorer_escalates_rather_than_merging(monkeypatch):
    """An exception object is truthy. Read as a verdict it would MERGE,
    which is the one outcome that loses data."""
    async def boom(*args, **kwargs):
        raise RuntimeError("typesafe is down")
    monkeypatch.setattr(jev_client, "api_key", lambda: "k")
    monkeypatch.setattr(jev_client, "ask", boom)
    assert asyncio.run(semantic_dedup.prescreen([("a", "b")])) == [None]


def test_no_pairs_costs_nothing(monkeypatch):
    def explode():
        raise AssertionError("must not even look for a key")
    monkeypatch.setattr(jev_client, "api_key", explode)
    assert asyncio.run(semantic_dedup.prescreen([])) == []


# ---------------------------------------------------------------- the wiring
def test_the_worker_prescreens_before_it_calls_the_judge():
    source = _source_without_comments(WORKER)
    assert "await prescreen_dedup_pairs(pairs)" in source
    at = source.index("await prescreen_dedup_pairs(pairs)")
    judge = source.index("system_prompt=DEDUP_JUDGE_SYSTEM")
    assert at < judge, "the cheap pass must run first or it saves nothing"


def test_only_the_escalated_pairs_reach_the_judge():
    """Sending every pair anyway would keep the full cost while adding one."""
    source = _source_without_comments(WORKER)
    assert "[pairs[i] for i in escalate]" in source


def test_an_escalation_that_fails_inserts_rather_than_merges():
    source = _source_without_comments(WORKER)
    assert "verdicts = [v is True for v in prescreened]" in source
