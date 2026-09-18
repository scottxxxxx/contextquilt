"""A malformed client_id is an ordinary credential rejection, and counts.

`applications.app_id` is a `uuid` column, so a client_id that is not
UUID-shaped made asyncpg raise `DataError` INSIDE the fetch. The token
endpoint's outer arm turned that into the same 401 a wrong secret gets,
which was fine for the caller and wrong for everything else:
`verify_password` was never reached, `record_failure` never ran, and the
attempt entered no counter at all.

Not an amplification hole, because no pbkdf2 is paid on that path. A
SILENT one: a credential failure to the caller, a backend error in the
logs, counted nowhere.

Found by a smoke test that used `cq-smoke-20260916` as a scratch id,
took this path, returned exactly the 401 predicted, and proved nothing
about the limiter it was written to test.

main.py cannot be imported here (no fastapi, no asyncpg), so the decision
is executed and the wiring is read from source.
"""
from pathlib import Path

from contextquilt.services.client_id import is_uuid_shaped

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def _code_only(text: str) -> str:
    """Comment lines stripped: main.py explains this bug in prose that
    names the very strings these assertions look for."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _token_endpoint() -> str:
    code = _code_only(MAIN)
    start = code.index('@app.post("/v1/auth/token"')
    return code[start:code.index('@app.get("/v1/auth/apps"', start)]


# --- the decision, executed ----------------------------------------------

def test_a_real_app_id_is_accepted():
    assert is_uuid_shaped("886a527b-1d8f-46e1-aadc-d4b05e16256e") is True
    assert is_uuid_shaped("930824d3-2ccb-4869-b3f0-0ed2693f183f") is True


def test_the_check_is_not_stricter_than_the_column():
    """The database accepts these forms, so refusing them here would
    reject a credential that would otherwise have worked. A guard that is
    stricter than the thing it guards is an outage waiting for the first
    caller who uses a legal spelling."""
    # ShoulderSurf's app id with the hyphens removed, as a plain literal.
    # Two earlier drafts built this string with .replace() calls; the
    # first produced a DIFFERENT uuid, so the assertion passed while
    # demonstrating nothing about the id it sits beside. Verified:
    # uuid.UUID(this) == uuid.UUID("886a527b-1d8f-46e1-aadc-d4b05e16256e").
    assert is_uuid_shaped("886a527b1d8f46e1aadcd4b05e16256e") is True
    assert is_uuid_shaped("{886a527b-1d8f-46e1-aadc-d4b05e16256e}") is True
    assert is_uuid_shaped("urn:uuid:886a527b-1d8f-46e1-aadc-d4b05e16256e") is True


def test_the_two_ids_that_actually_caused_this():
    """The smoke-test scratch id, and GhostPour's code default for
    cq_app_id, which is overridden today and would take this path if the
    env var were ever absent."""
    assert is_uuid_shaped("cq-smoke-20260916") is False
    assert is_uuid_shaped("cloudzap") is False


def test_empty_and_non_string_are_refused_without_raising():
    for value in ("", "   ", None, 12345, b"886a527b", ["x"]):
        assert is_uuid_shaped(value) is False


# --- the wiring, read from source ----------------------------------------

def test_main_imports_and_ASKS_the_helper():
    """Written deliberately: a helper that exists, works and is tested
    while nothing calls it is this codebase's recurring failure. A
    sabotage yesterday reverted exactly such a call site and turned
    nothing red."""
    assert "from contextquilt.services.client_id import is_uuid_shaped" in _code_only(MAIN)
    assert "if not is_uuid_shaped(form_data.username):" in _token_endpoint()


def test_the_shape_check_sits_between_the_limiter_and_the_database():
    """Order is the feature. Before the fetch, so it costs no round trip;
    after the limiter, so a client already over the threshold is refused
    with 429 rather than told its id is malformed."""
    body = _token_endpoint()
    assert body.index("auth_rate_limit.check(") < body.index("is_uuid_shaped("), \
        "the limiter must still run first"
    assert body.index("is_uuid_shaped(") < body.index("db_pool.fetchrow("), \
        "a malformed id must not reach the database"


def test_a_malformed_id_is_counted_like_any_other_failure():
    """The entire point. Previously this attempt entered no counter."""
    body = _token_endpoint()
    branch = body[body.index("if not is_uuid_shaped("):]
    branch = branch[:branch.index("db_pool.fetchrow(")]
    assert "auth_rate_limit.record_failure(" in branch, \
        "a malformed id must enter the failure counter"
    assert 'reason="malformed_client_id"' in branch, \
        "and must be distinguishable in the log from unknown_app"
    assert "HTTP_401_UNAUTHORIZED" in branch


def test_the_caller_cannot_tell_it_apart_from_a_wrong_secret():
    """Deliberate: the response body must not become an oracle for which
    app ids exist or what shape they take. Same detail as every other
    rejection on this endpoint."""
    body = _token_endpoint()
    branch = body[body.index("if not is_uuid_shaped("):]
    branch = branch[:branch.index("db_pool.fetchrow(")]
    assert 'detail="Incorrect client_id or client_secret"' in branch
