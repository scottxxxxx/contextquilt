"""Every key the woven route reads off the digest actually exists.

2026-08-31, and this is the test that would have prevented an outage.
Renaming `total_available` to `tiles_available` updated the RESPONSE but
missed one reader: the `logger.info` line added for observability. A
KeyError inside the handler 500s the whole route, so the Memory tab
showed "Couldn't reach your memory" and Scott reported the app as
broken. The logging added to make the route diagnosable is what took it
down.

NOTHING IN THE SUITE COULD SEE IT. Every route test here reads main.py
as text, and test_api_module_imports.py imports the module but never
CALLS a handler, so a subscript on a dict key that no longer exists is
invisible to all of them. A rename that passes a grep and a syntax check
is exactly the shape that reaches production.

So this compares the keys the route SUBSCRIPTS against the keys
`build_digest` actually RETURNS, both derived rather than listed: a
hand-maintained list of expected keys is a list somebody must remember
to update, and the entry that gets forgotten is the one that breaks.
"""

import ast
from pathlib import Path

from contextquilt.services.woven_digest import build_digest

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()


def _digest_keys_read_by(func_name: str) -> set:
    """Every string literal subscripted off `digest` in one route."""
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AsyncFunctionDef) and node.name == func_name):
            continue
        keys = set()
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Subscript)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "digest"
                    and isinstance(sub.slice, ast.Constant)
                    and isinstance(sub.slice.value, str)):
                keys.add(sub.slice.value)
        return keys
    raise AssertionError(f"{func_name} not found in main.py")


def _keys_build_digest_returns() -> set:
    out = build_digest([{
        "patch_id": "p1", "patch_type": "commitment", "origin_id": "m1",
        "value": {"text": "Ship the gateway", "headline": "Ship it"},
        "created_at": None,
    }], limit=6)
    return set(out.keys())


def test_the_route_reads_only_keys_the_digest_returns():
    """The outage, in one assertion.

    A logging line reading `digest["total_available"]` after the field
    became `tiles_available` raises KeyError, 500s the route, and
    surfaces to the user as "Couldn't reach your memory". Both sides of
    this comparison are derived, so a future rename cannot pass by
    updating a list here.
    """
    read = _digest_keys_read_by("woven_digest")
    assert read, "no digest subscripts found; this test would pass vacuously"
    returned = _keys_build_digest_returns()
    missing = read - returned
    assert not missing, (
        f"the woven route reads {sorted(missing)} off the digest and "
        f"build_digest returns {sorted(returned)}; that is a KeyError at "
        "request time, which is a 500 and an empty Memory tab"
    )


def test_the_check_covers_the_logging_lines_not_just_the_response():
    # The response was correct throughout; the LOGGING was the casualty.
    # A version of this test that only inspected the returned dict would
    # have passed while the route was down.
    assert "tiles_available" in _digest_keys_read_by("woven_digest")


def test_the_seam_route_is_covered_too():
    # It builds its own patch dicts rather than reading the digest, so
    # the set is legitimately empty. Asserted rather than assumed, since
    # "empty" and "I looked in the wrong place" are the same result.
    tree = ast.parse(MAIN)
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, ast.AsyncFunctionDef)}
    assert "woven_meeting_seam" in names
    assert _digest_keys_read_by("woven_meeting_seam") == set()


def test_the_served_log_records_the_scope_it_filtered_on():
    """Rule 8's last sentence, pointed at CQ's own half.

    2026-08-31: Scott saw another project's patches under a project
    heading. This log recorded candidates, tiles and window but NOT the
    project it filtered on, so answering "did a scope arrive, and which
    one" meant running the candidate SQL against every project id the
    user has until one returned exactly the count in the log. It was a
    different project's id: the client sent one project's id under
    another's heading and CQ filtered correctly on what it was given.

    A drop the other team cannot see has to be audible from the side
    that made it.
    """
    line = MAIN[MAIN.index('logger.info("woven_digest_served"'):]
    line = line[:line.index(")\n")]
    for field in ("project_id=", "project=", "project_known="):
        assert field in line, f"the served log omits {field}"


def test_project_known_is_logged_because_it_distinguishes_two_answers():
    # "the filter matched nothing" and "this project does not exist for
    # this user" are different answers and only one of them is a client
    # bug. Serving both as an empty array is what made the screen
    # ambiguous in the first place.
    line = MAIN[MAIN.index('logger.info("woven_digest_served"'):]
    assert "project_known=project_known" in line[:line.index(")\n")]
