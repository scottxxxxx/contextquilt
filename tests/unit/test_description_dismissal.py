"""A meeting's perception of a person can be wrong, and nothing could say so.

The case (2026-08-31): the meetings described Steven Williams as "an
immigration attorney exploring automation of case intake". He is not an
attorney. His STATED role already won the `title` precedence rule and
the card correctly showed "Stated, not inferred" — that part worked.
But `entity_descriptions` had no status column, no API write path, and
chat corrections operate on `context_patches` and never arrive there, so
the inferred series kept rendering underneath the correct title and
`who_they_are` kept opening with "Early meetings cast this person as an
immigration attorney".

These tests read source rather than executing routes, because main.py
cannot be imported without fastapi and asyncpg, which is the constraint
every other route test here works under. They pin the invariants that
would otherwise only live in a comment.
"""

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
MAIN = (SRC / "main.py").read_text()
WORKER = (SRC / "worker.py").read_text()
MIGRATION = (Path(__file__).resolve().parents[2]
             / "init-db" / "46_description_dismissal.sql").read_text()


# --------------------------------------------------------------------
# The row survives. This is the whole design.
# --------------------------------------------------------------------

def test_the_migration_adds_a_flag_and_never_a_delete():
    """Retained, not deleted: the meeting did say it.

    Deleting would destroy a true record of what was said AND make "the
    user rejected this" indistinguishable from "this was never
    observed", which is the argument that shaped `shelve`.
    """
    assert "ADD COLUMN IF NOT EXISTS dismissed_at" in MIGRATION
    upper = MIGRATION.upper()
    assert "DROP TABLE" not in upper
    assert "DELETE FROM" not in upper


