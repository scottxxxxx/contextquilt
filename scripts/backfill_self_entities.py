#!/usr/bin/env python3
"""One-time backfill: stamp each user's self entity (the ego link).

Go-forward stamping rides the (you) marker at ingest (store_entities,
self_source='you_marker'). History has no marker to read — transcripts
are derive-then-discard — so this backfill uses the one signal that
survives: the submitting user appears in (nearly) every one of their own
meetings, because they were holding the phone.

Candidate rule, per user (pure function below, unit-tested):
  - denominator = distinct origin_ids in person_appearances for the user
  - candidate  = the person entity with the highest distinct-meeting
    coverage, stamped ONLY when coverage >= --min-coverage (default 0.9)
    AND it leads the runner-up by >= --min-margin (default 0.1)
  - users already carrying a self stamp are skipped (keep-first, same
    rule as the write path)

Before stamping, "(you)"-suffixed stray entities are folded into their
canonical row (name match after stripping the marker): merged_into
forward pointer, appearances moved, aliases untouched. The prod example
is the literal "Scott (you)" row with 1 appearance.

Dry-run by default; --apply writes. Prints every decision either way,
including refusals with the numbers that caused them.
"""

import argparse
import asyncio
import os
import re
import sys

sys.path[:0] = [
    os.path.join(os.path.dirname(__file__), "..", "src"),
    "/app", "/app/src",
]

MIN_COVERAGE_DEFAULT = 0.9
MIN_MARGIN_DEFAULT = 0.1


def pick_self_candidate(
    coverage_by_entity: dict,
    total_meetings: int,
    min_coverage: float = MIN_COVERAGE_DEFAULT,
    min_margin: float = MIN_MARGIN_DEFAULT,
):
    """(entity_id, coverage, margin) for the one entity that can only be
    the user, or None with no guessing.

    coverage_by_entity maps entity_id -> distinct meetings appeared in.
    Refuses when the leader is not dominant enough (coverage below the
    floor) or not lonely enough (runner-up within the margin): a wrong
    ego silently reshapes every graph read, so ambiguity means "leave it
    for a human", never "pick the likelier one".
    """
    if total_meetings <= 0 or not coverage_by_entity:
        return None
    ranked = sorted(coverage_by_entity.items(), key=lambda kv: kv[1], reverse=True)
    top_id, top_n = ranked[0]
    top_cov = top_n / total_meetings
    runner_cov = (ranked[1][1] / total_meetings) if len(ranked) > 1 else 0.0
    margin = top_cov - runner_cov
    if top_cov < min_coverage or margin < min_margin:
        return None
    return top_id, top_cov, margin


