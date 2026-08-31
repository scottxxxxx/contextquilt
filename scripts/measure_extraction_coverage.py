"""READ ONLY. When does the MAIN extraction produce nothing?

Scott's tire store meeting kept ONE patch, a behavior observation, and
the main extraction produced nothing. His vet visit produced ten
including a real commitment. GP asked the right question about those
two: does the difference track the PRESENCE OF A PROJECT, or did the
vet visit simply have more to extract? Different fixes, and a sample of
two cannot separate them.

The behavior lane is a SEPARATE LLM call (doc 19.5). So a meeting whose
patches are ALL behavior is one where the behavior call fired and the
main extraction returned nothing. That is the observable, and it needs
no transcript, which CQ deliberately does not keep.

THE INSTRUMENT TRAP, and why the project signal comes from
`origin_project_assignments` rather than from the patches. The obvious
version asks whether any patch on the meeting carries a project. That
is CIRCULAR: only 79 of 1486 behavior patches are scoped, so "every
patch is behavior" almost entails "no patch is scoped", and the two
variables are one variable. Measured that way the correlation comes out
at a perfect 100%, which is an artifact of the query and not a fact
about the world. `origin_project_assignments` (migration 43) records
what the USER decided the meeting belongs to, independent of what
extraction produced, so it can disagree with the patches. Absence of a
row means never stated, which is NOT the same fact as a NULL project_id
meaning explicitly unassigned; both are kept apart below.

AND THAT TABLE IS TOO SPARSE TO SETTLE IT EITHER. On the 2026-08-30 run
only 4 of 162 meetings had an assignment row at all, because a row is
written when a user explicitly assigns and most meetings take their
project from the ingest request instead. So this script CANNOT
currently separate GP's two hypotheses, and saying so IS the result
rather than a failure to report one.

THE SECOND TRAP, and it cost 4 of the 38 on the first run. The behavior
lane's first row is 2026-08-17, and a backfill ran that call over
HISTORICAL meetings that day. Those meetings' own non-behavior patches
were created weeks earlier, so inside a 14-day window they look
behavior-only while being nothing of the kind: one had 28 non-behavior
patches dating from 2026-06-25. So silence is tested UNWINDOWED, "this
origin has no non-behavior patch EVER", and the windowed count is
reported separately as excluded artifacts. Corrected 38 to 34 before
the number left the building.

It is kept anyway, for two reasons. The traps above are worth not
re-falling into. And the coverage number stands on its own: on
2026-08-30, 34 of 162 meetings in 14 days produced ONLY behavior
patches, median 6 and max 17. A meeting yielding 17 behavior
observations and no commitment, decision or takeaway is not explained
by "too short".

One caveat on that number, because the file should not overclaim what
it counts. Dedup means a meeting whose content all merged into EXISTING
patches creates no new rows and looks identical from here. So this
counts meetings that produced no NEW non-behavior patch, which is not
quite "the main extraction returned nothing". For Scott's tire store
meeting the stronger claim does hold: one patch total, and no prior
tire content anywhere in the corpus to have merged into.

Usage:
    DATABASE_URL=postgres://... python scripts/measure_extraction_coverage.py
"""
import asyncio, os, collections
import asyncpg


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    rows = await c.fetch(
        """
        SELECT p.origin_id, p.total, p.behavior, p.day,
               opa.project_id AS assigned_project,
               (opa.origin_id IS NOT NULL) AS has_assignment_row,
               -- Unwindowed, and load bearing: see THE SECOND TRAP.
               (SELECT COUNT(*) FROM context_patches e
                 WHERE e.origin_id = p.origin_id
                   AND e.patch_type <> 'behavior') AS nonbehavior_ever
          FROM (
              SELECT origin_id,
                     COUNT(*) AS total,
                     COUNT(*) FILTER (WHERE patch_type = 'behavior') AS behavior,
                     MIN(created_at)::date AS day
                FROM context_patches
               WHERE origin_id IS NOT NULL
                 AND created_at > NOW() - INTERVAL '14 days'
               GROUP BY origin_id
          ) p
          LEFT JOIN origin_project_assignments opa
                 ON opa.origin_id = p.origin_id::text
        """
    )
    print(f"meetings with patches in the last 14 days: {len(rows)}")

    def bucket(r):
        if not r["has_assignment_row"]:
            return "no assignment row (never stated)"
        if r["assigned_project"] is None:
            return "row says explicitly unassigned"
        return "assigned to a project"

    cell = collections.defaultdict(lambda: [0, 0])  # [silent, produced]
    for r in rows:
        silent = r["behavior"] == r["total"] and r["nonbehavior_ever"] == 0
        cell[bucket(r)][0 if silent else 1] += 1

    print(f"\n{'meeting project state':36} {'main SILENT':>12} {'produced':>10}")
    for k, (silent, ok) in sorted(cell.items()):
        tot = silent + ok
        print(f"  {k:34} {silent:6} ({silent/tot:3.0%})  {ok:8}")

    silent_rows = [r for r in rows
                   if r["behavior"] == r["total"] and r["nonbehavior_ever"] == 0]
    artifacts = [r for r in rows
                 if r["behavior"] == r["total"] and r["nonbehavior_ever"] > 0]
    print(f"\nwindow artifacts excluded (behavior-only INSIDE the window, but "
          f"the meeting has older non-behavior patches): {len(artifacts)}")
    print(f"\nmain extraction produced NOTHING: {len(silent_rows)} of {len(rows)}")
    sizes = sorted(r["total"] for r in silent_rows)
    if sizes:
        print(f"  their patch counts: min={sizes[0]} "
              f"median={sizes[len(sizes)//2]} max={sizes[-1]}")
    allsizes = sorted(r["total"] for r in rows)
    print(f"  all meetings:       min={allsizes[0]} "
          f"median={allsizes[len(allsizes)//2]} max={allsizes[-1]}")
    await c.close()


asyncio.run(main())
