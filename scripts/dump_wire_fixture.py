"""
Emit a route's response EXACTLY as the wire carries it.

Why this exists
---------------

On 2026-08-31 ShoulderSurf asked for the leanest patch the woven routes
can serve, explicitly as real bytes rather than a description, because
`rows` versus `row_pairs` had already cost them a suite of tests written
against what two teams SAID.

I generated it from the deployed service, in the production container,
against real patches, and sent the output. It carried

    "occurred_at": "2026-08-31 14:14:10.707165+00:00"

a space where ISO-8601 wants a T. They decoded my exact bytes, found
that a present-but-unparseable date THROWS rather than yielding nil, and
that one bad timestamp failed the patch, then the array, then the whole
digest, surfacing as "Couldn't reach your memory just now". They shipped
a fix and sabotaged it to prove it bit.

THE API NEVER EMITTED THAT. The throwaway script used
`json.dumps(..., default=str)`, and `str(datetime)` uses a space. The
route returns a raw datetime and FastAPI's `jsonable_encoder` calls
`.isoformat()`. Same process, same image, same data, DIFFERENT
SERIALIZER.

So "generated from the running code" named the PROCESS, not the PATH,
and a generated artifact carries more authority than a description while
being just as capable of being wrong. That authority is the defect: SS
had a note in their own repo documenting CQ timestamps correctly, months
old and written from evidence, and did not look at it because the
fixture was newer and generated.

This script is the correct-by-construction version: it serializes
through `fastapi.encoders.jsonable_encoder`, the same call the framework
makes on the way out, so a fixture it produces cannot differ from the
wire in the way that one did.

    python scripts/dump_wire_fixture.py woven            # home digest
    python scripts/dump_wire_fixture.py seam             # meeting seam
    python scripts/dump_wire_fixture.py woven --lean     # sparsest patch

Run it inside the API container so the FastAPI version matches the one
serving traffic; a fixture generated against a different encoder version
is the same class of mistake one layer down.
"""

import argparse
import asyncio
import json
import os
import sys

# Resilient to being piped over stdin, which is how this gets run inside
# a container that does not yet carry the file. `__file__` is absent
# there, and the first version of this failed with ModuleNotFoundError
# for that reason alone: a tool about matching the deployed path that
# could not run against the deployed process.
for _candidate in (
    os.path.join(os.path.dirname(__file__), "..", "src") if "__file__" in dir() else None,
    "/app/src",
    os.path.join(os.getcwd(), "src"),
):
    if _candidate and os.path.isdir(_candidate):
        sys.path.insert(0, _candidate)
        break

import asyncpg  # noqa: E402
from fastapi.encoders import jsonable_encoder  # noqa: E402

from contextquilt.services import woven_digest as wd  # noqa: E402


def wire(obj) -> str:
    """The ONE line that matters in this file.

    `jsonable_encoder` is what FastAPI calls on a route's return value,
    so this is the wire shape by construction rather than by care.
    Anything that reaches for `json.dumps(default=str)` here reintroduces
    the exact defect the module docstring describes.
    """
    return json.dumps(jsonable_encoder(obj), indent=2, sort_keys=True)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("route", choices=["woven", "seam"])
    ap.add_argument("--lean", action="store_true",
                    help="the sparsest patch rather than the full body")
    ap.add_argument("--user", help="user_id; default is the busiest")
    ap.add_argument("--window", type=int, default=7)
    args = ap.parse_args()

    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"],
                                     min_size=1, max_size=2)
    subject = (f"user:{args.user}" if args.user else await pool.fetchval(
        "SELECT subject_key FROM patch_subjects GROUP BY subject_key "
        "ORDER BY count(*) DESC LIMIT 1"))

    from main import WOVEN_CANDIDATE_SQL
    rows = await pool.fetch(
        WOVEN_CANDIDATE_SQL.replace("{PROJECT}", ""), subject, args.window)
    candidates = [dict(r) for r in rows]
    edges = {str(r["patch_id"]): int(r["edge_count"] or 0) for r in rows}

    if args.route == "woven":
        digest = wd.build_digest(candidates, limit=6, edge_counts=edges)
        patches = digest["patches"]
    else:
        # The seam builds its own dict and is deliberately leaner: no
        # weight, span or height, because it is capture order with
        # nothing to tile.
        patches = [{
            "patch_id": p.get("patch_id"),
            "patch_type": p.get("patch_type"),
            "fact": (wd.patch_value(p).get("text") or "").strip(),
            "headline": wd.patch_value(p).get("headline") or None,
            "source_meeting_id": p.get("origin_id"),
            "occurred_at": p.get("created_at"),
        } for p in candidates if wd.why_not_a_tile(p, require_headline=False) is None]

    if not patches:
        print("no patches to dump for this subject and window")
        await pool.close()
        return 1

    if args.lean:
        # Fewest populated keys wins, so the fixture is the hardest case
        # a decoder has to survive rather than a convenient one.
        chosen = min(patches, key=lambda p: sum(1 for v in p.values() if v))
        chosen = dict(chosen)
        chosen.setdefault("stitched_to", [])
        print(wire(chosen))
    else:
        print(wire(patches[:2]))
    await pool.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
