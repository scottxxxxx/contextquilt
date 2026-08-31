"""READ ONLY. Can an unstamped patch have its project inferred, and how often?

Durable because this measurement DECIDED a design. Run 2026-08-30 it
rescoped GP's #827 item B from a corpus-wide backfill to a single patch
type, and dissolved an ordering that proposal had asserted. A number
that decides something has to be reproducible, or the next person to
ask "should we stamp?" re-derives it and may get a different answer for
reasons nobody can see.


GP's #827 recommends stamping the corpus first and making the recall
header project-aware second, but gates that ordering on a number
neither team has: what fraction of project-null patches could actually
BE stamped. If the number is poor, the ordering has to change.

The honest denominator is not "every project-null patch". Several types
are universal BY DESIGN: a preference does not belong to a project, and
the flat leg serves them into every scope on purpose. Stamping those
would be wrong, not missing. So they are separated out rather than
counted as a gap.

The strongest available signal is SAME-MEETING INHERITANCE: a patch
with no project whose origin has sibling patches that DO carry one.
Unambiguous means the siblings agree on exactly one project. Where they
disagree, inference would have to pick, and picking is what we do not
get to do silently.

Other signals are NOT measured here: a `parent` connection to a stamped
patch, entity overlap. So the rate this prints is a FLOOR for this
method, never a ceiling for all methods, and must not be quoted as
"the inferable fraction" without that qualifier.

READ THE PER-TYPE TABLE, NOT THE HEADLINE. On the 2026-08-30 run the
headline was 82.1% and would have read as a green light. 1210 of the
1211 inferable rows were a single type, `behavior`, and of the 68
non-behavior rows in the gap exactly ONE was inferable. The zeroes are
structural: those meetings have no project on anything, because they
are personal conversations rather than work meetings somebody failed
to tag.

Usage:
    DATABASE_URL=postgres://... python scripts/measure_project_inferability.py
"""
import asyncio, os, collections
import asyncpg

# Universal by design: unstamped is correct for these, not a gap.
UNIVERSAL_BY_DESIGN = {"preference", "trait", "person", "org", "insight", "project"}


async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"])

    rows = await c.fetch(
        """
        SELECT cp.patch_id, cp.patch_type, cp.origin_id,
               cp.project_id, cp.project,
               sib.n_projects, sib.only_project
          FROM context_patches cp
          LEFT JOIN LATERAL (
              SELECT COUNT(DISTINCT s.project_id) AS n_projects,
                     MIN(s.project_id)            AS only_project
                FROM context_patches s
               WHERE s.origin_id = cp.origin_id
                 AND s.project_id IS NOT NULL
          ) sib ON TRUE
         WHERE COALESCE(cp.status, 'active') = 'active'
        """
    )

    total = len(rows)
    stamped = [r for r in rows if r["project_id"] or r["project"]]
    unstamped = [r for r in rows if not (r["project_id"] or r["project"])]
    print(f"active patches: {total}")
    print(f"  stamped:   {len(stamped):5}  ({len(stamped)/total:.1%})")
    print(f"  unstamped: {len(unstamped):5}  ({len(unstamped)/total:.1%})")

    by_design = [r for r in unstamped if r["patch_type"] in UNIVERSAL_BY_DESIGN]
    gap = [r for r in unstamped if r["patch_type"] not in UNIVERSAL_BY_DESIGN]
    print(f"\nof the unstamped:")
    print(f"  universal BY DESIGN (stamping would be wrong): {len(by_design):5}")
    print(f"  the actual gap:                                {len(gap):5}")

    no_origin = [r for r in gap if not r["origin_id"]]
    inferable = [r for r in gap if r["origin_id"] and (r["n_projects"] or 0) == 1]
    ambiguous = [r for r in gap if r["origin_id"] and (r["n_projects"] or 0) > 1]
    orphan = [r for r in gap if r["origin_id"] and (r["n_projects"] or 0) == 0]

    print(f"\nTHE NUMBER GP's #827 IS GATED ON, over the {len(gap)}-row gap:")
    if gap:
        print(f"  inferable, siblings agree on ONE project: {len(inferable):5}"
              f"  ({len(inferable)/len(gap):.1%})")
        print(f"  ambiguous, siblings name >1 project:      {len(ambiguous):5}"
              f"  ({len(ambiguous)/len(gap):.1%})")
        print(f"  meeting has NO stamped sibling at all:    {len(orphan):5}"
              f"  ({len(orphan)/len(gap):.1%})")
        print(f"  no origin_id to inherit from:             {len(no_origin):5}"
              f"  ({len(no_origin)/len(gap):.1%})")

    print("\nthe gap by type (inferable / total):")
    per = collections.defaultdict(lambda: [0, 0])
    for r in gap:
        per[r["patch_type"]][1] += 1
        if r["origin_id"] and (r["n_projects"] or 0) == 1:
            per[r["patch_type"]][0] += 1
    for t, (inf, tot) in sorted(per.items(), key=lambda kv: -kv[1][1]):
        print(f"  {t:14} {inf:5} / {tot:5}   {inf/tot:6.1%}")

    await c.close()


asyncio.run(main())
