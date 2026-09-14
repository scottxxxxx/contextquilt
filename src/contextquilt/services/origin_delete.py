"""Deleting one meeting deletes what it produced (Scott's ruling, 2026-09-07).

The meeting-level form of the project delete (#466). Same three rulings,
inherited rather than re-decided, and stated here so the wire and the
docstring agree:

  SCOPE     everything THIS ORIGIN produced, except the self-typed set
            (trait, preference, goal, constraint). A durable fact about
            the user is not a meeting fact; the carve-out is
            `project_delete.SPARED_TYPES`, imported, not copied.
  DEPTH     archive, never hard delete. The delta's `deleted[]` is
            computed FROM archived rows, so a hard delete would stop
            other devices ever learning the meeting is gone.
  ORDER     the transcript XDEL is the only irreversible half and it runs
            LAST, after every reversible write has landed.

WHAT IS DIFFERENT FROM THE PROJECT FORM, and why:

  ABSENT MEANS PREVIEW. #466 made "absent" keep the old unscope
  behaviour because that route had one. This route has no prior
  behaviour, so the safe failure direction is "nothing was deleted": a
  middlebox that eats the `delete` flag produces a preview, never a
  deletion. GhostPour forwards the body untyped and asserts the key
  arrives intact at their hop; this is the CQ half of the same guarantee.

  PRESENCE IS DELETED, NOT UNSCOPED. A project delete nulls
  `person_appearances.project_id` because the person was still in the
  meeting. A meeting delete removes the meeting, so its appearance rows
  (and their per-meeting question and role counts, which are never
  backfillable) go with it. That is what "as if those events never
  occurred" means at meeting grain.

  200 ALWAYS, INCLUDING AN UNKNOWN MEETING AND A REPEAT. A delete is a
  sweep over a scope and an empty scope is a valid answer. A 404 here
  would make a retry after a lost 2xx read as an error when the first
  attempt succeeded. Idempotent by construction: the second call reports
  zeros.

WHAT IT DOES NOT REMOVE, published on the wire as `limits` rather than
left in this docstring: alignment events and the woven digest cache.
Both are derived and regenerate; neither is a record of the meeting.

The decision logic lives here so it can be EXECUTED by a test: which
mode a body selects, and the exact response shape for each. The route
owns the pool and the client.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

from contextquilt.services import project_delete

ARCHIVE_CAUSE = "meeting_deleted"

LIMITS_DEFINITION = (
    "Alignment events and the woven digest cache are derived from the "
    "quilt and regenerate; they are not removed by a meeting deletion. "
    "A transcript on the ingest stream is removed and does not come "
    "back: re-extraction of this meeting is impossible afterwards."
)

# The meeting's own patches. Origin type is part of the key because a
# meeting id and, say, a document id could collide across origin types.
SCOPE_SQL = """
    SELECT cp.patch_id, cp.patch_type
    FROM context_patches cp
    JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
    WHERE ps.subject_key = $1
      AND cp.origin_type = $2
      AND cp.origin_id = $3
      AND COALESCE(cp.status, 'active') = 'active'
"""

ARCHIVE_SQL = """
    UPDATE context_patches
       SET status = 'archived', updated_at = NOW(),
           value = jsonb_set(value, '{archive_cause}', '"meeting_deleted"')
     WHERE patch_id = ANY($1::uuid[])
       AND COALESCE(status, 'active') = 'active'
"""

APPEARANCES_COUNT_SQL = (
    "SELECT count(*) FROM person_appearances WHERE user_id = $1 AND origin_id = $2"
)
APPEARANCES_DELETE_SQL = (
    "DELETE FROM person_appearances WHERE user_id = $1 AND origin_id = $2"
)
ASSIGNMENT_DELETE_SQL = (
    "DELETE FROM origin_project_assignments "
    "WHERE user_id = $1 AND origin_id = $2 AND origin_type = $3"
)


def mode_for(body: Optional[Mapping[str, Any]]) -> str:
    """'delete' only when the body says `delete: true` and does not also
    say `preview: true`. Everything else, including no body and any key
    nobody recognises, is a preview."""
    if not isinstance(body, Mapping):
        return "preview"
    if body.get("delete") is True and body.get("preview") is not True:
        return "delete"
    return "preview"


def _base(origin_id: str, origin_type: str, applied: bool) -> Dict[str, Any]:
    return {"origin_id": origin_id, "origin_type": origin_type, "applied": applied}


def preview_body(
    origin_id: str, origin_type: str,
    doomed: Sequence[Mapping[str, Any]], spared: Sequence[Mapping[str, Any]],
    appearances: int, transcripts: Mapping[str, Any],
) -> Dict[str, Any]:
    """Counts only. The numbers a client warns with are the numbers the
    delete path removes, because both walk the same queries."""
    body = _base(origin_id, origin_type, applied=False)
    body["patches"] = {
        "archived": 0,
        "would_archive": len(doomed),
        "by_type": project_delete.counts_by_type(doomed),
        "spared_self_typed": len(spared),
    }
    body["presence"] = {"appearances": int(appearances)}
    body["transcripts"] = {
        "matched": int(transcripts.get("matched", 0)),
        "bytes": int(transcripts.get("bytes", 0)),
        "deleted": 0,
        "irreversible": True,
    }
    body["limits"] = LIMITS_DEFINITION
    return body


def delete_body(
    origin_id: str, origin_type: str,
    archived: int, by_type: Mapping[str, int], spared: int,
    appearances_deleted: int, cleared: Mapping[str, Any],
) -> Dict[str, Any]:
    """Echoed rather than inferred. A 200 has already told this group
    that a write was processed while a field it sent was not, and the
    transcript sweep is allowed to fail without failing the delete, so
    `transcripts.deleted` compared against the preview's `matched` is the
    only way a caller can notice it did."""
    body = _base(origin_id, origin_type, applied=True)
    body["patches"] = {
        "archived": int(archived),
        "would_archive": int(archived),
        "by_type": dict(by_type),
        "spared_self_typed": int(spared),
    }
    body["presence"] = {"appearances": int(appearances_deleted)}
    body["transcripts"] = {
        "matched": int(cleared.get("matched", 0)),
        "bytes": int(cleared.get("bytes", 0)),
        "deleted": int(cleared.get("deleted", 0)),
        "irreversible": True,
    }
    body["limits"] = LIMITS_DEFINITION
    return body
