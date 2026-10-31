"""The worker stores a replacing word correction and does not extract it.

The EXECUTING sibling of the source-reading placement test in
test_term_replace.py (doc 19.12): the real `ColdPathWorker.process_task`
is run against a stub `self` that records every lane it reaches.
"""

import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("asyncpg", MagicMock())

import worker  # noqa: E402


class Recorder:
    """Every attribute is an async method that records it was called."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def method(*args, **kwargs):
            self.calls.append(name)
            return None
        return method


def _payload(**extra):
    p = {"interaction_type": "meeting_transcript", "user_id": "u1",
         "app_id": "a1", "content": "ask Kore.ai",
         "metadata": {"origin_id": "m1", "origin_type": "meeting"}}
    p.update(extra)
    return p


@pytest.mark.asyncio
async def test_a_replacing_correction_reaches_no_lane():
    stub = Recorder()
    await worker.ColdPathWorker.process_task(
        stub, _payload(recovery="word-correction", replaced_entries=1))
    assert stub.calls == []


@pytest.mark.asyncio
async def test_a_correction_that_replaced_nothing_extracts():
    """No earlier entry: the meeting's first delivery, it must extract."""
    stub = Recorder()
    stub.redis = MagicMock()

    async def llen(_key):
        return 0
    stub.redis.llen = llen
    await worker.ColdPathWorker.process_task(
        stub, _payload(recovery="word-correction"))
    assert "handle_meeting_summary" in stub.calls


@pytest.mark.asyncio
async def test_an_ordinary_transcript_still_extracts():
    stub = Recorder()
    stub.redis = MagicMock()

    async def llen(_key):
        return 0
    stub.redis.llen = llen
    await worker.ColdPathWorker.process_task(stub, _payload())
    assert "handle_meeting_summary" in stub.calls