async def main() -> None:
    import asyncpg

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--min-coverage", type=float, default=MIN_COVERAGE_DEFAULT)
    ap.add_argument("--min-margin", type=float, default=MIN_MARGIN_DEFAULT)
    args = ap.parse_args()
    if not args.database_url:
        sys.exit("DATABASE_URL required (env or --database-url)")

    conn = await asyncpg.connect(args.database_url)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"== self-entity backfill ({mode}) ==")

    # Fold "(you)"-suffixed strays into their canonical rows first, so
    # the coverage count below sees one entity per person.
    strays = await conn.fetch(
        "SELECT entity_id, user_id, name, entity_type FROM entities "
        "WHERE name ILIKE '%(you)%' AND merged_into IS NULL"
    )
    # stray entity_id -> canonical, applied to the coverage count below
    # in BOTH modes: a dry run must predict what --apply would stamp, and
    # apply folds before counting, so the dry count has to fold too.
    fold_map: dict = {}
    # user_id -> entity_id proven to be the ego by a historical marker
    # leak; consulted before the coverage heuristic (evidence beats
    # inference).
    marker_evidence: dict = {}
    for s in strays:
        clean = re.sub(r"\(you\)", "", s["name"], flags=re.IGNORECASE).strip()
        canonical = await conn.fetchval(
            "SELECT entity_id FROM entities WHERE user_id = $1 "
            "AND entity_type = $2 AND LOWER(name) = LOWER($3) "
            "AND entity_id <> $4 AND merged_into IS NULL",
            s["user_id"], s["entity_type"], clean, s["entity_id"],
        )
        if canonical:
            print(f"fold stray: {s['name']!r} -> {clean!r} ({canonical})")
            fold_map[s["entity_id"]] = canonical
            # A marker-bearing NAME is direct evidence of the ego: "(you)"
            # names the submitting user by the marker's own contract, so
            # the fold target IS that user's self entity. This matters
            # because coverage cannot identify the real user: the self
            # gate drops their label from speaker counts whenever
            # metadata identifies them, so their own appearance record is
            # structurally sparse (prod: 152/289 for the primary user).
            marker_evidence.setdefault(s["user_id"], canonical)
            if args.apply:
                await conn.execute(
                    "UPDATE person_appearances src SET entity_id = $1 "
                    "WHERE src.entity_id = $2 AND NOT EXISTS ("
                    "  SELECT 1 FROM person_appearances dst "
                    "  WHERE dst.entity_id = $1 AND dst.origin_id = src.origin_id)",
                    canonical, s["entity_id"],
                )
                await conn.execute(
                    "DELETE FROM person_appearances WHERE entity_id = $1",
                    s["entity_id"],
                )
                await conn.execute(
                    "UPDATE entities SET merged_into = $1, merged_at = NOW() "
                    "WHERE entity_id = $2",
                    canonical, s["entity_id"],
                )
        else:
            print(f"rename stray in place: {s['name']!r} -> {clean!r}")
            marker_evidence.setdefault(s["user_id"], s["entity_id"])
            if args.apply:
                await conn.execute(
                    "UPDATE entities SET name = $1 WHERE entity_id = $2",
                    clean, s["entity_id"],
                )

    users = await conn.fetch(
        "SELECT user_id, COUNT(DISTINCT origin_id) AS total "
        "FROM person_appearances GROUP BY user_id"
    )
    stamped = refused = skipped = 0
    for u in users:
        already = await conn.fetchval(
            "SELECT entity_id FROM entities "
            "WHERE user_id = $1 AND self_at IS NOT NULL",
            u["user_id"],
        )
        if already:
            print(f"{u['user_id']}: already stamped ({already}), keep-first")
            skipped += 1
            continue
        if u["user_id"] in marker_evidence:
            entity_id = marker_evidence[u["user_id"]]
            sup = await conn.fetchval(
                "SELECT suppressed_at FROM entities WHERE entity_id = $1",
                entity_id,
            )
            if sup is not None:
                print(f"{u['user_id']}: marker evidence points at a "
                      f"suppressed row ({entity_id}), REFUSED")
                refused += 1
                continue
            name = await conn.fetchval(
                "SELECT name FROM entities WHERE entity_id = $1", entity_id
            )
            print(f"{u['user_id']}: stamp {name!r} ({entity_id}) "
                  f"via marker evidence")
            if args.apply:
                await conn.execute(
                    "UPDATE entities SET self_at = NOW(), self_source = 'backfill' "
                    "WHERE entity_id = $1 AND self_at IS NULL AND suppressed_at IS NULL",
                    entity_id,
                )
            stamped += 1
            continue
        pairs = await conn.fetch(
            "SELECT pa.entity_id, pa.origin_id "
            "FROM person_appearances pa "
            "JOIN entities e ON e.entity_id = pa.entity_id "
            "WHERE pa.user_id = $1 "
            "  AND (e.merged_into IS NULL OR e.entity_id = ANY($2::uuid[])) "
            "  AND e.suppressed_at IS NULL",
            u["user_id"], list(fold_map.keys()),
        )
        origins_by_entity: dict = {}
        for p in pairs:
            eid = fold_map.get(p["entity_id"], p["entity_id"])
            origins_by_entity.setdefault(eid, set()).add(p["origin_id"])
        coverage = {eid: len(o) for eid, o in origins_by_entity.items()}
        pick = pick_self_candidate(
            coverage, u["total"], args.min_coverage, args.min_margin,
        )
        if pick is None:
            top = sorted(coverage.items(), key=lambda kv: kv[1], reverse=True)[:2]
            detail = ", ".join(f"{eid}={n}/{u['total']}" for eid, n in top)
            print(f"{u['user_id']}: REFUSED (no dominant candidate: {detail})")
            refused += 1
            continue
        entity_id, cov, margin = pick
        name = await conn.fetchval(
            "SELECT name FROM entities WHERE entity_id = $1", entity_id
        )
        print(
            f"{u['user_id']}: stamp {name!r} ({entity_id}) "
            f"coverage={cov:.2f} margin={margin:.2f}"
        )
        if args.apply:
            await conn.execute(
                "UPDATE entities SET self_at = NOW(), self_source = 'backfill' "
                "WHERE entity_id = $1 AND self_at IS NULL AND suppressed_at IS NULL",
                entity_id,
            )
        stamped += 1

    print(f"== done: {stamped} stamped, {refused} refused, {skipped} already ==")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
