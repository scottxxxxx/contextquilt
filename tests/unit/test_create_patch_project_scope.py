"""A `project_id` that resolves to nothing, EXECUTED not read.

SS asked, before building against it, whether the create echo would tell
them a project failed to resolve. It would not. `create_patch` looked the
id up, got no row, left the name None, and STORED THE ID ANYWAY, because
`decision` and `commitment` are both project-scoped. The patch then sits
in the table carrying a `project_id` with a NULL name, which every
consumer renders as unscoped because they all join `projects`. Dated and
behaves undated, three lines below the comment that names that exact
trade for deadlines.

The match is exact and `projects.project_id` is TEXT, not uuid, so case
decides it. That is not theoretical: a client that lowercased Swift's
`uuidString` would scope to nothing, silently, on every write.

`test_create_patch_idempotency.py` guards this route by READING main.py.
That is the right instrument for "is ON CONFLICT present" and the wrong
one for "what does the caller receive", which is what SS has to build
against. So this file compiles the real function and CALLS it. Same
approach as `test_extraction_gate_path_executes.py`, and the same reason:
a source-reading test would pass on a warning that is assembled but never
reaches the response.
"""

from __future__ import annotations

import ast
import json
import sys
import textwrap
import uuid as uuid_module
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN_SRC = (ROOT / "src" / "main.py").read_text()

KNOWN_PROJECT = "F097ADB4-24FC-46AF-B32C-B37B3BBEB1F9"   # GL Unlimited, real shape
KNOWN_NAME = "GL Unlimited"


