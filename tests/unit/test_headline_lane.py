"""The headline lane: where it runs, what it pays for, what it must not touch.

Woven handoff section 6.3. `services/headlines.py` already owns whether a
line is VALID and is tested separately; this file is about the lane that
calls it, which is where the expensive mistakes live.

Read as source, because worker.py cannot be imported without asyncpg,
which is the constraint every other worker test here works under. That
constraint is exactly why these invariants are pinned rather than left in
a comment: a comment cannot be wrong out loud.
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKER = (ROOT / "src" / "worker.py").read_text()
DIGEST = (ROOT / "src" / "contextquilt" / "services" / "woven_digest.py").read_text()
BACKFILL = (ROOT / "scripts" / "backfill_headlines.py").read_text()


def _lane() -> str:
    start = WORKER.index("async def _generate_headlines(")
    return WORKER[start:WORKER.index("async def _extract_semantic_role_signals(", start)]


# --------------------------------------------------------------------
# The decay clock. This is the one that would have been expensive.
# --------------------------------------------------------------------

def test_the_lane_does_not_move_updated_at():
    """A headline is presentation, not a new observation.

    There is no trigger on context_patches, so `updated_at` moves only
    if something sets it. Types that are neither self-typed nor
    completable anchor their DECAY on `updated_at`, so stamping it here
    would extend the life of every patch the lane touched, and the
    backfill would bulk-shift the decay clock of the entire quilt as a
    side effect of writing a label.
    """
    for name, body in (("worker lane", _lane()), ("backfill", BACKFILL)):
        update = body[body.index("UPDATE context_patches"):]
        update = update[:update.index("WHERE")]
        assert "updated_at" not in update, f"{name} moves updated_at"
        assert "last_observed_at" not in update, f"{name} moves last_observed_at"


# --------------------------------------------------------------------
# What it pays for
# --------------------------------------------------------------------

def test_eligibility_comes_from_the_live_predicate_not_a_second_list():
    """`why_not_a_tile` decides what can tile, in both places.

    A restated type list here would be a second source of truth that
    drifts silently, and this one would drift toward paying a model to
    headline patches the quilt can never show.
    """
    assert "woven_digest.why_not_a_tile" in _lane()
    assert "woven_digest.why_not_a_tile" in BACKFILL


def test_the_lane_only_selects_patches_that_have_no_headline_yet():
    # Idempotent by query rather than by bookkeeping: a re-ingest, a
    # retry and the backfill can all cross the same meeting without
    # paying twice or overwriting a line already written.
    assert "value->>'headline' IS NULL" in _lane()
    assert "value->>'headline' IS NULL" in BACKFILL


def test_the_call_is_batched_and_capped():
    # Batched so the instruction is paid once per batch rather than once
    # per patch; capped so one enormous meeting cannot turn a lane
    # priced at $0.35 per 30 days into an open-ended one.
    assert "HEADLINE_BATCH" in _lane() and "HEADLINE_MAX_PER_MEETING" in _lane()


def test_the_tunables_are_module_scope_not_coroutine_local():
    """The worker gotcha CLAUDE.md names, pinned.

    "Constants" defined inside a coroutine body are invisible to every
    other loop, and a NameError in any gathered loop crash-loops the
    whole worker.
    """
    head = WORKER[:WORKER.index("class ColdPathWorker")]
    assert "HEADLINE_BATCH = 25" in head
    assert "HEADLINE_MAX_PER_MEETING = 100" in head


# --------------------------------------------------------------------
# Where it runs. Both sites, for the reason the extraction gate learned.
# --------------------------------------------------------------------

def test_it_runs_on_the_main_path_after_every_writing_lane():
    """Last among the write lanes, because it reads back what they stored.

    Running before the behavior lane would headline the main extraction
    and miss the behavior patches, which do tile.
    """
    main = WORKER.index("# Behavior observations: their own call")
    tail = WORKER[main:main + 1600]
    assert "_generate_headlines" in tail
    assert tail.index("_extract_behavior_observations") < tail.index("_generate_headlines")


def test_it_also_runs_in_the_GATED_branch():
    """The 18-patches-across-8-meetings shape, one lane later.

    The extraction gate's own test pins that the gated branch still runs
    the behavior lane. Those behavior patches can tile, so a gated
    meeting that skipped headlines would show raw fact text while every
    other meeting showed a written line, which reads as a rendering bug
    rather than as a gate.
    """
    start = WORKER.index('"extraction_skipped"')
    branch = WORKER[start:WORKER.index("response = await llm.extract(", start)]
    assert "_generate_headlines" in branch, (
        "the gated branch no longer writes headlines; its behavior "
        "patches would tile with raw fact text"
    )
    assert branch.index("_generate_headlines") < branch.rindex("return")


# --------------------------------------------------------------------
# Failure posture
# --------------------------------------------------------------------

def test_the_lane_never_raises():
    # It runs after everything real is already stored, so a failure here
    # must cost one call rather than the meeting.
    lane = _lane()
    assert "except Exception" in lane
    assert lane.rstrip().endswith("return 0")


def test_a_dead_batch_does_not_cost_the_rest_of_the_backfill():
    body = BACKFILL[BACKFILL.index("for start in range"):]
    assert "except Exception" in body and "continue" in body


def test_the_backfill_is_dry_run_by_default():
    # House rule for every script in scripts/: nothing writes without
    # being told to.
    assert '"--apply", action="store_true"' in BACKFILL
    assert "if args.apply:" in BACKFILL


# --------------------------------------------------------------------
# What the wire carries
# --------------------------------------------------------------------

def test_null_is_served_rather_than_the_key_being_absent():
    """A refused line and a pre-lane patch are the same served state.

    Section 6.3 is enforced by refusal rather than repair, since every
    repair available is a truncation. So null is a REAL state the client
    falls back from, not a missing value, and the key must always be
    present or the client cannot tell null from a dropped field.
    """
    assert '"headline": _value(patch).get("headline") or None,' in DIGEST


def test_the_seam_route_carries_the_headline():
    """The seam builds its own dict, so it needs the field by name.

    The two woven routes reach the wire by DIFFERENT mechanisms and this
    test asserted the wrong property for one of them on the first run:
    it looked for the literal string in both, and the digest route never
    names the field because it does not build the dict.
    """
    main = (ROOT / "src" / "main.py").read_text()
    body = main[main.index("async def woven_meeting_seam("):]
    body = body[:body.index("async def _attach_woven_links")]
    assert '"headline": value.get("headline") or None,' in body


def test_the_digest_route_serves_the_service_output_verbatim():
    """And the digest route needs the opposite property.

    It returns what `build_digest` produced rather than re-projecting
    it, which is why a field added to the service reaches the wire
    without the route being touched. A route that rebuilt the dict would
    be a second place to forget a field, which is the class GP keeps
    hitting from the other side.
    """
    main = (ROOT / "src" / "main.py").read_text()
    body = main[main.index("async def woven_digest("):]
    body = body[:body.index("@app.get(\"/v1/quilt/{user_id}/meetings")]
    assert 'digest["patches"]' in body
    assert '"fact"' not in body, (
        "the digest route is re-projecting service output; a field added "
        "to build_digest would silently not reach the wire"
    )
