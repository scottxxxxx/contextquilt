"""Which meetings belong to a project. ONE definition, two callers.

Written 2026-09-01 after `meeting_count` on the resolve endpoint was
found to be counting something else. It read:

    SELECT count(*) FROM origin_project_assignments WHERE project_id = ...

That table is written ONLY by the explicit assign-project route. A
meeting that arrives already scoped stamps `project_id` onto its own
patches and never creates a row there, so the column was an accurate
count of REASSIGNMENTS and a false count of MEETINGS. Measured on prod
the day it was found:

    160 projects, 87 disagreed with their patches' own origin_ids
     85 reported 0 meetings while patches existed
        'ABM'  1262 active patches, reported 0 meetings, actually 94

    origin_project_assignments held FIVE rows in the whole database

Five rows is the part that matters. The column was not slightly wrong
for a few projects, it was zero for nearly everything, and the same
join is the `observed` leg of the project roster, which had therefore
never once found a person. That surfaced the day explicit membership
shipped, because a roster whose observed leg can never match makes
`meetings: 0` on every declared member: a number Scott had already
ruled is a real state rather than missing data, and which would have
been missing data every time.

So the fix is not a better count. It is a single answer to "which
meetings belong to this project", used by everything that asks, because
two call sites each carrying their own SQL is how the two answers
drifted apart in the first place.

A meeting belongs to a project if EITHER record says so:

  - an explicit assignment row (the rescope path), or
  - a patch of that meeting stamped with the project (the ingest path)

UNION, not a preference between them. Neither is authoritative: the
assignment table knows about rescopes the patches never learned, and
the patches know about every meeting that arrived already scoped.

Status is deliberately NOT filtered. A meeting whose patches have all
decayed still happened, and for the question these counts actually
answer (a human choosing between two real projects) "5 meetings, 0
patches" is informative where a silent 0 is misleading. `patch_count`
stays active-only and means what it says.
"""


def meetings_for_project_sql(project_ref: str) -> str:
    """SQL returning one `origin_id` column: this project's meetings.

    `project_ref` is a SQL EXPRESSION naming the project, not a value:
    a correlated column (`p.project_id`) or a bind placeholder (`$2`).
    It is interpolated, so it must never carry caller input. Every
    current caller passes a literal written in this repository.
    """
    if not project_ref or not isinstance(project_ref, str):
        raise ValueError("project_ref must be a non-empty SQL expression")
    return f"""
        SELECT opa.origin_id AS origin_id
          FROM origin_project_assignments opa
         WHERE opa.project_id = {project_ref}
           AND opa.origin_id IS NOT NULL
         UNION
        SELECT cp.origin_id AS origin_id
          FROM context_patches cp
         WHERE cp.project_id = {project_ref}
           AND cp.origin_id IS NOT NULL
    """


def meeting_count_sql(project_ref: str) -> str:
    """A scalar subquery counting this project's meetings."""
    return f"(SELECT count(*) FROM ({meetings_for_project_sql(project_ref)}) m)"
