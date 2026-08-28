# 21. Role assertions: what the Memory Layer Spec needs from CQ

Source: Claude Design project `f13c3ec5-bde8-43c1-8691-e5d67773f29a`, file
`Memory Layer Spec.dc.html`, "Role Evolution: memory-layer and
visualization spec", Draft 1, 2026-08-27. Reference implementation
`Role Evolution` (402x874); alternates `Core Sample` and
`Morphing Sentence`. `support.js` in that project is the generated
`dc-runtime` harness and is NOT ported.

Scott's instruction, 2026-08-27: work out what it takes to support this,
and **add** rather than replace. Nothing described here removes an
existing surface. Pruning is a later decision and a separate one.

This document is the gap analysis and the staged plan. It is not a
ruling: three of the spec's requirements collide with rulings already in
force, and those are marked for Scott rather than decided here.

## 1. The one-line summary

The spec's second question, **where a person's self-description diverges
from the room's**, is largely answerable from what CQ already stores.
Its first question, **when the role actually changed**, is not, and the
blocker is not modelling: it is that CQ persists no meeting date at all.

## 2. The record, field by field

The spec's unit is a `RoleAssertion`: one claim, made in one meeting,
about who a person is. Against CQ today:

| Spec field | CQ today | Status |
|---|---|---|
| `person_id` | `entities.entity_id`, with aliasing, merge and `entity_separations` behind it | **have** |
| `meeting_id` | `origin_id` on patches and `person_appearances` | **have** |
| `timestamp` (uttered, not processed) | NOTHING. `payload.timestamp` arrives at ingest, builds the `Meeting date:` prompt line, and is dropped (`worker.py`, `_process_meeting`). Every surviving timestamp is an INGEST clock | **missing, and it blocks the most** |
| `source` SELF_STATED / THIRD_PARTY / OBSERVED | two values, not three: `who_they_are.receipts[].kind` is `stated` or `observed`, `stated_roles` is the self-stated set, and `entity_descriptions` is the meeting-inferred one. THIRD_PARTY and OBSERVED are conflated | **partial, and see 2.1** |
| `speaker_id` (who made the claim) | not recorded for a role claim. `person_appearances` knows who SPOKE in a meeting; nothing joins a role claim to its claimant | **missing** |
| `text`, verbatim and quotable | patch text is model paraphrase, not a quote. Precedent for verbatim exists: alignment's `evidence_match` takes the longest contiguous run against the transcript at ingest | **partial** |
| `facets[]`, normalized, weighted 0..1 | nothing. Roles are free text | **missing, largest new build** |
| `scope` team / project / org | `project_id` scoping exists; claimed BREADTH does not | **partial** |
| `confidence` 0..1 | deliberately absent. Doc 16: an insight carries its own `decay_state`, "never a synthesized confidence float" | **conflicts, see 5.B** |
| `span` char offsets into the transcript | CQ does not retain transcripts. GP holds one for 30 days | **missing, see 5.D** |

### 2.1 The source split, and the good news in it

The spec says the divergence chart is `SELF_STATED` against
`THIRD_PARTY + OBSERVED`. Those two are summed, so the chart itself does
NOT need the third value: CQ's existing `stated` versus `observed` split
is exactly the partition the headline finding is drawn from.

The third value earns its place elsewhere (a claim the room made out loud
is different evidence from behaviour nobody remarked on), so it is worth
having. But it is not on the critical path to the sheet's second
question, and saying otherwise would over-scope the work.

## 3. What CQ already satisfies

Two of the spec's four load-bearing properties are already true, and one
of them was paid for:

- **Nothing is overwritten.** Migration 39 (`entity_descriptions`) turned
  the description into a series after Brian noticed Suresh's description
  flapping between meetings and asked for every historical iteration.
  `described_as` serves `current`, `changed_from`, `history` and
  `iterations`; `stated_roles.items[]` is a list, not a latest value. The
  spec's "the card's current description is a VIEW over the newest
  window, never the stored truth" is the shape CQ already has.
- **Viewer scoping.** "The assertion is excluded entirely if the viewer
  was not in the meeting" is satisfied by construction: a CQ user's
  meetings are the meetings they recorded. Nobody should build an ACL for
  this.

Also present and reusable: `who_they_are` (the synthesis over stated
roles plus the description series, with typed receipts), the `behavior`
patch type and its dedicated extraction call, `person_appearances`
turn counts and the migration 37 question columns, and the trajectory
lens's window arithmetic.

## 4. The observed-behaviour signals

The spec lists six, each required to be individually citable with a date
and a count. Against CQ:

| Signal | CQ today |
|---|---|
| Opened or closed a meeting | missing |
| Assigned a follow-up to another attendee, and whether accepted | partial: commitments carry `value.owner` and the item ledger has a `reassigned` mode, but nothing records WHO assigned it |
| Terminal answerer of a question raised from outside their team | missing. Migration 37 counts questions asked and received, never answered, and CQ has no team model |
| Set or moved an agenda item | missing |
| Deferred a commitment pending upstream input | partial: blockers exist, that specific shape does not |
| Share of speaking turns that were directive versus responsive | missing: `turn_count` is a count with no split |

Every one of these is capture-side, and the transcript is available
exactly once (doc 19.5, doc 16 6.6). `services/behavior_extraction.py` is
the precedent and the argument: the same type competing with fourteen
others in one prompt produced 4 observations where a dedicated call
produced 48.

