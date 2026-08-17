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


def fused_candidates(people):
    """people: iterable of (entity_id, name). Returns ranked candidates.

    A candidate is a two-token name "A B" where A is the first token of a
    different person and B is the first token of a third person. Confidence
    drops when B is ALSO attested as somebody's surname on this roster,
    because then B is a real family name and the coincidence is ordinary.
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

        out.append({
            "entity_id": entity_id,
            "name": name,
            "head_sources": head_sources,
            "tail_sources": tail_sources,
            "tail_also_a_surname_of": tail_as_surname,
            "confidence": confidence,
        })

    out.sort(key=lambda c: (c["confidence"] != "high", c["name"].lower()))
    return out


SELECT_PEOPLE = """
    SELECT entity_id, name
    FROM entities
    WHERE entity_type = 'person'
      AND COALESCE(status, 'active') = 'active'
      AND user_id = $1
"""

EVIDENCE = """
    SELECT
      (SELECT count(*) FROM person_appearances pa
        WHERE pa.entity_id = $1) AS meetings,
      (SELECT count(*) FROM context_patches cp
        WHERE cp.value->>'owner' = $2
          AND COALESCE(cp.status,'active') = 'active') AS owned_patches
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

        candidates = fused_candidates(people)
        if not candidates:
            print("No fused-name candidates.")
            return 0

        for c in candidates:
            ev = await conn.fetchrow(EVIDENCE, c["entity_id"], c["name"])
            print(f"[{c['confidence']}] {c['name']}  ({c['entity_id']})")
            print(f"    meetings={ev['meetings']}  owned_patches={ev['owned_patches']}")
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
