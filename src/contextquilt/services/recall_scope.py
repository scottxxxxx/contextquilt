"""Project scope for the recall fetch legs: which rows a project chat may see.

Measured 2026-09-04 on the largest prod account. A project-scoped recall
for the Immigration project rendered 15 lines out of 84 stored for that
project, and NOT ONE of them was a decision, takeaway, blocker or
deliverable. Three of the fifteen were another project's interview
moments and four were that project's people. The flat leg admitted
`project_id IS NULL` rows, meant for legacy unstamped memory, and every
origin-scoped type (a moment carries its meeting and no project, by
manifest design) rides through that clause. Then LIMIT 20 newest across
the whole admitted set let one busy unrelated meeting evict the
project's own memory entirely. Two defects, one predicate.

Two changes, both here so the flat leg and the cue leg cannot drift
apart (the 2026-08-30 incident was exactly that drift):

1. A meeting-bound row with no project of its own RESOLVES to a project
   through its meeting: if any sibling patch from the same origin is
   stamped with this project, the row is in this project; if siblings
   are stamped with a different project and none with this one, the row
   is FOREIGN and excluded. A meeting nobody has assigned anywhere keeps
   today's reach (served, because there is no evidence it belongs
   elsewhere), as does legacy memory with no meeting at all.

2. The flat leg takes TWO windows, not one: the newest 20 rows the
   project holds, then the newest 20 of everything else the predicate
   admits. The project's own rows can no longer be crowded out by
   whatever happened most recently somewhere else. The scorer and the
   flat cap still decide what renders.

HOW THE RESOLUTION IS WRITTEN MATTERS FOR THE HOT PATH. A first cut put
a correlated EXISTS inside the row filter. Semantically right, and on
prod it turned an 11 ms leg into 570 ms: the subplan wrecked the row
estimates (cost 127k for a query that touches 5k rows), which tripped
JIT compilation on every call, and the second window got a nested loop
that discarded 8.8 million join rows. So the meetings are resolved ONCE
in a CTE at the top of the statement, and both predicates are hashed
set membership on (origin_type, origin_id). One round trip, the same
plan shape the old leg had.

Nothing here executes SQL; the caller owns the pool and the DB test
owns a real Postgres, same as cue_matching.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from contextquilt.services.origin_project import assignments_union_sql

FLAT_LIMIT = 20


def origins_cte(col: str, subject_param: str, project_param: str,
                include_assignments: bool = False) -> str:
    """WITH clause resolving the subject's meetings to projects.

    `origins`: every (origin, project) pair any stamped row proves.
    `held`: the meetings this project holds.
    `foreign_origins`: meetings some OTHER project holds and this one
    does not. origin_type is COALESCEd so a row-value IN test can never
    be NULL (a NULL inside NOT would drop the row silently).
    `col` is the bare column name, project_id or project.
    """
    # A meeting is resolved by a stamped sibling patch OR by the ingest's
    # own record of what it belonged to (services/origin_project.py).
    # The second leg exists because a meeting whose only output is
    # origin-scoped rows has no stamped sibling to resolve through.
    assignments = assignments_union_sql(col, subject_param) if include_assignments else ""
    return (
        "WITH origins AS MATERIALIZED ("
        "SELECT DISTINCT COALESCE(cp.origin_type, '') AS origin_type, cp.origin_id, "
        f"cp.{col} AS scope "
        "FROM context_patches cp JOIN patch_subjects ps ON ps.patch_id = cp.patch_id "
        f"WHERE ps.subject_key = {subject_param} AND cp.origin_id IS NOT NULL "
        f"AND cp.{col} IS NOT NULL{assignments}), "
        f"held AS (SELECT origin_type, origin_id FROM origins WHERE scope = {project_param}), "
        "foreign_origins AS ("
        f"SELECT origin_type, origin_id FROM origins WHERE scope <> {project_param} "
        "EXCEPT SELECT origin_type, origin_id FROM held) "
    )


_ROW = "(COALESCE(cp.origin_type, ''), cp.origin_id)"


def in_project_clause(col: str, project_param: str) -> str:
    """Rows the project HOLDS: stamped with it, or meeting-bound and their
    meeting is held. Never NULL, so a caller may negate it. Needs the
    CTE from origins_cte in the same statement."""
    return (
        f"(COALESCE(cp.{col} = {project_param}, false) "
        f"OR (cp.{col} IS NULL AND cp.origin_id IS NOT NULL "
        f"AND {_ROW} IN (SELECT origin_type, origin_id FROM held)))"
    )


def foreign_clause(col: str) -> str:
    """Rows that belong to a DIFFERENT project through their meeting and
    to this one by nothing. These are the leak. Never NULL."""
    return (
        f"(cp.{col} IS NULL AND cp.origin_id IS NOT NULL "
        f"AND {_ROW} IN (SELECT origin_type, origin_id FROM foreign_origins))"
    )


def project_scope_clause(col: str, project_param: str, universal_param: str) -> str:
    """The whole admission rule for a project-scoped leg, as one boolean:
    in this project, or a universal type, or unstamped and not foreign.
    The cue leg uses this verbatim; the flat leg splits it into its two
    windows below. Needs origins_cte in the same statement."""
    return (
        f"({in_project_clause(col, project_param)} "
        f"OR cp.patch_type = ANY({universal_param}::text[]) "
        f"OR (cp.{col} IS NULL AND NOT {foreign_clause(col)}))"
    )


_SELECT = (
    "SELECT cp.patch_id, cp.value, cp.patch_type, cp.source_prompt, "
    "cp.created_at, cp.last_observed_at, cp.project_id, cp.project "
    "FROM context_patches cp "
    "JOIN patch_subjects ps ON cp.patch_id = ps.patch_id "
    "WHERE ps.subject_key = $1 AND COALESCE(cp.status, 'active') = 'active' "
)

_ORDER = f"ORDER BY cp.created_at DESC, cp.patch_id ASC LIMIT {FLAT_LIMIT}"


def _scope_col(recall_project_id, recall_project):
    if recall_project_id:
        return "project_id", recall_project_id
    if recall_project:
        return "project", recall_project
    raise ValueError("project-scoped legs only")


def build_flat_fetch(
    subject_key: str,
    universal_types: List[str],
    max_age_days: Optional[int],
    age_sql: str,
    recall_project_id: Optional[str] = None,
    recall_project: Optional[str] = None,
    include_assignments: bool = False,
) -> Tuple[str, list]:
    """(sql, args) for the project-scoped flat leg: two windows, one round trip.

    `age_sql` is the recall age predicate already bound to $4 (days) and
    $3 (universal types), exactly as main.py formats it for every leg.
    $1 subject, $2 the project value, $3 universal types, $4 the window.
    """
    col, scope_val = _scope_col(recall_project_id, recall_project)
    args = [subject_key, scope_val, universal_types, max_age_days]
    held = in_project_clause(col, "$2")
    foreign = foreign_clause(col)
    # Window 1: what the project holds, newest first.
    first = f"({_SELECT}{age_sql} AND {held} {_ORDER})"
    # Window 2: everything else the rule admits. `NOT held` is only safe
    # because every term in `held` is a non-null boolean: the equality is
    # wrapped in COALESCE(.., false), the rest are IS NULL tests and a
    # row-value IN over non-null columns. A bare `cp.project_id = $2` is
    # NULL on an unstamped row, NOT NULL is NULL, and the row this window
    # exists for would vanish.
    second = (
        f"({_SELECT}{age_sql} AND NOT {held} "
        f"AND (cp.patch_type = ANY($3::text[]) OR (cp.{col} IS NULL AND NOT {foreign})) "
        f"{_ORDER})"
    )
    return f"{origins_cte(col, '$1', '$2', include_assignments)}{first} UNION ALL {second}", args


# The conduct guarantee. $1 subject, $2 project value, $3 universal types,
# $4 age window, $5 owner tokens (lowercased full names AND first tokens),
# $6 conduct types, $7 limit.
#
# Measured 2026-09-06 on the A/B: the user has 35 conduct rows about one
# person across six meetings, and the block rendered ONE, the shallowest,
# because the flat leg takes the newest 20 patches and everything richer
# was older than the last two meetings. The more history you have with
# somebody, the less of them the capsule sees. This leg puts a named
# person's conduct into the candidate set regardless of that window, the
# way the overdue guarantee does for completables; the scorer still
# decides what renders.
CONDUCT_BY_OWNER_SQL_TMPL = """
    SELECT cp.patch_id, cp.value, cp.patch_type, cp.source_prompt,
           cp.created_at, cp.last_observed_at
    FROM context_patches cp
    JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
    WHERE ps.subject_key = $1
      AND cp.patch_type = ANY($6::text[])
      AND COALESCE(cp.status, 'active') = 'active'
      AND (lower(cp.value->>'owner') = ANY($5::text[])
           OR lower(split_part(cp.value->>'owner', ' ', 1)) = ANY($5::text[]))
      {AGE}
      AND {SCOPE}
    ORDER BY cp.created_at DESC, cp.patch_id ASC
    LIMIT $7
