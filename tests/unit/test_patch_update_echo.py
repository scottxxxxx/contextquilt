"""PATCH /v1/quilt/{u}/patches/{p} echoes what it STORED (rule 4).

SS asked 2026-10-21: their updatePatch compares the response field by
field, and CQ answered with {status, patch_id, deadline_date} only, so
for fact, patch_type and owner they could log "echo absent" and nothing
more. The echo is only worth having if it comes from the row: echoing
the dict the handler built would hand the caller's request back to them
and prove nothing about what was written.

Source-reading by house convention for main.py routes (fastapi is absent
from the unit venv); the runtime half is the post-deploy smoke.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()

ECHOED = ("fact", "patch_type", "owner", "deadline_date",
          "project_id", "permanence_override")


def _func_source(name: str) -> str:
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(MAIN, node) or ""
    raise AssertionError(f"{name} not found")


def _final_return() -> ast.Dict:
    tree = ast.parse(_func_source("update_patch"))
    fn = tree.body[0]
    ret = fn.body[-1]
    assert isinstance(ret, ast.Return) and isinstance(ret.value, ast.Dict)
    return ret.value


def test_every_field_is_echoed():
    keys = {k.value for k in _final_return().keys if isinstance(k, ast.Constant)}
    for key in ECHOED:
        assert key in keys, f"update_patch does not echo {key}"


def test_the_echo_never_reads_the_in_memory_value():
    """Any echoed field read off `value` or `update` is the request
    reflected back, not the stored row."""
    for node in ast.walk(_final_return()):
        if isinstance(node, ast.Name):
            assert node.id not in ("value", "update", "row"), (
                f"the echo reads `{node.id}`, which is not the stored row"
            )


def test_the_read_back_runs_after_every_write():
    """A read placed before the last UPDATE would echo a half-written
    row. The permanence UPDATE is the last writer on this route."""
    src = _func_source("update_patch")
    read_at = src.index("stored = await db_pool.fetchrow(")
    for writer in ("UPDATE context_patches SET value = $1 WHERE",
                   "SET value = $1, patch_type = $2",
                   "UPDATE context_patches SET project_id",
                   "SET permanence_override = $1"):
        assert src.index(writer) < read_at, f"{writer!r} runs after the read-back"


def test_the_read_back_selects_each_echoed_column():
    src = _func_source("update_patch")
    query = src[src.index("stored = await db_pool.fetchrow("):]
    query = query[: query.index("patch_id,\n    )")]
    for col in ("value->>'text' AS fact", "patch_type",
                "value->>'owner' AS owner",
                "value->>'deadline_date' AS deadline_date",
                "project_id", "permanence_override"):
        assert col in query, f"read-back does not select {col}"
