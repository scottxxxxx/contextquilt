"""Where the description judge runs, and the client it must not reach for.

`store_entities` is a MODULE-LEVEL function with no `self`. The first
version of this lane called `self._get_llm_for_app(...)` inside it,
which raises NameError, which the surrounding guard swallows into
`same = None`, which resolves to APPEND, which is EXACTLY today's
behaviour in a lane whose entire purpose is that nothing ever confirms.

It would have been dead on arrival and looked healthy: no error surfaced,
no behaviour changed, and the only symptom would have been that the
thing this was built to fix stayed broken. That is the same class as the
gate that turned the headline writer into a no-op, and it is the third
today.

Read as source, the constraint every worker test here works under.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
WORKER = (SRC / "worker.py").read_text()


def _store_entities() -> ast.AsyncFunctionDef:
    for node in ast.walk(ast.parse(WORKER)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "store_entities":
            return node
    raise AssertionError("store_entities not found")


def test_store_entities_never_references_self():
    """The bug, asserted over the AST rather than by grep.

    A grep for "self." would also match the string in a docstring, and
    the point is whether a NAME resolves, which only the tree can say.
    """
    fn = _store_entities()
    names = [n.id for n in ast.walk(fn) if isinstance(n, ast.Name)]
    assert "self" not in names, (
        "store_entities reaches for `self`; it is module level, so this "
        "raises NameError, gets swallowed by the judge's guard, and "
        "silently appends forever"
    )


def test_the_client_arrives_as_a_parameter():
    fn = _store_entities()
    assert "llm" in [a.arg for a in fn.args.args]


def test_a_missing_client_raises_rather_than_appending_silently():
    """The guard must not be able to hide an absent client.

    `same = None` resolves to APPEND, which is indistinguishable from
    the judge running and disagreeing. Raising puts it in the log with
    a reason instead.
    """
    body = WORKER[WORKER.index("if verdict[\"action\"] == described_as.NEEDS_JUDGE:"):]
    body = body[:body.index("logger.info(\"described_as_judged\"")]
    assert "if llm is None:" in body
    assert "RuntimeError" in body


def test_every_ingest_call_site_passes_a_client():
    """Both lanes, because a lane without one silently keeps the old
    behaviour and nothing distinguishes that from the judge disagreeing."""
    tree = ast.parse(WORKER)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "store_entities"]
    assert len(calls) >= 2, "expected the extraction and structured lanes"
    ingest = [c for c in calls if len(c.args) >= 5]   # the two that pass metadata
    assert ingest, "no ingest-shaped call site found"
    for call in ingest:
        assert any(kw.arg == "llm" for kw in call.keywords), (
            "an ingest call site does not pass llm; that lane's "
            "descriptions will append forever with no error"
        )


def test_the_judge_never_raises_out_of_the_write_path():
    # A series is a nice-to-have and an ingest is not.
    body = WORKER[WORKER.index("if verdict[\"action\"] == described_as.NEEDS_JUDGE:"):]
    body = body[:body.index("logger.info(\"described_as_judged\"")]
    assert "except Exception" in body
    assert 'logger.warning("described_as_judge_failed"' in body


def test_the_outcome_is_logged_with_its_reason():
    # "confirmed", "the model disagreed" and "the judge broke" are three
    # different facts and produce two identical database states.
    assert 'logger.info("described_as_judged"' in WORKER
    block = WORKER[WORKER.index('logger.info("described_as_judged"'):][:300]
    for field in ("action=", "reason=", "similarity="):
        assert field in block


def test_there_is_a_kill_switch_and_off_means_todays_behaviour():
    """Off must restore the lexical path exactly, not some third thing.

    Matching CQ_SEMANTIC_DEDUP_ENABLED and CQ_ROLE_SEMANTICS_ENABLED.
    """
    assert "CQ_DESCRIPTION_JUDGE_ENABLED" in WORKER
    assert "judge_available=description_judge_enabled()" in WORKER
