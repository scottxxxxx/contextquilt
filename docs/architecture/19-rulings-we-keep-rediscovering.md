# 19. Rulings we keep rediscovering

Every rule here was learned twice or more, in different code, by someone
who did not know it had already been learned. That is the entry bar: a
principle nobody has yet paid for a second time does not belong in this
file, and one that has been paid for twice belongs here rather than in a
comment on the third site.

Each entry states the rule, the evidence that produced it, and how to
apply it. The evidence is not decoration. A rule without its receipts
gets argued with; a rule with two dated failures under it does not.

---

## 19.1 The model may identify. It may not count.

**Rule.** Every number CQ serves is computed in code. A model may find,
classify, quote and phrase. The moment a claim contains a quantity, the
quantity comes from a query and the model is only writing the sentence
around it.

**Paid for three times.**

1. The **follow-through lens** (2026-08-13, PR #240) works because it was
   built this way from the start: closure arithmetic computed from
   `completed_at` against `deadline_date`, the model writing only the
   claim and the do line from those numbers. It landed on the two people
   every prose lens had repeatedly declined.
2. The **person analyzer experiment** (2026-08-13) found Haiku failing
   INVISIBLY on cross-meeting aggregation: it missed the pattern and
   fabricated a receipt, while reading as confident. Conclusion recorded
   at the time: compute the pattern in code, let the model write it.
3. The **stated-priority experiment** (2026-08-15) asked Haiku to count
   conversational turns. Its totals were wrong on 4 of 8 real meetings by
   9, 22, 30 and 37 turns, up to 34% off, with one exact match. Three of
   its nine claims contradicted themselves arithmetically. Sonnet, given
   the same prompt and told to return null rather than estimate, returned
   nulls: the honest failure.

**Applying it.** Split the work at the boundary between judgment and
arithmetic. "Which turns discuss this topic" is classification and the
model is good at it. "How many turns was that" is counting and the model
is not. If a served claim carries a number the model produced, that is a
defect regardless of whether the number happens to be right.

**Cheap guardrail worth having anyway.** Where a claim's own fields imply
a relationship (a "neglected" topic must have FEWER turns than the topic
that beat it), assert it. That check alone would have caught a third of
the bad claims above, in three lines.

---

## 19.2 When a contract has exactly one carrier, its disappearance is silent by construction.

**Rule.** Any contract the model must honour is stated in the prompt AND
in the schema. Never in only one, however authoritative that one feels.

**Paid for twice, in two companies' code, on the same day.**

1. CQ's **entity extraction collapse** (2026-06-10, diagnosed 2026-08-15).
   The manifest declared entity types with per-type guidance and
   `build_prompt` rendered none of it: a 28k-char prompt whose entire
   instruction about the entities array was one line. That survived only
   because the OpenAI-compat client sends `json_schema` for constrained
   decoding. The Anthropic-direct cutover took the schema off the wire
   (`llm_client_anthropic.extract` accepts `json_schema` for interface
   parity and does not send it) and entity yield stepped from 4.37 per
   meeting to 1.24, with zero-entity meetings going 44% to 82%. Two
   months, no error, no alarm. Appearances are written from entities, so
   the visible symptom was a People roster that quietly froze.
2. GhostPour found the identical shape in a lane they had just built,
   within an hour of reading the post-mortem: a column contract living
   only in a tool schema, with the prompt merely saying the columns were
   already decided. They shipped a test that builds a request through
   their real adapter and asserts the contract survives into the body,
   and moved the contract into the prompt as well. Their phrasing, which
   is better than ours: setting a tools field is not the same as sending
   one.

**Applying it.** When adding a field the model must produce, ask what
would happen if the schema stopped arriving. If the answer is "plausible
output, no error", state it in prose too. The audit is mechanical: walk
the output schema's properties and enums against the rendered prompt
text. CQ's audit on 2026-08-15 found three schema-only fields;
`you_speaker_present` is safe because the sanitizer re-derives it and
does not trust the model, and the `connects_to` target field names were
measured with no regression, so entities was the only live instance.

---

## 19.3 An unstated field is an unemitted field.

**Rule.** Models obey the most concrete spec they are shown. A general
section describing a field loses to fourteen per-type shapes that omit
it.

**Paid for twice.**

1. **Cue starvation** (2026-07-30): 0% cue emission on the generated
   prompt against 85% on a prompt whose per-type `value_shape` included
   `cues`, on two different models. Fix: merge the universal optional
   fields into every rendered shape. The comment recording this lives in
   `_patch_types_section`.
2. **Entity collapse** (19.2 above) is the same failure one level up: the
   entity object's field shape existed only in the JSON schema.

**Applying it.** State the shape where the model is already looking, not
only where the documentation would naturally put it.

---

## 19.4 A re-ingest is the same observation arriving twice.

**Rule.** Ingesting one meeting again is not the world producing new
evidence. Nothing that measures recency, freshness or frequency may move
because a transcript arrived a second time.

**Paid for three times, at three different doors, found one at a time.**

1. The **ledger's same-origin restatement guard** (PR #243): one meeting
   can only restate an item once, so a re-ingest cannot read as a second
   hop months later.
2. **Patch freshness** (PR #246): `last_observed_at` and `updated_at` are
   held back when the incoming `origin_id` matches the stored one.
   `updated_at` too, not just the freshness column, because completables
   anchor decay on `GREATEST(updated_at, deadline_date)`.
3. **Presence rows** (PR #247): the appearance upsert took the column
   default on insert and stamped `last_seen_at = NOW()` on conflict, both
   correct exactly once and wrong every time after. Discovered by using
   it: replaying 59 real meetings to repair 19.2 dated 23 meetings' worth
   of presence to the replay, so people last met in July rendered as met
   that afternoon. 361 rows required repair.

**Applying it.** When adding any timestamp written at ingest, ask what it
does on the second ingest of the same origin. Doc 16 §6.2a already stated
this rule for presence and only the relabel routes implemented it, which
is why the ingest path failed: a rule stated in one place and implemented
in another is a rule with one carrier (see 19.2).

---

## 19.5 Prompt real estate is zero sum.

**Rule.** Adding instruction to the extraction prompt takes output from
the types already there. A new type is not free, and a type that competes
badly for attention will underperform its own capability.

**Measured twice, on real transcripts.**

1. The **entity types section** (PR #246) cost about 20% of patches and
   bought about 50% more entities: 14.7 down to 11.7 patches, 5.3 up to 8
   entities, three runs each on one transcript. A trade worth making, and
   still a trade.
2. The **behavior type** (2026-08-15, eight real meetings): inline as one
   of fifteen types, capped at five, eleventh in the priority order, it
   produced 4 observations with Haiku and 0 with Sonnet. The same cheap
   model given a DEDICATED call with the same task produced 48. Twelve
   times the yield, no model change.

**Applying it.** Before adding a type to extraction, ask whether it
deserves its own call. Precedent already exists: the communication
profile has run as a separate lightweight call at ingest since early on,
and the transcript is available exactly once, at ingest, which is also
why speaker turn counts and question attribution are captured there
(doc 16 §6.6). A second call at ingest is a known shape, not a new one.

---

## 19.6 Check what your instrument resolves through.

**Rule.** Before trusting a check, ask what the check reads and whether
the failure you are investigating would corrupt it. A derived view cannot
witness its own correctness.

**Paid for repeatedly, most expensively on 2026-08-13/14.**

- Asked "is this meeting in CQ" by reading patch owners and
  `person_appearances`. Both are written from the entities array. The
  meeting had extracted zero entities, which was the bug under
  investigation. Concluded the meeting was absent. It was not. Same
  session in which that bug had already been diagnosed.
- Checked the follow-through lens's arithmetic with a naive
  `lower(owner) LIKE '%suresh%'` and got a different number. The query
  was wrong, not the lens. Re-running against the exact `source_patch_ids`
  the insight cites matched exactly.
- Split a replay set on whether the `(you)` marker was present, on the
  theory that markerless meetings are unextractable. Those meetings
  average 9,859 chars and 4.39 patches, and entity extraction never
  needed the marker at all. The split would have pre-labelled 100 real
  meetings as expected-nothing and made every successful repair read as
  noise. Caught before the list was handed over, by looking at what the
  meetings actually contained.
- Aimed a truncation hazard at GhostPour's 10,000-char request-log ring
  buffer. Transcripts live in a different table with an uncapped column.
  The hazard was real for a store that was not the one in question.

**Applying it.** When an approximation disagrees with the system, check
against the system's own cited inputs before reporting a discrepancy. And
when a check passes, ask what it would have looked like had it failed:
if the answer is "the same", the check is not one.

---

## 19.7 A pause that depends on the operator knowing why it exists is not a pause.

**Rule.** A safety step enforced by convention will be skipped by someone
doing the obvious thing with the affordance in front of them. If a gate
matters, it has to be a gate.

**Paid for once, expensively enough to record** (2026-08-15). The entity
repair replay was designed to run in batches of ten with a stop between
them so each batch could be verified. The batching was built; the pause
was a convention. The operator was told to tap Run and send the report,
the button sat there, the queue counter counted down, and all 59 meetings
went in one sitting. That is how 19.4's presence bug reached 23 meetings
instead of one. ShoulderSurf's framing, recorded verbatim because it
generalises: a pause that depends on the operator knowing why it exists
is not a pause, it is a hope.

**Applying it.** Either make the confirmation explicit and blocking, or
design for the whole queue draining and make the repair re-runnable. Both
are fine. Assuming restraint is not.

---

## 19.8 A model describes the SHAPE of what it is given.

**Rule.** Hand a model a body of material and ask it an open question, and
the honest answer it finds will often be a description of how the material
is organised rather than of what it says. This is not hallucination and it
is not a weak model. The answer is usually correct. It is just an answer
about the container.

**Paid for twice in one week, by two teams, neither knowing the other had
paid.**

1. **CQ's prose person lenses** (2026-08-16). `how_they_decide` and
   `what_moves_them` read a corpus of `commitment`, `blocker`, `decision`
   and `takeaway`. Every one of those types records what is owed or what
   is stuck, so read alone almost anybody "gates on dependencies", and
   three of the four people on the user's own pages carried exactly that
   sentence. Every claim was accurate. The model had described the schema.
   Sampling was ruled out first: the same person was declined identically
   at ten sources and at his full 39, with the same stated reason.
2. **GhostPour's plan extraction** (same week). A prompt asking for every
   task and milestone, run against source material grouped under meeting
   headings, produced the MEETING CONTAINERS as task rows. Every bar came
   out one day long and the progress line was dragged down by rows that
   were never work. "Dates on 29 of 29" read like a perfect score and was
   actually the symptom, since a meeting always has a date.

**Applying it.** Two defences, and the second is the one that works.

- Ask what the material is a record OF before asking a model what it
  shows. If every row records an obligation, no amount of prompting
  produces a claim about temperament.
- **Contrast is the fix, not phrasing.** A fact computed but never
  compared still reads as generic. CQ's replacement lens
  (`what_stands_out`) measures the whole roster, then reports only the
  measure on which one person is unlike everybody else, with the
  comparison published beside the claim. A person who is unremarkable
  gets no card. See `services/relationship_lenses.py`.

Corollary worth keeping separately: **a claim that is true of the whole
population carries no information even when it is perfectly accurate.**
Accuracy was never what was wrong with those four cards.

---

## 19.9 A failure invisible from your side of a boundary stays invisible.

**Rule.** Additive at the writer, additive at the gateway and additive at
the reader are three different claims. Each side can prove its own half
and none of them can see the failure that lives on the other side. This is
the standing argument for the three-way check, stated as a rule rather
than as a habit.

**Paid for three times, across all three teams.**

1. **The closed `PatchType` enum** (2026-08-16). CQ added a `behavior`
   type; GhostPour proved their gateway carries an unknown `patch_type`
   verbatim and pinned it with a test. Both halves correct. ShoulderSurf's
   decoder held a CLOSED enum, so an unrecognised type threw, the lossy
   per-patch wrapper caught it, and the ENTIRE patch vanished with one log
   line reading "skipped malformed patch". That wording is the whole
   lesson: it reads as corruption, so nobody would go looking for a
   vocabulary they had not learned yet. It is believed to be why an
   earlier card never appeared on any build. Found only because one team
   went looking on another team's side.
2. **The Japanese ordinal string** (2026-08-16). SS's string catalogue
   already held `"%1$d of %2$d"` from a pagination surface, where the
   Japanese translation is an ORDINAL: it reads "the 11th of 21". Reusing
   that key for "11 out of 21 items closed late" would have turned a count
   into a position for every Japanese reader while every English reader
   watched it work perfectly. Caught by reading the existing translation
   rather than trusting the English.
3. **The `uncomplete` route** (2026-08-10, recorded in the ops runbook).
   Direct-smoked on CQ's socket, declared live, and 404ing from every real
   device, because GP's edge declares exact routes and CQ's own socket
   cannot see a route-table miss.

**Applying it.** When a change crosses a team boundary, name which side
each claim was proved on, and get the far side checked by whoever stands
there. GP's phrasing is the compact version: fields are additive at the
reader, routes are additive only at the gateway, same word, different
mechanism. And when a decoder drops something, log a COUNT at notice with
the unrecognised value, never "malformed": the first is a vocabulary you
have not learned, the second is corruption, and only one of them makes
anyone go looking.

---

## See also

- **Doc 16 §5.10 / §5.13**: a served name may assert only what was
  OBSERVED, and where inference is unavoidable, publish the definition on
  the wire rather than in a docstring. That ruling has its own audit
  there and is not duplicated here.
- **Doc 15**: the locked three-team contract.
- **Doc 17**: prompt composition authority.
