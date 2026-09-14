"""Deleting one meeting: the mode a body selects, and the exact bodies served.

Executed through `services/origin_delete`, which is where the decisions
live so they can be run without fastapi. The route's wiring (order of
writes, the sweep last, no 404) is pinned by source-reading tests at the
bottom; the SQL is executed in test_origin_delete_db.py.
"""

from pathlib import Path

from src.contextquilt.services import origin_delete as od
from src.contextquilt.services.project_delete import SPARED_TYPES


# --------------------------------------------------------------------
# Which mode a body selects: absent means preview
# --------------------------------------------------------------------

def test_no_body_is_a_preview():
    assert od.mode_for(None) == "preview"
    assert od.mode_for({}) == "preview"


def test_preview_true_is_a_preview():
    assert od.mode_for({"preview": True}) == "preview"


def test_delete_true_is_a_delete():
    assert od.mode_for({"delete": True}) == "delete"


def test_an_unrecognised_key_is_a_preview():
    """A middlebox that eats the flag produces a preview, never a delete."""
    assert od.mode_for({"nuke": True}) == "preview"
    assert od.mode_for({"delete_patches": True}) == "preview"


def test_delete_must_be_the_boolean_true():
    for v in ("true", 1, "yes", [True]):
        assert od.mode_for({"delete": v}) == "preview", repr(v)


def test_preview_beats_delete_when_both_are_true():
    assert od.mode_for({"delete": True, "preview": True}) == "preview"


# --------------------------------------------------------------------
# The bodies, byte for byte (GhostPour pins a request-side test to these)
# --------------------------------------------------------------------

def _rows(*types):
    return [{"patch_id": f"p{i}", "patch_type": t} for i, t in enumerate(types)]


def test_preview_body_exact_shape():
    doomed = _rows("commitment", "decision", "commitment")
    spared = _rows("trait")
    body = od.preview_body("m-1", "meeting", doomed, spared, 4,
                           {"matched": 1, "bytes": 48213, "deleted": 0, "applied": False})
    assert body == {
        "origin_id": "m-1", "origin_type": "meeting", "applied": False,
        "patches": {"archived": 0, "would_archive": 3,
                    "by_type": {"commitment": 2, "decision": 1},
                    "spared_self_typed": 1},
        "presence": {"appearances": 4},
        "transcripts": {"matched": 1, "bytes": 48213, "deleted": 0, "irreversible": True},
        "limits": od.LIMITS_DEFINITION,
    }


def test_delete_body_exact_shape():
    body = od.delete_body("m-1", "meeting", 3, {"commitment": 2, "decision": 1}, 1, 4,
                          {"matched": 1, "bytes": 48213, "deleted": 1, "applied": True})
    assert body == {
        "origin_id": "m-1", "origin_type": "meeting", "applied": True,
        "patches": {"archived": 3, "would_archive": 3,
                    "by_type": {"commitment": 2, "decision": 1},
                    "spared_self_typed": 1},
        "presence": {"appearances": 4},
        "transcripts": {"matched": 1, "bytes": 48213, "deleted": 1, "irreversible": True},
        "limits": od.LIMITS_DEFINITION,
    }


def test_an_empty_scope_is_a_valid_answer_not_an_error():
    body = od.preview_body("m-none", "meeting", [], [], 0, {})
    assert body["patches"]["would_archive"] == 0
    assert body["transcripts"] == {"matched": 0, "bytes": 0, "deleted": 0, "irreversible": True}


def test_a_failed_sweep_shows_as_zero_deleted_against_matched():
    """The only way a caller can see the sweep silently failing."""
    body = od.delete_body("m-1", "meeting", 2, {"commitment": 2}, 0, 0,
                          {"matched": 0, "bytes": 0, "deleted": 0})
    assert body["transcripts"]["deleted"] == 0
    assert body["applied"] is True


def test_the_limits_sentence_is_on_the_wire_and_names_both_survivors():
    assert "lignment" in od.LIMITS_DEFINITION and "woven" in od.LIMITS_DEFINITION
    assert "does not come" in od.LIMITS_DEFINITION


def test_the_carve_out_is_the_project_forms_not_a_copy():
    # Same import path the service uses, or `is` compares two module
    # objects loaded from two spellings of one file.
    from contextquilt.services import project_delete
    assert od.project_delete is project_delete
    assert SPARED_TYPES == frozenset({"trait", "preference", "goal", "constraint"})


def test_the_scope_sql_keys_on_origin_type_and_id():
    assert "cp.origin_type = $2" in od.SCOPE_SQL
    assert "cp.origin_id = $3" in od.SCOPE_SQL
    assert "COALESCE(cp.status, 'active') = 'active'" in od.SCOPE_SQL


def test_archive_never_hard_deletes_and_names_its_cause():
    assert "UPDATE context_patches" in od.ARCHIVE_SQL
    assert "DELETE" not in od.ARCHIVE_SQL.upper().replace("MEETING_DELETED", "")
    assert "meeting_deleted" in od.ARCHIVE_SQL


# --------------------------------------------------------------------
# The route (source-read: main.py cannot import here)
# --------------------------------------------------------------------

MAIN = (Path(__file__).resolve().parents[2] / "src" / "main.py").read_text()


def _impl() -> str:
    """The helper's CODE, comment lines stripped. The first version of
    these tests grepped the raw text and failed on the author's own
    comment ("sweeps once, at the end, with apply=True"), which is the
    documented trap of source-reading tests pointed the other way: a
    string in a comment is not a statement, in either direction."""
    start = MAIN.index("async def _origin_delete(")
    body = MAIN[start:MAIN.index("UNREACHABLE_DEFINITION = (")]
    return "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))


def test_the_route_exists_as_a_sibling_of_assign_and_unassign():
    assert '@app.post("/v1/origins/{user_id}/{origin_type}/{origin_id}/delete"' in MAIN


def test_the_route_resolves_mode_through_the_service():
    route = MAIN[MAIN.index("async def delete_origin("):MAIN.index("async def _origin_delete(")]
    assert "origin_delete.mode_for(req.dict() if req else None)" in route
    assert 'preview=(mode == "preview")' in route


def test_the_route_never_404s():
    route = MAIN[MAIN.index("async def delete_origin("):MAIN.index("UNREACHABLE_DEFINITION = (")]
    assert "404" not in route
    assert "HTTPException" not in route


def test_the_transcript_sweep_runs_last_and_cannot_fail_the_delete():
    impl = _impl()
    archive = impl.index("origin_delete.ARCHIVE_SQL")
    presence = impl.index("origin_delete.APPEARANCES_DELETE_SQL")
    sweep = impl.index("apply=True")
    assert archive < presence < sweep
    guarded = impl[impl.rindex("try:", 0, sweep):impl.index("except Exception", sweep)]
    assert "apply=True" in guarded


def test_the_preview_path_never_applies():
    impl = _impl()
    preview_branch = impl[impl.index("if preview:"):impl.index("archived = 0")]
    assert "apply=False" in preview_branch
    assert "apply=True" not in preview_branch
    assert "ARCHIVE_SQL" not in preview_branch


def test_presence_is_deleted_for_the_origin_not_unscoped():
    impl = _impl()
    assert "origin_delete.APPEARANCES_DELETE_SQL" in impl
    assert "DELETE FROM person_appearances WHERE user_id = $1 AND origin_id = $2" in od.APPEARANCES_DELETE_SQL


def test_the_delete_path_wakes_the_worker_to_rehydrate():
    impl = _impl()
    assert '"type": "hydrate"' in impl
