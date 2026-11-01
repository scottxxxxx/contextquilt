"""Fix a misheard word in what ONE meeting produced (2026-10-31).

ShoulderSurf's "fix a misheard word once, everywhere it landed": Scott
saw "Kore.ai" come through the recognizer as "Cora" in several meetings
and the mistake propagate into the summary, the report and the answers.
The client corrects every artifact it holds for the meeting; this is the
CQ half, POST /v1/origins/{u}/{ot}/{oid}/replace-term.

WHY A ROUTE AND NOT A RE-INGEST. A re-extraction of the corrected
transcript cannot fix a stored fact: the dedup merge keeps the EXISTING
text by design (store_connected_patches), so the stored row would keep
saying "Cora", and a fact the model words differently would land as a
second patch beside it. The corrected transcript is stored without
re-extraction (ingest_markers.REPLACE_MARKER) and this route rewrites
the facts in place.

THE MATCH IS THE CLIENT'S, EXACTLY, because the client shows our count
beside its own and two different word rules would make the comparison
noise. ShoulderSurf's rule (NSRegularExpression, ICU): the term is split
on whitespace, each piece escaped, joined with \\s+, and wrapped in
(?<![\\p{L}\\p{N}]) ... (?![\\p{L}\\p{N}]). Python's `re` has no \\p{L},
so the boundary is written [^\\W_], which is Unicode letters and digits
without the underscore. Case-insensitive unless match_case. The
replacement is LITERAL. Known edges where ICU and Python could differ:
full case folding (ß against SS) and a few numeric code points
(superscripts, Roman numeral characters). The echoed count is what
would reveal either.

ENTITIES ARE USER-WIDE, not meeting-scoped, so "Cora" in this meeting
can be a real person called Cora in twenty others. An entity is renamed
only when it is provably this meeting's alone: a person whose every
recorded appearance is this meeting. Everything else that matches is
reported with its reason and left alone. A non-person entity carries no
per-meeting record at all, so it is always reported, never guessed at.

DERIVED PROSE IS COUNTED, NOT EDITED. Insight cards and person summaries
quote their sources in model-written prose; editing generated text by
regex would put words in a card nobody generated. They are counted so
the caller can say so.

Everything that decides is pure and executed by test_term_replace.py.
`apply` is the database half, here rather than in the route so that
test_replace_term_db.py can EXECUTE it against a migrated Postgres in CI
(the route cannot import without fastapi, and lifted SQL agrees with
itself, not with production). The route owns the pool, the transaction
and the queue writes that follow the commit.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Pattern, Tuple

from contextquilt.services.headlines import self_headline

# The string fields of a patch value that carry words a recognizer can
# mishear. `headline` is NOT here: it is derived from `text` and is
# recomputed by the route the same way a PATCH fact edit recomputes it.
TEXT_KEYS = ("text", "owner", "owed_to", "deadline")

# The entity verdicts. Open vocabulary: a client that meets a reason it
# does not know should show the entity as untouched, which it is.
RENAMED = "renamed"
SKIP_OTHER_MEETINGS = "appears_in_other_meetings"
SKIP_NO_MEETING_RECORD = "no_meeting_record"
SKIP_TARGET_EXISTS = "target_name_exists"

# Published on the wire so the limits are read, not discovered.
LIMITS_DEFINITION = (
    "Rewrites this meeting's stored facts only. Entities are renamed only "
    "when every recorded appearance is this meeting; others are listed in "
    "entities_skipped and unchanged. Insight cards and summaries that "
    "quote the old wording are counted in derived_referencing, not "
    "edited. Undo by swapping from and to: an occurrence of the new term "
    "that was already there before the fix is indistinguishable and "
    "would be swapped too. Repeating the same rule is a no-op, including "
    "when the new term contains the old one as a whole word: an old term "
    "that already sits inside an occurrence of the new term is left alone."
)


def compile_term(term: Any, match_case: bool = False) -> Optional[Pattern[str]]:
    """The client's whole-word pattern for `term`, or None when the term
    is empty or not a string (the route answers 422)."""
    if not isinstance(term, str):
        return None
    pieces = term.split()
    if not pieces:
        return None
    body = r"\s+".join(re.escape(p) for p in pieces)
    flags = 0 if match_case else re.IGNORECASE
    return re.compile(rf"(?<![^\W_]){body}(?![^\W_])", flags)


def already_applied_guard(pattern: Pattern[str], to: Any) -> Optional[Pattern[str]]:
    """The pattern that recognises an occurrence of `to` in stored text,
    or None when the rule needs no guard.

    THE ROUTE IS STATELESS, so a rule whose new term CONTAINS the old
    one as a whole word ("Kore" to "Kore AI", "Jon" to "Jon Smith",
    "Kore" to "Kore.ai") would, on a second identical call, find the old
    term inside its own replacement and apply again: "Kore AI AI".
    Every retry path converges on this route (a client's verification
    call, a retry after a gateway 504, a user tapping twice), and the
    first client to repeat such a rule was a dev build within hours of
    the ship (2026-10-31). So an old term that already sits inside an
    occurrence of the new term is treated as already corrected.

    The guard is the SAME whole-word rule compiled on `to`, with the
    same case flag, so what counts as "an occurrence of the new term"
    is decided the way the client decides it. When `to` does not contain
    the old term there is nothing to protect and behaviour is exactly
    the single-pass replacement.
    """
    if not isinstance(to, str) or pattern.search(to) is None:
        return None
    return compile_term(to, match_case=not (pattern.flags & re.IGNORECASE))


def _protected_spans(text: str, guard: Optional[Pattern[str]]) -> List[Tuple[int, int]]:
    if guard is None:
        return []
    return [m.span() for m in guard.finditer(text)]


def _inside(span: Tuple[int, int], spans: List[Tuple[int, int]]) -> bool:
    start, end = span
    return any(s <= start and end <= e for s, e in spans)


def replace_text(text: Any, pattern: Pattern[str], to: str,
                 guard: Optional[Pattern[str]] = None) -> Tuple[Any, int]:
    """(new_text, matches). Literal replacement: a backslash or `\\1` in
    `to` is inserted as typed, never expanded. With a `guard`, a match
    lying inside an occurrence of the new term is neither replaced nor
    counted (see already_applied_guard)."""
    if not isinstance(text, str) or not text:
        return text, 0
    spans = _protected_spans(text, guard)
    count = 0

    def repl(m: "re.Match[str]") -> str:
        nonlocal count
        if spans and _inside(m.span(), spans):
            return m.group(0)
        count += 1
        return to

    new = pattern.sub(repl, text)
    return new, count


def term_occurs(text: Any, pattern: Pattern[str],
                guard: Optional[Pattern[str]] = None) -> bool:
    """Whether the old term still occurs, under the same guard as the
    rewrite, so a count and a rewrite never disagree about one text."""
    if not isinstance(text, str) or not text:
        return False
    spans = _protected_spans(text, guard)
    return any(not (spans and _inside(m.span(), spans)) for m in pattern.finditer(text))


def replace_in_value(value: Mapping[str, Any], pattern: Pattern[str],
                     to: str, guard: Optional[Pattern[str]] = None,
                     ) -> Tuple[Dict[str, Any], int, List[str]]:
    """A patch value with the term replaced in every word-bearing field.

    Returns (new_value, matches, changed_keys). `new_value` is a copy;
    the input is never mutated, so a caller that decides not to write
    holds the original unchanged. Restatements (the ledger's record of
    what was said each time) carry their own `text` and are corrected
    too, because they are rendered as quotes.
    """
    out = dict(value or {})
    matches = 0
    changed: List[str] = []
    for key in TEXT_KEYS:
        new, n = replace_text(out.get(key), pattern, to, guard)
        if n:
            out[key] = new
            matches += n
            changed.append(key)
    rest = out.get("restatements")
    if isinstance(rest, list):
        new_rest = []
        rest_n = 0
        for item in rest:
            if isinstance(item, dict):
                new, n = replace_text(item.get("text"), pattern, to, guard)
                if n:
                    item = {**item, "text": new}
                    rest_n += n
            new_rest.append(item)
        if rest_n:
            out["restatements"] = new_rest
            matches += rest_n
            changed.append("restatements")
    return out, matches, changed


def replace_cue(cue: Any, pattern: Pattern[str], to: str,
                guard: Optional[Pattern[str]] = None) -> Optional[str]:
    """The corrected cue, or None when it does not match. Cues are stored
    lowercase (sanitize_cues), so the result is lowercased too."""
    new, n = replace_text(cue, pattern, to, guard)
    if not n:
        return None
    return " ".join(new.lower().split())


def entity_verdict(entity_type: Any, appearance_origins: Iterable[Any],
                   origin_id: str, target_taken: bool) -> str:
    """Whether a matching entity may be renamed by a fix in ONE meeting.

    Only a person whose EVERY recorded appearance is this meeting. No
    appearance at all means CQ has no record tying the entity to any
    meeting, so it cannot be shown to be this meeting's alone. A target
    name that already exists is a merge, which is a different operation
    with its own route, never a side effect of a word fix.
    """
    origins = {str(o) for o in appearance_origins if o}
    if entity_type != "person" or not origins:
        return SKIP_NO_MEETING_RECORD
    if origins != {str(origin_id)}:
        return SKIP_OTHER_MEETINGS
    if target_taken:
        return SKIP_TARGET_EXISTS
    return RENAMED


def validate_body(body: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """A 422 detail, or None when the body is usable. `to` must be a
    non-empty string: a fix that deletes words from stored facts is not
    a word correction, and an empty value eaten by a middlebox must not
    silently become one."""
    if compile_term(body.get("from")) is None:
        return {"code": "INVALID_TERM",
                "message": "`from` must be a non-empty string."}
    to = body.get("to")
    if not isinstance(to, str) or not to.strip():
        return {"code": "INVALID_REPLACEMENT",
                "message": "`to` must be a non-empty string."}
    mc = body.get("match_case")
    if mc is not None and not isinstance(mc, bool):
        return {"code": "INVALID_MATCH_CASE",
                "message": "`match_case` must be true, false or absent."}
    return None


def response_body(origin_id: str, origin_type: str, *, from_term: str,
                  to_term: str, match_case: bool, matches: int,
                  patch_ids: List[str], cues_changed: int,
                  entities_renamed: List[Dict[str, Any]],
                  entities_skipped: List[Dict[str, Any]],
                  derived_referencing: int) -> Dict[str, Any]:
    """The echo. Counts the caller compares with its own (rule 4), the
    ids behind them, and the limits stated on the wire."""
    return {
        "origin_id": origin_id,
        "origin_type": origin_type,
        "from": from_term,
        "to": to_term,
        "match_case": bool(match_case),
        "matches": int(matches),
        "patches_changed": len(patch_ids),
        "patch_ids": list(patch_ids),
        "cues_changed": int(cues_changed),
        "entities_renamed": list(entities_renamed),
        "entities_skipped": list(entities_skipped),
        "entities_skipped_shared": len(entities_skipped),
        "derived_referencing": int(derived_referencing),
        "limits": LIMITS_DEFINITION,
    }


# --- the database half ---------------------------------------------------

# Every patch this origin produced, ANY status: a completed commitment is
# still served in completion history and quotes the misheard word there.
SCOPE_SQL = """
    SELECT cp.patch_id, cp.value
    FROM context_patches cp
    JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
    WHERE ps.subject_key = $1
      AND cp.origin_type = $2
      AND cp.origin_id = $3
    ORDER BY cp.created_at, cp.patch_id
    FOR UPDATE OF cp
