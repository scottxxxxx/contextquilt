# 16: People (DRAFT, not locked, no code has landed)

ShoulderSurf is adding **People** as a fourth object type in Review,
next to Meetings, Projects and Memory. A person becomes its own entity
with meetings, notes and commitments attached to it.

This doc is the ContextQuilt side of that feature: what CQ already
owns, what it cannot answer today, and the surface it would need to
grow. It exists because People is a **shared surface**, and the
standing rule is that shared surfaces lock with SS and GP before code
lands on either side.

Nothing here is implemented. Every shape below is a proposal.

---

## 1. Why this is a CQ problem at all

The design's person is not a new concept. CQ already owns person
identity in three separate places:

| Layer | Where | What it holds |
|---|---|---|
| Factual | `context_patches` where `patch_type='person'` | the person as a stated fact, with connections (`works_on`, `member_of`, `reports_to`, `held_by`, `describes`) |
| Episodic | `entities` where `entity_type='person'` | canonical name, `mention_count`, `first_seen_at`, `last_seen_at`, and the graph neighborhood in `relationships` |
| Identity | `entity_aliases` (migration 23) | every alternate surface form ("Sarah", "Sarah C", "S. Chen") resolved to one canonical entity |

If SS builds a People store that does its own merging and confirming,
those aliases diverge, and the graph fragments exactly the way the ABM
project-id split did, one layer down. Recall matching "Sarah" then
misses everything filed under "Sarah Chen".

**Proposed rule (needs SS ack):** an SS `Person` is a projection of a
CQ person entity, keyed on CQ's `entity_id`. Every user action that
asserts identity (confirm, merge, keep separate, create) writes back to
CQ. SS may cache freely; SS may not be the source of truth for who
someone is.

### 1a. A wrinkle worth naming

The person patch and the person entity are joined **only by
case-insensitive name**. There is no `patch_entities` link table and no
`entity_id` column on `context_patches`. That is survivable today
because recall traverses the entity graph and fetches patches
separately, but it means "give me everything about Sarah" is currently
two lookups keyed on a string.

The proposal below keys the app-facing API on `entity_id` and returns
the associated `patch_id` when one exists, rather than introducing a
hard foreign key. A hard link is a bigger migration than People needs
and can be revisited later.

---

## 2. Tier

CQ performs **no tier gating**. `subscription_tier` (migration 18) is a
cost-attribution column on `extraction_metrics`; `tier_signals`
(migration 28) is a lifecycle inbox. Nothing in `src/main.py` reads a
tier to decide whether to serve a request.

The Pro gate lives entirely in GhostPour. Opening CQ to all tiers is a
GP configuration change with **zero CQ code change**. What changes is
cost (one Haiku extraction per free-user meeting, plus that user's
share of decay and consolidation load), which is a pricing decision,
not an architecture one.

Consequence worth stating plainly: if People is CQ-backed and CQ stays
Pro-only, a free user's People tab is empty. The feature's dependency
forces the tier decision rather than merely benefiting from it.

---

## 3. What CQ can serve today, unchanged

* **The person list.** `entities` filtered to `entity_type='person'`,
  or `GET /v1/quilt/{user_id}?category=person`.
* **Which projects a person appears in.** `works_on` connection, person
  patch to project patch.
* **What they owe you.** Commitments and blockers where
  `value.owner` names them, or the `owns` connection (person to
  commitment/blocker/decision/goal).
* **Their role and org.** `role` patch (`describes` to person,
  `belongs_to` to project) and `member_of` (person to org).
* **Deadline state on those items.** `value.deadline_date`,
  `value.overdue_since`, and the completion machinery already flow
  through the delta sync.
* **Alternate names.** `entity_aliases`, already populated by the
  worker's conservative heuristic.

---

## 4. Gaps, in order of risk

### 4.1 Identity confirmation has no write path (highest risk)

The design's "Is this the same person?" nudge offers **Same person** and
**Keep separate**. Neither has anywhere to land in CQ.

