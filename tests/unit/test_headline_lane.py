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


def test_the_lane_and_the_backfill_share_one_query_builder():
    """Both callers ask the SERVICE, and the service is executed by a test.

    Written inline in the worker, the first version of this query
    filtered on `cp.user_id`, a column context_patches has not had since
    migration 26 dropped it. Every test here reads SOURCE, because
    worker.py cannot be imported without asyncpg, so the wrong column
    name was present in the text and every test passed. The lane never
    raises by design, so production would have swallowed the error and
    written zero headlines forever behind a warning nobody reads.

    A string cannot be wrong out loud; only an execution can. See
    test_headline_query_db.py, which runs it against a real Postgres.
    """
    assert "headlines.build_pending_fetch" in _lane()
    assert "headlines.build_pending_fetch" in BACKFILL
    for name, body in (("worker lane", _lane()), ("backfill", BACKFILL)):
        assert "SELECT" not in body, (
            f"{name} builds its own SQL again; the shared builder is the "
            "only copy a DB test executes"
        )


def test_the_builder_is_idempotent_by_query():
    # Idempotent by asking rather than by bookkeeping: a re-ingest, a
    # retry and the backfill can all cross one meeting without paying
    # twice or overwriting a line already written.
    from contextquilt.services.headlines import PENDING_SELECT
    assert "value->>'headline' IS NULL" in PENDING_SELECT
    assert "status = 'active'" in PENDING_SELECT


def test_the_builder_scopes_through_patch_subjects_not_a_user_column():
    """The exact defect, named.

    context_patches carries no user_id. patch_subjects carries the link.
    """
    from contextquilt.services.headlines import PENDING_SELECT
    assert "JOIN patch_subjects" in PENDING_SELECT
    assert "cp.user_id" not in PENDING_SELECT


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


# --------------------------------------------------------------------
# Wire-shape stability, which SS pays for and cannot see from their side
# --------------------------------------------------------------------

def test_a_failed_link_query_serves_null_rather_than_dropping_the_key():
    """Absent, empty and null were one observable on the failure path.

    `_attach_woven_links` used to return early when its query failed,
    leaving `stitched_to` off EVERY patch. Three states collapsed into
    one, and the wire shape varied under a condition the client cannot
    see. ShoulderSurf hit the consequence: a decoder requiring the key
    threw, the fetch returned nil, and the screen fell back to its local
    builder, which on a device is indistinguishable from a 404. A first
    successful deploy could have read as an undeployed route.

    Three states now, the same ones doc 16 uses for `capabilities`: a
    list means these are the links, `[]` means none, null means CQ could
    not tell.
    """
    main = (ROOT / "src" / "main.py").read_text()
    body = main[main.index("async def _attach_woven_links"):]
    body = body[:body.index("async def _woven_lifetime_totals")]
    handler = body[body.index("except Exception"):body.index("by_patch")]
    assert 'patch["stitched_to"] = None' in handler, (
        "the failure path drops the key again; absent and empty become "
        "one state and the client cannot tell a broken query from none"
    )


def test_the_empty_patch_list_shortcut_cannot_hide_the_same_bug():
    # `if not patches: return` is safe only because there is nothing to
    # stamp. Pinned so it is not later "tidied" into covering the
    # failure path too.
    main = (ROOT / "src" / "main.py").read_text()
    body = main[main.index("async def _attach_woven_links"):]
    body = body[:body.index("by_patch")]
    assert "if not patches:" in body


# --------------------------------------------------------------------
# The second pass, where it runs and what it must not cost
# --------------------------------------------------------------------

def test_both_callers_run_the_retry_pass():
    for name, body in (("worker lane", _lane()), ("backfill", BACKFILL)):
        assert "RETRY_SYSTEM" in body, f"{name} does not retry a refused line"
        assert "build_retry_content" in body, name


def test_a_failed_retry_does_not_cost_the_first_passs_headlines():
    """The lane already holds good lines when the retry is attempted.

    Losing them to a second call that failed would make the improvement
    a net loss, which is the shape of every "optimisation" that ships a
    regression.
    """
    lane = _lane()
    block = lane[lane.index('if out["retryable"]:'):lane.index('for pid, line in')]
    assert "except Exception" in block
    assert 'logger.warning("headline_retry_failed"' in block


def test_the_merge_is_delegated_rather_than_rewritten_here():
    """The merge lives in the service because a test could not see it here.

    Sabotage swapping `update` for an assignment discarded every
    first-pass headline and the whole suite stayed green, since these
    tests read source. `apply_retry` is executable and
    test_headlines.py exercises it directly, so the rule these callers
    have to follow is simply: do not do it yourself.
    """
    for name, body in (("worker lane", _lane()), ("backfill", BACKFILL)):
        assert "headlines.apply_retry" in body, name
        assert '["headlines"].update(' not in body, (
            f"{name} merges the retry itself; the executable version in "
            "the service is the only one a test covers"
        )


def test_the_retry_happens_at_most_once():
    # A stubborn fact costs two calls, never a loop. Pinned because a
    # while-loop here would be an unbounded spend on the exact inputs
    # that are hardest to satisfy.
    lane = _lane()
    assert lane.count("headlines.RETRY_SYSTEM") == 1
    assert "while" not in lane


# --------------------------------------------------------------------
# The writer's question is not the reader's
# --------------------------------------------------------------------

def test_the_writer_asks_whether_a_patch_COULD_tile_not_whether_it_does():
    """The bug that made the lane a no-op, caught by CI in one commit.

    The gate means a patch with no headline cannot earn a tile. The
    headline lane selects exactly the patches with no headline and then
    asks whether each could earn one. Asking the READER's question there
    answers `no_headline_written` for every candidate by construction,
    so the lane finds nothing to do and writes zero headlines forever
    while logging a perfectly healthy zero.

    Nothing in the unit suite could have caught it: every fixture here
    supplies a headline, because a tile needs one. The DB test that
    EXECUTES the fetch did.
    """
    from contextquilt.services.woven_digest import why_not_a_tile, DROP_NO_HEADLINE
    bare = {"patch_id": "a", "patch_type": "commitment", "origin_id": "m1",
            "value": {"text": "Send the revised scope by Thursday"}}
    assert why_not_a_tile(bare) == DROP_NO_HEADLINE          # the reader
    assert why_not_a_tile(bare, require_headline=False) is None  # the writer


def test_the_writer_still_honours_every_OTHER_prune_rule():
    # One function, one copy of the rules. Relaxing the headline check
    # must not relax person, shelved, sensitive, resolved or orphan.
    from contextquilt.services.woven_digest import why_not_a_tile
    for bad, why in (
        ({"patch_type": "person", "value": {"text": "Steven Williams"}}, "person"),
        ({"patch_type": "event", "value": {"text": "A thing happened"}}, "orphan"),
        ({"patch_type": "commitment", "origin_id": "m1",
          "value": {"text": "Ship it", "shelved_at": "2026-08-01"}}, "shelved"),
    ):
        assert why_not_a_tile(dict(bad, patch_id="x"),
                              require_headline=False) is not None, why


def test_both_writers_ask_the_writers_question():
    for name, body in (("worker lane", _lane()), ("backfill", BACKFILL)):
        assert "require_headline=False" in body, (
            f"{name} asks the reader's question; it would select nothing"
        )
