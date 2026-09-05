"""Which entities a recall request names, and which person a bare name means.

Two defects read off the live route on 2026-09-04, both in the header a
project chat is handed.

1. "RV" matched the word "interview". The match was a bare substring
   test (`name.lower() in text_lower`) over an index that holds dozens
   of two and three letter names (RV, Don, Mac, Sam, QA, CD), so an
   ordinary word could summon a person. The cue leg already solved this
   for topic phrases with a word-boundary scan; the entity index now
   uses the same helper, one carrier for one rule.

2. "Steven" in an Immigration chat resolved to Steven Levy from a
   different project, because he holds the recorded alias "Steven" and
   Steven Williams, who is actually in the Immigration meetings, holds
   none. A bare first name is the one form the resolver was taught not
   to trust on 2026-09-04 (#434), and the read side trusted it. So when
   a matched term is a single token and more than one person could
   answer to it, the project decides: the candidates with presence in
   the recall's project win, and a candidate with no presence there is
   dropped from the header. No presence anywhere in the project means no
   evidence to override the alias, and the old resolution stands.

Pure functions; the route owns the pool.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Sequence

from contextquilt.services.cue_matching import cue_matches

# An apostrophe joins a word for a NAME: "don't" must not summon Don, and
# "Steven's mother" is its own entity rather than a mention of Steven.
NAME_JOINERS = "'\u2019"


def match_entity_names(known: Iterable[str], text_lower: str) -> List[str]:
    """Index names that occur in the request text on word boundaries.

    Sorted, because the result reaches the header, the graph walk and
    the LLM prompt prefix, and set iteration order is not stable across
    calls (prompt caching downstream depends on byte-stable output).
    """
    if not known or not text_lower:
        return []
    text_lower = text_lower.lower()
    return [
        name for name in sorted(known)
        if name and cue_matches(name.lower(), text_lower, extra_word_chars=NAME_JOINERS)
    ]


def bare_terms(matched_names: Sequence[str]) -> List[str]:
    """The matched names that are a single token: the contested form."""
    out: List[str] = []
    for n in matched_names:
        t = n.strip().lower()
        if t and " " not in t and t not in out:
            out.append(t)
    return out


def _first_token(name: str) -> str:
    return (name or "").strip().lower().split(" ")[0]


def disambiguate_bare_names(
    entity_rows: Sequence[Any],
    matched_names: Sequence[str],
    candidates: Sequence[Any],
    person_entity_type: str = "person",
) -> List[Dict[str, Any]]:
    """Resolve each bare matched term to the people the PROJECT knows.

    `entity_rows` is what the name/alias lookup resolved. `candidates`
    are person entities whose name, first token or alias equals a bare
    term, each carrying `present` (has an appearance in the recall's
    project) and `term` (which bare term it answers to). For a term with
    at least one present candidate, the present ones are the answer and
    any other candidate for that term leaves the header, unless its full
    name was itself matched in the text. A term with no present
    candidate keeps whatever the lookup resolved.

    Output is ordered by entity_id like the lookup, so the render stays
    byte-stable for identical input.
    """
    full_names = {n.strip().lower() for n in matched_names if " " in n.strip()}
    by_term: Dict[str, List[Any]] = {}
    for c in candidates:
        by_term.setdefault(str(c["term"]).lower(), []).append(c)

    drop: set = set()
    add: Dict[Any, Dict[str, Any]] = {}
    for term, cands in by_term.items():
        present = [c for c in cands if c["present"]]
        if not present:
            continue
        for c in cands:
            name_l = (c["name"] or "").strip().lower()
            if c["present"] or name_l in full_names:
                continue
            drop.add(c["entity_id"])
        for c in present:
            add[c["entity_id"]] = {
                "entity_id": c["entity_id"], "name": c["name"],
                "entity_type": c.get("entity_type") or person_entity_type,
                "description": c.get("description"),
            }

    out: Dict[Any, Dict[str, Any]] = {}
    for r in entity_rows:
        if r["entity_id"] in drop:
            continue
        out[r["entity_id"]] = {
            "entity_id": r["entity_id"], "name": r["name"],
            "entity_type": r["entity_type"], "description": r["description"],
        }
    for eid, row in add.items():
        out.setdefault(eid, row)
    return [out[k] for k in sorted(out, key=lambda x: str(x))]


# Person entities that could answer to a bare term: exact name, first
# token of the name, or a recorded alias. `present` is an appearance in
# the recall's project. Merged and suppressed rows are out: a folded row
# is an alias of its survivor and a suppressed one was disowned.
BARE_NAME_CANDIDATES_SQL = """
    SELECT DISTINCT e.entity_id, e.name, e.entity_type, e.description,
           t.term,
           EXISTS (SELECT 1 FROM person_appearances pa
                    WHERE pa.user_id = $1 AND pa.entity_id = e.entity_id
                      AND pa.project_id = $3) AS present
    FROM entities e
    JOIN unnest($2::text[]) AS t(term)
      ON lower(e.name) = t.term
      OR lower(split_part(e.name, ' ', 1)) = t.term
      OR EXISTS (SELECT 1 FROM entity_aliases a
                  WHERE a.entity_id = e.entity_id AND lower(a.alias) = t.term)
    WHERE e.user_id = $1 AND e.entity_type = $4
      AND e.merged_into IS NULL AND e.suppressed_at IS NULL
    ORDER BY e.entity_id
"""
