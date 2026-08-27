"""Recall timings must mean what their names say.

WHAT THIS IS FOR, 2026-08-27. A recall took 740ms and its log line read
`redis_entity_lookup: 247.04`. That key was measured from the START OF
THE REQUEST while every neighbour was a delta, so it silently spanned a
vocabulary lookup that can touch Postgres and the render-cache read, and
the vocabulary lookup had no key of its own. The cue index lookup had no
key either, so its cost landed nowhere. I read the label, concluded
Redis was slow, sent another team a mechanism built on that, and had to
retract it.

The number was real. The NAME was wrong, and nothing in the line could
have revealed it, which is the whole point: an instrument that cannot
show its own blind spot is how a wrong attribution survives being
checked.
"""

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"
MAIN = (SRC / "main.py").read_text()


def _recall_body():
    """The recall endpoint ONLY. Deliberately excludes the constants and
    the `_stamp_recall_total` helper above it: both mention `t0` and the
    retired name legitimately, and a slice that swallowed them would make
    these tests pass on prose instead of on code."""
    i = MAIN.index('@app.post("/v1/recall"')
    return MAIN[i:MAIN.index("\n@app.", i + 10)]


def _helper():
    i = MAIN.index("def _stamp_recall_total")
    return MAIN[i:MAIN.index("return timings", i)]


def _declared(tuple_name):
    m = re.search(tuple_name + r" = \((.*?)\)", MAIN, re.S)
    return re.findall(r'"(\w+)"', m.group(1))


def test_every_step_timing_is_a_delta_never_from_request_start():
    """THE BUG. `- t0` is the wall clock for the WHOLE request, so any
    step key measured from it silently contains its predecessors. Only
    the helper that stamps `total` may use t0."""
    body = _recall_body()
    for m in re.finditer(r'timings\["(\w+)"\] = round\(\(time\.monotonic\(\) - (\w+)\)', body):
        key, start = m.group(1), m.group(2)
        assert start != "t0", (
            f'timings["{key}"] is measured from t0, the start of the request. '
            "That is exactly the defect this file exists for: it makes the key "
            "a cumulative total wearing a step's name."
        )


def test_the_vocabulary_lookup_has_a_key_of_its_own():
    """It can touch Postgres and it used to hide inside the key named
    for Redis."""
    body = _recall_body()
    assert 'timings["vocab_lookup_ms"]' in body
    stamp = body.index('timings["vocab_lookup_ms"]')
    call = body.index("_people_vocab_cached(app_id)")
    assert call < stamp, "the key must bound the call it names"


def test_the_cue_index_lookup_has_a_key_of_its_own():
    """Untimed before this, so its cost landed in no key at all."""
    body = _recall_body()
    assert 'timings["cue_index_ms"]' in body
    stamp = body.index('timings["cue_index_ms"]')
    call = body.index("smembers(cue_index_key)")
    assert call < stamp


def test_the_misleading_name_is_gone_and_the_new_one_bounds_its_step():
    """`redis_entity_lookup` never bounded only Redis. The replacement
    starts at the Redis call rather than at the request."""
    body = _recall_body()
    # the KEY must be gone; the comment explaining why may stay, and
    # should, since a comment cannot be wrong out loud
    assert 'timings["redis_entity_lookup"]' not in body
    assert 'timings["entity_index_ms"]' in body
    start = body.index("entity_t = time.monotonic()")
    call = body.index("smembers(entity_index_key)")
    stamp = body.index('timings["entity_index_ms"]')
    assert start < call < stamp


def test_every_step_key_ends_in_ms_so_the_rule_is_checkable():
    """Two keys did not, which is why a summing helper could not simply
    trust the suffix."""
    body = _recall_body()
    names = _declared("RECALL_STEP_TIMINGS")
    assert names, "the tuple must not be empty"
    for n in names:
        assert n.endswith("_ms"), n
    for legacy in ("postgres_entities_and_graph", "postgres_patches"):
        assert f'timings["{legacy}"]' not in body, f"{legacy} must carry the _ms suffix"


def test_every_declared_step_key_is_actually_stamped():
    """A key in the tuple that nothing stamps would silently count as
    zero and make the parts look complete when they are not."""
    body = _recall_body()
    for name in _declared("RECALL_STEP_TIMINGS"):
        assert f'timings["{name}"] = round(' in body, f"{name} is declared but never stamped"


def test_nested_timings_are_never_summed_into_the_parts():
    """`entity_index_rehydrated_ms` is measured INSIDE the entity index
    step. Adding it to the parts would double count and turn a real gap
    into a negative number."""
    step = _declared("RECALL_STEP_TIMINGS")
    for name in _declared("RECALL_NESTED_TIMINGS"):
        assert name not in step, f"{name} is nested and must not be summed"


def test_the_gap_between_the_parts_and_the_clock_is_published():
    """The fix for how the wrong attribution survived: if the steps do
    not add up to the wall clock, the difference has to appear as a
    number rather than hide inside whichever key starts earliest."""
    helper = _helper()
    assert 'timings["unaccounted_ms"]' in helper
    assert "RECALL_STEP_TIMINGS" in helper
    assert 'timings["total"]' in helper


def test_every_exit_from_recall_stamps_the_total_the_same_way():
    """Including the early returns. A blind spot that only appears on
    the slow path is the one you are reading when it matters."""
    body = _recall_body()
    assert body.count("_stamp_recall_total(timings, t0)") >= 2, "early returns too"
    # no exit inside recall may hand-roll the total and skip the accounting
    assert 'timings["total"] = round(' not in body
