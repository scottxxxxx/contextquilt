"""Resolve a project reference to CQ's own record, or refuse.

Written 2026-08-31 after a client-side repair matched projects BY NAME
and pointed one project's record at another's id. Scott opened
"Immigration  Interview App" and saw CBE's work: the app asked CQ for
CBE's project id under the Immigration heading, and CQ filtered exactly
as asked. Scott's ruling: repairs match on ID, never on name.

This exists so a repair that holds only a name has somewhere to ask.
The entire design is that IT WILL NOT GUESS. Guessing is what caused
the incident, and an endpoint that returns a best match would move the
guess from the client into CQ without removing it.

The collisions are real in live data, which is why "just normalise and
match" is not available:

    'CBE' and 'Cbe'                 two different project ids
    'ABM' with rows stamped 'ABC'   one id, two stamps
    'Immigration  Interview App'    a name with a double space

Normalising resolves the first two to one string each. That is exactly
the false confidence to avoid.
"""

from typing import Any, Dict, List, Optional

RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
UNKNOWN = "unknown"

# Match kinds, so a caller can tell how much to trust the answer.
EXACT = "exact"
NORMALIZED = "normalized"
BY_ID = "by_id"


def normalize(name: Optional[str]) -> str:
    """Case-folded, whitespace-collapsed. Used ONLY to find candidates.

    Never used to pick between them: two names that normalise alike are
    the ambiguous case, which is the whole point of this module.
    """
    return " ".join((name or "").split()).casefold()


def resolve(rows: List[Dict[str, Any]], name: Optional[str] = None,
            project_id: Optional[str] = None) -> Dict[str, Any]:
    """One answer, or an explicit refusal to give one.

    `rows` is every project CQ holds for this user, each carrying
    project_id, name, status, patch_count and meeting_count.

    BY ID FIRST, because that is the question a repair should be asking:
    "is the id I am holding real for this user". A client that can
    validate its own id detects drift without needing a name at all,
    which is the direct fix for the incident this was written for.
    """
    if project_id:
        for row in rows:
            if str(row.get("project_id")) == str(project_id):
                return {"status": RESOLVED, "match": BY_ID,
                        "project_id": str(row["project_id"]),
                        "name": row.get("name"), "candidates": []}
        # An id CQ does not hold is the most actionable answer this
        # endpoint gives: the client is scoped to something that does
        # not exist here, which is precisely what went wrong.
        return {"status": UNKNOWN, "match": None, "project_id": None,
                "name": None, "candidates": []}

    if not (name or "").strip():
        return {"status": UNKNOWN, "match": None, "project_id": None,
                "name": None, "candidates": []}

    # An exact string match is unambiguous even when other names
    # normalise to it, so it wins outright and is reported as exact.
    exact = [r for r in rows if (r.get("name") or "") == name]
    if len(exact) == 1:
        return {"status": RESOLVED, "match": EXACT,
                "project_id": str(exact[0]["project_id"]),
                "name": exact[0].get("name"), "candidates": []}

    target = normalize(name)
    near = [r for r in rows if normalize(r.get("name")) == target]
    if not near:
        return {"status": UNKNOWN, "match": None, "project_id": None,
                "name": None, "candidates": []}
    if len(near) == 1 and not exact:
        return {"status": RESOLVED, "match": NORMALIZED,
                "project_id": str(near[0]["project_id"]),
                "name": near[0].get("name"), "candidates": []}

    # Two or more. THE ANSWER IS THAT THERE IS NO ANSWER. Every
    # candidate ships with its counts so a HUMAN can choose; a client
    # that ranks these and picks the largest has reinvented the bug.
    return {
        "status": AMBIGUOUS, "match": None, "project_id": None, "name": None,
        "candidates": [{
            "project_id": str(r["project_id"]),
            "name": r.get("name"),
            "status": r.get("status"),
            "patch_count": int(r.get("patch_count") or 0),
            "meeting_count": int(r.get("meeting_count") or 0),
        } for r in sorted(near, key=lambda r: str(r.get("name") or ""))],
    }
