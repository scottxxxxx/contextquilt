"""What a project deletion takes, and what it spares.

Scott reversed the old ruling on 2026-09-06: deleting a project now
deletes what its meetings produced, with a warning, because his
requirement is that it be "as if those events never occurred and we
don't leave any remnants".

THE CARVE-OUT IS THE PART THAT NEEDED A DECISION, and he made it against
real numbers. Scope narrow to rows stamped with the project_id and a
real project kept 35 of its 37 patches, which is the remnant problem
rather than a fix for it. Scope full and it swept up durable facts about
HIM that merely happened to be learned in those meetings: "Mixtral model
cannot be deployed at Florida Blue due to excessive resource
consumption" was one, a UI constraint about caption placement another.
Those are not project facts and a delete that takes them is a delete
nobody expects.

So: everything the project's meetings produced, EXCEPT the self-typed
set. That set is imported rather than restated, because two lists of the
same four type names drift and this codebase spent a day proving it.

This lives in its own module for one reason: the partition is the
guarantee, and a guarantee asserted by grepping a source file is not
asserted at all. A sabotage that deleted the carve-out entirely passed a
source-reading test, because the constant it looked for was still
present in a COMMENT. Here it can be executed.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from contextquilt.services.recall_scorer import FRESHNESS_TRACKED_TYPES

# The types a project deletion never takes. Self-disclosure: what the
# user is like, wants, or is limited by. True of the person rather than
# of the project, and still true after the project is gone.
SPARED_TYPES = FRESHNESS_TRACKED_TYPES


def partition_for_delete(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[List[Mapping[str, Any]], List[Mapping[str, Any]]]:
    """Split the project's patches into (doomed, spared).

    `rows` carry at least `patch_type`. Order is preserved within each
    side so a caller reporting counts and a caller doing the write see
    the same thing in the same order.
    """
    doomed: List[Mapping[str, Any]] = []
    spared: List[Mapping[str, Any]] = []
    for row in rows or ():
        target = spared if row["patch_type"] in SPARED_TYPES else doomed
        target.append(row)
    return doomed, spared


def counts_by_type(rows: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """Per-type counts, so a client can warn with a real number instead
    of an adjective. "This will delete your memories" and "this will
    delete 1,422 memories including 89 things about how you work" are
    different products, and only the second lets somebody decide."""
    out: Dict[str, int] = {}
    for row in rows or ():
        t = row["patch_type"]
        out[t] = out.get(t, 0) + 1
    return out
