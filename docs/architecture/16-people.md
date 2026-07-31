# 16: People (DRAFT, not locked)

> **Status, 2026-07-31.** All of section 5 has shipped, plus
> `person_appearances`: the read surface (5.1, 5.2), the identity
> write-back (5.3 merge, 5.4 keep separate, 5.5 create, 5.6 confirm), and
> migrations 29 and 30. Sections 4.1, 4.3, 4.5 and 5 are now descriptions
> of live behavior.
>
> **Still proposal only: `owed_to` (4.2).** Commitments have no
> counterparty, so `you_owe` is structurally unanswerable and every read
> returns it as `null` with a stated reason in a `capabilities` block,
> never `0`. Open question 1 is therefore answered in code but the label
> itself still needs SS and GP. The rest of section 8 is still open.

ShoulderSurf is adding **People** as a fourth object type in Review,
next to Meetings, Projects and Memory. A person becomes its own entity
with meetings, notes and commitments attached to it.

This doc is the ContextQuilt side of that feature: what CQ already
owns, what it cannot answer today, and the surface it would need to
grow. It exists because People is a **shared surface**, and the
standing rule is that shared surfaces lock with SS and GP before code
lands on either side.

See the status note above for what is live versus proposed. The endpoint
shapes in section 5 are as-built; the `owed_to` design in 4.2 is not.

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

Both answers must be recorded. Recording only the positive leaves the
negative answer with nowhere to live, and every consumer that proposes
a merge will keep proposing the one the user already refused.

**Correction to an earlier draft of this doc:** that risk was first
written up as the live extraction path silently re-merging a separated
pair. Reading `store_entities` closely, it cannot. Step 3's heuristic
only attaches a *new* surface form to an existing entity, or promotes an
entity to a fuller incoming name; an incoming name that already is an
entity gets caught by the exact-match step first, so two existing rows
can never be fused there. The real consumers of a separation are the
merge endpoint, `scripts/backfill_entity_aliases.py` (which does delete
the duplicate row), and any future merge-proposal read. The harm is a
nag loop and a destructive ops script, not a silent worker merge.

**Shipped.** `POST /v1/people/{user_id}/merge` and
`.../keep-separate` (section 5), backed by `entity_separations` and
`entities.merged_into` in migration 29. The backfill script now reads
separations and **fails closed** if it cannot: merging blind there
deletes a row the user asked to keep.

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

