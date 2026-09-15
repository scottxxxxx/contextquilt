# What is actually duplicated on the ingest stream, and what a cleanup may delete

**Date:** 2026-09-14
**Teams:** CQ, ShoulderSurf

## Where we landed

**Most of what looked like duplication was not.** Of 278 groups where
one meeting carried more than one stream entry, 102 were an `analysis`
and a `meeting_transcript` (two different records of one meeting), 61
were byte-identical repeats, and 149 hold two genuinely DIFFERENT
transcripts a median of 14 days apart. **Corrected 2026-09-15:** those
149 are not an ongoing producer. 147 of their later copies arrived in
one authorised bulk re-send on 2026-08-17: a GhostPour session replaying
GP's stored transcripts into CQ from inside the prod container, once.
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

What the data says instead, first copy to last. **Corrected 2026-09-15**
(the first version of this table was wrong on two rows, see below):

| | |
| --- | --- |
| `metadata.user_identified` present → **key absent** | 147 of 149 (135 true, 12 false) |
| `metadata.language` present → key absent | 146 of 149 |
| also absent on the later copy only: `email`, `identification_source`, `user_label` | 148 / 147 / 135 |
| later copy shorter / longer / same-length-but-different | 34 / 1 / 114 |
| `interaction_type` | `meeting_transcript` on both copies, all 149 |
| `response`, `call_type`, `prompt_mode`, recovery marker | absent on both copies, all 149 |
| app id | **136 of 149: first `930824d3` (ghostpour), later `886a527b` (ShoulderSurf)** |
| later copy arrived **2026-08-17 13:52:55Z to 13:59:32Z** | **147 of 149** |

**The later copies are one bulk re-send, not a producer.** 147 of 149
arrived in one burst of exactly 163 bare `meeting_transcript` entries
(one at 13:52:55Z, then 162 about 0.77s apart from 13:57:29Z), all
under ShoulderSurf's writing app id,
created 08-07. Their first copies landed June to August under the
ghostpour app id. The other two later copies arrived 2026-04-20 and
2026-06-10.

**Who ran it (settled 2026-09-15): a GhostPour session.** GP's own
memory note (`reference_cq_capture_wire_shape.md`, written 2026-08-17
14:21Z) records replaying GP's `meeting_transcripts` store into CQ by
calling `capture()` inside the running prod container: "178 rows over
163 distinct origin_ids", with the outbound metadata measured as
origin_id, origin_type, project_id, project and subscription_tier. That
explains every property here. GP never stored the client keys, so it
could not replay them, and a call from inside the container never
reaches the edge, which is why GP's access logs showed zero. It also
explains why CQ was ruled out: CQ never sets `subscription_tier`
(`worker.py:6126` only reads it), and CQ's own replay (the 08-15 20Z
batch, 60 entries, one user) copied client metadata verbatim.

GP's session log (session `c80fbcfe`), read by GP: Scott authorised it
in that session at 13:29:52Z. A CQ session (`contextquilt-bd`) asked for
a one-meeting canary first and confirmed it merged into the existing
origin. GP's `canary.py` sent origin `54E27791` at 13:52:46Z, and
`replay.py` (a scratchpad script, never committed, run once) sent the
other 162 from 13:57:25Z, logging "DONE ok=162 fail=0" at 13:59:33Z.

Checked on CQ's stream rather than taken from the log: the canary lands
at 13:52:55.100Z with the 3,802 chars the note records, and it is the
one burst entry carrying `display_name`. Then nothing for 274s, then 162
entries from 13:57:29.528Z to 13:59:32.701Z. That matches the log to the
second.

My first read of this window put the burst's start at 13:47:05Z with
164 entries. Wrong: that entry is an ordinary client capture of origin
`EED21245`, carrying full client metadata, which the time window swept
in. GP's note had recorded the same second as the canary's, by grepping
its container log for the one `cq_capture_ok` line after the fact. The
replay then sent `EED21245` a second time as its last entry. Two
parties, one misread second, one line in a log that was not about the
thing either was looking for.

Still open: ops doc `2026-08-19-CQ-review-project-item-close.md` says
"the 110 on 08-17 were a replay". Nobody has reconciled 110 with 163.

Two rows in the first version were wrong, and both errors came from the
same misreading of an ABSENT key:
- "`user_identified` True → False" was an absent key read as false. The
  ingest route writes `update.dict(exclude_none=True)` (`main.py:1966`)
  and never defaults it.
- "later copy re-stamped `timestamp` to its own arrival, 148 of 149" was
  CQ's own default. The route fills a missing `timestamp` with
  `utcnow().isoformat()` (`main.py:1981`), and BOTH copies carry that
  shape within 5s of their own arrival in 148 of 149. The sender supplied
  none either time, so this was never a fingerprint.

### A separate finding that fell out of it

**RETRACTED 2026-09-15.** The first version said
`metadata.speaker_identities` had arrived "zero times, ever" and called
it a feature dead at GP's hop. Both parts were wrong. The stream holds
**15** entries with a non-empty `metadata.speaker_identities`, the
earliest sampled around 2026-08-23, which matches GP's allowlist date
(GP `context_quilt.py:451`, with a request-side passthrough test).
ShoulderSurf challenged the zero with 13 device meetings holding
non-empty maps. It went to GP as a blocker before anyone read the
stream, which is rule 9's shape exactly. Whether
`worker._apply_speaker_identities` actually rewrote labels on those 15
is NOT verified: its `speaker_identities_applied` log died with the
worker containers.

Also retracted: "`recording_started_at` never arrives". It is on **137**
entries currently on the stream.

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
- ~~GhostPour showing no path that re-captures from its own store would
  remove the only remaining candidate.~~ Done 2026-09-15: GP read all five
  `cq.capture()` callers and none rebuilds from its store. The candidate
  that replaced it is the 08-17 bulk re-send above. A later copy of any
  group arriving outside that window with no replay behind it would
  reopen the "live producer" reading.

## What this does not settle

- **Who authorised GP's 2026-08-17 replay, and whether it was meant to
  be repeatable.** GP is reading the session log. The actor is settled
  (above); a repeatable replay would reopen the ingest question.
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