"""

# updated_at moves so the delta carries the corrected text to other
# devices. last_observed_at does NOT (nobody said anything again) and
# origin_mode is NOT flipped (the fact is still what the meeting said).
UPDATE_SQL = "UPDATE context_patches SET value = $1, updated_at = $2 WHERE patch_id = $3"

CUES_SQL = "SELECT patch_id, cue FROM patch_cues WHERE patch_id = ANY($1::uuid[])"
CUE_INSERT_SQL = ("INSERT INTO patch_cues (patch_id, cue) VALUES ($1, $2) "
                  "ON CONFLICT DO NOTHING")
CUE_DELETE_SQL = "DELETE FROM patch_cues WHERE patch_id = $1 AND cue = $2"

# A cheap ILIKE on the longest piece narrows the user's whole roster;
# the client's pattern then decides. Backslash is the escape character.
ENTITY_CANDIDATES_SQL = """
    SELECT entity_id, name, entity_type FROM entities
    WHERE user_id = $1 AND name ILIKE '%' || $2 || '%' ESCAPE '\\'
    ORDER BY name, entity_id
"""
ENTITY_ORIGINS_SQL = ("SELECT DISTINCT origin_id FROM person_appearances "
                      "WHERE user_id = $1 AND entity_id = $2")
ENTITY_TAKEN_SQL = ("SELECT 1 FROM entities WHERE user_id = $1 AND name = $2 "
                    "AND entity_type = $3 AND entity_id <> $4")
ENTITY_RENAME_SQL = "UPDATE entities SET name = $1 WHERE entity_id = $2"

# source_patch_ids is TEXT[] (01_init.sql), so the overlap is on text.
DERIVED_SQL = """
    SELECT cp.value::text AS body
    FROM context_patches cp
    JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
    WHERE ps.subject_key = $1
      AND cp.origin_mode = 'derived'
      AND COALESCE(cp.status, 'active') = 'active'
      AND cp.source_patch_ids && $2::text[]
