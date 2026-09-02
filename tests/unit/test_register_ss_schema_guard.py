"""The registration script refuses an app that does not ingest.

2026-09-01: three manifest versions went to the app id ShoulderSurf
stopped using on 08-07, every POST answered 200, and the worker kept
extracting under the old wording for three weeks. Doc 19.6.
"""

import importlib.util
import pathlib
import re
from datetime import datetime, timedelta, timezone

SPEC = importlib.util.spec_from_file_location(
    "register_ss_schema", pathlib.Path("scripts/register_ss_schema.py"))
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)
SRC = pathlib.Path("scripts/register_ss_schema.py").read_text()

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
WRITERS = [("886a527b", NOW, 109), ("a3be3fee", NOW, 5)]
# The real 2026-09-01 shape: the pre-split id had ingested during the
# switchover, 20 days behind the app that writes now.
SWITCHOVER = [("886a527b", NOW, 3194), ("a3be3fee", NOW - timedelta(days=2), 17),
              ("930824d3", NOW - timedelta(days=20), 149)]


def test_a_writing_app_passes_and_says_so():
    ok, msg = MOD.judge_writer("886a527b", WRITERS)
    assert ok
    assert "109" in msg and "writer" in msg


def test_a_non_writing_app_is_refused_and_the_real_writers_are_named():
    """The refusal must hand the operator the right id, not just say no."""
    ok, msg = MOD.judge_writer("930824d3", WRITERS)
    assert not ok
    assert msg.startswith("REFUSED")
    assert "886a527b" in msg and "a3be3fee" in msg
    assert "--force" in msg


def test_nobody_writing_is_still_a_refusal():
    ok, msg = MOD.judge_writer("886a527b", [])
    assert not ok
    assert "no app has ingested" in msg


def test_an_app_twenty_days_behind_the_newest_ingest_is_refused():
    """A fixed 30-day window passed the pre-split id on 2026-09-01
    (149 patches, last on 08-12). Recency is relative to the app that
    actually writes, not to the calendar."""
    ok, msg = MOD.judge_writer("930824d3", SWITCHOVER)
    assert not ok
    assert "more than 14 days behind" in msg
    assert " * 886a527b" in msg and "   930824d3" in msg
    ok, _ = MOD.judge_writer("886a527b", SWITCHOVER)
    assert ok
    ok, _ = MOD.judge_writer("a3be3fee", SWITCHOVER)
    assert ok


def test_only_origin_bound_rows_count_as_ingest():
    """Derived lanes (profile pass, consolidation) stamp the source
    patches' app and keep a dead app looking alive: ghostpour's last
    write was 08-31 by that measure and 08-07 by this one."""
    assert "cp.origin_id IS NOT NULL" in MOD.WRITERS_SQL
    assert "acl.can_write" in MOD.WRITERS_SQL


def test_the_real_run_is_gated_and_check_only_reports():
    body = SRC.split("def do_register(")[1].split("\ndef ")[0]
    assert re.search(r"if writer_guard\(app_id, force\):\s*\n\s*return 1", body)
    check = SRC.split("def do_check(")[1].split("\ndef ")[0]
    assert "writer_guard(app_id, force=False, report_only=True)" in check


def test_no_database_url_refuses_unless_forced(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert MOD.writer_guard("x", force=False) == 1
    assert MOD.writer_guard("x", force=True) == 0
