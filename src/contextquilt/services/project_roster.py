"""Who is on a project, and how CQ knows: declared, observed, or both.

HERE RATHER THAN IN THE ROUTE, because a sabotage proved nothing could
see it there. Deleting the line that promotes a person to `both` left
every source-reading test green: they check that the strings appear in
main.py, and "both" still appeared in the docstring. A person who is
declared AND observed would have been reported as merely declared, and
the count of observed members would have been wrong, silently.

Third time today the same remedy applied. Pure logic inside a route
body is logic no test executes.
"""

from typing import Any, Dict, Iterable, List

DECLARED = "declared"
OBSERVED = "observed"
BOTH = "both"


def merge_roster(declared: Iterable[Dict[str, Any]],
                 observed: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """One roster, with the two sources kept distinct.

    `source` is the only thing separating a fact the user stated from an
    inference CQ drew, and this whole surface runs on that distinction.
    A client that flattens them loses it, so it is served per person
    rather than left to be worked out from whether `meetings` is zero.

    A DECLARED PERSON WITH NO MEETINGS IS THE POINT. They were invisible
    before this existed, and they are exactly who a user reaches for the
    feature to add, so they must survive a merge whose other input knows
    nothing about them.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in declared or ():
        eid = str(row.get("entity_id"))
        added = row.get("added_at")
        by_id[eid] = {
            "entity_id": eid,
            "name": row.get("name"),
            "source": DECLARED,
            "meetings": 0,
            "added_at": added.isoformat() if hasattr(added, "isoformat") else added,
        }
    for row in observed or ():
        eid = str(row.get("entity_id"))
        meetings = int(row.get("meetings") or 0)
        existing = by_id.get(eid)
        if existing:
            # Both, and the meeting count comes from the observed leg
            # because the declared leg has no idea.
            existing["source"] = BOTH
            existing["meetings"] = meetings
        else:
            by_id[eid] = {
                "entity_id": eid, "name": row.get("name"),
                "source": OBSERVED, "meetings": meetings, "added_at": None,
            }
    # Deterministic: two calls must not offer the same roster in a
    # different order, and Postgres promises none without an ORDER BY.
    people = sorted(by_id.values(),
                    key=lambda p: ((p["name"] or "").casefold(), p["entity_id"]))
    return {
        "people": people,
        "declared_count": sum(1 for p in people if p["source"] in (DECLARED, BOTH)),
        "observed_count": sum(1 for p in people if p["source"] in (OBSERVED, BOTH)),
    }
