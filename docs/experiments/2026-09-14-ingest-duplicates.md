# What is actually duplicated on the ingest stream, and what a cleanup may delete

**Date:** 2026-09-14
**Teams:** CQ, ShoulderSurf

## Where we landed

**Most of what looked like duplication was not.** Of 278 groups where
one meeting carried more than one stream entry, 102 were an `analysis`
and a `meeting_transcript` (two different records of one meeting), 61
were byte-identical repeats, and 149 hold two genuinely DIFFERENT
transcripts a median of 14 days apart whose producer is still unnamed.
The cleanup's original "latest wins" rule would have irreversibly
deleted the longer transcript in every differing group where the shorter
one arrived second, on the only copy that exists.

## The question, as asked

Two, in sequence. First, from ShoulderSurf after #482 shipped: **do the
61 duplicated origins the handler docstring names correspond to replays
that carried `X-CZ-Recovery`?** If any repeat arrived unmarked, that is
a producer of duplicates nobody has named and the marker-gated dedupe
does not stop it. Second, from ShoulderSurf when the cleanup was
proposed: **why is latest-wins the right tie-break rather than merely
the conventional one?**

## What shipped as a result

PR #486, three things, all in `services/ingest_replay.py` and
`scripts/dedupe_memory_updates.py`:

1. **The dedupe key now carries the payload type.** #482 shipped keyed
   on `(user_id, origin_id)` alone, which is a live correctness bug: a
   MARKED replay of a transcript would have been refused whenever an
   `analysis` for that origin had already landed, returning the same 200
   the first delivery got. ShoulderSurf's pending-ingest sweep is the
   main marked producer in the system, so it would have settled its
   debt, dropped its ledger entry, and lost the transcript with every
   party seeing success.
2. **Deletion is restricted to groups whose copies are byte-identical.**
   Any group with a differing copy is reported as `ambiguous` and left
   entirely alone, including an identical pair inside it.
3. **A note in both carriers** that stamping the repair tool
   (`scripts/replay_gated_meetings.py`) with the recovery marker would
   silently disable the repair, because #482 makes the marker a veto.

## The numbers, in full

Prod, read-only dry run of `scripts/dedupe_memory_updates.py`:

| | before the key fix | after |
| --- | --- | --- |
| stream entries | 1,539 | 1,539 |
| distinct records | 719 | **822** |
| "duplicates to delete" | 387 | **71** (byte-identical only) |
| ambiguous, never touched | — | **149 groups / 362 entries** |
| repeats since the marker stamp (09-10) | 1 | **0** |

Composition of the 278 groups the old key called duplicates: 102
`analysis` + `meeting_transcript` (never duplicates), 61 byte-identical,
149 same-type differing, with overlap resolved by the type key.

Repeats by month, before the key fix: Apr 16, May 67, Jun 50, Jul 27,
**Aug 208**, Sep 19. The August burst and clusters of six different
meetings republished within 2 milliseconds are the signature of
`replay_gated_meetings.py --apply`, which republishes verbatim.

**Marked repeats: 0 of 387. None, in any era.** That was the answer to
ShoulderSurf's first question and it is not the comfortable one: the
dedupe that shipped fires only on a marked replay, and no duplicate on
disk has ever been marked.

### The 149 differing groups: four hypotheses, all falsified

| hypothesis | discriminator | result |
| --- | --- | --- |
| SS import path (raw vs GP-cleaned transcript) | `material_kind` | **absent on both copies in all 217 groups measured** |
| same, via cleaning | disfluencies per 1k chars | shorter copy has fewer in **100 of 217** — a coin flip |
| SS project re-scope pass | `project_id` differs | **6 of 149** |
| second live recording under a reused id | metadata richness | falsified by its own prediction, below |

What the data says instead, first copy to last:

| | |
| --- | --- |
| `user_identified` True → False | 135 of 149 |
| `language` en-US → None | 135 of 149 |
| later copy shorter / longer / same-length-but-different | 34 / 1 / 114 |
| later copy **re-stamped `timestamp` to its own arrival** | **148 of 149** |

The later copy is systematically POORER. A second live recording would
carry MORE metadata, not less. The fingerprint is a payload rebuilt
from stored data by something that repopulates no client fields. Neither
ShoulderSurf's five senders nor CQ's verbatim replay script can produce
it. The open candidate is GhostPour calling `cq.capture()` from its own
store; nobody has read that code yet.

### A separate finding that fell out of it

`metadata.speaker_identities` has arrived **zero times, ever**, and CQ
has a live consumer for it (`worker._apply_speaker_identities` rewrites
speaker labels to canonical names before extraction). ShoulderSurf sends
it; GP's capture allowlist reportedly carries "only `material_kind`".
A shipped feature is dead at a middle hop. Not fixed here.

`recording_started_at` also never arrives, and that one is CORRECT: GP
converts it into the `timestamp` CQ reads. The zero is the expected
observation. Raised as an alarm and withdrawn within the hour.

## How to re-run it

```
REDIS_URL=... python scripts/dedupe_memory_updates.py          # dry run
```
On the box: `docker exec contextquilt sh -lc 'cd /app; REDIS_URL=$(PYTHONPATH=/app/src python -c "from contextquilt.config import get_settings; print(get_settings().redis_url)") python scripts/dedupe_memory_updates.py'`

The per-group analyses (payload digests, metadata diffs, timestamp
re-stamping) were ad-hoc reads over `ingest_replay.load_all()` plus
`payload_origin()`; both are in the service and take the client.

## What would falsify it

- **New unmarked repeats appearing with no replay run behind them.**
  That means a live producer rather than a historical one. The count
  since 09-10 is currently zero, and four days is four days.
- A differing group whose copies are a partial and a complete transcript
  (rather than same-length-but-different) would reopen the
  partial-then-final story the timestamps currently rule out.
- GhostPour showing no path that re-captures from its own store would
  remove the only remaining candidate and leave the 149 unexplained.

## What this does not settle

- **The producer of the 149.** Open.
- **Whether to delete the 71 byte-identical entries at all.** They are
  repair artifacts from `replay_gated_meetings.py`. Deleting them is
  irreversible on the only copy of those transcripts, and re-extraction
  of a cleared meeting becomes impossible. Scott's call; not run.
- **The marker vocabulary.** #482 made presence-of-marker a veto, which
  means a DEBUG repair tool on ShoulderSurf's side that correctly stamps
  every replay now no-ops while reporting success. Three states are
  needed (plain retry, recovery replay, deliberate re-ingest) and
  presence-versus-absence expresses two. Proposed shape: intent carried
  by the marker VALUE. Not built.
