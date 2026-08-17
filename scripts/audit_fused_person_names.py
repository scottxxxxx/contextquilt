"""Find person entities that look like TWO people fused into one name.

The receipt: `Pallavi Vijay` is not a person. Scott's words: two people
concatenated together. The roster also holds `Pallavi Kandanu` and
`Vijay Rayudu`, so the extractor took two adjacent first names out of a
transcript and welded them into one entity.

WHY THE COLLISION AUDIT CANNOT SEE THIS. That audit groups entities that
share a first token, so it finds `Pallavi` / `Pallavi Kandanu` (one human,
two spellings). A fused row is the opposite shape: its two halves come
from two DIFFERENT humans, so it collides with each of them on only one
token and looks like a third person to any first-token grouping.

THE SIGNAL. On a roster, a surname is rarely also somebody else's first
name. So a two-token name whose BOTH tokens are first names of other live
people is suspicious in a way that a normal full name is not. `Mike
Peterson` is safe (nobody on the roster is named Peterson-first). `Pallavi
Vijay` is not.

THIS IS A CANDIDATE FINDER, NOT A FIXER, AND IT HAS NO --apply BY DESIGN.
A fused row must be split or deleted, and only a human knows which of its
patches belong to whom, if that is recoverable at all. Merging it into
either source would move a second person's facts onto them, which is
worse than leaving it. It also cannot be perfectly precise: a real person
named `Ryan Thomas` trips the same rule when the roster holds a
`Thomas Someone`, so every hit is ranked and evidenced for a human read.
"""
import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict

TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)


def tokens(name):
    """Lowercased word tokens. Empty list for anything unusable."""
    if not name or not isinstance(name, str):
        return []
    return [t for t in TOKEN_RE.split(name.strip().lower()) if t]


def fused_candidates(people, volumes=None):
    """people: iterable of (entity_id, name). Returns ranked candidates.

    A candidate is a two-token name "A B" where A is the first token of a
    different person and B is the first token of a third person. Confidence
    drops when B is ALSO attested as somebody's surname on this roster,
    because then B is a real family name and the coincidence is ordinary.

    `volumes` maps entity_id -> meeting count. Optional, because the
    structural rule stands without it, but supply it when you can: the
    ratio of a candidate's volume to its biggest source is what says how
    much a WRONG call costs, and therefore whether a hit is worth putting
    in front of a human at all. A 6-meeting row beside an 87-meeting
    source is a small artifact next to a real person and cheap to settle.
    Three rows at 40 each is an expensive question, and a "fused" row as
    substantial as its supposed sources undercuts the fusion story rather
    than supporting it.
    """
    parsed = []
    for entity_id, name in people:
        toks = tokens(name)
        if toks:
            parsed.append((entity_id, name, toks))

    first_token = defaultdict(list)   # token -> [(entity_id, name)]
    last_token = defaultdict(list)
    for entity_id, name, toks in parsed:
        if len(toks) >= 2:
            first_token[toks[0]].append((entity_id, name))
            last_token[toks[-1]].append((entity_id, name))

    out = []
    for entity_id, name, toks in parsed:
        if len(toks) != 2:
            continue
        head, tail = toks

        head_sources = [p for p in first_token.get(head, []) if p[0] != entity_id]
        tail_sources = [p for p in first_token.get(tail, []) if p[0] != entity_id]
        if not head_sources or not tail_sources:
            continue
        # The two halves must point at two DIFFERENT people, else this is
        # just one person's name colliding with itself.
        if {p[0] for p in head_sources} == {p[0] for p in tail_sources}:
            continue

        tail_as_surname = [p for p in last_token.get(tail, []) if p[0] != entity_id]
        confidence = "low" if tail_as_surname else "high"

        candidate = {
            "entity_id": entity_id,
            "name": name,
            "head_sources": head_sources,
            "tail_sources": tail_sources,
            "tail_also_a_surname_of": tail_as_surname,
            "confidence": confidence,
            "meetings": None,
            "biggest_source": None,
            "volume_ratio": None,
        }

        if volumes:
            mine = volumes.get(entity_id)
            source_volumes = [
                (volumes.get(p[0]) or 0, p[1])
                for p in head_sources + tail_sources
            ]
            biggest = max(source_volumes, default=(0, None))
            candidate["meetings"] = mine
            candidate["biggest_source"] = biggest[1]
            # Guard the denominator: a source with no appearances tells us
            # nothing about cost, so leave the ratio honestly null rather
            # than inventing an infinity.
            if mine is not None and biggest[0] > 0:
                candidate["volume_ratio"] = round(mine / biggest[0], 3)

        out.append(candidate)

    # High confidence first, then cheapest-to-settle first: a candidate
    # dwarfed by its source is both more likely spurious and less costly
    # to act on. Unknown ratio sorts mid, never ahead of a measured one.
    out.sort(key=lambda c: (
        c["confidence"] != "high",
        c["volume_ratio"] if c["volume_ratio"] is not None else 0.5,
        c["name"].lower(),
    ))
    return out


SELECT_PEOPLE = """
    SELECT entity_id, name
    FROM entities
    WHERE entity_type = 'person'
      AND COALESCE(status, 'active') = 'active'
      AND user_id = $1
"""

# Every person's meeting count in one pass. Ranking needs the SOURCES'
# volumes as well as each candidate's, so a per-candidate query would be
# N+1 against a table that is cheap to read whole.
SELECT_VOLUMES = """
    SELECT entity_id, count(*) AS meetings
    FROM person_appearances
    WHERE user_id = $1
    GROUP BY entity_id
"""

OWNED_PATCHES = """
    SELECT count(*) AS owned_patches
    FROM context_patches
    WHERE value->>'owner' = $1
      AND COALESCE(status,'active') = 'active'
"""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="user_id / subject to audit")
    args = parser.parse_args()

    import asyncpg
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(SELECT_PEOPLE, args.user)
        people = [(str(r["entity_id"]), r["name"]) for r in rows]
        print(f"{len(people)} live person entities for {args.user}\n")

        vol_rows = await conn.fetch(SELECT_VOLUMES, args.user)
        volumes = {str(r["entity_id"]): r["meetings"] for r in vol_rows}

        candidates = fused_candidates(people, volumes=volumes)
        if not candidates:
            print("No fused-name candidates.")
            return 0

        for c in candidates:
            owned = await conn.fetchval(OWNED_PATCHES, c["name"])
            print(f"[{c['confidence']}] {c['name']}  ({c['entity_id']})")
            print(f"    meetings={c['meetings']}  owned_patches={owned}")
            if c["volume_ratio"] is not None:
                print(f"    volume vs biggest source ({c['biggest_source']}): "
                      f"{c['volume_ratio']}  <- cost of a wrong call")
            print(f"    head matches: {', '.join(n for _, n in c['head_sources'])}")
            print(f"    tail matches: {', '.join(n for _, n in c['tail_sources'])}")
            if c["tail_also_a_surname_of"]:
                names = ", ".join(n for _, n in c["tail_also_a_surname_of"])
                print(f"    NOTE: '{c['name'].split()[-1]}' is a real surname here ({names})")
            print()

        print(f"{len(candidates)} candidate(s). Report only, nothing written.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.path.insert(0, "/app/src")
    sys.exit(asyncio.run(main()))