def test_the_dismissal_carries_its_cause_not_just_its_fact():
    # Ship the count AND make the cause recoverable: a year out, "the
    # user dismissed this from the card" and "a chat correction
    # superseded it" must still be distinguishable.
    for column in ("dismissed_source", "dismissed_note", "superseded_by_patch_id"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in MIGRATION


def test_the_index_matches_the_actual_access_pattern():
    # Every read filters on live rows, so the partial index is the whole
    # access pattern rather than a reporting convenience.
    assert "WHERE dismissed_at IS NULL" in MIGRATION


# --------------------------------------------------------------------
# Every reader excludes dismissed rows. Missing one is the whole bug.
# --------------------------------------------------------------------

def test_the_two_SERVING_reads_exclude_dismissed():
    """The served series and the who_they_are inputs both filter.

    These are the two reads that put a description in front of a user,
    directly or through a synthesis. Missing either one renders text the
    user already rejected.
    """
    served = MAIN[MAIN.index("FROM entity_descriptions"):][:400]
    assert "dismissed_at IS NULL" in served, "the served series has no filter"

    inputs = WORKER[WORKER.index("SELECT description, first_origin_id"):][:400]
    assert "dismissed_at IS NULL" in inputs, "who_they_are inputs have no filter"


def test_the_WRITE_path_lookup_deliberately_sees_dismissed_rows():
    """And this is the subtle one, so it is pinned rather than commented.

    The write path fetches the latest description to classify a new
    observation as CONFIRM or CHANGE. It must see dismissed rows. If it
    filtered them out, a meeting re-inferring the rejected text would
    find no match, take the CHANGE branch, and INSERT A FRESH LIVE ROW,
    which silently undoes the user's dismissal on the next mention.

    Seeing them, the same text takes the CONFIRM branch instead: the
    observation count rises, because the observation is real, and the
    row stays dismissed. Both truths kept. The user's correction sticks
    even while the extractor keeps disagreeing with it.
    """
    lookup = WORKER[WORKER.index("SELECT description_id, description, last_origin_id"):]
    lookup = lookup[:lookup.index("ORDER BY")]
    assert "dismissed_at" not in lookup, (
        "the write-path lookup must NOT filter dismissed rows, or a "
        "re-inference inserts a new live row and undoes the dismissal"
    )


def test_the_who_they_are_candidate_count_also_excludes_dismissed():
    # The count gates which people the profile pass spends budget on. A
    # person whose only perception is dismissed should not look rich.
    assert "AND d.dismissed_at IS NULL) AS perceptions" in WORKER


# --------------------------------------------------------------------
# The two affordances
# --------------------------------------------------------------------

def test_both_verbs_exist_and_are_reversible():
    assert '@app.post("/v1/people/{user_id}/{entity_id}/descriptions/dismiss"' in MAIN
    assert '@app.delete("/v1/people/{user_id}/{entity_id}/descriptions/dismiss"' in MAIN


def test_undismiss_clears_every_stamp():
    # Nothing was destroyed, so nothing needs reconstructing.
    body = MAIN[MAIN.index("async def undismiss_descriptions"):]
    body = body[:body.index("return {")]
    for column in ("dismissed_at = NULL", "dismissed_source = NULL",
                   "dismissed_note = NULL"):
        assert column in body, f"undismiss leaves {column} set"


def test_a_note_is_routed_to_the_existing_correction_lane():
    """"Correct this" reuses handle_correction rather than a new writer.

    A second path that supersedes patches would be a second source of
    truth about one person. The existing lane already archives the
    contradicted patch, lands the new fact as origin_mode='declared',
    and connects them with `replaces`.
    """
    body = MAIN[MAIN.index("async def dismiss_descriptions"):]
    body = body[:body.index("async def undismiss_descriptions")]
    assert 'if payload.note:' in body
    assert '"task_type": "correction"' in body
    assert '"subject_entity_id": entity_id' in body


def test_a_failed_correction_enqueue_cannot_fail_the_dismissal():
    # The dismissal has already taken effect on this response; the patch
    # rewrite is cold-path work and must not cost the user the thing
    # they actually asked for.
    body = MAIN[MAIN.index("if payload.note:"):]
    body = body[:body.index("return {")]
    assert "try:" in body and "except Exception" in body


def test_the_response_says_what_happened_rather_than_assuming_it():
    body = MAIN[MAIN.index("async def dismiss_descriptions"):]
    body = body[:body.index("async def undismiss_descriptions")]
    for key in ('"dismissed"', '"correction_enqueued"', '"who_they_are"'):
        assert key in body


def test_the_summary_is_not_rewritten_inline():
    """`who_they_are` regenerates on its own fingerprint rule.

    Dropping the rows from its INPUT set is what makes a dismissal take
    effect. Rewriting the summary here would be a second synthesis path,
    on a read route, calling a model. The response says "regenerating"
    instead of pretending to a fresh sentence.
    """
    body = MAIN[MAIN.index("async def dismiss_descriptions"):]
    body = body[:body.index("async def undismiss_descriptions")]
    assert '"who_they_are": "regenerating"' in body
    assert "CQ_WHO_THEY_ARE_MODEL" not in body


# --------------------------------------------------------------------
# The syntheses built from rejected readings go too, immediately
# --------------------------------------------------------------------

def _dismiss_body() -> str:
    body = MAIN[MAIN.index("async def dismiss_descriptions"):]
    return body[:body.index("async def undismiss_descriptions")]


def test_a_dismissal_archives_the_summaries_built_from_those_readings():
    """Steven Williams, 2026-08-31, and the reason this is not optional.

    The user marked the "who they are" paragraph inaccurate. All four
    readings were stamped, the route answered that the summary was
    regenerating, and the paragraph was STILL ACTIVE in the database and
    still being served when he looked again. `who_they_are` regenerates
    only when its input fingerprint changes, on a periodic worker pass,
    so "it will be here next time you look" was a promise this endpoint
    could not keep.
    """
    body = _dismiss_body()
    assert "status = 'archived'" in body
    assert '"who_they_are", "trajectory"' in body


def test_the_trajectory_card_goes_with_it():
    """It is the OTHER synthesis of the same series.

    It kept narrating the rejected arc, "first seen as a
    practitioner-buyer, then reidentified as an advisor", directly under
    a paragraph the user had just struck through.
    """
    assert '"trajectory"' in _dismiss_body()


def test_only_the_syntheses_are_archived_never_the_whole_card_stack():
    # The insight lenses are separate claims with their own evidence. A
    # user rejecting a characterisation is not rejecting every
    # observation ever made about the person, and the durable-no rule is
    # PER LENS for the same reason.
    body = _dismiss_body()
    lens_filter = body[body.index("value->>'lens' = ANY"):][:120]
    assert "$1::text[]" in lens_filter


def test_archiving_stamps_a_cause_rather_than_vanishing():
    # Every archive site stamps `value.archive_cause` (doc 16 5.7), so a
    # year out "the user rejected this" is still distinguishable from
    # decay, replacement or cleanup.
    assert """'{archive_cause}', '"corrected"'""" in _dismiss_body()


def test_a_failed_archive_cannot_fail_the_dismissal():
    # The dismissal is what the user actually asked for and it has
    # already taken effect. Losing a card must not cost them that.
    body = _dismiss_body()
    block = body[body.index("syntheses_archived = 0"):body.index("if payload.note:")]
    assert "try:" in block and "except Exception" in block


def test_the_response_reports_what_was_archived_rather_than_assuming_it():
    # The count is the difference between a summary that was withdrawn
    # and one still sitting on somebody's screen, and the previous
    # response could not tell the caller which.
    assert '"syntheses_archived": syntheses_archived,' in _dismiss_body()


def test_the_dismissal_speaks_the_apps_person_type_not_the_literal():
    """The People surface was born speaking ShoulderSurf's dialect.

    A literal here is the overfit the vocabulary exists to prevent, and
    it is guarded by test_people_vocabulary as a family. This one caught
    the first version of this code.
    """
    body = _dismiss_body()
    assert "_people_vocab_cached" in body
    assert "patch_type = 'person'" not in body
    # AND THAT THE RESULT IS ACTUALLY USED. Sabotage put the literal
    # back in the PARAMETER position, leaving the vocabulary lookup
    # sitting above it unused, and this test passed: it had checked that
    # the call existed rather than that its answer reached the query.
    # A resolved value nobody passes is the same as never resolving it.
    lookup = body[body.index("person_patch_ids = await db_pool.fetch"):]
    lookup = lookup[:lookup.index(")")]
    assert "dismiss_vocab.person_type" in lookup, (
        "the resolved person type never reaches the query"
    )
    assert '"person"' not in lookup


# --------------------------------------------------------------------
# Saying so out loud while it settles
# --------------------------------------------------------------------

def test_the_person_detail_says_when_a_correction_is_still_settling():
    """Scott's ask on 2026-08-31, in his words: a way for the app to show
    that we are reconciling and that there may be an inaccuracy until it
    does.

    Three things happen at three speeds when a characterisation is
    rejected: readings stamped now, syntheses archived now, replacement
    summary written by the worker whenever it next runs. In between,
    `who_they_are` is legitimately absent, and without this field that
    gap is indistinguishable from "this person never had a summary".
    """
    assert '"reconciling": reconciling,' in MAIN
    block = MAIN[MAIN.index("reconciling = None"):MAIN.index('"described_as": described_as_series')]
    for key in ('"since"', '"dismissed_readings"', '"correction_recorded"'):
        assert key in block, key


def test_a_summary_written_BEFORE_the_rejection_does_not_clear_it():
    """Otherwise a stale card marks the person settled.

    A summary derived before the user objected was built from the very
    material they rejected, so its existence is not evidence that the
    replacement has been written.
    """
    block = MAIN[MAIN.index("reconciling = None"):MAIN.index('"described_as": described_as_series')]
    assert "newest_card is None or" in block
    assert "newest_card < rec[\"latest\"]" in block


def test_reconciling_is_derived_rather_than_stored():
    # A stored flag needs clearing, and the thing that would clear it is
    # a worker pass with no reason to know the field exists.
    block = MAIN[MAIN.index("reconciling = None"):MAIN.index('"described_as": described_as_series')]
    assert "UPDATE" not in block and "INSERT" not in block


def test_a_failed_reconciling_check_serves_null_rather_than_settled():
    # Null means CQ cannot tell, which is honest. Claiming a person is
    # settled because a query failed is not.
    block = MAIN[MAIN.index("reconciling = None"):MAIN.index('"described_as": described_as_series')]
    assert "except Exception" in block
    assert 'logger.warning("reconciling_state_unavailable"' in block
