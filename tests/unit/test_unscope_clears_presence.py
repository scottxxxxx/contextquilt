"""`unscope` and `unassign-project` are documented mirrors. Only one was.

`POST /v1/projects/{u}/{pid}/unscope` is the project-DELETION form and
`POST /v1/origins/{u}/{ot}/{oid}/unassign-project` is the single-meeting
form. Both are supposed to take the project scope off what the meetings
produced. The single-meeting one cleared `person_appearances.project_id`;
the project one cleared `context_patches` and stopped there, so every
deleted project left its presence rows still stamped with it. 32 such
rows were on prod when this was found.

Nothing read them, so nothing looked broken. That is why it survived: two
routes described as mirrors, one of which was not, and no consumer that
grouped presence by project. It stops being invisible the moment
something does, which is what the affected-people screen does.

Executed, not read. `test_create_patch_project_scope.py` earned that
approach tonight and the same reasoning applies harder here: the bug was
an ABSENT statement, and no source-reading assertion about the presence
of a statement can fail on a statement that was never written. The only
thing that catches a missing UPDATE is running the route and looking at
what it touched.
"""

from __future__ import annotations

import ast
import textwrap
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN_SRC = (ROOT / "src" / "main.py").read_text()

USER = "u-1"
PROJECT = "F097ADB4-24FC-46AF-B32C-B37B3BBEB1F9"


def _func_source(name: str) -> str:
    tree = ast.parse(MAIN_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(MAIN_SRC, node)
            assert seg, f"could not recover source for {name}"
            return textwrap.dedent(seg)
    raise AssertionError(f"{name} not found in main.py")


class _Recorder:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self.published: list[dict] = []

    def tables_written(self) -> set[str]:
        out = set()
        for sql, _ in self.statements:
            for table in ("context_patches", "person_appearances"):
                if f"UPDATE {table}" in sql:
                    out.add(table)
        return out

    def statement_for(self, table: str):
        for sql, args in self.statements:
            if f"UPDATE {table}" in sql:
                return sql, args
        return None, None


def _build(patch_rows: int = 7, appearance_rows: int = 4):
    rec = _Recorder()

    class _Pool:
        async def execute(self, sql, *args):
            rec.statements.append((sql, args))
            if "person_appearances" in sql:
                return f"UPDATE {appearance_rows}"
            return f"UPDATE {patch_rows}"

    class _Redis:
        async def xadd(self, key, fields):
            import json as _json
            rec.published.append(_json.loads(fields["data"]))
            return "1-1"

    import json as _json
    ns: dict = {
        "db_pool": _Pool(),
        "redis_client": _Redis(),
        "json": _json,
        "datetime": datetime,
        "Depends": lambda dep: None,
        "verify_application_access": lambda: None,
    }
    exec(compile(_func_source("unscope_project"), "main.py", "exec"), ns)
    return ns["unscope_project"], rec


# ----------------------------------------------------------------------
# The bug
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unscope_clears_presence_as_well_as_patches():
    """The whole defect in one assertion.

    Before the fix this route touched `context_patches` and nothing else,
    so a deleted project's presence rows kept pointing at it forever.
    """
    fn, rec = _build()

    await fn(USER, PROJECT, app_id="ss")

    assert rec.tables_written() == {"context_patches", "person_appearances"}, (
        f"unscope wrote only {rec.tables_written()}"
    )


@pytest.mark.asyncio
async def test_the_presence_update_is_scoped_to_this_user_and_project():
    """A cleanup that is not scoped is a much worse bug than the one it fixes.

    This route takes a project id from a path and runs an UPDATE with no
    row-level guard. If either predicate went missing it would null the
    project on presence rows belonging to other projects, or to every
    other user.
    """
    fn, rec = _build()

    await fn(USER, PROJECT, app_id="ss")

    sql, args = rec.statement_for("person_appearances")
    assert "WHERE user_id = $1" in sql, sql
    assert "project_id = $2" in sql, sql
    assert args == (USER, PROJECT), args


@pytest.mark.asyncio
async def test_it_sets_the_project_to_null_rather_than_deleting_the_row():
    """Presence is not the project's to destroy.

    The person WAS in that meeting. Deleting a project removes a
    container, and the row must survive with its scope cleared, exactly
    like the patches half, which the docstring already promises.
    """
    fn, rec = _build()

    await fn(USER, PROJECT, app_id="ss")

    sql, _ = rec.statement_for("person_appearances")
    assert "SET project_id = NULL" in sql
    assert "DELETE" not in sql.upper()


# ----------------------------------------------------------------------
# The echo, which is how this half gets noticed if it regresses
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_both_counts_are_served_back():
    """Rule 4. A 200 says the request was processed, never that it did
    what the caller meant, and this group has already been bitten by a
    write that reported success while half of it did not happen."""
    fn, _ = _build(patch_rows=7, appearance_rows=4)

    result = await fn(USER, PROJECT, app_id="ss")

    assert result["patches_updated"] == 7
    assert result["appearances_updated"] == 4
    assert result["project_id"] == PROJECT


@pytest.mark.asyncio
async def test_zero_appearances_is_reported_as_zero_not_omitted():
    """A project whose meetings recorded nobody is a real case.

    The key must be present and 0, not absent, or a client cannot tell
    "no presence rows" from "this build does not clear presence".
    """
    fn, _ = _build(patch_rows=3, appearance_rows=0)

    result = await fn(USER, PROJECT, app_id="ss")

    assert "appearances_updated" in result
    assert result["appearances_updated"] == 0


@pytest.mark.asyncio
async def test_the_cache_hydrate_still_fires():
    """Unchanged behavior that the new statement sits in front of."""
    fn, rec = _build()

    await fn(USER, PROJECT, app_id="ss")

    assert rec.published and rec.published[0]["type"] == "hydrate"
    assert rec.published[0]["user_id"] == USER