"""


def like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def apply(conn, user_id: str, origin_type: str, origin_id: str,
                pattern: Pattern[str], to: str, *, term: str) -> Dict[str, Any]:
    """Rewrite one origin's stored facts. Run inside the caller's
    transaction. Returns the counts and ids the echo is built from, plus
    `headline_jobs`: patches whose new text is not its own headline and
    must be rewritten on the cold path, as a PATCH fact edit does."""
    subject_key = f"user:{user_id}"
    now = datetime.utcnow()
    matches = 0
    changed_ids: List[str] = []
    headline_jobs: List[str] = []
    # One guard for every text this call touches, so a repeat of a rule
    # whose new term contains the old one is a no-op on all of them.
    guard = already_applied_guard(pattern, to)

    rows = await conn.fetch(SCOPE_SQL, subject_key, origin_type, origin_id)
    scope_ids = [str(r["patch_id"]) for r in rows]
    for r in rows:
        value = r["value"]
        if isinstance(value, str):
            value = json.loads(value)
        new_value, n, keys = replace_in_value(value or {}, pattern, to, guard)
        if not n:
            continue
        if "text" in keys:
            own = self_headline(new_value.get("text"))
            if own is not None:
                new_value["headline"] = own
            else:
                new_value.pop("headline", None)
                headline_jobs.append(str(r["patch_id"]))
        await conn.execute(UPDATE_SQL, json.dumps(new_value), now, r["patch_id"])
        matches += n
        changed_ids.append(str(r["patch_id"]))

    cues_changed = 0
    if scope_ids:
        for c in await conn.fetch(CUES_SQL, scope_ids):
            new_cue = replace_cue(c["cue"], pattern, to, guard)
            if not new_cue or new_cue == c["cue"]:
                continue
            await conn.execute(CUE_INSERT_SQL, c["patch_id"], new_cue)
            await conn.execute(CUE_DELETE_SQL, c["patch_id"], c["cue"])
            cues_changed += 1

    renamed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    longest = max(str(term).split(), key=len)
    for e in await conn.fetch(ENTITY_CANDIDATES_SQL, user_id, like_escape(longest)):
        new_name, n = replace_text(e["name"], pattern, to, guard)
        if not n:
            continue
        origins = [a["origin_id"] for a in
                   await conn.fetch(ENTITY_ORIGINS_SQL, user_id, e["entity_id"])]
        taken = bool(await conn.fetchval(
            ENTITY_TAKEN_SQL, user_id, new_name, e["entity_type"], e["entity_id"]))
        verdict = entity_verdict(e["entity_type"], origins, origin_id, taken)
        entry = {"entity_id": str(e["entity_id"]), "name": e["name"],
                 "entity_type": e["entity_type"]}
        if verdict == RENAMED:
            await conn.execute(ENTITY_RENAME_SQL, new_name, e["entity_id"])
            renamed.append({**entry, "new_name": new_name})
        else:
            skipped.append({**entry, "reason": verdict})

    derived_referencing = 0
    if scope_ids:
        derived_referencing = sum(
            1 for d in await conn.fetch(DERIVED_SQL, subject_key, scope_ids)
            if term_occurs(d["body"] or "", pattern, guard))

    return {"matches": matches, "patch_ids": changed_ids,
            "headline_jobs": headline_jobs, "cues_changed": cues_changed,
            "entities_renamed": renamed, "entities_skipped": skipped,
            "derived_referencing": derived_referencing}
