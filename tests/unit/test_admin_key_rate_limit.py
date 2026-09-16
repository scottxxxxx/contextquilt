"""Guessing CQ_ADMIN_KEY costs something now, and the counter cannot be aimed.

Every admin-gated route answers 403 or 200, so every one is an oracle for
the single long-lived operator key; `GET /api/dashboard/verify-key` is
just the politest, being unauthenticated by design so the dashboard login
can check a typed key. GhostPour's edge IP-gates two literal prefixes
(`cz/admin`, `cq/dashboard`) and CQ mounts the dashboard API at
`/api/dashboard/`, which that prefix never matches, on a host CQ does not
distinguish from any other. So the oracle was internet-reachable with no
limit (2026-09-16).

The counter lives in the check every admin route shares, not on one
endpoint: limiting verify-key alone would move the guessing to
/api/dashboard/stats.

THE SOURCE IDENTITY IS THE DELICATE PART. A counter keyed on a header
anybody can set is worse than no counter: it can be rotated to evade, and
it can be AIMED at the operator's address to lock them out of the
dashboard, which is the denial of service this design is supposed to
avoid. GhostPour reaches CQ container to container without passing
through the proxy at all, so "no forwarding header" is a normal case, not
an anomaly.

api_deps imports fastapi, absent from the local unit venv, so the wiring
is asserted as source and the decisions are executed.
"""
import asyncio
from pathlib import Path

from contextquilt.services import auth_rate_limit as arl
from contextquilt.services.admin_auth import (
    GLOBAL_BUCKET,
    resolve_source,
    resolve_trusted,
    trusted_proxies,
)

DEPS_PATH = Path(__file__).resolve().parents[2] / "src" / "contextquilt" / "api_deps.py"
DEPS = DEPS_PATH.read_text()
MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()
ADMIN_PREFIX = "admin_fail:"
PROXY = "172.18.0.9"
TRUSTED = frozenset({PROXY})


class FakeRedis:
    def __init__(self):
        self.values, self.ttls = {}, {}

    async def get(self, key):
        v = self.values.get(key)
        return None if v is None else str(v)

    async def incr(self, key):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def expire(self, key, seconds):
        self.ttls[key] = seconds
        return True

    async def ttl(self, key):
        return self.ttls.get(key, -1)

    async def delete(self, key):
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return 1


# --- the source identity, executed ---------------------------------------

def test_an_untrusted_peer_is_counted_as_itself():
    """GP reaches CQ container to container with no forwarding header. That
    is normal traffic, and anything else on that network could send one.

    The headers here are the point. An earlier version of this test passed
    None for both, so trusted and untrusted peers produced the same answer
    and it never exercised the distinction in its own name: a sabotage that
    believed headers from ANY peer left it green.
    """
    assert resolve_source("172.18.0.4", None, None, TRUSTED) == "172.18.0.4"
    assert resolve_source("172.18.0.4", "203.0.113.7", "203.0.113.7",
                          TRUSTED) == "172.18.0.4"


def test_a_spoofed_header_cannot_poison_another_bucket():
    """The attack this rules out: claim the operator's address so THEIR
    counter trips and the dashboard login locks them out."""
    victim = "203.0.113.7"
    resolved = resolve_source("172.18.0.66", victim, f"{victim}", TRUSTED)
    assert resolved == "172.18.0.66", "a non-proxy peer chose its own bucket"
    assert resolved != victim


def test_a_trusted_proxy_supplies_the_real_ip():
    """X-Real-IP is set from $remote_addr, replacing anything the client
    sent, so from the proxy it is a single trustworthy value."""
    assert resolve_source(PROXY, "203.0.113.7", "9.9.9.9", TRUSTED) == "203.0.113.7"


def test_a_trusted_proxy_falls_back_to_the_last_forwarded_entry():
    """X-Forwarded-For is APPENDED to a client-supplied value, so only the
    final element was observed by the proxy."""
    assert resolve_source(PROXY, None, "9.9.9.9, 203.0.113.7", TRUSTED) == "203.0.113.7"


def test_a_trusted_proxy_with_no_headers_counts_as_the_proxy():
    assert resolve_source(PROXY, None, None, TRUSTED) == PROXY


def test_no_peer_and_no_headers_share_one_bucket():
    assert resolve_source(None, None, None, TRUSTED) == GLOBAL_BUCKET
    assert resolve_source("", "", "", TRUSTED) == GLOBAL_BUCKET


def test_the_default_configuration_never_believes_a_header():
    """CQ_TRUSTED_PROXY_IPS is empty by default: until an operator names
    the proxy, every caller is counted by the address CQ actually sees."""
    assert resolve_source(PROXY, "203.0.113.7", None, frozenset()) == PROXY


def test_the_trusted_list_parses_and_defaults_empty():
    assert trusted_proxies(None) == frozenset()
    assert trusted_proxies("") == frozenset()
    assert trusted_proxies(" 172.18.0.9 , 10.0.0.2 ,, ") == frozenset(
        {"172.18.0.9", "10.0.0.2"})


def test_an_address_literal_passes_through_without_a_lookup():
    looked_up = []

    def resolver(name):
        looked_up.append(name)
        return ["should not be called"]

    assert resolve_trusted(frozenset({"172.18.0.9"}), resolver) == frozenset({"172.18.0.9"})
    assert looked_up == []


