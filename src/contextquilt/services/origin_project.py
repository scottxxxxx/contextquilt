"""What a meeting belongs to, recorded at ingest so a moment can be found.

Migration 43 stores the USER's project decision for an origin, because an
assignment made mid-ingest was being lost. This module adds the other
half: the APP's statement, recorded when a meeting arrives carrying a
project, and read back by the recall scope rule.

Why it was needed, measured 2026-09-05 on prod. A four minute recording
came in stamped `project: GL Unlimited`. Its main extraction stored
nothing, and its only output was seven `moment` rows. `moment` is
`project_scoped: false` by manifest design, so those rows stored with a
null project, and with no project-stamped sibling anywhere, NOTHING IN
THE DATABASE recorded which project the meeting belonged to. The recall
scope rule resolves a meeting through its stamped siblings, found none,
read the meeting as unassigned, and served those rows into an unrelated
project's chat. One ingest that yields only origin-scoped types loses
its project association entirely.

TWO RULES, both deliberate:

A HUMAN STATEMENT IS NEVER OVERWRITTEN. The write is ON CONFLICT DO
NOTHING, so an explicit assignment or an explicit unassignment (a row
whose project_id is NULL, the third state migration 43 exists for)
survives every re-ingest. The ingest fills a gap; it does not argue.

THE READ IS A UNION, NOT A REPLACEMENT. A meeting's project can be known
from a stamped sibling patch or from this table, and either is enough.
Rows from before this shipped keep resolving exactly as they did.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# $1 user_id, $2 origin_id, $3 origin_type, $4 project_id, $5 project.
RECORD_INGEST_PROJECT_SQL = """
    INSERT INTO origin_project_assignments
        (user_id, origin_id, origin_type, project_id, project)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (user_id, origin_id, origin_type) DO NOTHING
"""

_TABLE_PROBE = "SELECT to_regclass('origin_project_assignments') IS NOT NULL AS ok"

_available: Optional[bool] = None


async def assignments_available(fetch: Callable) -> bool:
    """Whether this database has the table, probed once per process.

    The MCP deployment runs the same image against its own Postgres and
    can lag migrations; a recall leg naming a missing table would 500 the
    hot path rather than degrade. Probed rather than assumed, cached
    because the answer only changes at deploy time.
    """
    global _available
    if _available is None:
        try:
            rows = await fetch(_TABLE_PROBE)
            _available = bool(rows and rows[0]["ok"])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("origin_project_probe_failed: %s", str(exc)[:160])
            _available = False
    return _available


def reset_probe() -> None:
    """Tests and a deploy-time refresh."""
    global _available
    _available = None


def assignments_union_sql(col: str, subject_param: str) -> str:
    """The `origins` CTE's second leg: meetings this table knows about.

    `col` is the bare column name the caller scopes on, project_id or
    project; the table carries both. The subject key is `user:<id>` and
    the table holds the bare id, so it is split here rather than threaded
    as a second parameter through three builders. A subject key of some
    other shape yields an empty string, matches nothing, and degrades to
    the sibling rule alone.
    """
    return (
        " UNION SELECT COALESCE(opa.origin_type, '') AS origin_type, opa.origin_id, "
        f"opa.{col} AS scope "
        "FROM origin_project_assignments opa "
        f"WHERE opa.user_id = split_part({subject_param}, ':', 2) "
        f"AND opa.{col} IS NOT NULL"
    )
