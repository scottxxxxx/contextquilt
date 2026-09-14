# 25. The recall path, audited as a memory system (2026-09-13)

**Status: three findings, three fixes, one PR.** Scott asked for a
self-audit of ContextQuilt as an agentic memory layer, the top three
things to improve, and the fixes shipped without breaking production.
This document is the audit. Every finding below was read off the code,
not inferred from the design docs, and each names the file and line that
settles it. Two candidate findings were investigated and DECLINED, and
they are recorded at the end, because a finding that was checked and
rejected is worth more to the next reader than one that was never
raised.

The constraint that shaped every fix: recall output must stay
byte-stable within a UTC day (doc 05, upstream prompt caching), the
wire is consumed through a typed gateway that drops unmodelled fields
(GhostPour, doc 15), and no schema moved. All three fixes are additive
on the wire, each carries its own kill switch, and the one that changes
a decay input defaults to the corrected behaviour with a documented way
back.

## Finding 1: the access signal measured candidacy, not service

**What the code did.** `POST /v1/recall` builds `matched_patch_ids` from
`all_patches`, which is every row any fetch leg pulled: the flat
latest-20, the connected leg, the overdue guarantee, the conduct
guarantee and the cue leg (`src/main.py`, the block headed "Collect
matched patch IDs from all_patches"). It then ran
`_bump_patch_access(matched_patch_ids)`, which stamps
`patch_usage_metrics.last_accessed_at`, and the decay loop exempts any
patch whose stamp is inside its TTL (`worker.decay_loop`, the `NOT IN`
subquery).

So a patch that was fetched, scored, and then cut by the token budget or
the fifteen-row cap was stamped as recalled. It never reached the block.
The model never saw it. It was exempt from decay for a full TTL anyway.
`access_count`'s own docstring says "Record a recall hit ... for the
returned patches"; what it counted was candidacy. This is the state-
versus-history shape from doc 19.10 stated on a counter: an instrument
that fires cleanly and answers a different question than its name.

**The second carrier.** The people-scoped lane (`recall_scope=people`)
already returned rendered ids from `format_people_scope` and bumped
only those. Same rule, two lanes, one holding it. And the ordering of
`matched_patch_ids` came from a SECOND scorer inside the route, a
hand-rolled loop with its own `TYPE_PRIORITY` table and a bare
substring entity test, whose comment said "if we ever add true semantic
scoring ... replace this block". `score_patches` arrived and the block
was never replaced, so the list a client reads is ordered by a scorer
that predates the one that rendered the block.

**Who reads it.** ShoulderSurf, from the decoder rather than memory:
`matched_patch_ids` reaches the app as a GhostPour header
(`X-CQ-Patch-IDs`), is consumed only when `X-CQ-Gated` is true, and
its first five ids hydrate the "Almost Had It" upgrade teaser
(`CQTeaserSparkleView.swift:124`). So a prospective subscriber was being
shown five candidates a stale scorer liked, some of which the model
never reasoned over. `patch_count` is never decoded on that path.

**The fix.** `format_flat_ranked_served` returns the ids whose text
reached the block, in scorer order: a list row, or a conduct row that
made a person's capsule. A row folded out by capsule-or-nothing is not
in it. The route serves that as `served_patch_ids`, ADDITIVE beside
`matched_patch_ids`, which is byte-identical because the teaser reads
it through a header GP maps and SS asked that it not move under them.
The access bump runs on served ids. The render-cache blob carries both,
and a blob written before the field existed falls back to the candidate
list rather than bumping nothing. Kill switch
`CQ_RECALL_ACCESS_SERVED_ONLY=0` restores the candidate bump.

**What this changes in production.** Fewer phantom exemptions. A patch
that only ever reached the candidate set will now decay on its TTL, as
it always should have. Nothing served changes. SS will switch the teaser
to `served_patch_ids` when GP forwards it (`X-CQ-Served-Patch-IDs`
follows their pattern); until then the teaser is exactly what it was.

## Finding 2: the scorer's name matching was looser than the matcher feeding it

**What the code did.** Two name tests inside `score_patches`, both
looser than the read side's own rule:

- The +100 entity boost, the largest signal in the scorer, was
  `if name in text_lower` (`recall_scorer.py`, entity-match block). The
  QUERY side was fixed to word boundaries on 2026-09-04 with the receipt
  "RV matched interview" (`entity_match.match_entity_names`). The patch
  side was not. So once "Al", "Sam" or "RV" was legitimately matched in
  the request, every candidate whose text contained "also", "same" or
  "interview" took +100, which outranks a real cue match (+75) and every
  keyword overlap (capped at 60).
- The conduct owner boost, `_owner_matches`, compared FIRST TOKENS
  unconditionally, so a conduct row owned by "Steven Levy" took the boost
  for a query about "Steven Williams". The formatter's capsule fold,
  `_same_person`, had the same body under a docstring that said "when one
  side is a bare first name", which the code did not check (rule 7: the
  comment was true of the intent and false of the code).

