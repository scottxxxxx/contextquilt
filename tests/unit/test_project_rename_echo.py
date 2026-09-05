"""`PATCH /v1/projects/{u}/{pid}` said "updated" and nothing else.

Found by ShoulderSurf on 2026-09-04: a rename from Twit to Twit2 got a
200 and their echo check logged ECHO ABSENT, because the body carried
`status` and `project_id` and no `name`. The rename had landed; the
receipt was missing. Read from the handler (rule 9), not inferred from
the observable: the return statement was two keys and the name was not
one of them.

Why it matters more than a missing field usually does: the client's
echo check exists because a 200 says the request was processed, never
that the value it sent is the value that landed. With no echo the check
degrades to "trust the 200", and it logs at error level on EVERY rename,
so the one line that exists to catch a silent normalisation is the line
everyone learns to ignore. An alarm that always fires is an alarm nobody
hears.

The echo is READ BACK FROM THE ROW after the write, never copied from
the request. Copying the request would make the echo agree with the
caller by construction, which is the same claim as the 200.

Executed, not read, on the same harness `test_unscope_clears_presence`
uses: the route body is compiled against a recording pool, so these
tests fail on the return value the route actually produces.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN_SRC = (ROOT / "src" / "main.py").read_text()

USER = "u-1"
PROJECT = "8DEBE602-0000-4000-8000-000000000000"


def _func_source(name: str) -> str:
    tree = ast.parse(MAIN_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(MAIN_SRC, node)
            assert seg, f"could not recover source for {name}"
            return textwrap.dedent(seg)
    raise AssertionError(f"{name} not found in main.py")


class _HTTPException(Exception):
    def __init__(self, status_code: int, detail: str = "") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _Store:
    """One project row that the route's UPDATEs actually mutate.

    `normalise` stands in for anything CQ might one day do to a name on
    the way in (trim, collapse whitespace). The echo must report what
    the store holds, so a normalising store is how the test tells "read
    back" from "copied from the request".
    """

    def __init__(self, name: str = "Twit", status: str = "active",
                 exists: bool = True, patch_rows: int = 5,
                 normalise=lambda s: s) -> None:
        self.name = name
        self.status = status
        self.exists = exists
        self.patch_rows = patch_rows
        self.normalise = normalise
        self.statements: list[tuple[str, tuple]] = []


def _build(store: _Store):
    class _Pool:
        async def fetchrow(self, sql, *args):
            store.statements.append((sql, args))
            if not store.exists:
                return None
            if "SELECT project_id FROM projects" in sql:
                return {"project_id": args[0]}
            if "SELECT name, status FROM projects" in sql:
                return {"name": store.name, "status": store.status}
            raise AssertionError(f"unexpected fetchrow: {sql}")

        async def execute(self, sql, *args):
            store.statements.append((sql, args))
            if "UPDATE projects SET name" in sql:
                store.name = store.normalise(args[0])
                return "UPDATE 1"
            if "UPDATE projects SET status = 'archived'" in sql:
                store.status = "archived"
                return "UPDATE 1"
            if "UPDATE context_patches" in sql:
                return f"UPDATE {store.patch_rows}"
            raise AssertionError(f"unexpected execute: {sql}")

    ns: dict = {
        "db_pool": _Pool(),
        "HTTPException": _HTTPException,
        "Depends": lambda dep: None,
        "verify_application_access": lambda: None,
    }
    exec(compile(_func_source("update_project"), "main.py", "exec"), ns)
    return ns["update_project"]


def _update(name=None, status=None):
    return SimpleNamespace(name=name, status=status)


# ----------------------------------------------------------------------
# The finding
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_rename_echoes_the_name_under_the_key_the_request_used():
    """The whole report in one assertion: sent `name`, read `name` back."""
    store = _Store(name="Twit")
    fn = _build(store)

    result = await fn(USER, PROJECT, _update(name="Twit2"), app_id="ss")

    assert result["name"] == "Twit2", result
    assert result["project_id"] == PROJECT
    assert result["status"] == "updated"


@pytest.mark.asyncio
async def test_the_echo_is_read_back_from_the_row_not_copied_from_the_request():
    """If the store changes the name on the way in, the echo must show
    the stored spelling. An echo that repeats the request cannot see a
    normalisation, which is the exact failure the client's check exists
    to catch."""
    store = _Store(name="Twit", normalise=lambda s: s.strip())
    fn = _build(store)

    result = await fn(USER, PROJECT, _update(name="  Twit2  "), app_id="ss")

    assert result["name"] == "Twit2"
    assert result["name"] != "  Twit2  "


@pytest.mark.asyncio
async def test_the_read_back_happens_after_the_write():
    """Ordering is load bearing: a SELECT issued before the UPDATE reads
    the old name and echoes a rename that has not happened yet."""
    store = _Store(name="Twit")
    fn = _build(store)

    await fn(USER, PROJECT, _update(name="Twit2"), app_id="ss")

    kinds = [
        "read_back" if "SELECT name, status FROM projects" in sql
        else "write" if "UPDATE projects SET name" in sql
        else None
        for sql, _ in store.statements
    ]
    kinds = [k for k in kinds if k]
    assert kinds.index("write") < kinds.index("read_back"), kinds


@pytest.mark.asyncio
async def test_a_status_only_patch_still_serves_the_current_name():
    """The key is always present, so a client never has to branch on
    which fields it sent to know whether to look for the echo."""
    store = _Store(name="Twit")
    fn = _build(store)

    result = await fn(USER, PROJECT, _update(status="archived"), app_id="ss")

    assert result["name"] == "Twit"
    assert result["project_status"] == "archived"


# ----------------------------------------------------------------------
# The counts, same convention as unscope: present and numeric when the
# half ran, null when the request did not ask for it
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rename_reports_how_many_patches_it_rewrote_and_archive_is_null():
    store = _Store(name="Twit", patch_rows=5)
    fn = _build(store)

    result = await fn(USER, PROJECT, _update(name="Twit2"), app_id="ss")

    assert result["patches_renamed"] == 5
    assert result["patches_archived"] is None
    assert result["project_status"] == "active"


@pytest.mark.asyncio
async def test_archive_reports_the_cascade_and_rename_is_null():
    store = _Store(name="Twit", patch_rows=3)
    fn = _build(store)

    result = await fn(USER, PROJECT, _update(status="archived"), app_id="ss")

    assert result["patches_archived"] == 3
    assert result["patches_renamed"] is None
    assert result["project_status"] == "archived"


@pytest.mark.asyncio
async def test_a_rename_that_touches_no_patches_reports_zero_not_null():
    """A brand new project has no patches yet. Zero is a real answer and
    must not be confused with "this build does not count"."""
    store = _Store(name="Twit", patch_rows=0)
    fn = _build(store)

    result = await fn(USER, PROJECT, _update(name="Twit2"), app_id="ss")

    assert result["patches_renamed"] == 0


# ----------------------------------------------------------------------
# Unchanged behaviour the echo sits behind
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_missing_project_is_still_a_404_and_writes_nothing():
    store = _Store(exists=False)
    fn = _build(store)

    with pytest.raises(_HTTPException) as exc:
        await fn(USER, PROJECT, _update(name="Twit2"), app_id="ss")

    assert exc.value.status_code == 404
    assert not any(sql.lstrip().upper().startswith("UPDATE") for sql, _ in store.statements)


@pytest.mark.asyncio
async def test_the_read_back_is_scoped_to_the_caller_user():
    """The existence check already guards the user; the read back must
    not be the one statement on the route that forgets to."""
    store = _Store(name="Twit")
    fn = _build(store)

    await fn(USER, PROJECT, _update(name="Twit2"), app_id="ss")

    read_back = [
        (sql, args) for sql, args in store.statements
        if "SELECT name, status FROM projects" in sql
    ]
    assert len(read_back) == 1, read_back
    sql, args = read_back[0]
    assert re.search(r"user_id\s*=\s*\$2", sql), sql
    assert args == (PROJECT, USER), args
