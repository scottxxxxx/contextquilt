"""The project-delete confirmation read, EXECUTED.

Scott's idea was: when you delete a project, offer to remove the people
who appear nowhere else. Measured on prod before anything was built, the
naive version of that rule matched 305 people, and 51 of them provably
owned memory outside the project, including the account owner three times
over at 157 patches each. 96 of 305 were clean by every test.

So this route exists to SHOW that, not to act on it. What it must never
do is collapse the answer into a boolean, because the middle category is
most of the population and a boolean hides it.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN_SRC = (ROOT / "src" / "main.py").read_text()

PROJECT = "F097ADB4-24FC-46AF-B32C-B37B3BBEB1F9"
USER = "u-1"


def _func_source(name: str) -> str:
    tree = ast.parse(MAIN_SRC)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(MAIN_SRC, node)
            assert seg, name
            return textwrap.dedent(seg)
    raise AssertionError(f"{name} not found")


def _module_value(name: str):
    tree = ast.parse(MAIN_SRC)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found at module level")


DEFINITION = _module_value("AFFECTED_PEOPLE_DEFINITION")
CONFIDENCE = _module_value("AFFECTED_PEOPLE_CONFIDENCE")
LIMIT = _module_value("AFFECTED_PEOPLE_LIMIT")


def _row(name, here=2, owns=0, hits=0, edges=0, dupes=1, eid=None):
    return {
        "entity_id": eid or f"e-{name}",
        "name": name,
        "appearances_in_project": here,
        "owns_elsewhere": owns,
        "name_hits_elsewhere": hits,
        "graph_edges": edges,
        "entities_with_this_name": dupes,
    }


def _build(rows, project_name="GL Unlimited"):
    class _Pool:
        async def fetch(self, sql, *args):
            return rows

        async def fetchval(self, sql, *args):
            return project_name

    async def _vocab(app_id):
        # The caller's vocabulary, not a literal. The route resolves the
        # person type through this, and a stub returning a fixed name is
        # fine here because what is under test is the CLASSIFICATION, not
        # the lookup; `test_people_vocabulary.py` guards the lookup.
        return SimpleNamespace(person_type="person", person_entity_type="person")

    ns: dict = {
        "db_pool": _Pool(),
        "_people_vocab_cached": _vocab,
        "Depends": lambda dep: None,
        "verify_application_access": lambda: None,
        "AFFECTED_PEOPLE_DEFINITION": DEFINITION,
        "AFFECTED_PEOPLE_CONFIDENCE": CONFIDENCE,
        "AFFECTED_PEOPLE_LIMIT": LIMIT,
    }
    exec(compile(_func_source("project_affected_people"), "main.py", "exec"), ns)
    return ns["project_affected_people"]


# ----------------------------------------------------------------------
# The three states, which are the whole point
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_account_owner_is_reported_as_appearing_elsewhere():
    """The measurement that got this built, as a test.

    Scott owns 157 patches outside the project and the naive rule
    nominated him three times. If this ever comes back `appears_only_here`
    the route is lying in the most expensive direction available.
    """
    fn = _build([_row("Scott", here=4, owns=157, dupes=3)])

    result = await fn(USER, PROJECT, app_id="ss")

    person = result["people"][0]
    assert person["confidence"] == "appears_elsewhere"
    assert person["signals"]["owns_patches_elsewhere"] == 157


@pytest.mark.asyncio
async def test_a_substring_only_hit_is_uncertain_never_clean():
    """The name test matches 'Ian' inside 'Brian'.

    A weak signal must degrade to unresolved, not to clean. Rounding
    uncertainty toward the convenient answer is how a screen ends up
    recommending a deletion nobody checked.
    """
    fn = _build([_row("Ian", hits=3)])

    result = await fn(USER, PROJECT, app_id="ss")

    assert result["people"][0]["confidence"] == "uncertain"


@pytest.mark.asyncio
async def test_graph_edges_alone_are_also_uncertain():
    fn = _build([_row("Dana", edges=2)])

    result = await fn(USER, PROJECT, app_id="ss")

    assert result["people"][0]["confidence"] == "uncertain"


@pytest.mark.asyncio
async def test_clean_requires_every_signal_to_agree():
    fn = _build([_row("Ana")])

    result = await fn(USER, PROJECT, app_id="ss")

    assert result["people"][0]["confidence"] == "appears_only_here"


# ----------------------------------------------------------------------
# What SS asked for and could not compute themselves
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_bare_boolean_anywhere_in_the_payload():
    """SS asked for the uncertainty rather than a verdict.

    A `safe_to_remove` key would make the client infer the reason from an
    absence, which is the exact shape this group keeps paying for.
    """
    fn = _build([_row("Ana"), _row("Scott", owns=157)])

    result = await fn(USER, PROJECT, app_id="ss")

    flat = repr(result)
    for banned in ("safe_to_remove", "should_remove", "removable"):
        assert banned not in flat, banned
    for p in result["people"]:
        assert set(p["signals"]) == {
            "owns_patches_elsewhere", "name_hits_elsewhere_uncertain", "graph_edges"
        }


@pytest.mark.asyncio
async def test_duplicate_names_are_flagged_per_row_and_counted():
    """A user reading nine names cannot tell that two are one person twice.

    Alex resolves to 4 entities on prod and the account owner to 3.
    """
    fn = _build([_row("Alex", dupes=4, eid="e-a1"),
                 _row("Alex", dupes=4, eid="e-a2"),
                 _row("Ana", dupes=1)])

    result = await fn(USER, PROJECT, app_id="ss")

    assert result["duplicate_names"] == 2
    assert [p["entities_with_this_name"] for p in result["people"]] == [4, 4, 1]


@pytest.mark.asyncio
async def test_the_total_is_computed_before_the_cap():
    """Or the client renders a number that is quietly the page size."""
    fn = _build([_row(f"P{i}") for i in range(LIMIT + 25)])

    result = await fn(USER, PROJECT, app_id="ss")

    assert result["total_affected"] == LIMIT + 25
    assert result["returned"] == LIMIT
    assert result["truncated"] is True
    assert len(result["people"]) == LIMIT


@pytest.mark.asyncio
async def test_counts_by_confidence_cover_every_state_even_at_zero():
    """Present and zero, so a client can test the key rather than its
    absence, and so a category never silently disappears from a screen."""
    fn = _build([_row("Ana")])

    result = await fn(USER, PROJECT, app_id="ss")

    assert set(result["counts_by_confidence"]) == set(CONFIDENCE)
    assert result["counts_by_confidence"]["appears_only_here"] == 1
    assert result["counts_by_confidence"]["appears_elsewhere"] == 0


# ----------------------------------------------------------------------
# Doc 16 5.13: the definition travels
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_definition_and_vocabulary_are_on_the_wire():
    """A docstring cannot reach the person reading the screen.

    The client renders the sentence to a human and cannot render a caveat
    it was never sent.
    """
    fn = _build([_row("Ana")])

    result = await fn(USER, PROJECT, app_id="ss")

    assert result["definition"] == DEFINITION
    assert set(result["vocabulary"]["confidence"]) == set(CONFIDENCE)


def test_the_definition_refuses_to_recommend():
    """This route answers a question. It must not read as advice, because
    the decision it feeds is a deletion and the rule behind it was
    measured wrong for two thirds of the population."""
    lowered = DEFINITION.lower()
    # The disclaimer, however it is worded. Asserting a literal phrase
    # here would pin a spelling, and a spelling-pinned test reports a
    # rewrite as a regression (see #417's re-anchoring).
    assert "recommendation" in lowered and "remove" in lowered
    assert "nothing here" in lowered or "not a recommendation" in lowered
    # And the predicate itself, so the definition cannot drift away from
    # what the query actually does.
    assert "every meeting" in lowered


@pytest.mark.asyncio
async def test_an_empty_result_still_carries_the_definition():
    """Nobody affected is an ANSWER, not a blank. The screen still has to
    say what it looked for."""
    fn = _build([])

    result = await fn(USER, PROJECT, app_id="ss")

    assert result["total_affected"] == 0
    assert result["people"] == []
    assert result["definition"] == DEFINITION
    assert result["counts_by_confidence"]["appears_only_here"] == 0
