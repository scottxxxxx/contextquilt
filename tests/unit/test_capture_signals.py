"""The 12a capture-side signals: turn counts and deadline history.

Both exist because the raw material is derive-then-discard: transcripts
are not retained and a displaced deadline was previously overwritten
into nothing, so neither signal can ever be backfilled. These guards pin
the single-parser rule, the null-means-unknown discipline, and the
supersede-plus-history dedup behavior (which also FIXES a real bug: a
rescheduled deadline used to be silently ignored and the patch stayed
overdue against the stale date forever).
"""

import re
from pathlib import Path

from contextquilt.services.extraction_schema import (
    speaker_labels_in,
    speaker_turn_counts,
)

SRC = Path(__file__).resolve().parents[2] / "src"
WORKER = (SRC / "worker.py").read_text()
MAIN = (SRC / "main.py").read_text()

TRANSCRIPT = """
[Suresh Muchakurti] Let's start with the QA numbers.
[Scott (you)] The p95 is at 240ms.
[Suresh Muchakurti] That works. What about the fallback path?
[Speaker 3] (inaudible)
[Suresh Muchakurti] I'll take the fallback item.
[Pallavi] I can review it Thursday.
"""


def test_turn_counts_count_turns():
    counts = speaker_turn_counts(TRANSCRIPT, "Scott")
    assert counts["suresh muchakurti"] == 3
    assert counts["pallavi"] == 1


def test_placeholders_and_self_are_excluded():
    counts = speaker_turn_counts(TRANSCRIPT, "Scott")
    assert "speaker 3" not in counts
    assert "scott" not in counts  # the (you) speaker


def test_labels_derive_from_counts_one_parser():
    """speaker_labels_in is set(speaker_turn_counts(...)): the two can
    never disagree about who spoke (the shared-predicate rule)."""
    assert speaker_labels_in(TRANSCRIPT, "Scott") == set(
        speaker_turn_counts(TRANSCRIPT, "Scott")
    )


def test_migration_exists_with_no_backfill():
    mig = (SRC.parent / "init-db" / "34_appearance_turn_count.sql").read_text()
    assert "turn_count INTEGER" in mig
    assert "NO backfill" in mig


def test_appearance_writer_stamps_and_maxes():
    """NULL never clobbers a known value; re-ingest keeps the MAX,
    never sums."""
    m = re.search(r"INSERT INTO person_appearances.*?turn_count = CASE.*?END",
                  WORKER, re.DOTALL)
    assert m, "appearance writer does not stamp turn_count"
    assert "GREATEST(COALESCE(person_appearances.turn_count, 0), EXCLUDED.turn_count)" in m.group(0)


def test_merge_fold_carries_the_metric():
    """A merge folding appearances must not silently drop turn counts;
    same-meeting overlap keeps the max (one human, two labels)."""
    m = re.search(r"INSERT INTO person_appearances\n\s+\(user_id, entity_id, origin_id, origin_type,.*?turn_count\s+= CASE.*?END,",
                  MAIN, re.DOTALL)
    assert m, "merge fold drops turn_count"


def test_detail_meetings_serve_turn_count():
    assert '"turn_count": a["turn_count"]' in MAIN


def test_dedup_supersedes_and_keeps_history():
    """A differing re-observed deadline replaces the stale one (the old
    fill-only behavior silently DROPPED reschedules), appends the
    displaced date to deadline_history (capped), and clears a stale
    overdue_since so the sweep can re-judge against the new date."""
    m = re.search(r"A DIFFERENT date supersedes.*?value->>'deadline_date' <> \$1",
                  WORKER, re.DOTALL)
    assert m, "supersede branch missing"
    body = m.group(0)
    assert "deadline_history" in body
    assert "- 'overdue_since'" in body
    assert "jsonb_array_length" in body  # the cap