def test_a_name_expands_to_the_addresses_it_resolves_to():
    """The proxy's docker address is assigned from the subnet pool at
    container start (IPAM nil), so a literal is correct only until
    something restarts in a different order."""
    resolved = resolve_trusted(
        frozenset({"project-bifrost-app-1"}),
        lambda name: ["172.18.0.9", "172.18.0.11"])
    assert resolved == frozenset({"172.18.0.9", "172.18.0.11"})


def test_a_name_that_does_not_resolve_is_dropped_not_raised():
    """Failing shut: an unresolvable entry means that header is not
    believed, which is the safe direction."""
    def angry(name):
        raise OSError("no such host")

    assert resolve_trusted(frozenset({"nope"}), angry) == frozenset()


def test_a_mixed_set_keeps_the_literal_and_the_resolved_name():
    resolved = resolve_trusted(
        frozenset({"10.0.0.2", "bifrost"}), lambda name: ["172.18.0.9"])
    assert resolved == frozenset({"10.0.0.2", "172.18.0.9"})


# --- the buckets, executed ------------------------------------------------

def test_admin_failures_do_not_consume_the_app_credential_counter():
    """Two populations, two thresholds. One locking out the other would be
    a surprise nobody would find quickly."""
    r = FakeRedis()

    async def run():
        for _ in range(5):
            await arl.record_failure(r, "1.2.3.4", prefix=ADMIN_PREFIX, window=900)
        return (await arl.check(r, "1.2.3.4", limit=3),
                await arl.check(r, "1.2.3.4", prefix=ADMIN_PREFIX, limit=3))

    app_side, admin_side = asyncio.run(run())
    assert app_side["failures"] == 0, "admin attempts leaked into the app bucket"
    assert admin_side["refused"] is True


def test_the_per_source_and_global_buckets_are_separate():
    r = FakeRedis()

    async def run():
        for _ in range(3):
            await arl.record_failure(r, "1.2.3.4", prefix=ADMIN_PREFIX, window=900)
        return (await arl.check(r, "1.2.3.4", prefix=ADMIN_PREFIX, limit=3),
                await arl.check(r, GLOBAL_BUCKET, prefix=ADMIN_PREFIX, limit=3))

    per_source, global_bucket = asyncio.run(run())
    assert per_source["refused"] is True
    assert global_bucket["failures"] == 0


def test_an_explicit_window_is_used_for_the_expiry():
    r = FakeRedis()
    asyncio.run(arl.record_failure(r, GLOBAL_BUCKET, prefix=ADMIN_PREFIX, window=3600))
    assert r.ttls[arl.key_for(GLOBAL_BUCKET, ADMIN_PREFIX)] == 3600


# --- the wiring, read from source ----------------------------------------

def test_both_buckets_are_checked_before_the_key_is_compared():
    """A refusal that still compares keys would leave the oracle intact."""
    body = DEPS[DEPS.index("async def verify_admin_key"):]
    assert body.index("auth_rate_limit.check(") < body.index("is_authorized("), \
        "the limiter must run before the comparison"
    assert "(source, per_source_limit, per_source_window)" in body
    assert "(GLOBAL_BUCKET, global_limit, global_window)" in body


def test_the_identity_comes_from_the_socket_peer_not_a_header_alone():
    body = DEPS[DEPS.index("async def verify_admin_key"):]
    assert "request.client.host" in body
    assert "resolve_source(peer," in body
    assert "current_trusted()" in body, "the resolved set must be the one consulted"


def test_the_resolved_trusted_set_is_logged_so_staleness_is_visible():
    """The proxy's address is not pinned. A stale literal does not error, it
    stops matching, and every edge request quietly shares one bucket."""
    assert "admin_trusted_proxies_resolved" in DEPS
    assert "NONE (no forwarding header will be believed)" in DEPS


def test_a_refusal_is_429_with_retry_after():
    assert "HTTP_429_TOO_MANY_REQUESTS" in DEPS
    assert "Retry-After" in DEPS


def test_a_failure_is_counted_in_both_buckets():
    body = DEPS[DEPS.index("if not is_authorized("):]
    counted = body[:body.index("raise HTTPException")]
    assert counted.count("auth_rate_limit.record_failure(") == 2


def test_success_clears_only_the_source_bucket():
    """One operator typing the right key must not reset a distributed
    campaign's budget."""
    tail = DEPS[DEPS.rindex("auth_rate_limit.clear("):]
    assert "source" in tail.split(")")[0]
    assert "GLOBAL_BUCKET" not in tail.split(")")[0]


def test_it_is_inert_without_redis():
    """Fail open: a cache hiccup must not lock every admin surface."""
    assert DEPS.count("if _redis is not None:") >= 2
    assert "def bind_redis(" in DEPS


def test_dev_mode_counts_nothing():
    body = DEPS[DEPS.index("async def verify_admin_key"):]
    guard = body[:body.index("peer = request.client.host")]
    assert "if not configured:" in guard and "return" in guard


def test_main_binds_the_client_and_imports_the_module():
    """main.py referenced api_deps.bind_redis while importing only the name
    verify_admin_key, which is a NameError at import and would not start the
    app. Same shape as the missing import an hour earlier, so it is pinned."""
    assert "from contextquilt import api_deps" in MAIN
    assert "api_deps.bind_redis(redis_client)" in MAIN
    assert MAIN.index("from contextquilt import api_deps") < MAIN.index("api_deps.bind_redis(")
