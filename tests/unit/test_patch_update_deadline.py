"""Editing an action item's due date, and the guard that keeps an edit
from becoming evidence about a person.

Until now nothing could change `deadline_date` after creation, so an item
created with the wrong date, or with none, was stuck that way forever on
every surface. Scott asked for the edit; the interesting part is what it
must NOT do.
"""

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()
LEDGER = (ROOT / "src" / "contextquilt" / "services" / "item_ledger.py").read_text()


def _func_source(name: str) -> str:
    tree = ast.parse(MAIN)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(MAIN, node) or ""
    raise AssertionError(f"{name} not found")


# --- THE GUARD, which is the whole reason this is delicate -------------

def test_an_edit_never_writes_deadline_history():
    """`value.deadline_history` is written by the WORKER on the
    re-observation path and means THE PERSON MOVED THEIR OWN DEADLINE,
    observed in a room. The item ledger derives `re_dated` by counting
    it.

    If a user edit appended to it, a colleague's card would report that
    they pushed a deadline when the app's user moved it in the UI. That
    is a served claim about something nobody observed.

    CHECKED ON THE AST, so a COMMENT saying the field is not touched does
    not read as touching it. The first version was a substring check and
    failed on its own explanatory comment, which would have pressured the
    next person to delete the comment rather than the code.
    """
    tree = ast.parse(_func_source("update_patch"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "deadline_history":
            raise AssertionError("update_patch references deadline_history in code")
        if isinstance(node, ast.Name) and node.id == "deadline_history":
            raise AssertionError("update_patch binds deadline_history")
        if isinstance(node, ast.Attribute) and node.attr == "deadline_history":
            raise AssertionError("update_patch reaches deadline_history")


def test_re_dated_really_is_derived_from_deadline_history():
    """Proves the premise of the guard above rather than asserting it. If
    the ledger ever stops deriving re_dated from that array, the guard is
    defending nothing and this test says so."""
    assert "_history_count(row.get(\"deadline_history\"))" in LEDGER
    assert "RE_DATED" in LEDGER


def test_the_precedent_for_the_guard_still_stands():
    """item_ledger already refuses to treat updated_at as somebody
    speaking, for exactly this reason. The guard is that rule applied to
    a second field, not a new opinion."""
    assert "none of them is anybody" in LEDGER


def test_an_edit_does_not_move_last_observed_at():
    """`last_observed_at` moves ONLY via the worker's re-observation
    path; an edit moves `updated_at` only. Writing it here would make an
    edit look like the item was said again."""
    src = _func_source("update_patch")
    assert "last_observed_at" not in src


# --- set, clear, refuse ------------------------------------------------

def test_empty_string_clears_the_date():
    """Matches permanence_override's convention ON THIS SAME ROUTE
    rather than inventing a second one for the same idea."""
    src = _func_source("update_patch")
    window = src.split("if update.deadline_date is not None:", 1)[1]
    assert 'if update.deadline_date == "":' in window
    assert 'value.pop("deadline_date", None)' in window


def test_omitting_the_field_leaves_the_stored_date_alone():
    src = _func_source("update_patch")
    assert "if update.deadline_date is not None:" in src


def test_a_malformed_date_is_REFUSED_here_unlike_on_create():
    """Deliberately different from the create route, and the difference
    is the point. On create, refusing loses the task the user just typed.
    Here the item already exists and is safe, the user is deliberately
    editing one field, and silently keeping the old date while answering
    200 would report a no-op as success."""
    src = _func_source("update_patch")
    window = src.split("if update.deadline_date is not None:", 1)[1].split("new_type", 1)[0]
    assert "INVALID_DEADLINE_DATE" in window
    assert "422" in window


def test_the_same_validator_as_create_is_used():
    """Two validators for one field would drift, and the stricter one
    would start rejecting values the other stored."""
    assert "_valid_calendar_day(update.deadline_date)" in _func_source("update_patch")


def test_the_new_date_is_echoed_back():
    """So a caller compares rather than assumes, and so a cleared date is
    distinguishable from an untouched one."""
    src = _func_source("update_patch")
    assert '"deadline_date": value.get("deadline_date")' in src


# --- the three-way name mismatch GP's audit surfaced -------------------

def test_patch_type_is_accepted_as_well_as_category():
    """GP modelled `category` while SS sends `patch_type`, so the key was
    dropped and every patch-type edit was a silent no-op.

    THEIR FIX ALONE DOES NOT CLOSE IT: once the key stops being dropped
    it arrives here, and this model would have ignored it for the same
    reason under a different roof. Three components, two names, and the
    mismatch survives any one of them being corrected alone.
    """
    model = MAIN.split("class PatchUpdate(BaseModel):", 1)[1].split("\n\n\n", 1)[0]
    assert re.search(r"^\s*patch_type:\s*Optional\[str\]\s*=\s*None", model, re.M)
    assert re.search(r"^\s*category:\s*Optional\[str\]\s*=\s*None", model, re.M)


def test_either_spelling_actually_reaches_the_type_update():
    """Declaring the field and not reading it would be the same bug with
    an extra step."""
    src = _func_source("update_patch")
    assert "update.category or update.patch_type" in src


def test_null_is_never_the_clear_sentinel_on_this_route():
    """Every clearable field on PATCH .../patches/{id} clears on "" and
    treats null as "not supplied". That is not a style choice: GP's proxy
    filters `if v is not None` on this route (one of five), so an
    explicit null is stripped before CQ sees it, and a client that sent
    null hoping to clear would get a 200 and no change, invisible from
    both ends. Verified against their source 2026-09-02. The error text
    used to say "or null", which invited exactly that."""
    body = _func_source("update_patch")
    assert 'update.deadline_date == ""' in body
    assert 'new_override == ""' in body
    assert "or null." not in MAIN
    model = MAIN.split("class PatchUpdate(")[1].split("\nclass ")[0]
    assert 'NOT null' in model
