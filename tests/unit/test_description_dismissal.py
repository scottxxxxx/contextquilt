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
