"""Guardrail 12b's second carrier: the decision, and that it is CALLED.

The call-site tests are not ceremony. Twice now a helper has been written,
unit tested and never wired (`api_deps.bind_redis`, `should_announce_trusted`),
and both times reverting the call site turned nothing red. A rule with two
carriers wired to one is the exact shape that let this lane run unsanitized
for weeks, so the wiring is asserted here as well as the logic.
"""
import asyncio
import re
from pathlib import Path

import pytest

from contextquilt.services import character_check, jev_client

WORKER = Path(__file__).resolve().parents[2] / "src" / "worker.py"


def _source_without_comments(path: Path) -> str:
    """Comments are not code. A call named only in a comment must not pass."""
    return "\n".join(
        re.sub(r"#.*$", "", line) for line in path.read_text().splitlines())


# ---------------------------------------------------------------- the decision
def test_a_confident_character_verdict_drops_the_row():
    assert character_check.verdict(0.78) is True


def test_confident_conduct_keeps_the_row():
    assert character_check.verdict(0.03) is False


@pytest.mark.parametrize("noul", [0.45, 0.5, 0.55])
def test_the_undecided_band_keeps_the_row(noul):
    """0.5 means yes and no are equally likely. Rounding that up would
    delete a memory on a coin flip."""
    assert character_check.verdict(noul) is False


def test_no_answer_keeps_the_row():
    assert character_check.verdict(None) is False


# ---------------------------------------------------------------- the shape
def test_only_conduct_types_are_checked():
    assert character_check.text_of(
        {"type": "moment", "value": {"text": "Asked for the breakdown"}})
    assert character_check.text_of(
        {"type": "commitment", "value": {"text": "Send the deck"}}) is None


def test_a_row_with_no_text_is_not_checked():
    assert character_check.text_of({"type": "moment", "value": {}}) is None
    assert character_check.text_of({"type": "moment", "value": {"text": "  "}}) is None


def test_a_malformed_answer_reads_as_no_answer():
    assert jev_client.noul_of(None, "is_character") is None
    assert jev_client.noul_of({"is_character": {}}, "is_character") is None
    assert jev_client.noul_of({"is_character": {"noul": "0.9"}}, "is_character") is None
    assert jev_client.noul_of({"is_character": {"noul": 0.9}}, "is_character") == 0.9


def test_a_boolean_is_not_a_probability():
    """True is an int in Python and would read as a confident 1.0."""
    assert jev_client.noul_of({"is_character": {"noul": True}}, "is_character") is None


# ---------------------------------------------------------------- the run
def _fake_ask(mapping):
    async def ask(state, questions, **kwargs):
        return {"is_character": {"type": "noul",
                                 "noul": mapping[state["observation"]]}}
    return ask


def test_screen_drops_only_what_the_scorer_flagged(monkeypatch):
    patches = [
        {"type": "moment", "value": {"text": "conduct"}},
        {"type": "moment", "value": {"text": "verdict"}},
        {"type": "commitment", "value": {"text": "never checked"}},
    ]
    monkeypatch.setattr(jev_client, "ask",
                        _fake_ask({"conduct": 0.04, "verdict": 0.81}))
    kept, dropped = asyncio.run(character_check.screen(patches))
    assert [p["value"]["text"] for p in kept] == ["conduct", "never checked"]
    assert [p["value"]["text"] for p in dropped] == ["verdict"]


def test_the_kill_switch_keeps_everything(monkeypatch):
    monkeypatch.setenv("CQ_JEV_CHARACTER_ENABLED", "0")
    monkeypatch.setattr(jev_client, "ask", _fake_ask({"verdict": 0.99}))
    patches = [{"type": "moment", "value": {"text": "verdict"}}]
    kept, dropped = asyncio.run(character_check.screen(patches))
    assert kept == patches and dropped == []


def test_a_raising_scorer_keeps_everything(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("typesafe is down")
    monkeypatch.setattr(jev_client, "ask", boom)
    patches = [{"type": "moment", "value": {"text": "verdict"}}]
    kept, dropped = asyncio.run(character_check.screen(patches))
    assert kept == patches and dropped == []


def test_apply_records_the_drop_audibly(monkeypatch):
    monkeypatch.setattr(jev_client, "ask", _fake_ask({"verdict": 0.9}))
    content = {"patches": [{"type": "moment", "value": {"text": "verdict"}}]}
    result = asyncio.run(character_check.apply(content))
    assert result["patches"] == []
    assert result["_character_check_dropped"]["count"] == 1
    assert result["_character_check_dropped"]["texts"] == ["verdict"]


# ---------------------------------------------------------------- the wiring
def test_the_worker_actually_calls_the_check_in_BOTH_lanes():
    source = _source_without_comments(WORKER)
    calls = source.count("await character_check.apply(")
    assert calls == 2, f"expected both lanes wired, found {calls}"


def _apply_calls(source: str) -> list:
    """The text of each `character_check.apply(...)` call, and nothing else.

    Anchored on the call because the first version of these tests searched
    the WHOLE FILE for `lane="behavior_observations"`, which already appears
    in that lane's existing log line. Deleting the call left both tests
    green. A test that cannot fail on the bug it was written for is
    decoration, so the match now starts at the call site.

    A fixed window rather than a paren match: the first `)` after the call
    lands inside `str(origin_id or "")`, which cut the slice before the
    `lane=` kwarg and made this read as "no lanes named" on a correct tree.
    """
    return [source[at:at + 240]
            for at in (m.start() for m in
                       re.finditer(r"await character_check\.apply\(", source))]


def test_both_lanes_are_named_so_a_drop_can_be_attributed():
    calls = _apply_calls(_source_without_comments(WORKER))
    named = {lane for call in calls
             for lane in ("meeting_summary", "behavior_observations")
             if f'lane="{lane}"' in call}
    assert named == {"meeting_summary", "behavior_observations"}


def test_the_check_runs_AFTER_the_denylist_in_both_lanes():
    """Order is the design: the free list goes first and this reads what
    survives. Swapped, every row would cost a network call."""
    source = _source_without_comments(WORKER)
    for at in (m.start() for m in
               re.finditer(r"await character_check\.apply\(", source)):
        before = source[:at]
        assert "sanitize_behavior_observations(" in before
        # ...and the nearest one, not merely some earlier one in the file.
        assert at - before.rindex("sanitize_behavior_observations(") < 900
