"""The token endpoint refuses a repeatedly-wrong credential before hashing.

The executing tests below cover the decision, the key, the counter and the
fail-open posture. The source-read tests at the end cover the one property
that only exists in main.py: that the check runs BEFORE `verify_password`,
because a refusal that still pays the pbkdf2 cost does nothing (main.py
cannot be imported here: no fastapi, no asyncpg).
"""
import asyncio
import os
from pathlib import Path

import pytest

from contextquilt.services import auth_rate_limit as arl

CLIENT = "886a527b-1d8f-46e1-aadc-d4b05e16256e"


class FakeRedis:
    """Minimal INCR/EXPIRE/TTL/GET/DELETE, enough for the window logic."""

    def __init__(self, broken=False):
        self.values = {}
        self.ttls = {}
        self.broken = broken
        self.expire_calls = []

    async def get(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        v = self.values.get(key)
        return None if v is None else str(v)

    async def incr(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key, seconds):
        if self.broken:
            raise ConnectionError("redis down")
        self.expire_calls.append((key, seconds))
        self.ttls[key] = seconds
        return True

    async def ttl(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        return self.ttls.get(key, -1)

    async def delete(self, key):
        if self.broken:
            raise ConnectionError("redis down")
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return 1


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("CQ_AUTH_RATELIMIT_ENABLED", "CQ_AUTH_MAX_FAILURES",
                 "CQ_AUTH_FAILURE_WINDOW_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def test_the_limit_closes_at_the_limit_not_past_it():
    assert arl.is_refused(9, 10) is False
    assert arl.is_refused(10, 10) is True
    assert arl.is_refused(11, 10) is True


def test_the_key_is_hashed_so_a_hostile_client_id_cannot_shape_it():
    key = arl.key_for("a\nb: " + "x" * 9000)
    assert key.startswith(arl.KEY_PREFIX)
    assert len(key) == len(arl.KEY_PREFIX) + 32
    assert "\n" not in key and "x" * 20 not in key


def test_different_clients_get_different_keys_and_one_client_is_stable():
    assert arl.key_for(CLIENT) == arl.key_for(CLIENT)
    assert arl.key_for(CLIENT) != arl.key_for("930824d3")


def test_a_clean_client_is_allowed():
    r = FakeRedis()
    v = asyncio.run(arl.check(r, CLIENT))
    assert v["refused"] is False and v["failures"] == 0


def test_failures_accumulate_and_then_refuse(monkeypatch):
    monkeypatch.setenv("CQ_AUTH_MAX_FAILURES", "3")
    r = FakeRedis()

    async def run():
        for _ in range(2):
            await arl.record_failure(r, CLIENT)
        allowed = await arl.check(r, CLIENT)
        await arl.record_failure(r, CLIENT)
        return allowed, await arl.check(r, CLIENT)

    allowed, refused = asyncio.run(run())
    assert allowed["refused"] is False, "under the limit still mints"
    assert refused["refused"] is True and refused["failures"] == 3


def test_the_window_starts_at_the_first_failure_and_is_not_pushed_forward(monkeypatch):
    monkeypatch.setenv("CQ_AUTH_FAILURE_WINDOW_SECONDS", "900")
    r = FakeRedis()

    async def run():
        for _ in range(4):
            await arl.record_failure(r, CLIENT)

    asyncio.run(run())
    assert r.expire_calls == [(arl.key_for(CLIENT), 900)], \
        "a trickle of failures must not hold the key open forever"


def test_a_correct_credential_forgets_the_failures(monkeypatch):
    monkeypatch.setenv("CQ_AUTH_MAX_FAILURES", "2")
    r = FakeRedis()

    async def run():
        await arl.record_failure(r, CLIENT)
        await arl.record_failure(r, CLIENT)
        before = await arl.check(r, CLIENT)
        await arl.clear(r, CLIENT)
        return before, await arl.check(r, CLIENT)

    before, after = asyncio.run(run())
    assert before["refused"] is True
    assert after["refused"] is False and after["failures"] == 0


def test_retry_after_reports_the_remaining_window(monkeypatch):
    monkeypatch.setenv("CQ_AUTH_MAX_FAILURES", "1")
    monkeypatch.setenv("CQ_AUTH_FAILURE_WINDOW_SECONDS", "600")
    r = FakeRedis()

    async def run():
        await arl.record_failure(r, CLIENT)
        return await arl.check(r, CLIENT)

    v = asyncio.run(run())
    assert v["refused"] is True and v["retry_after"] == 600


def test_redis_down_allows_the_request(monkeypatch):
    """A closed limiter turns a cache outage into a total auth outage."""
    monkeypatch.setenv("CQ_AUTH_MAX_FAILURES", "1")
    r = FakeRedis(broken=True)

    async def run():
        await arl.record_failure(r, CLIENT)   # must not raise
        await arl.clear(r, CLIENT)            # must not raise
        return await arl.check(r, CLIENT)

    assert asyncio.run(run())["refused"] is False


def test_the_kill_switch_disables_counting_and_refusal(monkeypatch):
    monkeypatch.setenv("CQ_AUTH_RATELIMIT_ENABLED", "0")
    monkeypatch.setenv("CQ_AUTH_MAX_FAILURES", "1")
    r = FakeRedis()

    async def run():
        await arl.record_failure(r, CLIENT)
        await arl.record_failure(r, CLIENT)
        return await arl.check(r, CLIENT)

    assert asyncio.run(run())["refused"] is False
    assert r.values == {}, "disabled means no writes at all"


def test_a_malformed_knob_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("CQ_AUTH_MAX_FAILURES", "not a number")
    assert arl.max_failures() == arl.DEFAULT_MAX_FAILURES
    monkeypatch.setenv("CQ_AUTH_MAX_FAILURES", "0")
    assert arl.max_failures() == arl.DEFAULT_MAX_FAILURES, "0 would lock everyone out"


# --- source-read: the ORDER is the feature -------------------------------

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def _token_endpoint() -> str:
    start = MAIN.index('@app.post("/v1/auth/token"')
    return MAIN[start:MAIN.index('@app.get("/v1/auth/apps"')]


def test_main_actually_imports_the_module():
    """Written because the first version of this file did not. Every
    source-read test below passed while `auth_rate_limit` was an undefined
    name in main.py: they matched the call text, which is present either
    way, and the failure would have been a NameError on the first real
    token request. main.py cannot be imported here, so the import is
    asserted as text."""
    assert "from contextquilt.services import auth_rate_limit" in MAIN


def test_the_check_runs_before_the_password_hash():
    body = _token_endpoint()
    check_at = body.index("auth_rate_limit.check(")
    assert check_at < body.index("verify_password("), \
        "a refusal that still pays the pbkdf2 cost does nothing"
    assert check_at < body.index("db_pool.fetchrow("), \
        "and it should not pay the DB lookup either"


def test_a_refusal_is_429_with_retry_after():
    body = _token_endpoint()
    assert "HTTP_429_TOO_MANY_REQUESTS" in body
    assert "Retry-After" in body


def test_a_failure_is_counted_and_a_success_clears_it():
    body = _token_endpoint()
    assert "auth_rate_limit.record_failure(" in body
    assert "auth_rate_limit.clear(" in body
    assert body.index("auth_rate_limit.record_failure(") < body.index("auth_token_issued"), \
        "the count belongs in the rejection arm, not after a successful mint"
