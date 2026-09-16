"""Guard: auth failures are observable, because nowhere else can see them.

GhostPour translates a CQ 401 into a 502 at its edge, deliberately: a CQ auth
failure is their misconfiguration rather than the caller's, so it should read
as their fault downstream. The consequence is that **CQ is the only place a
wrong credential is identifiable as a wrong credential.** Downstream sees a
gateway error and cannot tell a bad secret from CQ being down.

That matters most during a credential cutover, which is exactly when the
question "is the secret wrong?" needs answering in seconds rather than by
elimination.

Source-level, like `test_connection_status_filter.py`: the logging is inside
FastAPI request handlers with no local test harness for them, so this asserts
the call sites exist and that the secret never reaches a log line.
"""

import pathlib
import re

MAIN = (pathlib.Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def _token_endpoint() -> str:
    """Slice to the NEXT route, not to a character count.

    This read `MAIN[start:start + 3200]` until 2026-09-16, when adding the
    rate-limit guard pushed `auth_token_backend_error` and one of the two
    401s past 3200 characters. Two tests went red for a reason that had
    nothing to do with what they assert: a fixed-length window is a
    boundary with no structural meaning, so every edit inside the function
    silently changes what the tests can see. Same family as slicing a
    source file on a delimiter that also appears somewhere else.
    """
    start = MAIN.index('@app.post("/v1/auth/token"')
    return MAIN[start:MAIN.index('@app.get("/v1/auth/apps"', start)]


def test_a_rejected_credential_is_logged():
    assert "auth_token_rejected" in _token_endpoint()


def test_the_log_says_WHICH_failure_it_was():
    """"unknown app" and "wrong secret for a known app" are different
    problems during a cutover: one means the id never landed, the other means
    the secret did not travel intact."""
    body = _token_endpoint()
    assert "unknown_app" in body
    assert "secret_mismatch" in body


def test_a_backend_error_is_distinguishable_from_a_bad_credential():
    """The known defect this compensates for: the outer arm turns ANY failure
    into a credential error, so a database outage presents as "Incorrect
    client_id or client_secret". Until that is fixed, the log line is what
    tells the two apart."""
    body = _token_endpoint()
    assert "auth_token_backend_error" in body
    assert "error_type" in body


def test_a_successful_exchange_is_logged_too():
    """Silence has to mean something. Without a success line, "no rejection
    logged" cannot be told from "no request arrived", which is the exact
    ambiguity the gateway's 502 already creates downstream."""
    assert "auth_token_issued" in _token_endpoint()


def test_the_submitted_secret_is_never_logged():
    """The one thing that must never appear. `form_data.password` IS the
    client secret."""
    body = _token_endpoint()
    calls = re.findall(r"logger\.\w+\((.*?)\n\s*\)", body, re.S)
    assert calls, "no logger calls found; the anchor moved"
    for args in calls:
        assert "password" not in args, (
            f"a log call references the submitted secret: {args[:120]}"
        )


def test_bearer_rejections_are_logged_as_well():
    """A bad or expired token on a normal request has the same invisibility
    problem as a bad secret on the token endpoint."""
    assert "auth_bearer_rejected" in MAIN


def test_the_credential_paths_still_answer_401():
    """Originally "logging did not change the status codes": that change
    shipped hours before a credential cutover and was observability only.

    One code has been added since, on purpose: 429 when a client_id keeps
    failing (2026-09-16, services/auth_rate_limit). That is a new REFUSAL
    of a caller already failing, not a change to what a wrong or right
    credential answers, so the original guarantee stands as written and is
    kept here. A THIRD code appearing is a behaviour change on a live auth
    path and should fail this test until somebody states why."""
    body = _token_endpoint()
    assert body.count("HTTP_401_UNAUTHORIZED") >= 2
    assert "HTTP_500" not in body
    codes = set(re.findall(r"HTTP_(\d{3})_", body))
    assert codes == {"401", "429"}, f"unexpected status codes on the token endpoint: {codes}"