## 5. The surface (as built)

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
      "open_they_owe": 2,
      "open_you_owe": null
    }
  ],
  "total": 155,
  "deleted": ["…"],
  "capabilities": { "you_owe": {"available": false, "reason": "…"}, "…": {} },
  "server_time": "…"
}
```

`patch_id` is null for a person CQ has as an entity but never emitted a
person patch for. That is the unconfirmed Tom Bakker case and it renders
as such rather than being filtered out: hiding it would mean the app can
never offer the confirmation.

`total` counts the filtered population before `limit`, so a client can
tell "showing 50 of 155" without a second call. `deleted` carries the
entity_ids folded away by a merge since `since`, since a merge removes a
person from the list and clients holding that id need a tombstone (open
question 5, answered yes).

`open_you_owe` is always null. See 6.4.

### 5.2 `GET /v1/people/{user_id}/{entity_id}`

Resolves a folded entity_id forward, so a client that cached an id
before a merge still lands on the right person. Adds to the above:

```json
{
  "projects": [
    {"project_id": "…", "project": "Atlas Migration",
     "meeting_count": 5, "observed": true, "stated": false}
  ],
  "commitments": {
    "they_owe":  [{"patch_id": "…", "type": "commitment", "text": "…",
                   "deadline": "Friday", "deadline_date": "2026-08-04",
                   "overdue_since": null, "project_id": "…", "origin_id": "…"}],
    "you_owe": null
  },
  "meetings": [
    {"origin_id": "…", "origin_type": "meeting", "project_id": "…",
     "last_seen_at": "2026-07-28T…Z"}
  ],
  "provenance": {
    "name_mentions": 11,
    "meetings_observed": 9,
    "confirmed": true,
    "confirmation_source": "user_confirmation",
    "alias_sources": {"heuristic": 2, "user_confirmation": 1},
    "confirmed_mentions": null,
    "assumed_mentions": null
  }
}
```

`projects` carries `observed` and `stated` separately, because they are
different claims. `observed` means the person and the project co-occur
in real meetings and `meeting_count` is real. `stated` means a
`works_on` connection says they are on it, which may involve no
co-attended meeting at all (`meeting_count: 0`). Collapsing them into
one number would hand the client a figure it cannot interpret. Ordering
is meeting_count descending then name, so a browse surface does not
reshuffle between polls.

`you_owe` is `null`, not `[]`. An empty list means "none open"; null
means CQ cannot tell. See 6.4.

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

## 6. Schema

### 6.1 Shipped (migration 29)

`entity_separations` (the pair is canonicalised to `lo < hi` at write
time and a CHECK enforces it, so an unordered pair cannot be stored two
ways), plus four columns on `entities`: `confirmed_at`,
`confirmation_source`, `merged_into`, `merged_at`.

**Merge is a forward pointer, not a delete.** Deleting the folded row
would cascade its relationships away (`relationships.from/to_entity_id`
are `ON DELETE CASCADE`) and would break entity ids clients already
hold: SS passes `to_person_id` to `POST /v1/quilt/{u}/reassign-speaker`
today. A merged row stays readable and resolves forward, so a stale
client id self-heals instead of 404ing.

Every write path that resolves an entity by name has to hop that
pointer. `store_entities` now does, via `_resolve_merged_forward`, which
degrades to the input id if the column is absent (the MCP deployment's
Postgres can lag migrations, and entity storage must not start failing
there because People shipped here). Step 3's alias-candidate scan
excludes merged entities.

Recall is deliberately untouched. A merged entity keeps its name in the
Redis entity index on purpose, because the merge records that name as an
alias of the canonical and recall must still match it: the alias leg of
the entity lookup resolves it. The dead row seeds graph traversal with
an empty neighborhood, which costs nothing and changes no output bytes.

### 6.2 person_appearances (shipped, migration 30)

Keyed `(user_id, entity_id, origin_id)`, so a person mentioned five
times in one meeting is one appearance. Written from `store_entities`
on the cold path, for people only, and only when the ingest carried an
`origin_id` (a person named in a chat turn has no meeting to record).
Degrades silently if the table is absent, same reason as
`_resolve_merged_forward`. Resolution happens first, so an appearance
recorded from an alias lands on the canonical.

**A merge folds appearances forward**, and two identities seen in the
same meeting collapse to one row rather than double-counting. This was
caught in verification, not review: the first cut of merge folded
aliases and relationships but not appearances, so merging silently cost
the canonical every meeting the folded identity was seen in.

**Deviation from the earlier draft: no per-appearance `confirmed`
column.** It was specced here to back "six mentions you confirmed, five
still assumed", but nothing in CQ produces a per-meeting confirmation
signal for a third party. Voice matching is the app's, and
`identification_source` / `user_attribution_hint` describe the *user's*
identity, not a participant's. A column no writer populates would read
as "zero confirmed" and become a quiet lie, so the split is reported as
untracked instead (see 6.4).

### 6.3 Still proposed

Manifest v9 adds the `owed_to` label (4.2) and its extraction guidance,
plus a backfill so existing commitments gain a counterparty.

### 6.4 Honesty over convenience: the `capabilities` block

Every People read carries a `capabilities` map naming what CQ can and
cannot answer, each unavailable entry with a reason.

The alternative was worse. Without `owed_to`, returning `open_you_owe:
0` renders in ShoulderSurf as "you owe her nothing", which is a
confident lie from a memory product. Returning `null` plus a reason lets
the client render "not tracked yet". Same treatment for the
confirmed/assumed mention split. An empty *list* means "none open"; a
`null` means "CQ cannot tell". Those are different claims and the API
distinguishes them.

Flip an entry to `available: true` in the same PR that makes it true.

### 6.5 Known gap: person patches are not folded by a merge

A merge collapses the *entity* layer. It does not touch the duplicate
`person` patches the two surface forms may have produced. That is
deliberate for now: patch dedup is the worker's job (trigram plus the
semantic judge) and reaching into patches from an identity endpoint
means ACLs, delta sync and connections all move at once.

It is deferrable because the People list is keyed on entities (5.1), so
the duplicate patch never surfaces as a second person in the UI. It
stops being deferrable if the quilt view ever renders people directly.

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

1. ~~**Empty `you_owe` before v9.**~~ **Answered in code (6.4):** every
   read carries a `capabilities` block, and `you_owe` comes back as
   `null` with a stated reason rather than `0` or `[]`. SS should render
   "not tracked yet". Still needs SS to confirm they will render it that
   way rather than treating null as empty.
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
5. ~~**Delta sync shape.**~~ **Answered yes:** `since=` returns only
   people whose `last_seen_at` or `confirmed_at` moved, plus a `deleted`
   array of entity_ids folded away by a merge. Still needs SS to confirm
   they will decode `deleted` (the 2026-07 `action_items` lesson: an
   array nobody decodes is an array that does not exist).
6. **Backfill scope.** `person_appearances` can be reconstructed for
   history from the ingest stream, which preserves original payloads
   verbatim. Worth doing at launch, or let it fill forward and accept
   that older people show low meeting counts?

---

## 8a. Behavior notes for the shipped endpoints

Things a client integrator will hit that the shapes in section 5 do not
convey:

* **Merge refuses the whole batch on a separation conflict** (409), not
  just the offending pair. Quietly merging part of a batch while
  reporting success is how "why does CQ think my two Sarahs are one
  person" investigations start.
* **Merge is idempotent.** Re-sending a merge whose targets already
  resolve to the canonical returns `status: "noop"`, not an error.
* **Stale entity ids resolve forward** on every endpoint, so a client
  that cached an id before a merge keeps working.
* **Confirm is first-write-wins.** Re-confirming does not rewrite the
  original timestamp or source.
* **Merging and separating both vouch for the entities involved**
  (`confirmed_at` is set if unset). A user answering an identity
  question is confirmation, whichever way they answer.
* **Create is resolve-or-create**, matching `store_entities`' order:
  exact name, then recorded alias. Creating "Sarah C" after it merged
  into "Sarah Chen" returns Sarah Chen rather than reviving a duplicate.
  It answers with the name CQ stores, not the caller's casing.
* **Create fills in a missing person patch** for an entity that only
  existed as an inference. `status` still reads `"exists"` because the
  entity did.
* **Placeholder names are refused** (422 `PLACEHOLDER_NAME`).
  `drop_placeholder_entities` spends real effort keeping "Speaker 3" out
  of the graph and the create endpoint must not be a hole through it.

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

Verification actually run (local docker, fresh DB, all 30 migrations in
order, 2026-07-31).

Write-back: all four endpoints over HTTP across 11 cases, then the
merge's database side effects checked directly. A separation blocks a
merge from **either** argument order; a duplicate relationship is
dropped while a unique one repoints and the resulting self-loop is
deleted; mention counts fold and first/last seen widen; a separation the
folded entity owned transfers to the canonical; an extraction naming the
merged surface form re-observes the canonical without resurrecting the
duplicate.

Reads: list and detail against a fixture with three people, two
projects, an alias-owned commitment and a completed one. Confirmed the
alias-owned commitment counts toward `they_owe` while the completed one
does not; the entity-only person returns `patch_id: null` and
`confirmed: false` rather than being hidden; `observed` and `stated`
projects both appear and are distinguishable; `confirmed=` filtering,
`limit` with an unfiltered `total`, `since` deltas, and merge tombstones
in `deleted` all behave; a stale entity_id forward-resolves on detail;
unknown ids 404 and malformed ids 422.

Appearances: written for people only, never for other entity types or
for a person with no origin; one row per meeting no matter how many
times the name appears; an alias-resolved mention lands on the
canonical; **and a merge folds them forward with same-meeting overlap
collapsing rather than double-counting** (that last one was a real bug
this pass caught, see 6.2).

Still outstanding from section 9: the prod smoke and the pass through
GP's proxied path.

## 10. Status

Written from the SS design project
`e9e9f9be-a105-4b29-8b48-2f2bd3efb760` (`ShoulderSurf People.dc.html`)
before any SS code existed. Not reviewed by SS or GP, not locked.

Section 5 and `person_appearances` shipped 2026-07-31. All of it is
additive: new routes, new tables, one new column set on `entities`. No
existing route, response shape or recall output changed, which is why it
was safe to land ahead of the lock. `owed_to` (4.2) is the piece that
touches the manifest and the extraction prompt, so it waits for SS and
GP.

Before SS builds against this: the shapes are as-built but unreviewed,
so treat them as a proposal SS can still push back on. The cheap changes
to make now are field names and nesting. The expensive one later is
`entity_id` as the key, since that is the whole anti-split-brain
premise.
