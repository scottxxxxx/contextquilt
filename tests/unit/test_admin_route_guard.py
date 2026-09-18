"""Every route that administers the app registry needs the admin key.

Three routes in main.py administered the `applications` table with NO auth
dependency until 2026-09-16: register (mints an app and returns its
client_secret), list apps, and PATCH (sets `enforce_auth`, rotates LLM
keys). `enforce_auth=False` is what makes CQ accept a bare X-App-ID, so an
unauthenticated PATCH could switch ShoulderSurf or ghostpour into
header-only auth and then speak as them.

The guard existed the whole time, twice, applied to every dashboard and
app_schemas route. main.py imported neither copy.

The structural test is the point: it fails for the NEXT route added
without a dependency, which is the only version of this that keeps working
after everyone forgets the incident.
"""
import ast
from pathlib import Path

import pytest

from contextquilt.services.admin_auth import is_authorized

MAIN_PATH = Path(__file__).resolve().parents[2] / "src" / "main.py"
MAIN = MAIN_PATH.read_text()

# Routes that are SUPPOSED to be reachable with no credential. Adding to
# this set is the deliberate act; forgetting a dependency is not.
INTENTIONALLY_OPEN = {
    ("GET", "/"),                # redirects to the docs
    ("GET", "/health"),          # the deploy gate and compose healthcheck poll it
    ("POST", "/v1/auth/token"),  # the credential exchange itself, rate limited
    # The dashboard SHELL, added 2026-09-17 when the StaticFiles mount was
    # replaced by an explicit allowlist. These serve index.html, app.js and
    # style.css and nothing else; the login screen has to load before anyone
    # can present a key, exactly as it did under the mount. Every dashboard
    # API route behind them still requires the admin key.
    ("GET", "/dashboard"),
    ("GET", "/dashboard/{asset:path}"),
}


def _routes():
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            f = dec.func
            if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                    and f.value.id == "app"
                    and f.attr in ("get", "post", "put", "patch", "delete")):
                continue
            path = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else "?"
            guarded = "Depends" in ast.dump(node.args) or "dependencies" in ast.dump(dec)
            yield f.attr.upper(), path, guarded, node.lineno


def test_no_route_is_unguarded_unless_it_says_so_out_loud():
    unguarded = {(m, p): line for m, p, guarded, line in _routes() if not guarded}
    unexpected = {k: v for k, v in unguarded.items() if k not in INTENTIONALLY_OPEN}
    assert not unexpected, (
        "routes with no auth dependency that are not on the open list: "
        + ", ".join(f"{m} {p} (main.py:{line})" for (m, p), line in unexpected.items())
    )


def test_the_open_list_has_not_quietly_grown():
    """A route joining INTENTIONALLY_OPEN should be a visible decision in a
    diff, not something that happens because a test was going red.

    3 -> 5 on 2026-09-17: the two dashboard shell routes above. They are
    not new public surface, they are the same surface the StaticFiles
    mount served, narrowed to three named files."""
    assert len(INTENTIONALLY_OPEN) == 5


def test_every_route_was_actually_inspected():
    """Guard against the scan silently matching nothing: if the decorator
    shape changes, the structural test above would pass vacuously."""
    routes = list(_routes())
    assert len(routes) > 50, f"only {len(routes)} routes parsed; the scan shape broke"


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/auth/register"),
    ("GET", "/v1/auth/apps"),
    ("PATCH", "/v1/auth/apps/{app_id}"),
])
def test_the_registry_routes_carry_the_admin_gate(method, path):
    found = [(m, p, g) for m, p, g, _ in _routes() if (m, p) == (method, path)]
    assert found, f"{method} {path} is gone; this test needs updating deliberately"
    assert found[0][2], f"{method} {path} lost its admin dependency"


def test_main_imports_the_shared_guard():
    """The bug was never a missing guard, it was a guard nobody imported."""
    assert "from contextquilt.api_deps import verify_admin_key" in MAIN


def test_the_duplicate_definitions_are_gone():
    """Two identical copies, neither of them the one main.py needed."""
    src = MAIN_PATH.parent
    for rel in ("dashboard/router.py", "contextquilt/routers/app_schemas.py"):
        text = (src / rel).read_text()
        assert "async def verify_admin_key" not in text, f"{rel} redefines the guard"
        assert "from contextquilt.api_deps import verify_admin_key" in text, rel


def test_the_dependency_reads_a_HEADER_not_a_query_parameter():
    """An earlier draft attached the Header default after definition so the
    module would import without fastapi. FastAPI reads the signature at
    route registration, and a plain-string default becomes a QUERY
    parameter, which would ignore the header every admin caller sends and
    403 them. Asserted as source because fastapi is absent locally."""
    deps = (MAIN_PATH.parent / "contextquilt" / "api_deps.py").read_text()
    assert "x_admin_key: str = Header(default=\"\")" in deps
    assert "__defaults__" not in deps, "no post-hoc signature patching"


# --- the decision, executed (no fastapi in the local unit venv) -----------

def test_a_wrong_key_is_refused():
    assert is_authorized("the-real-key", "not-it") is False


def test_an_absent_key_is_refused_when_one_is_configured():
    assert is_authorized("the-real-key", "") is False
    assert is_authorized("the-real-key", None) is False


def test_the_right_key_passes():
    assert is_authorized("the-real-key", "the-real-key") is True


def test_no_configured_key_means_open():
    """Dev mode, asserted on purpose rather than left as an accident of the
    `if`: a local stack runs with no CQ_ADMIN_KEY. Prod sets it."""
    assert is_authorized("", "anything") is True
    assert is_authorized(None, "") is True


def test_the_dependency_raises_403_when_fastapi_is_present():
    """The wrapper half: skipped in the local venv, runs in CI.

    It lives in api_deps, not in services/admin_auth, because the Header
    default has to be a real fastapi object at import time."""
    pytest.importorskip("fastapi")
    import asyncio

    from fastapi import HTTPException

    import contextquilt.api_deps as deps

    class _S:
        cq_admin_key = "the-real-key"

    class _Request:
        """Only `.client.host` is read; the limiter is inert here because no
        Redis client is bound in a unit run."""

        def __init__(self, host="172.18.0.4"):
            self.client = type("C", (), {"host": host})() if host else None

    saved = deps.get_settings
    deps.get_settings = lambda: _S()
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(deps.verify_admin_key(_Request(), x_admin_key="not-it"))
        assert exc.value.status_code == 403
        assert asyncio.run(
            deps.verify_admin_key(_Request(), x_admin_key="the-real-key")) is None
        # A request with no client (ASGI allows it) must not explode: the
        # identity falls back to the shared bucket rather than raising.
        assert asyncio.run(
            deps.verify_admin_key(_Request(host=None), x_admin_key="the-real-key")) is None
    finally:
        deps.get_settings = saved
