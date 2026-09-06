"""
Cue matching for associative recall.

A cue is a short lowercase topic phrase attached to a patch at
extraction time ("pricing model"), indexed per user so that request
text naming a topic but no entity still recalls the right patches.

The matcher used to be `if cue in text_lower`, a bare substring test.
The cue index holds three-letter cues, so ordinary English carried
matches nobody intended: "projects", "products", "aspects" and
"effects" all contain "cts"; "rapidly" and "capital" both contain
"api". On prod the cue "cts" sat on 19 patches spanning two projects,
and the cue leg serves up to 10 rows per matched cue, so a common word
in an unrelated sentence could pull another project's memory into the
context block. A cue names a topic, and a topic is a word, so the
match has to respect word boundaries.

Everything here is a pure function of (cue, text): no clock, no I/O,
no randomness. Recall output must stay byte-stable within a UTC day
because upstream prompt caching depends on it.

Cost note, since this is the hot path: the substring test stays as the
prefilter and the boundary check only runs where it already hit, so a
non-matching cue costs exactly what it cost before. No regex, so no
per-pattern compile and no cache thrash on a user with hundreds of
distinct cues.
"""

from typing import Iterable, List

from contextquilt.services.recall_scope import origins_cte, project_scope_clause


def _is_word_char(ch: str, extra: str = "") -> bool:
    # Mirrors the \w class rather than str.isalnum() alone: an
    # underscore joins words in identifiers, and cues are lowercase
    # topic phrases that should not match inside one. `extra` lets a
    # caller widen the class: the entity matcher counts an apostrophe
    # as part of a word so "Don" cannot answer to "don't".
    return ch.isalnum() or ch == "_" or (bool(extra) and ch in extra)


def cue_matches(cue: str, text_lower: str, extra_word_chars: str = "") -> bool:
    """True when `cue` occurs in `text_lower` on word boundaries.

    Checks every occurrence, not just the first: "the api rate" contains
    "api" twice over if the first hit is inside "rapidly", and stopping
    at the first hit would decline a real match.

    Boundaries are checked on the neighbouring character, so a cue that
    itself begins or ends with punctuation still behaves: what matters
    is that the text does not continue the word on either side.
    """
    if not cue:
        return False
    n, m = len(text_lower), len(cue)
    start = 0
    while True:
        i = text_lower.find(cue, start)
        if i < 0:
            return False
        before_ok = i == 0 or not _is_word_char(text_lower[i - 1], extra_word_chars)
        j = i + m
        after_ok = j == n or not _is_word_char(text_lower[j], extra_word_chars)
        if before_ok and after_ok:
            return True
        start = i + 1


def match_cues(known_cues: Iterable[str], text_lower: str) -> List[str]:
    """Cues from the index that the request text actually names.

    Sorted, because the returned list reaches the rendered block and
    the SQL leg, and set iteration order is not stable across runs.
    """
    if not known_cues or not text_lower:
        return []
    return [cue for cue in sorted(known_cues) if cue_matches(cue, text_lower)]


# --------------------------------------------------------------------
# Cue fetch leg
# --------------------------------------------------------------------

# The leg sits outside the latest-20 window on purpose: that is the
# whole associative-recall idea. It used to sit outside PROJECT scope
# too, and that was also on purpose, until 2026-08-30, when GhostPour
# reported one project's chat serving another customer's overdue
# commitment. The scope predicate below is the flat leg's predicate
# character for character (this project, or unstamped, or a universal
# type) rather than a stricter rule of its own, so a patch can never be
# visible on one leg and invisible on another within one request.
#
# An unscoped request keeps the old behavior: there is no current
# project to scope to, and that is the case the leg was built for.
CUE_FETCH_SQL = """
                SELECT DISTINCT cp.patch_id, cp.value, cp.patch_type, cp.source_prompt,
                       cp.created_at, cp.last_observed_at
                FROM patch_cues pc
                JOIN context_patches cp ON cp.patch_id = pc.patch_id
                JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                WHERE ps.subject_key = $1 AND pc.cue = ANY($2)
                  AND COALESCE(cp.status, 'active') = 'active'
                  {SCOPE}
                  {AGE}
                ORDER BY cp.created_at DESC, cp.patch_id ASC
                LIMIT 10
"""


def build_cue_fetch(
    subject_key: str,
    matched_cues: List[str],
    universal_types: List[str],
    max_age_days,
    age_sql: str,
    recall_project_id=None,
    recall_project=None,
    include_assignments: bool = False,
):
    """(sql, args) for the cue leg, scoped when the caller named a project.

    `age_sql` is the recall age predicate already bound to $4 (the day
    count) and $3 (the universal types), exactly as every other leg
    formats it, so the window applies here too. The scope value, when
    there is one, lands on $5.

    Returned as a pair rather than executed here because this module
    holds no I/O: the caller owns the pool, and the test owns a real
    Postgres.
    """
    args = [subject_key, matched_cues, universal_types, max_age_days]
    if recall_project_id:
        scope_col = "project_id"
        args.append(recall_project_id)
    elif recall_project:
        scope_col = "project"
        args.append(recall_project)
    else:
        scope_col = None
    # The flat leg's admission rule, from the one module that defines it,
    # so the two legs cannot drift (2026-08-30 was that drift; 2026-09-04
    # was the rule itself being wrong on both legs at once).
    scope_sql = (
        f"AND {project_scope_clause(scope_col, '$5', '$3')}"
    ) if scope_col else ""
    sql = CUE_FETCH_SQL.replace("{SCOPE}", scope_sql).replace("{AGE}", age_sql)
    if scope_col:
        # The rule reads the `held` / `foreign_origins` CTEs, so the
        # statement carries them; an unscoped request has no project to
        # resolve and keeps the bare statement.
        sql = origins_cte(scope_col, "$1", "$5", include_assignments) + sql
    return sql, args