"""


def build_conduct_fetch(
    subject_key: str,
    conduct_types,
    owner_tokens: List[str],
    universal_types: List[str],
    max_age_days: Optional[int],
    age_sql: str,
    recall_project_id: Optional[str] = None,
    recall_project: Optional[str] = None,
    include_assignments: bool = False,
    limit: int = 12,
) -> Tuple[str, list]:
    """(sql, args) for a named person's conduct, outside the recency window.

    Project-scoped recalls only, the same restriction the overdue
    guarantee carries: without a project there is no scope to admit
    against and every colleague's conduct would arrive at once.
    """
    col, scope_val = _scope_col(recall_project_id, recall_project)
    sql = (
        origins_cte(col, "$1", "$2", include_assignments)
        + CONDUCT_BY_OWNER_SQL_TMPL
        .replace("{AGE}", age_sql)
        .replace("{SCOPE}", project_scope_clause(col, "$2", "$3"))
    )
    args = [subject_key, scope_val, universal_types, max_age_days,
            [t.lower() for t in owner_tokens], list(conduct_types), limit]
    return sql, args


def build_scoped_count(
    subject_key: str,
    universal_types: List[str],
    max_age_days: Optional[int],
    age_sql: str,
    recall_project_id: Optional[str] = None,
    recall_project: Optional[str] = None,
    include_assignments: bool = False,
) -> Tuple[str, list]:
    """(sql, args) for the coverage denominator: how many active rows the
    project HOLDS, by the same rule window 1 uses, inside the tier window.
    "showing N of M" must count the rows N was drawn from."""
    col, scope_val = _scope_col(recall_project_id, recall_project)
    args = [subject_key, scope_val, universal_types, max_age_days]
    sql = (
        f"{origins_cte(col, '$1', '$2', include_assignments)}"
        "SELECT count(*) FROM context_patches cp "
        "JOIN patch_subjects ps ON ps.patch_id = cp.patch_id "
        "WHERE ps.subject_key = $1 AND COALESCE(cp.status, 'active') = 'active' "
        f"{age_sql} AND {in_project_clause(col, '$2')}"
    )
    return sql, args