**The fix.** One name authority on the read side, in
`services/entity_match.py`: `name_in_text` (the query matcher's own
word-boundary test with the same apostrophe joiner rule) and
`same_person` (equal, or one side is a BARE first name that equals the
other's first token; two different full names sharing a first name are
two people). The scorer's text boost, the scorer's owner boost and the
formatter's fold all call them. Four carriers of one rule became one.

**What this changes in production.** Rankings move for users whose
entity index holds short names, in the direction of the block the
matcher already intended. Deterministic, so byte-stable within a day as
before. No wire change. No kill switch: the old behaviour was a defect
with a receipt on the query side already, and restoring it would need a
reason nobody has.

## Finding 3: two API lanes created patches recall could never keep alive

**What the code did.** All nine patch-inserting sites in the worker pair
`INSERT INTO context_patches` with `INSERT INTO patch_usage_metrics`.
Two sites in `src/main.py` do not: `POST /v1/quilt/{user_id}/patches`
(`create_patch`) and the person create behind `POST /v1/people`
(`_resolve_or_create_person`). A patch from either had no metrics row.
`_bump_patch_access` was an UPDATE, so it matched nothing for them, and
the decay loop's exemption (`last_accessed_at > NOW() - ttl`) could never
see them. An app-created commitment that a user's chat recalled every
day archived on day 30 exactly as if nobody had used it.

Counted, not estimated: nine worker sites with a paired metrics insert,
two API sites without (`grep -n "INSERT INTO context_patches"` against
`grep -n "INSERT INTO patch_usage_metrics"`).

**The fix.** `_bump_patch_access` is an UPSERT: `INSERT ... SELECT FROM
unnest($1) JOIN context_patches ... ON CONFLICT (patch_id) DO UPDATE`.
The JOIN keeps the foreign key honest, so an id that is no longer a
patch (account purge) is skipped rather than failing the whole
statement, which matters because the bump is fire-and-forget and a
failure there was already swallowed silently. Executed against a real
database in `tests/unit/test_recall_access_upsert_db.py`: a patch with
no row gets one at count 1, a patch with a row goes to count+1, a
foreign id is skipped, and the contiguous-parameter guard that caught
#464 covers the statement.

**What this changes in production.** Patches from the two API lanes now
enter the usage feedback loop the first time they are served. Nothing
else moves. No kill switch: the old statement was a no-op for the
affected rows, so there is no behaviour to restore.

## Declined after checking, and why

**Read-time near-duplicate suppression in the flat block.** The 2026-09-04
measurement (doc 23, `services/semantic_dedup.py` header) shows the
duplicates that render twice sit at 0.20 to 0.35 trigram, below the
judged band. A read-time lexical suppressor was drafted and then tested
on paper against the actual doc 23 pair: "Deliver Portuguese language
support by end of weekend" and "Complete Portuguese language support
implementation and Apple archive submission" share three content words.
Any threshold loose enough to catch them would eat distinct rows; any
threshold tight enough to be safe catches only pairs the write-side
trigram already merges. Zero yield on the measured case, so it was not
built. The write side is where that problem lives (doc 23), and a
read-side fix for its shape would have been the adjacent-number trap.

**Same-text, different-owner dedup on the fast path.** The non-self
trigram candidate query has no owner guard, so "Send the deck by Friday"
owned by Raj merges into the same text owned by Priya. Investigated as a
misattribution defect and found to be a DESIGN: `_apply_patch_dedup`
treats a different owner on a ledger-tracked type as a handover, stamps
`value.owner_restated_at`, and the ledger publishes it as the
`reassigned` mode (doc 16 §5.9a). Two people independently committing
to identical text in one meeting is the case that loses, and the design
accepted it. Not relitigated here.

## What was NOT done

- No change to `matched_patch_ids` ordering or contents. The stale
  second scorer still produces it. Retiring it needs SS to move the
  teaser to `served_patch_ids` first, which needs GP to forward the new
  field as a header. Both were told the same thing on 2026-09-13.
- No change to `patch_count`, which is `len(fact_rows) + len(rel_rows)`
  and means nothing a client could rely on. SS decodes it nowhere on the
  recall path; GP may map it to `X-CQ-Matched`, read only as "greater
  than zero". Left alone because a change buys nothing and risks a gate.
- Grouped output mode has no rendered-id channel; `served_patch_ids`
  falls back to the candidate list there and the bump is what it was.
- No embedding or reranker. The scorer is heuristic by design (doc 05)
  and this audit did not find the heuristics wrong, only their inputs.

## How to check any of this

- Finding 1: `tests/unit/test_recall_formatter.py` (the served-id
  cases) and `tests/unit/test_recall_served_access.py` (the route
  wiring, source-read because main.py cannot import without asyncpg).
- Finding 2: `tests/unit/test_recall_name_rule.py`, one file for one
  rule across its four former carriers.
- Finding 3: `tests/unit/test_recall_access_upsert_db.py`, executed in
  CI against Postgres with the `assert_tests_ran` floor.
- Every test was watched failing under a targeted mutation before this
  shipped; the PR carries the runs.