`rename-speaker` renames an entity. `reassign-speaker` moves patches
between diarization labels. Neither means "these two entities are one
human." `entity_aliases.source` already anticipates the value `'app'`,
but nothing writes it.

Both answers must be recorded. Recording only the positive is a bug:
the conservative aliaser in `store_entities` runs on every extraction,
and without a negative record it can re-merge a pair the user
explicitly separated, silently undoing their answer on a later meeting.

**Needs:** a merge endpoint, a keep-separate endpoint, and a table to
hold the negatives.

### 4.2 "You owe her" is not representable

`commitment.value.owner` is a single named owner string. The `owns`
connection runs person to commitment. There is no counterparty
anywhere in the model.

CQ can say *Sarah owes you something*. CQ can say *you owe something*.
CQ cannot say *you owe it to Sarah*. That is the entire left column of
the ledger in both 1a and 1b.

**Proposed fix:** a new connection label `owed_to`, from `commitment`
to `person`, role `informs`. A connection label rather than a value
field, because it then rides the existing manifest from/to enforcement
and `enforce_connection_vocabulary`, instead of becoming free text the
model gets wrong in a new way.

Schema note: `patch_connections` is `UNIQUE(from_patch_id,
to_patch_id, connection_role)`. No existing label runs commitment to
person, so `owed_to` occupies a fresh pair direction and cannot collide
with `owns` (which runs the other way).

Cost: a manifest bump to v9 plus prompt guidance, and per the ops rule
the v9 manifest must be re-registered on prod before the new prompt is
live.

### 4.3 Per-person meeting history is unanswerable

The design wants "9 meetings, last 3d ago" on the person, and "5
meetings together" per project.

`person` patches are deliberately user-scoped: `origin_id` and
`project_id` are forced NULL on them (`src/worker.py:502`;
`project_scoped_types` at `src/worker.py:408`). That is by design and
should not be changed, see the origin-id design note.

`entities` is close but not sufficient. `mention_count` counts
extractions rather than distinct meetings, and `entities.metadata` is
written as `metadata || $2::jsonb`, so only the most recent
ingestion's `origin_id` survives the merge.

**Proposed fix:** a `person_appearances` table, written from
`store_entities` when the entity is a person and the ingest metadata
carries an `origin_id`. Additive, cold path only, hot path untouched.

### 4.4 Provenance (1e) has no read surface

"Matched to an enrolled speaker profile in 4 of 9 meetings" is SS's,
correctly: voice enrollment is the app's identity layer and should stay
there.

"Named in 11 transcripts. Six of those you confirmed, five are still
assumed from the name alone" is CQ's, and needs 4.3 plus a confirmation
state. Today `identification_source` (migration 17) and
`user_attribution_hint` (migration 19) live on `extraction_metrics`,
which is telemetry, not a per-person read surface.

**Proposed fix:** confirmation lives on the entity, not the appearance,
because "confirmed person" is a person-level fact. Counts come from
`person_appearances`.

### 4.5 No person-scoped read

`GET /v1/quilt/{user_id}` filters on `category`, `since`, `origin_id`,
`group_by`, `project_id` and `limit`. Nothing person-scoped.

At 155 people SS could full-sync and filter on device and it would
work. It is the wrong shape at a few thousand, and it pushes the
"2 you owe her" rollup onto the client, where it will drift from CQ's
lifecycle state.

---

## 5. Proposed surface

All endpoints app-authenticated the same way as `/v1/quilt` (JWT or
X-App-ID). All reads are hot-path-adjacent browse surfaces, like the
meeting views: deterministic ordering, no relevance ranking, no LLM
call.

### 5.1 `GET /v1/people/{user_id}`

Query: `since=` (delta), `limit=`, `confirmed=true|false|all`
(default `all`).

