# 23. Two commitments that are one undertaking

**Status: SPEC, not a decision and not built. Scott asked for it on
2026-09-09 after seeing two open action items on his phone that are the
same piece of work, and asked for a spec before any code. Every number
below is measured on prod; the open questions at the end are his.**

## What he saw

Two open commitments in Immigration Interview App, both his, both due
"end of weekend":

- `db45b39d` "Deliver Portuguese language support by end of weekend."
  (meeting of 2026-08-28)
- `84b99a40` "Complete Portuguese language support implementation and
  Apple archive submission" (2026-09-01)

The cluster is wider than the pair. Six ACTIVE rows describe the same
piece of work across three meetings: those two commitments, two
`deliverable` rows (`07ca3613`, `8a745458`) and two `decision` rows
(`94389272`, `eb774a69`). The action items list shows the two
commitments as separate outstanding obligations.

## Why the machinery already in the write path cannot see it

`store_connected_patches` dedups in two tiers: trigram similarity above
0.6 is the same fact on the fast path, and 0.35 to 0.6 is the gray zone
where one batched LLM judge call decides (`services/semantic_dedup.py`).

**The two Portuguese commitments score 0.304.** They fall below the
floor, so no judge has ever looked at them, and no amount of tuning the
existing threshold fixes it. ShoulderSurf's second example is worse:

- "Steven will travel to his mother's location in the next 2 weeks to
  conduct interviews"
- "Steven will visit his mother this weekend to test ShoulderSurf in a
  real interview call"

One undertaking, almost no shared words. Lexical similarity is the wrong
instrument for this class, and lowering the floor to reach it would
admit an enormous number of unrelated pairs (see the sizing below).

## Sizing, measured on the largest account 2026-09-09

| | |
|---|---|
| open commitments | 383 |
| distinct owners | 75 |
| projects | 14 |
| candidate pairs (same owner, same project, different meeting) | **3,510** |
| of those, similarity >= 0.6 (already collapse) | 0 |
| 0.35 to 0.6 (the existing judge would see) | 12 |
| below 0.35 (invisible today) | **3,498** |

**A design that judges pairs is dead on arrival.** The pair space is
quadratic and it is already 3,510 on one account. The judgment has to be
per NEW item against the open set, at ingest, which is bounded:

| (owner, project) groups | 95 |
|---|---|
| average open commitments per group | 4.0 |
| p95 | 20 |
| largest (Suresh on ABM) | 51 |

So the worst case a single ingest faces is a handful of new commitments
against 51 existing ones, which fits comfortably in one batched call.

## Where the judgment runs, and why it is not negotiable

**Nothing calls an LLM on the read path.** That is the first
architectural principle in this repo, and it means
`possible_duplicate_of` cannot be computed when the client asks for it.
It is computed at ingest, stored, and served from storage.

Proposed shape, following the semantic dedup and behavior-lane
precedents:

1. After the main extraction is stored, for each NEW commitment, fetch
   OPEN commitments with the same owner and project from other meetings.
2. ONE batched judge call per meeting, shown every new commitment and
   its candidate set, returning pairs it judges to be the same
   undertaking. Same shape as `semantic_dedup`'s batched call.
3. Store the verdict on the LATER patch as `value.possible_duplicate_of`
   (a list of patch ids) with the model's one-line reason.
4. Serve it on `/v1/quilt` (the action items list reads that route) and
   later on the woven digest.

**A separate call rather than an extension of the main extraction.** Doc
19.5: prompt real estate is zero sum, and the behavior lane measured 4
observations inline versus 48 from a dedicated call. The main extraction
already carries open commitments for `resolved_commitments`; adding a
second task to that prompt risks the first.

**It never raises and an empty result is a null, never an empty
assertion.** Same rule as the role semantics call.

## Wire shape

On a commitment, additive and absent-means-none:

```
"possible_duplicate_of": [
  {"patch_id": "db45b39d-...", "reason": "<one line, the model's>"}
]
```

A bare list of ids was considered and rejected: doc 16 §5.13 says a
served name may assert only what was observed, and "these are the same
undertaking" is a JUDGMENT. The reason is the receipt that lets a user
see why before they tap, and lets us audit the judge later.

The block should publish its definition on the wire the way
`ADVANCE_DEFINITION` and `CHASE_DEFINITION` do, so a client never has to
guess what "same undertaking" was taken to mean.

## Recommendations on the three questions SS deferred

**Direction: the LATER item names the EARLIER, one direction only.** The
affordance then renders once rather than on both rows, and the earlier
item stays the origin of record, which matches how the ledger already
treats `origin_id` as the receipt.

**Cross-type: NO for v1.** A `deliverable` is a thing and a `commitment`
is an undertaking; folding one into the other loses a distinction the
model is already asked to make. `commitments.they_owe` filters to
completables explicitly so that a non-completable can never arrive as an
obligation, and this must not become the back door. The Portuguese
cluster's two deliverables and two decisions stay where they are.

**CQ owns the merge, the client POSTs the tap.** A merge is a write that
needs a receipt: a `replaces` connection, `value.archive_cause`, and the
restatement stamps the ledger already understands
(`value.restatements`, `value.restatement_count`). Two writers for one
merge is the shape that relocated identity in August. Proposed:
`POST /v1/quilt/{user}/patches/{patch_id}/merge-into` with the surviving
id in the body, CQ archives the later row, and the earlier row records
the restatement. Nothing merges without a tap.

## Cost, stated plainly

One additional LLM call per meeting that produced at least one
commitment. That is a SIXTH per-meeting call (main extraction,
communication profile, behavior observations, role semantics, alignment
when a project exists). It fires whether or not a duplicate exists,
because absence is the answer in most cases and you cannot know before
asking.

Cheaper alternatives considered and why they lose:

- **Reuse the gray-zone judge with a lower floor.** Would have to reach
  below 0.304, which on this account means judging most of the 3,498
  invisible pairs. Not cheaper.
- **Run it in the 24h consolidation loop instead of at ingest.** Cheaper
  per meeting, but a duplicate is most confusing in the hours right
  after the second meeting, which is exactly when it would not yet be
  flagged.
- **Deadline proximity as a pre-filter.** Fails the Steven pair, whose
  deadlines are "next 2 weeks" and "this weekend".

## How we would know it works, and what would falsify it

The dry run is the measurement, the same as every backfill here. Before
shipping, run the judge over the existing 3,510 candidate pairs offline
and have Scott mark the output. Two numbers decide it:

- **Precision.** A false "same as" that a user taps costs them a real
  commitment. This must be high or the affordance is dangerous.
- **Recall against a known set.** The Portuguese pair and the Steven
  pair are the two we know. If the judge misses either, the prompt is
  wrong.

**What would falsify the whole feature:** if the offline run finds that
most flagged pairs are genuinely distinct work that merely sounds
similar, then the affordance trains users to ignore it and the
sixth call buys nothing.

## Open, and Scott's

1. Ship it, or spec only? (This document is the spec he asked for.)
2. Is a sixth per-meeting call acceptable, given it fires on every
   ingest with commitments regardless of yield?
3. Does the offline precision run happen before any code, or does a
   kill-switched implementation ship and get measured live?
4. Cross-type stays out of v1 above. Confirm, since the Portuguese
   cluster he actually saw is 6 rows and v1 addresses 2 of them.