## 5. Decisions, RULED 2026-08-27

Scott ruled all three the day this was written. The reasoning that was
put to him is kept below, because a ruling without its argument gets
relitigated by whoever inherits it.

- **A. Meeting dates: PERSIST THEM AT INGEST.** Stage 0 below is
  therefore live work, not a proposal.
- **B. Confidence: INTERNAL ONLY, never served.** It weights band widths
  and suppresses low-confidence assertions from the evidence list, and it
  never reaches the wire or the screen. Doc 16's rule stands unchanged.
- **C. Facet vocabulary: PER PERSON.** Four to seven facets derived from
  that person's own history. Cross-person comparison is deliberately
  impossible here; that is what `relationship_lenses` is for.

### The arguments as put

**A. Meeting dates** (ruled: persist). The spec is built on a time axis: calendar-month
buckets, quarters below eight meetings a month, a dated inflection point.
CQ persists no meeting date, on purpose, and the trajectory lens went out
of its way to split by MEETING SEQUENCE instead, because every timestamp
CQ holds is an ingest clock and a weekly chart drawn from those is a
chart of when the importer ran.

Two ways forward:
1. **Persist it.** `payload.timestamp` is already in hand at ingest and is
   currently thrown away. A small append-only table keyed on
   `(user_id, origin_id)` holding the meeting date would unlock month
   bucketing, the turning point, and every future time-axis feature, and
   would change nothing that exists today.
2. **Keep the client pattern.** CQ ships per-meeting buckets carrying
   `origin_id`, and the client joins them to its own meeting store, which
   is how the trajectory sparkline already works.

Option 2 costs nothing but moves the bucketing, and therefore the
turning-point detection, onto the client, which puts an insight's
arithmetic on the device. Option 1 is the recommendation: it is cheap, it
is additive, and it keeps the arithmetic where every other lens computes
it. Note it does NOT weaken the recall byte-stability rule, which is
about output, not storage.

**B. Confidence** (ruled: internal only). The spec puts a 0..1 float on every assertion. Doc 16
forbids serving a synthesized confidence float, for reasons that were
paid for. These are reconcilable: keep confidence INTERNAL, as a
weighting term for band widths, and never serve it. The spec itself only
requires it for weighting and for suppressing low-confidence assertions
from the evidence list, so nothing is lost.

**C. Facet vocabulary** (ruled: per person). The spec's own open
question 4, and it leans per-person ("makes cross-person comparison
impossible, probably correctly"). Per-person matches CQ's grain and
avoids a global taxonomy nobody can maintain; it also means no roster
comparison, which is what `relationship_lenses` is for and which would
otherwise duplicate it.

**D. Verbatim quotes and spans.** "Each claim defensible in one tap, in
the words actually used" needs text captured while the transcript is in
hand. Character offsets into a transcript CQ does not keep are not
servable; the quote itself is. Recommendation: capture the quote, drop
the offsets, and let the client deep-link by `origin_id` as it already
does for receipts.

## 6. Staged plan, additive throughout

Each stage is useful on its own and none removes a surface.

0. **Meeting dates** (CQ, small). RULED and in progress. Unblocks every
   later stage that has a time axis.
1. **The assertion record** (CQ). Append-only, alongside `stated_roles`
   and `entity_descriptions` rather than replacing them: `source`,
   `speaker_id`, verbatim `text`, `origin_id`, `scope`. Backfillable from
   existing rows for `source` and `text`, never for `speaker_id`.
2. **Observed-behaviour capture** (CQ, own extraction call). The six
   signals, counts and rates per meeting, on the `behavior_extraction`
   pattern. Not backfillable: the transcript is gone. This is the doc
   19.3 rule, an unstated field is an unemitted field, so the earlier
   this lands the more history it accumulates.
3. **Facets** (CQ). Per-person vocabulary derived from that person's own
   history, 4 to 7 live facets, ordered least to most directive, stable
   once assigned. A vocabulary that changes shape mid-history makes the
   ribbon lie.
4. **Read-time derivation** (CQ). Band widths, reconstructed description
   (the SAME generator on a truncated stream, never a separately authored
   string), divergence series, turning point with its sustained-change
   rule, guidance with a countable basis. All computed, none persisted,
   so a corrected assertion re-renders the whole history.
5. **The sheet** (SS). Five sections, in the spec's order.

## 7. Rules from this repo that the spec already agrees with

Worth stating, because they are the places a shortcut would be tempting:

- "Guidance items only emitted with a countable basis attached. No basis,
  no card." This is the ledger rule (ship the count, never the cause) and
  the 12b guardrail arriving from outside.
- "A trend drawn from eight meetings is a decoration." Same instinct as
  `MIN_SPAN_MEETINGS` and the roster floor.
- "Show both" on a contradiction in one bucket. Same as the
  trajectory-not-duplicate rule that `collapse_duplicates: false` exists
  for.
- "Interpretive copy is observational, never evaluative." Same as the
  behaviour sanitizer that drops a `behavior` patch stating character
  rather than conduct, and as the who_they_are parse refusing a summary
  that grades.
- "No detectable change: say so plainly." Same as the honest empty hero
  slot.

## 8. Not in scope of this document

The sheet's motion spec, its visual language, and the three open
questions the spec raises about its own surface (turning point as date or
range, guidance fixed to the present or per scrub position, whether the
subject may see their own sheet). Those are SS and design decisions and
are recorded in the spec itself.