```json
{
  "people": [
    {
      "entity_id": "…",
      "name": "Sarah Chen",
      "aliases": ["Sarah", "Sarah C"],
      "patch_id": "…",
      "description": "Director of Engineering, Northwind",
      "confirmed": true,
      "confirmation_source": "user_confirmation",
      "first_seen_at": "2026-05-02T…Z",
      "last_seen_at": "2026-07-28T…Z",
      "meeting_count": 9,
      "project_count": 3,
      "open_you_owe": 2,
      "open_they_owe": 2
    }
  ],
  "total": 155,
  "deleted": [],
  "server_time": "…"
}
```

`patch_id` is null for a person CQ has as an entity but never emitted a
person patch for. That is the unconfirmed Tom Bakker case and it should
render as such, not be filtered out.

### 5.2 `GET /v1/people/{user_id}/{entity_id}`

Adds to the above:

```json
{
  "projects": [
    {"project_id": "…", "project": "Atlas Migration", "meeting_count": 5}
  ],
  "commitments": {
    "you_owe":   [{"patch_id": "…", "text": "…", "deadline_date": "2026-08-07", "overdue_since": null, "project_id": "…"}],
    "they_owe":  [{"patch_id": "…", "text": "…", "deadline_date": "2026-08-04", "overdue_since": null, "project_id": "…"}]
  },
  "meetings": [
    {"origin_id": "…", "origin_type": "meeting", "last_seen_at": "2026-07-28T…Z"}
  ],
  "provenance": {
    "name_mentions": 11,
    "confirmed_mentions": 6,
    "assumed_mentions": 5,
    "alias_sources": {"heuristic": 2, "app": 1}
  }
}
```

`you_owe` is populated by the `owed_to` connection from 4.2 and is
empty until v9 ships plus a backfill. **That must be honest in the
response, not silently zero.** Options for the meaning of an empty
`you_owe` are an open question in section 8.

CQ deliberately does **not** return meeting titles or durations. Per
doc 15 item 5, CQ wins on state and SS wins on content. SS joins
`origin_id` to its own meeting records for "Atlas cutover checkpoint,
Jul 28, 47m".

### 5.3 `POST /v1/people/{user_id}/merge`

```json
{"canonical_entity_id": "…", "merge_entity_ids": ["…"], "source": "user_confirmation"}
```

Writes an `entity_aliases` row per merged name with `source='app'`,
repoints `relationships` onto the canonical entity, folds
`person_appearances`, sums `mention_count`, and rebuilds the Redis
entity index (same pattern as `rename-speaker`).

Losing entities are **archived, never hard-deleted**, and their
relationships are repointed rather than dropped. This follows the
delta-sync tombstone lesson.

### 5.4 `POST /v1/people/{user_id}/keep-separate`

```json
{"entity_ids": ["…", "…"], "source": "user_confirmation"}
```

Writes a negative record that the aliaser consults before proposing a
merge. Stored with a canonical ordering so the pair is symmetric.

### 5.5 `POST /v1/people/{user_id}`

The "+" button and 1d's "Create person from a name". Creates the entity
and a person patch with `origin_mode='declared'`, marks it confirmed,
and indexes it in Redis so recall can match the name immediately.

```json
{"name": "Marcus Webb", "description": "Vendor evaluation", "confirmed": true}
```

### 5.6 `POST /v1/people/{user_id}/{entity_id}/confirm`

Marks an entity confirmed without merging anything. This is the
"unconfirmed to confirmed" transition for someone CQ inferred from a
transcript and the user vouched for.

---

## 6. Schema additions (proposed)

New migration files, never edits to applied ones.

```sql
-- person_appearances: which meetings a person actually showed up in.
CREATE TABLE person_appearances (
    user_id      TEXT NOT NULL,
    entity_id    UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    origin_id    TEXT NOT NULL,
    origin_type  TEXT NOT NULL,
    project_id   TEXT,
    confirmed    BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, entity_id, origin_id)
);

-- entity_separations: pairs the user said are NOT the same person.
-- Ordering is canonicalised at write time so (a,b) and (b,a) collide.
CREATE TABLE entity_separations (
    user_id      TEXT NOT NULL,
    entity_id_lo UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    entity_id_hi UUID NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    source       TEXT NOT NULL DEFAULT 'app',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, entity_id_lo, entity_id_hi)
);

ALTER TABLE entities ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;
ALTER TABLE entities ADD COLUMN IF NOT EXISTS confirmation_source TEXT;
```

