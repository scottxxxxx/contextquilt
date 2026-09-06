"""The #450 backfill: give already-stored meetings the record they lack."""
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[2] / "scripts" / "backfill_origin_projects.py").read_text()


def test_it_writes_through_the_live_constant_not_its_own_sql():
    assert "from contextquilt.services.origin_project import" in SCRIPT
    assert "RECORD_INGEST_PROJECT_SQL" in SCRIPT
    assert "INSERT INTO origin_project_assignments" not in SCRIPT


def test_it_skips_every_meeting_that_already_has_a_decision_on_file():
    assert "NOT EXISTS" in SCRIPT and "FROM origin_project_assignments opa" in SCRIPT
    assert "opa.origin_type = COALESCE(cp.origin_type, 'meeting')" in SCRIPT


def test_a_stamped_patch_outranks_the_ingest_stream():
    """A rescope is newer than the payload that first arrived, so the
    stream is read only when no patch carries a project."""
    i = SCRIPT.index("for r in rows:")
    body = SCRIPT[i:i + 600]
    assert 'r["stamped_project_id"], r["stamped_project"], "patch"' in body
    assert "if not pid:" in body
    assert body.index('"patch"') < body.index("stream.get(")
    assert body.index("stream.get(") < body.index('src = "stream"')


def test_the_dry_run_is_the_default_and_says_so():
    assert 'ap.add_argument("--apply"' in SCRIPT
    assert "DRY RUN. Re-run with --apply to write." in SCRIPT
    assert "if not apply:" in SCRIPT


def test_a_meeting_with_no_project_anywhere_is_left_alone():
    assert "unresolved.append(r)" in SCRIPT
    assert "no project anywhere, left alone" in SCRIPT
