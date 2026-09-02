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
