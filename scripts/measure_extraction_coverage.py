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
"""
import asyncio, os, collections
import asyncpg


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    rows = await c.fetch(
        """
        SELECT p.origin_id, p.total, p.behavior, p.day,
               opa.project_id AS assigned_project,
               (opa.origin_id IS NOT NULL) AS has_assignment_row
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
        silent = r["behavior"] == r["total"]
        cell[bucket(r)][0 if silent else 1] += 1

    print(f"\n{'meeting project state':36} {'main SILENT':>12} {'produced':>10}")
    for k, (silent, ok) in sorted(cell.items()):
        tot = silent + ok
        print(f"  {k:34} {silent:6} ({silent/tot:3.0%})  {ok:8}")

    silent_rows = [r for r in rows if r["behavior"] == r["total"]]
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
