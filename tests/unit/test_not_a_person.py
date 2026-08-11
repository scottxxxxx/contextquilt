"""Not-a-person (boundary piece 3): the suppressed row is the negative
record.

The verb exists because removing Delete Memory from person rows removed
the only affordance, however wrong, a user had against ASR garbage, and
no identity verb handled "this was never a person". These guards pin
the exclusions (every serving surface), the durable-no property (the
row survives to absorb re-observations), and the reversal's cause guard
(a lift must never resurrect a patch that decay or a merge archived).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()
WORKER = (ROOT / "src" / "worker.py").read_text()
MIGRATION = (ROOT / "init-db" / "33_entity_suppression.sql").read_text()


def test_migration_adds_the_mark():
    assert "ADD COLUMN IF NOT EXISTS suppressed_at" in MIGRATION
    assert "ADD COLUMN IF NOT EXISTS suppressed_source" in MIGRATION


def test_recall_index_excludes_suppressed_names_and_aliases():
    """Both legs: the entity's own name AND its aliases leave the index,
    or recall keeps greeting garbage the user disowned."""
    m = re.search(r'ENTITY_INDEX_NAMES_SQL = """(.*?)"""', MAIN, re.DOTALL)
    assert m
    sql = m.group(1)
    assert sql.count("suppressed_at IS NULL") == 2


def test_people_list_excludes_suppressed():
    assert "AND e.merged_into IS NULL AND e.suppressed_at IS NULL" in MAIN


def test_identity_verbs_refuse_suppressed_rows():
    """The standard loader raises SUPPRESSED, so merge, rename, confirm,
    keep-separate and the detail route all refuse in one place."""
    assert '"SUPPRESSED"' in MAIN


def test_the_verb_pair_exists():
    assert '@app.post("/v1/people/{user_id}/{entity_id}/not-a-person"' in MAIN
    assert '@app.delete("/v1/people/{user_id}/{entity_id}/not-a-person"' in MAIN


def test_suppression_tombstones_ride_the_people_delta():
    assert "OR suppressed_at > $2" in MAIN


def test_patches_archive_with_the_cause_and_lift_is_cause_guarded():
    """Suppress archives person patches with cause not_a_person; the
    lift restores ONLY rows carrying that cause, so it can never
    resurrect what decay, a merge, or the user's own delete archived."""
    assert MAIN.count("'\"not_a_person\"'") == 1
    assert "cp.value->>'archive_cause' = 'not_a_person'" in MAIN


def test_appearances_stop_for_suppressed_entities():
    """SS's condition: the row absorbs re-observations (the durable-no
    property) but accumulates no meeting history."""
    m = re.search(r"def _record_appearance.*?INSERT INTO person_appearances",
                  WORKER, re.DOTALL)
    assert m
    assert "suppressed_at" in m.group(0)