def _func_source(name: str) -> str:
    tree = ast.parse(MAIN_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(MAIN_SRC, node)
            assert seg, f"could not recover source for {name}"
            # Drop the decorator line: this calls the function directly
            # rather than through FastAPI's routing.
            return textwrap.dedent(seg)
    raise AssertionError(f"{name} not found in main.py")


def _module_value(name: str):
    """Read a real module-level constant out of main.py without importing it.

    These three decide the behavior under test (which types are scoped,
    which are valid, how long they persist). Restating them here would be
    a second source of truth that drifts, and the drift would be silent
    in exactly the direction that matters: a type dropping out of
    PROJECT_SCOPED_TYPES upstream while this file still believes it is in.
    """
    tree = ast.parse(MAIN_SRC)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found at module level")


VALID_PATCH_TYPES = _module_value("VALID_PATCH_TYPES")
PROJECT_SCOPED_TYPES = _module_value("PROJECT_SCOPED_TYPES")
PATCH_PERSISTENCE = _module_value("PATCH_PERSISTENCE")


def _real_valid_calendar_day():
    """`_valid_calendar_day` itself, compiled from main.py.

    It depends on a module-level `_ISO_DAY` regex and `date`, both of
    which are supplied here rather than restated.
    """
    import re
    from datetime import date

    ns: dict = {"re": re, "date": date, "_ISO_DAY": _module_regex("_ISO_DAY")}
    exec(compile(_func_source("_valid_calendar_day"), "main.py", "exec"), ns)
    return ns["_valid_calendar_day"]


def _module_regex(name: str):
    """Recover a module-level `re.compile(...)` constant from main.py."""
    import re as _re
    tree = ast.parse(MAIN_SRC)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    pattern = ast.literal_eval(node.value.args[0])
                    return _re.compile(pattern)
    raise AssertionError(f"{name} not found at module level")


class _Captured:
    def __init__(self) -> None:
        self.insert_args: tuple | None = None
        self.warnings: list | None = None
        self.logged: list[tuple[str, dict]] = []

    def stored(self, index: int):
        assert self.insert_args is not None, "nothing was inserted"
        return self.insert_args[index]


def _build(known_projects: dict[str, str]):
    cap = _Captured()

    class _Pool:
        async def fetchrow(self, sql, *args):
            if "FROM projects" in sql:
                pid = args[0]
                name = known_projects.get(pid)
                return {"name": name} if name is not None else None
            return None

        async def fetchval(self, sql, *args):
            if "INSERT INTO context_patches" in sql:
                cap.insert_args = args
                return args[0]
            return None

        async def fetch(self, sql, *args):
            return []

        async def execute(self, sql, *args):
            return "INSERT 0 1"

    async def _created(user_id, app_id, patch_id, patch_type, *,
                       created, connections, warnings=None):
        cap.warnings = list(warnings or [])
        return {
            "status": "created" if created else "exists",
            "patch_id": patch_id,
            "type": patch_type,
            "warnings": cap.warnings,
        }

    async def _existing(client_id, subject_key):
        return None

    class _Redis:
        async def xadd(self, key, fields):
            return "1-1"

    ns: dict = {
        "db_pool": _Pool(),
        "redis_client": _Redis(),
        "json": json,
        "uuid": uuid_module,
        "_uuid": uuid_module,
        "datetime": datetime,
        "timedelta": timedelta,
        "logger": SimpleNamespace(
            info=lambda e, **kw: cap.logged.append((e, kw)),
            warning=lambda e, **kw: cap.logged.append((e, kw)),
            error=lambda e, **kw: cap.logged.append((e, kw)),
        ),
        "VALID_PATCH_TYPES": VALID_PATCH_TYPES,
        "PROJECT_SCOPED_TYPES": PROJECT_SCOPED_TYPES,
        "PATCH_PERSISTENCE": PATCH_PERSISTENCE,
        "_created_patch_response": _created,
        "_existing_client_id_patch": _existing,
        # The REAL validator, compiled out of main.py, not a stand-in.
        # The stand-in written first here accepted "2026-13-45" because it
        # only checked length and dash positions, so the both-warnings
        # test passed the date through as valid and reported one warning
        # where two were expected. A fixture you wrote yourself cannot
        # falsify an assumption you wrote into it; the cheapest way out is
        # not to write one.
        "_valid_calendar_day": _real_valid_calendar_day(),
        # The signature carries `app_id: str = Depends(verify_application_access)`.
        # This calls the function directly rather than through FastAPI, so
        # both names only have to EXIST for the def to evaluate; `app_id`
        # is passed explicitly by every test below.
        "Depends": lambda dep: None,
        "verify_application_access": lambda: None,
        "HTTPException": type("HTTPException", (Exception,), {
            "__init__": lambda self, status_code=None, detail=None: (
                setattr(self, "status_code", status_code),
                setattr(self, "detail", detail), None)[-1]
        }),
    }
    src = _func_source("create_patch")
    exec(compile(src, "main.py", "exec"), ns)
    return ns["create_patch"], cap


def _patch(**kw):
    base = dict(type="decision", text="Use Slack for project communications",
                owner=None, deadline_date=None, project_id=None,
                client_id=None, connections=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ----------------------------------------------------------------------
# The gap SS asked about
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unresolvable_project_id_warns():
    """The whole reason this PR exists.

    Before the fix the caller got a clean create and no signal at all,
    and the only way to notice was `project` being null in the echo,
    which is an inference from an absence.
    """
    fn, cap = _build({KNOWN_PROJECT: KNOWN_NAME})

    await fn("u1", _patch(project_id="NOT-A-REAL-PROJECT"), app_id="ss")

    assert cap.warnings, "an unresolvable project produced no warning"
    assert any("NOT-A-REAL-PROJECT" in w for w in cap.warnings), cap.warnings


@pytest.mark.asyncio
async def test_the_item_is_still_created():
    """Same trade as the malformed deadline: keep the item, flag the scope.

    A user typed a decision. Refusing the write because the scope was
    wrong loses the decision, and the client mints the id, so a bad one
    is its bug to fix rather than the user's to retype.
    """
    fn, cap = _build({KNOWN_PROJECT: KNOWN_NAME})

    result = await fn("u1", _patch(project_id="NOT-A-REAL-PROJECT"), app_id="ss")

    assert result["status"] == "created"
    assert cap.insert_args is not None


@pytest.mark.asyncio
async def test_a_resolving_project_is_silent_and_stores_the_name():
    """The happy path stays quiet, or the warning means nothing."""
    fn, cap = _build({KNOWN_PROJECT: KNOWN_NAME})

    await fn("u1", _patch(project_id=KNOWN_PROJECT), app_id="ss")

    assert cap.warnings == []
    assert KNOWN_NAME in cap.insert_args, "the resolved name was not stored"


@pytest.mark.asyncio
async def test_no_project_id_is_silent():
    """Most creates send none. A warning here would be noise in every
    response and would train the client to ignore the key."""
    fn, cap = _build({KNOWN_PROJECT: KNOWN_NAME})

    await fn("u1", _patch(), app_id="ss")

    assert cap.warnings == []


# ----------------------------------------------------------------------
# Case sensitivity, which is what SS actually has to get right
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_lowercased_project_id_does_not_resolve_and_says_so():
    """`projects.project_id` is TEXT and the match is exact.

    Swift's `uuidString` is uppercase and every project on prod carrying
    real data is stored that way, so SS matches today. A client that
    lowercased would scope to nothing on EVERY write, and before this
    change it would have done so in silence. This test is the alarm for
    that specific regression.
    """
    fn, cap = _build({KNOWN_PROJECT: KNOWN_NAME})

    await fn("u1", _patch(project_id=KNOWN_PROJECT.lower()), app_id="ss")

    assert cap.warnings, "a lowercased id resolved to nothing, silently"
    assert any(KNOWN_PROJECT.lower() in w for w in cap.warnings)


# ----------------------------------------------------------------------
# Both warnings at once
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_bad_date_and_a_bad_project_both_arrive():
    """A client reading only the first warning would act on half of it.

    This is why the response carries a LIST built from every slot rather
    than a single warning field, and it is the assertion that fails if
    someone later adds a third warning and forgets to collect it.
    """
    fn, cap = _build({KNOWN_PROJECT: KNOWN_NAME})

    await fn("u1", _patch(type="commitment", deadline_date="2026-13-45",
                          project_id="NOPE"), app_id="ss")

    assert len(cap.warnings) == 2, cap.warnings
    assert any("deadline_date" in w for w in cap.warnings)
    assert any("project_id" in w for w in cap.warnings)


# ----------------------------------------------------------------------
# The two types Scott ruled should travel
# ----------------------------------------------------------------------

@pytest.mark.parametrize("ptype", ["decision", "commitment"])
def test_the_wired_types_are_valid_and_project_scoped(ptype):
    """SS is wiring Decision and Reminder to these two.

    If either fell out of `PROJECT_SCOPED_TYPES`, its `project_id` would
    be discarded on TYPE grounds before the lookup ever ran, and the
    warning above would never fire because nothing would be looked up.
    A decision would land under no project and nothing would say so.
    """
    assert ptype in VALID_PATCH_TYPES
    assert ptype in PROJECT_SCOPED_TYPES
