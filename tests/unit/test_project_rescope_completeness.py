"""A reassignment must move everything, and be remembered.

FOUND ON SCOTT'S OWN DATA, 2026-08-28. He recorded a meeting with no
project, assigned it when the meeting ended, and asked whether everything
had followed. It had not, twice over.

    15:26:38  main extraction stored its patches
    15:26:50  assign-project ran, 200 OK, patches_updated = 3
    15:26:58  the behaviour extraction stored 12 MORE patches

An ingest is multi-phase and takes about twenty seconds. The rescope
moved what existed, nothing re-ran it, and twelve patches written eight
seconds later stayed unscoped permanently. Separately, the route only
ever touched `context_patches`, so all three of that person's meetings
had every patch correctly scoped and every appearance unscoped.

Two fixes, and they fail in different directions, which is why both are
here: the decision is STORED so a later phase can honour it, and the
rescope MOVES THE PEOPLE as well as the facts.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
MAIN = (ROOT / "src" / "main.py").read_text()
WORKER = (ROOT / "src" / "worker.py").read_text()
MIGRATION = (ROOT / "init-db" / "43_origin_project_assignments.sql").read_text()


def _assign():
    i = MAIN.index("async def assign_origin_to_project")
    return MAIN[i:MAIN.index("class OriginProjectUnassignment", i)]


def _unassign():
    i = MAIN.index("async def unassign_origin_from_project")
    return MAIN[i:MAIN.index('@app.post("/v1/projects/{user_id}/{project_id}/unscope"', i)]


# ------------------------------------------------------------------
# The people move with the meeting.
# ------------------------------------------------------------------

def test_assign_rescopes_the_presence_rows_too():
    """A person is IN a project because they were in its meetings. A
    rescope that moves the facts and leaves the attendance behind has
    done half the job."""
    body = _assign()
    assert "UPDATE person_appearances SET project_id = $1" in body
    stmt = body[body.index("UPDATE person_appearances"):][:400]
    assert "origin_id = $3" in stmt and "origin_type = $4" in stmt
    assert "user_id = $2" in stmt, "never rescope another user's presence"


def test_assign_echoes_both_counts_so_the_caller_can_see_both_halves():
    """Rule 4: check the echo, not the status. A 200 with only
    patches_updated cannot tell a caller the people moved."""
    body = _assign()
    assert '"patches_updated": patches_updated' in body
    assert '"appearances_updated": appearances_updated' in body


def test_unassign_mirrors_it_under_the_same_guard():
    """Without the guard, a stale 'remove it from the old project' would
    strip a meeting that has since been reassigned, which is the exact
    case the guard exists for on the patches half."""
    body = _unassign()
    assert "UPDATE person_appearances SET project_id = NULL" in body
    assert "appearance_guard" in body
    assert 'AND project_id = $4' in body
    assert '"appearances_updated": appearances_updated' in body


# ------------------------------------------------------------------
# The decision is remembered, not merely applied.
# ------------------------------------------------------------------

def test_the_decision_is_recorded_BEFORE_the_rescope():
    """The rescope can only move rows that already exist. Recording the
    decision first is what makes a phase finishing later still correct;
    recording it after would leave the same race in a smaller window."""
    body = _assign()
    record = body.index("INSERT INTO origin_project_assignments")
    move = body.index("UPDATE context_patches SET")
    assert record < move


def test_an_explicit_unassignment_is_stored_as_NULL_not_as_a_deleted_row():
    """Three states, and the third is the point: no row means never
    stated, a NULL project means explicitly unassigned. Collapsing them
    would let a later phase adopt the project still sitting on this
    origin's earlier patches and silently undo the removal."""
    body = _unassign()
    assert "INSERT INTO origin_project_assignments" in body
    assert "VALUES ($1, $2, $3, NULL, NULL)" in body
    assert "DELETE FROM origin_project_assignments" not in body
    assert "SET project_id = NULL, project = NULL" in body


def test_recording_the_decision_can_never_fail_the_users_assignment():
    """The rescope is the visible effect. A bookkeeping row that cannot
    be written must not turn the user's action into an error."""
    for body in (_assign(), _unassign()):
        i = body.index("INSERT INTO origin_project_assignments")
        around = body[max(0, i - 300):i + 900]
        assert "try:" in around and "except Exception" in around
        assert "origin_project_intent_not_recorded" in around


# ------------------------------------------------------------------
# The ingest honours a decision made while it was still running.
# ------------------------------------------------------------------

def _ingest():
    i = WORKER.index("async def handle_meeting_summary")
    return WORKER[i:WORKER.index("\n    async def ", i + 10)]


def test_the_ingest_adopts_a_decision_made_mid_ingest():
    body = _ingest()
    assert "SELECT project_id, project FROM origin_project_assignments" in body
    assert "origin_project_adopted" in body


def test_it_only_adopts_when_this_payload_names_no_project_of_its_own():
    """A payload that names a project is the newer statement and wins."""
    body = _ingest()
    assert "if origin_id and not project_id:" in body
    gate = body.index("if origin_id and not project_id:")
    read = body.index("FROM origin_project_assignments")
    assert gate < read


def test_an_explicit_unassignment_leaves_the_ingest_unscoped():
    """The row exists with project_id NULL, so adopting it sets
    project_id to None. It must NOT fall through to 'never stated' and
    keep whatever the payload had, which would restore a project the
    user just removed."""
    body = _ingest()
    block = body[body.index("FROM origin_project_assignments"):][:700]
    assert "if decided is not None:" in block, "presence of the ROW decides, not truthiness of the project"
    assert 'project_id = decided["project_id"]' in block


def test_the_lookup_can_never_break_an_ingest():
    """The table may lag on the MCP deployment's own Postgres, the same
    degradation entity_aliases and patch_cues already take."""
    body = _ingest()
    i = body.index("FROM origin_project_assignments")
    around = body[max(0, i - 700):i + 400]
    assert "except Exception" in around
    assert "decided = None" in around


# ------------------------------------------------------------------
# The table
# ------------------------------------------------------------------

def test_project_id_is_nullable_because_unassigned_is_a_decision():
    line = [l for l in MIGRATION.splitlines() if l.strip().startswith("project_id")][0]
    assert "NOT NULL" not in line


def test_the_table_is_keyed_per_origin_and_carries_no_app_id():
    assert "PRIMARY KEY (user_id, origin_id, origin_type)" in MIGRATION
    body = MIGRATION[MIGRATION.index("CREATE TABLE"):MIGRATION.index("PRIMARY KEY")]
    assert "app_id" not in body
