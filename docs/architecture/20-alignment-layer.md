# 20. The Alignment Layer

Design: Claude Design project e6ee7ae8, artboards "Alignment in the App"
(the mock) and "Alignment Layer - Requirements" (the rules). Directed by
Scott 2026-08-23. This doc records CQ's half and the decisions in it; the
requirements artboard stays the source for product intent.

## 20.1 What it is

One object, the **alignment event**, authored once by CQ after a meeting
and rendered three ways by the app: confirmed in the meeting view,
aggregated on the project, rehearsed before the next meeting. It exists
because direction changes on a project are individually reasonable and
cumulatively expensive, and nobody can point at the pattern without
appearing to accuse someone. The record states the sequence; four dated
events in a row is the whole argument, and a label would turn a neutral
record into an accusation and stop the stakeholder confirming.

## 20.2 The privacy boundary is in the data model

Two content classes, enforced in storage and in the SELECT, never by
prompt discipline:

| | Private (the reader) | Shared (the project) |
|---|---|---|
| column | `alignment_events.private_instruction` | everything in `ALIGNMENT_SHARED_COLUMNS` |
| text | what the reader should do, count computed in CODE | what the project believes, what changed, who owns it, what it cost |
| forbidden | motive, mood, diagnosis, character | any reference to a person's tendencies; no counts framed as a person's score |
| served by | nothing yet (phase 3, a private route) | `/v1/alignment/...` reads |

`guard_shared_text` (services/alignment.py) rejects a shared string that
contains a character word (the behavior guardrail's denylist) or a
tendency term (`inconsistent`, `reversal`, `again`, `keeps changing`,
`third time`, ...). Rejection means REJECT: the worker regenerates once
naming the term, then drops the event. Nothing is softened. A user's
correction passes the same guard (422 `SHARED_TEXT_REJECTED`). This is
the acceptance criterion "verified by an automated guard test, not by
review": the guard is code and its tests are sabotaged.

## 20.3 The four steps, and where each lives

1. **Detect, supersede not sentiment.** Worker `_extract_alignment_events`
   runs after the main extraction is stored (its own call, doc 19.5).
   Inputs: this meeting's `decision` patches (ids) and the project's
   ACTIVE set: live alignment events + active decision patches from
   other meetings, capped at 40 newest each (deltas, not history). The
   model names supersession BY ID (resolved_commitments pattern, 19.1);
   a hallucinated id is dropped. First decisions on a project are not
   changes: no active set, no call.
2. **Scope, attach the cost.** `derive_impact` in code from the open
   completables and deliverables that reference the superseded decisions
   (an active connection in either direction, or a shared cue). Each
   line is a fact about the item (overdue since / due / open, owner,
   type), magnitude is a fixed ladder that drives the bar only. Never
   dev-days: requirements 11 left that open and CQ stays qualitative
   until Scott rules.
3. **Phrase, facts only.** Statement and rationale from the model, through
   the guard. Low confidence is phrased as a question (`emerging`).
4. **Route, two audiences.** Shared columns are what the routes serve.
   `private_instruction` is built by `private_instruction()` with
   `topic_change_count` computed from stored rows, and is selected by no
   shared read.

**Evidence is mandatory.** `evidence_quote` must be found in the
transcript (case and punctuation folded, six-word floor) or the event is
stored `shippable = FALSE` and never served. A guard hit that the
regeneration did not clear is the same.

## 20.4 Lifecycle

- `proposed` at ingest, `expires_at = proposed_at + 72h`. The deadline
  sweep lapses open, unsuperseded proposals to `expired`.
- `POST .../confirm` (`confirmed_by` required, `on_behalf` marks the
  admin override, attributed): status `confirmed`, `expires_at` cleared
  (a direction has no expiry; it is active until superseded), and every
  event it supersedes plus any older confirmed event on the same topic
  gets `superseded_by`. 409 `NOT_CONFIRMABLE` otherwise.
- `POST .../correct` (`statement`, `reason`, `corrected_by`): a NEW event,
  status `corrected`, `supersedes = [proposal]`, inherits evidence and
  impact, its own 72h; the proposal gets `superseded_by` immediately so
  the owner confirms the corrected wording. History is append-only. A
  second correction while one is open is 409 `CORRECTION_CONFLICT`
  carrying both texts; CQ never merges (requirements 6).
- Project deletion: events are keyed by `project_id`; the unscope path
  should archive them with the project (TODO, phase 2).

## 20.5 Read surfaces

- `GET /v1/alignment/{u}/meetings/{origin_id}`: the card. `events: []`
  means no card and the meeting view is byte-identical to today.
- `GET /v1/alignment/{u}/projects/{project_id}`: `current_directions`
  (newest confirmed, unsuperseded, per topic), `awaiting_confirmation`,
  `history` (the sequence, never annotated), `direction_change_count`
  (confirmed superseding events; a count of the record, never of a
  person; definition on the wire), `cumulative_impact` (union of derived
  lines, deduplicated by the item they derive from).

## 20.6 Phasing (requirements 10)

1. the record: THIS. 2. the rail: served already as the project read; SS
renders. 3. the brief: calendar match, T-15 assembly, private coaching
from the profile. 4. the register: assumptions, rituals, pulse.

## 20.7 Not done, stated plainly

No private route; no project-deletion cascade; impact is qualitative;
the model has not been evaluated on real meetings yet (first real event
is the proof, read it off prod). GP passthrough and the SS card are the
other two halves.
