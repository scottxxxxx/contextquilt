"""The day a meeting happened, recorded rather than spent.

Stage 0 of doc 21, ruled by Scott 2026-08-27. Until now CQ persisted no
meeting date at all: `payload.timestamp` arrived at ingest, built the
`Meeting date:` line that lets the model resolve "by Friday", and was
dropped. Every surviving timestamp was an INGEST clock, which is why the
trajectory lens splits by meeting SEQUENCE and ships `origin_id` for the
client to lay on a time axis.

These tests pin the two things that make the new row trustworthy: it is
written from the PARSED date rather than a sliced string, and a re-ingest
that carries no timestamp can never erase one that does.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKER = (ROOT / "src" / "worker.py").read_text()
MIGRATION = (ROOT / "init-db" / "41_meeting_origins.sql").read_text()


def _ingest():
    i = WORKER.index("async def handle_meeting_summary")
    return WORKER[i:WORKER.index("\n    async def ", i + 10)]


def test_the_table_holds_a_date_and_requires_one():
    """A row exists only when the app told us a day, so a missing row is
    'not told' and never 'undated'. A nullable column would make the two
    indistinguishable."""
    assert "CREATE TABLE IF NOT EXISTS meeting_origins" in MIGRATION
    assert re.search(r"meeting_date\s+DATE NOT NULL", MIGRATION)
    assert "PRIMARY KEY (user_id, origin_id)" in MIGRATION


def test_the_table_has_no_app_id_column():
    """Doc 18: each app gets its own subject space, and there are no
    per-app read filters or app_id columns to add."""
    body = MIGRATION[MIGRATION.index("CREATE TABLE"):MIGRATION.index("PRIMARY KEY")]
    assert "app_id" not in body


def test_the_date_is_a_DATE_and_not_a_timestamp():
    """What the app reports is a calendar day, the bucket boundary the
    spec asks for is a calendar month, and a time of day would invite
    timezone arithmetic on a number that never had that precision."""
    assert "meeting_date   DATE NOT NULL" in MIGRATION or \
        re.search(r"meeting_date\s+DATE NOT NULL", MIGRATION)
    assert not re.search(r"meeting_date\s+TIMESTAMP", MIGRATION)


def test_ingest_writes_the_row_from_the_PARSED_date():
    """Not from the raw timestamp. The existing parse handles the Z
    suffix and yields None on anything it cannot read; slicing the
    string would write a confident wrong day for any unexpected format."""
    body = _ingest()
    assert "INSERT INTO meeting_origins" in body
    assert "if origin_id and meeting_date:" in body
    assert "str(timestamp)[:10]" not in body, "the string slice is the bug this avoids"
    parse = body.index("meeting_date = datetime.fromisoformat")
    write = body.index("INSERT INTO meeting_origins")
    assert parse < write, "the parse must run before the write that uses it"


def test_a_reingest_without_a_timestamp_cannot_erase_a_date():
    """A re-ingest is the same observation arriving twice (doc 19.4). A
    backdated import may CORRECT the date; a payload that simply omits
    one must leave the stored date alone, which the gate guarantees by
    never reaching the write at all."""
    body = _ingest()
    gate = body.index("if origin_id and meeting_date:")
    write = body.index("INSERT INTO meeting_origins")
    assert gate < write
    # and the upsert never writes a NULL date over a real one
    stmt = body[write:write + 900]
    assert "SET meeting_date = EXCLUDED.meeting_date" in stmt
    assert "IS DISTINCT FROM EXCLUDED.meeting_date" in stmt


def test_first_seen_at_is_never_moved_by_a_correction():
    """The row's own history: when we first heard about this meeting is
    not the same fact as when it happened, and a correction to one must
    not overwrite the other."""
    body = _ingest()
    stmt = body[body.index("INSERT INTO meeting_origins"):][:900]
    update = stmt[stmt.index("DO UPDATE"):]
    assert "first_seen_at" not in update


def test_the_write_can_never_cost_an_extraction():
    """Bookkeeping beside the ingest, never a reason to lose a meeting.
    The table may also be absent on the MCP deployment's lagging
    Postgres, the same degradation entity_aliases and patch_cues take."""
    body = _ingest()
    write = body.index("INSERT INTO meeting_origins")
    around = body[max(0, write - 400):write + 1400]
    assert "try:" in around
    assert "except Exception" in around
    assert "meeting_origin_not_recorded" in around
