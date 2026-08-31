"""The API module actually loads.

Nothing else in this suite proves it. Every test that inspects routes,
including `test_openapi_matches_routes`, reads `main.py` as TEXT and
matches it with a regex, because the module cannot be imported in the
bare local venv. That is a reasonable accommodation and it has a blind
spot big enough to drive a release through: a NameError, a missing
import, or a typo in a decorator leaves every one of those tests green
while the API does not start.

This is not hypothetical. Writing the description-dismissal routes
(2026-08-31) I used `Body(...)` without importing it, and `ast.parse`
was satisfied, every source-reading test passed, and the only reason it
was caught was a hand-written name check I happened to run. That is not
an instrument, it is luck with good intentions.

GhostPour found the same shape in their own tooling the same night: an
`Optional[Model]` annotation slipped past an `issubclass` check, so two
instruments built to catch models that drop fields both reported a route
as clean. Their conclusion is the one this file exists for: an
instrument that has never been sabotaged has not been tested, only run.

Skips locally where fastapi and asyncpg are absent, and RUNS IN CI,
which installs requirements.txt. A skip here is itself worth noticing:
if this starts skipping in CI, the import coverage has silently gone
away and the source-reading tests are back to being the only check.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

# Guarded rather than imported at module scope: an ImportError here
# would look like a broken test file rather than a missing dependency.
pytest.importorskip("fastapi", reason="API deps absent in the bare local venv")
pytest.importorskip("asyncpg", reason="API deps absent in the bare local venv")


@pytest.fixture(scope="module")
def api():
    """Import src/main.py by path, the way the container runs it.

    Imported by path rather than by package name because that is how the
    process starts it, and an import that only works through a different
    entry point is not the one that matters.
    """
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("cq_main_import_check",
                                                  SRC / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_api_module_imports_at_all(api):
    """The whole point. Every name resolves, every decorator evaluates."""
    assert api.app is not None


def test_every_route_decorator_actually_registered(api):
    """Source-reading tests count `@app.post(...)` strings; this counts
    what FastAPI holds. A decorator that raised at import, or one whose
    path string differs from what the regex matched, shows up here as a
    mismatch rather than as a passing regex."""
    registered = {
        (method, route.path)
        for route in api.app.routes
        for method in getattr(route, "methods", set()) or set()
        if method not in {"HEAD", "OPTIONS"}
    }
    assert registered, "no routes registered"

    import re
    src = (SRC / "main.py").read_text()
    in_source = {
        (m.group(1).upper(), m.group(2))
        for m in re.finditer(r'@app\.(get|post|put|patch|delete)\("([^"]+)"', src)
    }
    missing = in_source - registered
    assert not missing, (
        "decorators present in source but not registered on the app: "
        + ", ".join(f"{v} {p}" for v, p in sorted(missing))
    )


def test_the_dismissal_routes_carry_a_usable_body_model(api):
    """The `Body` import this file was written for.

    Asserts the request model resolves and its optional fields are
    genuinely optional, which is what makes "this is inaccurate" (no
    body at all) a valid call rather than a 422.
    """
    model = api.DescriptionDismissal
    instance = model()
    assert instance.note is None
    assert instance.source == "user_card"
    assert model(note="not an attorney").note == "not an attorney"
