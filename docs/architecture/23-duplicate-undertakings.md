# 23. Two commitments that are one undertaking

**Status: the gate HAS NOW RUN and all four open questions are
answered. Still not built. See
`docs/experiments/2026-09-10-duplicate-undertakings.md` for the run and
`docs/experiments/2026-09-10-commitment-normalization.md` for a finding that changes the
merge mechanic. Amended 2026-09-12 with the result and with Scott's
rulings from marking the output.**

**The original spec, unchanged below. Scott asked for it on
2026-09-09 after seeing two open action items on his phone that are the
same piece of work, and asked for a spec before any code. Every number
below is measured on prod; the open questions at the end are his.**

**Amended 2026-09-09, before any of the open questions were answered.**
The first version called cross-type's exclusion "a real limitation"
on the grounds that v1 addresses 2 of the 6 rows in the cluster. That
framing was wrong in a way that could have skewed the answer: 4 of
those 6 are not completable, so they never reach
`commitments.they_owe`, and the surface Scott actually complained
about is covered in full. The recommendation is unchanged; only the
reason it is not a compromise is now stated.

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

## What the gate found, 2026-09-10

51 judge calls over the last 30 days, one per meeting per owner/project
with prior open work, zero failures, RUN TWICE on identical input.

| | |
|---|---|
| flagged, run 1 | 19 |
| flagged, run 2 | 19 |
| stable across both | **18 of 20 distinct pairs (90%)** |
| known positives | both FOUND and both STABLE |
| adversarial control | not flagged |

The control is worth naming: a third Steven row, "Steven's mother will
send over the psychological evaluation forms", shares "Steven" and
"mother" with both halves of the Steven pair and is different work by a
different person. A judge matching on vocabulary flags it. This one did
not.

**SELF-AGREEMENT WAS NOT A REQUIREMENT IN THIS DOCUMENT AND IS NOW THE
ONE THAT MATTERS MOST.** A suggestion that appears on one load and not
the next cannot be attached to a tap that closes a commitment somebody
still owes. It was measured only because the experiments README insists
on rerun noise on an arm the change cannot touch.

**THE FIRST HARNESS MEASURED THE WRONG TASK AND SCORED 42%.** It showed
the model a whole owner/project group at once, up to 51 commitments,
asking which PAIRS matched: 1,275 combinations in one call. Three
identical runs returned 18, 14 and 23 pairs. That number nearly entered
the record as a property of the feature. Rebuilt directional, the way
this document actually specifies, the same judge scores 90%. A harness
that does not match the design measures something nobody proposed.

## What Scott ruled, marking the output

**PRECISION IS BIMODAL BY OWNER.** Eleven of the eighteen stable pairs
are other people's items and he marked all eleven correct. The seven
that are his read wrong. The judge is right where the item belongs to
somebody else and wrong where it belongs to the reader.

**AND OWNER IS NOT THE UNDERLYING TRUTH, IT IS TOLERANCE.** On somebody
else's item he wants the gist, so an over-merge is cheap. On his own he
has to act on it, so an over-merge costs him a real task. Same
judgment, two thresholds, and the threshold depends on whether the
reader is the one who owes the thing.

**THE REAL DISCRIMINATOR IS SAME-NAMED-DELIVERABLE VERSUS
DIFFERENT-SLICES-OF-ONE-EFFORT.** Asked what separates the two cases,
with both rows in front of him: the Portuguese pair is one deliverable,
the Cigna rows are separate work. Portuguese names "Portuguese language
support" twice and the second adds a step to finishing it. The Cigna
rows name P1 against P2 and P3, different use cases, different
documents. Sharing a customer and a spreadsheet is not sharing an
undertaking. That rule keeps Portuguese and rejects all four Cigna
pairs; an owner rule does neither.

**THE DESIGN IS INVERTED FOR HIS OWN ITEMS.** The merge he would
consider is combining two Aug 17 Cigna scenario rows, `3ddab0d8` and
`ce9ceed2`. They are from the SAME MEETING (`EED21245`). This document
judges each new commitment against open items from OTHER meetings, so
same-meeting pairs are structurally excluded and can never be
suggested, while the cross-meeting ones it does offer him are the ones
he rejects. Same-meeting pairs need their own candidate set, and
extraction already has both items in one call.

**UNRESOLVED:** that sits against his separate statement that the Cigna
items are separate work. Recorded as a tension rather than settled.

## The merge mechanic has to change, and a second experiment says how

Scott's condition for merging other people's items is that the merge
COMBINES THE DESCRIPTIONS into something more verbose, rather than
keeping one and discarding the other. The mechanic below (keep the
earlier, close the later) throws the later wording away, so it does not
satisfy the condition it was written for.

Combining descriptions means a model rewrites text that sits next to a
commitment somebody still owes. The normalization experiment of
2026-09-10 measured exactly that class of rewrite and found the safety
property is not obtainable from a prompt: across five runs on identical
input, changing only wording, the refusal rate went 3, 4, 0, 32 out of
49. A rule about VERB CONJUGATION, touching nothing about refusal,
switched refusal off entirely, and that run then invented a deadline
("by ongoing for 4-6 weeks") and turned a constraint into a purpose.

**So a combining merge must be: model proposes, CODE decides.** A
mechanical check discards a synthesis that adds a deadline phrase the
sources lack, drops a scope token present in either source, or changes
who does the work. Same shape as `enforce_person_ownership`,
`enforce_role_holder` and `sanitize_behavior_observations`. And both
original texts stay on the row, because a combining merge is the only
irreversible half of this operation.

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

And that is not a shortfall. `commitments.they_owe` is the action
items list, and it admits completables only, so a deliverable or a
decision never reaches it. What Scott reported was two open ACTION
ITEMS for one job, and v1 addresses that completely. A decision that
Portuguese ships this weekend, and a deliverable called Portuguese
language support, are different objects from the obligation to do it;
the quilt is where they belong.

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

## Answered, 2026-09-10

1. **Ship it, or spec only?** Ship it, gated on question 3. "Ship it"
   is conditional and is NOT a green light on its own.
2. **Is a sixth per-meeting call acceptable?** Yes. Measured: 32 of his
   last 89 meetings produced commitments, so it fires about once a day
   rather than on every ingest.
3. **Offline precision run before code, or kill-switched live?**
   Offline first, before any code. In his words: "If it doesn't hold up
   on the 3,510 pairs, I don't want the feature." The run is above.
4. **Cross-type stays out of v1?** Confirmed. The four non-commitment
   rows never reach `commitments.they_owe`, so v1 covers the surface he
   complained about in full.

## Still open

1. **Both review sheets are UNMARKED per row.** Scott's rulings above
   were given verbally while reading; the per-pair boxes in
   `docs/experiments/2026-09-10-duplicate-undertakings-precision.md`
   are still empty, so there is no scored precision number.
2. **The same-meeting candidate set is unspecified.** It is the one
   merge he would accept and this design cannot propose it.
3. **The mechanical checker for a combining merge is untested.** It is
   the obvious next step and it is not evidence yet.
4. **Direction, merge ownership and the reason string remain CQ
   recommendations awaiting Scott.** They were never among his four
   questions. ShoulderSurf holds them in an awaiting column, not as
   rulings.
