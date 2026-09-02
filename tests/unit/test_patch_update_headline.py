"""Editing a fact refreshes what was derived from the old wording.

2026-09-02: Scott edited "next couple of days" to "weeks" on Steven's
travel commitment. The text changed; the tile kept reading "Travel to
mother's location in days" and `value.deadline` kept saying "next
couple of days", because both were derived from the fact and the route
touched neither. A 200 that leaves a derived field contradicting the
edit is the no-op-reported-as-success this route's own docstring
forbids.
"""

import ast
import pathlib

from contextquilt.services import headlines

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()
WORKER = (ROOT / "src" / "worker.py").read_text()

SCOTTS_FACT = ("Steven will travel to his mother's location in the next couple "
               "of Weeks to conduct interviews and gather data for app testing.")


def _func_source(src: str, name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{name} not found")


# --- the pure parts ------------------------------------------------------

def test_a_long_fact_cannot_be_its_own_headline_and_a_short_one_can():
    assert headlines.self_headline(SCOTTS_FACT) is None
    assert headlines.self_headline("Kevin Thompson case") == "Kevin Thompson case"


def test_pending_fetch_can_target_one_patch():
    sql, args = headlines.build_pending_fetch(subject_key="user:u1", patch_id="abc")
    assert "cp.patch_id = $2::uuid" in sql
    assert args == ["user:u1", "abc"]
    sql2, args2 = headlines.build_pending_fetch(subject_key="user:u1")
    assert "cp.patch_id = $" not in sql2 and args2 == ["user:u1"]


# --- the route -----------------------------------------------------------

def test_a_fact_edit_takes_the_free_headline_or_retires_the_stale_one():
    body = _func_source(MAIN, "update_patch")
    assert "self_headline(update.fact)" in body
    assert 'value["headline"] = own' in body
    assert 'value.pop("headline", None)' in body


def test_a_retired_headline_is_rewritten_by_the_worker_not_left_blank():
    body = _func_source(MAIN, "update_patch")
    assert '"task_type": "headline_patch"' in body
    assert '"patch_id": patch_id' in body
    # The key the worker's router ACTUALLY reads. The first version sent
    # only task_type and the job was ignored; a test that named the key
    # the author thought was read passed anyway (2026-09-02).
    assert '"interaction_type": "headline_patch"' in body
    router = _func_source(WORKER, "process_task")
    assert 'payload.get("interaction_type") or payload.get("type")' in router
    # Enqueued after the row is written, and guarded: a queue failure
    # must not fail the edit.
    assert body.index("UPDATE context_patches") < body.index('"headline_patch"')
    assert "headline_patch_enqueue_failed" in body


def test_a_changed_fact_retires_the_spoken_deadline_into_a_receipt():
    body = _func_source(MAIN, "update_patch")
    assert 'value["prior_deadline"] = value.pop("deadline")' in body
    # Only when the text really changed, and only when the caller did
    # not set the date themselves.
    assert "if fact_changed and update.deadline_date is None" in body


def test_the_route_still_never_touches_deadline_history():
    body = _func_source(MAIN, "update_patch")
    assert "deadline_history" not in body.replace("`deadline_history` is NOT touched", "")


# --- the worker ----------------------------------------------------------

def test_the_worker_rewrites_one_patch_on_a_headline_patch_task():
    assert 'if task_type == "headline_patch":' in WORKER
    branch = WORKER.split('if task_type == "headline_patch":')[1].split("return")[0]
    assert "_generate_headlines(" in branch
    assert 'patch_id=str(payload["patch_id"])' in branch
    lane = _func_source(WORKER, "_generate_headlines")
    assert "patch_id=patch_id" in lane


def test_every_name_the_route_uses_resolves_at_module_scope():
    """2026-09-02: the route called `headlines_svc.self_headline` and the
    import that was supposed to define `headlines_svc` never landed; a
    source-reading test saw the right words and every text edit returned
    500 for six hours. Rule 7: a name that sounds right and is never
    opened. So resolve every free name the function body uses against
    the module's own bindings, the way the interpreter would."""
    import builtins
    tree = ast.parse(MAIN)
    module_names = set(dir(builtins))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                module_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        module_names.add(n.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            module_names.add(node.target.id)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "update_patch")
    local = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            local.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                local.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ExceptHandler) and n.name:
            local.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for a in n.args.args + n.args.kwonlyargs:
                local.add(a.arg)
    unresolved = sorted({n.id for n in ast.walk(fn)
                         if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                         and n.id not in local and n.id not in module_names})
    assert unresolved == [], unresolved


# --- the deliberate trigger ---------------------------------------------

def test_refresh_headline_recomputes_from_stored_text_without_a_fact():
    """Scott ruled an unchanged Save sends nothing on any edit screen, so
    a stale tile gets its own trigger: `refresh_headline: true` on the
    PATCH, no text. Same recompute as a fact edit, from the STORED text."""
    body = _func_source(MAIN, "update_patch")
    assert "elif update.refresh_headline:" in body
    assert 'headlines_svc.self_headline(str(value.get("text") or ""))' in body
    model = MAIN.split("class PatchUpdate(")[1].split("\nclass ")[0]
    assert "refresh_headline: Optional[bool] = None" in model


def test_a_refresh_only_call_moves_neither_updated_at_nor_origin_mode():
    """A headline is presentation. A bump would extend decay on a patch
    nobody re-observed, and an origin_mode flip would record a refresh as
    the user declaring something."""
    body = _func_source(MAIN, "update_patch")
    assert "if refresh_only:" in body
    branch = body.split("if refresh_only:")[1].split("else:")[0]
    assert "updated_at" not in branch and "origin_mode" not in branch
    assert "UPDATE context_patches SET value = $1 WHERE patch_id = $2" in branch


def test_refresh_only_is_false_when_anything_else_is_being_written():
    body = _func_source(MAIN, "update_patch")
    guard = body.split("refresh_only = bool(")[1].split(")")[0]
    for field in ("update.fact is None", "update.owner is None",
                  "update.deadline_date is None", "not update.category",
                  "not update.patch_type"):
        assert field in guard, field
