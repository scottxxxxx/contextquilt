"""The dashboard serves three named files, not a directory.

Until 2026-09-17 main.py mounted `StaticFiles(directory=src/dashboard,
html=True)`, and that directory holds the dashboard's own SOURCE. So
`/dashboard/router.py` returned the entire 2,200-line admin API
implementation and `/dashboard/test_db.py` returned a database script,
both with no credential. The edge IP-gates `/dashboard/` on one hostname
while CQ answers on two and checks the Host header nowhere, so it was
reachable.

A directory mount is a BLOCK LIST BY OMISSION: it serves whatever happens
to be in the directory and depends on nobody ever adding anything else.
That is the same shape as the edge rule that forwarded /v1/auth/apps
because no pattern named it. The replacement is default deny.

main.py cannot be imported here (no fastapi in the unit venv), so the
allowlist is read out of the source with `ast` and asserted as a real
value rather than matched as text.
"""
import ast
from pathlib import Path

MAIN_PATH = Path(__file__).resolve().parents[2] / "src" / "main.py"
MAIN = MAIN_PATH.read_text()


def _code_only(text: str) -> str:
    """Source minus comment-only lines: main.py now EXPLAINS this bug in a
    comment that names the old mount, and a negative assertion against raw
    source would match the explanation instead of any code."""
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _allowlist() -> dict:
    """The DASHBOARD_ASSETS literal, evaluated."""
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DASHBOARD_ASSETS":
                    return ast.literal_eval(node.value)
    raise AssertionError("DASHBOARD_ASSETS is gone; the allowlist must exist")


def test_nothing_mounts_a_directory_any_more():
    assert "app.mount(" not in _code_only(MAIN), (
        "a directory mount serves whatever is in the directory, including "
        "source files nobody meant to publish")
    assert "StaticFiles" not in _code_only(MAIN)


def test_the_allowlist_is_exactly_the_three_real_assets():
    served = {filename for filename, _ in _allowlist().values()}
    assert served == {"index.html", "app.js", "style.css"}


def test_no_python_source_can_be_addressed():
    """The two files that were exposed. Named explicitly so that a future
    edit re-adding either fails loudly rather than quietly."""
    allowlist = _allowlist()
    assert "router.py" not in allowlist
    assert "test_db.py" not in allowlist
    for filename, _ in allowlist.values():
        assert not filename.endswith(".py"), f"{filename} is source, not an asset"


def test_the_bare_path_still_resolves_to_the_page():
    """`html=True` used to serve index.html for /dashboard/ and redirect
    /dashboard. Both behaviours are preserved, or the dashboard breaks for
    everyone who has it bookmarked."""
    allowlist = _allowlist()
    assert allowlist[""][0] == "index.html", "/dashboard/ must still serve the page"
    code = _code_only(MAIN)
    assert '@app.get("/dashboard", include_in_schema=False)' in code
    assert 'RedirectResponse(url="/dashboard/")' in code


def test_an_unknown_asset_is_a_404():
    code = _code_only(MAIN)
    handler = code[code.index("async def dashboard_asset("):]
    handler = handler[:handler.index("return FileResponse")]
    assert "status_code=404" in handler, "unknown assets must 404, not fall through"


def test_the_path_is_a_key_not_a_filesystem_path():
    """Traversal is impossible by construction: the request path selects a
    dict entry, and the filename comes from the entry rather than from the
    caller. This test pins that shape, because joining the raw path onto a
    directory is the obvious refactor and would reintroduce everything."""
    code = _code_only(MAIN)
    handler = code[code.index("async def dashboard_asset("):]
    handler = handler[:handler.index("return FileResponse") + 40]
    assert "DASHBOARD_ASSETS.get(asset)" in handler
    assert "os.path.join(dashboard_path, filename)" in handler
    assert "os.path.join(dashboard_path, asset)" not in handler