Worker changes: `store_entities` writes the appearance row and consults
`entity_separations` before proposing a heuristic alias. Both are cold
path. The recall hot path is untouched by everything in this doc.

Manifest v9 adds the `owed_to` label and its extraction guidance.

---

## 7. What stays out of CQ

**Contacts.** The design says "Contacts is an optional link, never a
sync," and CQ should hold that line harder than SS does. The Contacts
identifier stays on device. No emails, no phone numbers, no addresses
enter CQ. CQ is not built as a PII store and importing contact details
drags it into the account-deletion purge lane for no memory benefit.
Call, Email and Message in the person header resolve entirely on
device.

**Voice profiles and enrollment.** SS owns identity attribution at the
source. CQ consumes the result (`user_identified`,
`identification_source`, `user_attribution_hint`) and should keep
consuming it, not storing embeddings.

**Notes.** SS's `ProjectNote` is SS-local. The 1c People section shows
notes alongside people, which is fine; those notes do not need to
become CQ patches to make People work.

**Meeting titles, durations, transcripts.** Doc 15 item 5 already
splits this. Unchanged.

---

## 8. Open questions for SS and GP

1. **Empty `you_owe` before v9.** Until `owed_to` ships and backfills,
   `you_owe` is structurally empty. Should the response carry an
   explicit capability flag so SS renders "not tracked yet" rather than
   an empty ledger that reads as "you owe her nothing"? Silent zero is
   the wrong answer for a memory product.
2. **Does the merge nudge originate in CQ or SS?** The design shows SS
   surfacing it. CQ's aliaser has the candidate pairs. Either CQ
   exposes proposals as a read, or SS proposes from voice and CQ only
   records the answer. This changes who owns the false-positive rate.
3. **Unconfirmed people in the count.** Does "155 people" include
   entities CQ inferred from transcripts but nobody confirmed? The
   sidebar shows Tom Bakker as unconfirmed inline, which suggests yes,
   but it changes the number a lot.
4. **GP passthrough.** These are new routes, not new metadata keys. GP
   needs explicit route allowlisting, and per the standing rule these
   get verified through GP's proxied path, not just CQ's socket. GP has
   eaten query params before.
5. **Delta sync shape.** Should `GET /v1/people` support `since=` with
   a `deleted` array like `/v1/quilt` does, or does SS re-fetch the
   list wholesale? Merges make people disappear, so tombstones probably
   matter here.
6. **Backfill scope.** `person_appearances` can be reconstructed for
   history from the ingest stream, which preserves original payloads
   verbatim. Worth doing at launch, or let it fill forward and accept
   that older people show low meeting counts?

---

## 9. Verification plan

People is a browse surface and does not ride the project-chat context
flow, so doc 15 item 8's three-way test is not automatically triggered.
The lighter version still applies:

1. Migrations applied in local docker against a fresh DB, plus a worker
   write path exercise (the appearance row is written by the cold path,
   so a real extraction has to run).
2. Prod smoke after deploy: curl each new route with valid and invalid
   bodies, confirm status codes, confirm the Redis entity index is
   rebuilt after a merge.
3. **Through GP's proxied path, not just CQ's socket.**
4. Manifest v9 re-registered on prod before claiming `owed_to` is live.
5. A merge and a keep-separate exercised end to end on a synthetic
   user, then re-run an extraction naming both surface forms to prove
   the aliaser respects the separation.

---

## 10. Status

Draft, 2026-07-31. Written from the SS design project
`e9e9f9be-a105-4b29-8b48-2f2bd3efb760` (`ShoulderSurf People.dc.html`)
before any code exists on either side. Not reviewed by SS or GP. Not
locked. No CQ code has landed.
