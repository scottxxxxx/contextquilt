# 16: People

> ## Status, 2026-08-02, after SS review round 1
>
> **SS has acked the section 1 premise.** An SS Person is a projection
> keyed on CQ's `entity_id`; SS may cache but is never the source of
> truth for who someone is. That was the whole anti-split-brain bet and
> it is now agreed rather than proposed.
>
> **All six open questions in section 8 are closed.** The two SS asked CQ
> to land on: the merge nudge is **SS proposes, CQ records** (Q2), and
> the ledger collision is answered in the new **8d**.
>
> **Live on prod:** all of section 5 plus `person_appearances`, on
> migrations 29 and 30. Sections 4.1, 4.3, 4.5 and 5 describe live
> behavior. Verified 2026-08-02: 272 people returned, capabilities block
> correct, `/v1/quilt` unaffected.
>
> **8b shipped 2026-08-02** after SS acked all three with conditions:
> ledger `owner` is the raw surface form, `total_unfiltered` counts
> active entities only, `min_meetings=` runs server-side before `limit`,
> and the query echo reports RECEIVED values plus an `ignored` array.
>
> **Still held: the person-patch fold in 6.5.** Ready to build; SS
> confirmed their client half already works.
>
> **Still proposal only: `owed_to` (4.2).** Commitments have no
> counterparty, so `you_owe` is structurally unanswerable and every read
> returns it as `null` with a stated reason in a `capabilities` block,
> never `0`. Needs SS and GP before it lands.
>
> **Hard gate before either side calls this integrated:** the GP
> proxied-path pass (Q4, section 9 item 3), testing `since` specifically.
>
> ### Where to look first
>
> If you only read four things: **1** (the entity_id rule, now acked),
> **6.4** (why `you_owe` is null and not zero), **8b** (the three shipped
> deltas and the conditions attached to each), and **8d** (the ledger
> collision).

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
| Identity | `entity_aliases` (migration 23) | every alternate surface form ("Lockridge", "Lockridge C", "S. Chen") resolved to one canonical entity |

If SS builds a People store that does its own merging and confirming,
those aliases diverge, and the graph fragments exactly the way the ABM
project-id split did, one layer down. Recall matching "Lockridge" then
misses everything filed under "Lockridge Chen".

**ACKED BY SS 2026-08-02, this is the rule:** an SS `Person` is a
projection of a CQ person entity, keyed on CQ's `entity_id`. Every user action that
asserts identity (confirm, merge, keep separate, create) writes back to
CQ. SS may cache freely; SS may not be the source of truth for who
someone is.

### 1a. A wrinkle worth naming

The person patch and the person entity are joined **only by
case-insensitive name**. There is no `patch_entities` link table and no
`entity_id` column on `context_patches`. That is survivable today
because recall traverses the entity graph and fetches patches
separately, but it means "give me everything about Lockridge" is currently
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

CQ can say *Lockridge owes you something*. CQ can say *you owe something*.
CQ cannot say *you owe it to Lockridge*. That is the entire left column of
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
      "name": "Lockridge Chen",
      "aliases": ["Lockridge", "Lockridge C"],
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

### 6.5 SCHEDULED (was deferred): person patches are not folded by a merge

> **Escalated 2026-08-02 by SS review.** This section shipped saying the
> gap "stops being deferrable if the quilt view ever renders people
> directly." SS reports that it already does: `person` is a first-class
> rendered patch type in the Memory segment today, with its own icon and
> display name, and nothing filters it out.
>
> Verified against the code rather than taken on trust: `VALID_PATCH_TYPES`
> includes `person`, `GET /v1/quilt` applies no type exclusion, and merge
> touches `entities` only and never `context_patches`. So the sequence is
> real today: a user merges two Sarahs in People, switches one segment
> over to Memory, and sees two Lockridge patches. **The condition was written
> in future tense and is present tense.** Moved from deferred to
> scheduled. SS confirms it does not block their first cut.
>
> Planned fix, mirroring what merge already does one layer down: pick a
> surviving person patch, repoint its inbound and outbound connections
> (`works_on`, `member_of`, `reports_to`, `describes`, `held_by`) onto the
> survivor the same way relationships repoint, then archive the losers so
> they ride the existing delta-sync `deleted` array. No new machinery, and
> archival rather than deletion for the usual tombstone reason.
>
> **Estimate shrunk by SS, 2026-08-02:** the client half is already done.
> SS's `QuiltService` has decoded `deleted` on every delta sync since
> delta sync shipped, and removes those patch ids from the local store.
> So archiving really is all CQ needs: no SS change, no new decode path,
> and no repeat of the `action_items` lesson, because this array has had
> a consumer from day one. Ready to build on request.

A merge collapses the *entity* layer. It does not touch the duplicate
`person` patches the two surface forms may have produced. That was
deliberate at ship time: patch dedup is the worker's job (trigram plus
the semantic judge) and reaching into patches from an identity endpoint
means ACLs, delta sync and connections all move at once.

It was thought deferrable because the People list is keyed on entities
(5.1), so the duplicate patch never surfaces as a second person in the
People UI. It
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

## 8. Open questions for SS and GP (ALL CLOSED, SS round 1, 2026-08-02)

SS acked section 1's `entity_id` projection rule: an SS Person is a
projection keyed on CQ's `entity_id`, SS may cache but is never the
source of truth for who someone is. That was the whole premise and it is
now agreed rather than proposed.

1. ~~**Empty `you_owe` before v9.**~~ **CLOSED.** SS will render
   `you_owe: null` as "not tracked yet", not as empty. The
   `capabilities` block stands (6.4).
2. ~~**Who originates the merge nudge?**~~ **CLOSED: SS proposes, CQ
   records.** SS owns the false-positive rate explicitly. Their signal is
   voice enrollment, which is acoustic and high precision; CQ's aliaser
   candidate pairs would raise volume and lower precision, turning a
   light "is this the same person?" nudge into the merge manager the
   design deliberately avoids. **CQ will not expose candidate pairs as a
   read.** If it ever does it is additive, lower priority, and SS looks at
   it then. CQ records both answers, which is already built (5.3, 5.4).
3. ~~**Unconfirmed people in the count.**~~ **CLOSED: yes in the data,
   no in the UI.** SS defaults the list to confirmed plus a floor on
   appearances and puts unconfirmed behind `confirmed=`. Consequence SS
   raised: when filtering, `total` comes back filtered, so "40 confirmed
   of 155 known" needs a second call. **Action: add an unfiltered count
   alongside the filtered one** (see 8b).
4. ~~**GP passthrough.**~~ **CLOSED as a HARD GATE**, stronger than
   originally written. These routes carry meaningful query params
   (`since`, `limit`, `confirmed`) and GP has silently eaten params
   before. Neither side calls this integrated until the proxied-path pass
   runs, and **the `since` path is tested specifically**: a dropped
   `since` degrades quietly into a full sync instead of erroring, which
   is the worst failure shape available. **Action: echo the effective
   query back** so the degradation is detectable rather than silent
   (see 8b).
5. ~~**Delta sync shape.**~~ **CLOSED.** SS will decode `deleted` as
   tombstones.
6. ~~**Backfill scope.**~~ **CLOSED: do it at launch, not close.** If
   `person_appearances` only fills forward then on day one every person
   the user has ever met shows a low or zero meeting count, and the
   feature reads as knowing nothing. See 8c for the plan, which corrects
   one assumption in the original question.

### 8b. API deltas (SS acked 2026-08-02, SHIPPED)

Three additions, each carrying a condition SS attached to their ack.
Every condition has a test that names the reason, so a later "cleanup"
fails loudly rather than quietly reverting the contract.

**1. Ledger items gain `owner`, and it is the RAW extracted surface
form.** Not normalized to the canonical entity name, ever.

This is the condition that decides whether the field works at all. SS's
action items carry whatever string the meeting report produced. If a
commitment was extracted with `value.owner` of `"Lockridge C"` and CQ
returned `"Lockridge Chen"` because that is the canonical entity, the field
would look helpful and do nothing, because SS never sees the form that
would let it match. The caller already knows the canonical identity: it
is the person whose endpoint they called.

**Normalizing this field is a regression, not a tidy-up.** It is one of
the few places where the raw string is the payload and the resolved
identity is the redundant part. That sentence is in `_item()`'s
docstring and asserted by a test.

**2. `total_unfiltered`, plus `min_meetings=` server-side.**

`total` stays filtered and is what paginates. `total_unfiltered` is the
full count, so "40 confirmed of 272 known" costs one call.

`min_meetings` moved server-side because the client-side version is
broken by construction: request `limit=50`, filter to whatever clears
the floor, and you get an arbitrary subset with no way to know whether
the next page holds more that pass. A floor applied after pagination is
a truncation with extra steps. It runs before `limit` and `total`
reflects it, the same shape `confirmed=` already had.

**Pinned definition:** `total_unfiltered` counts ACTIVE person entities
and excludes anything folded away by a merge. Otherwise "272 known"
would inflate every time someone tidied their roster, which is the
opposite of what the number is for.

Three counts, three meanings: `len(people)` is what came back after
`limit`, `total` is what matched the filters before `limit`,
`total_unfiltered` is every active person ignoring all filters.

**3. Reads echo the query. RECEIVED values, not applied ones.**

The distinction only matters when a value arrives malformed, which is
exactly when it will be read, so it is named rather than left to
inference. `query` carries `since`, `confirmed`, `min_meetings` and
`limit` exactly as they arrived, plus an `ignored` array listing
anything CQ could not use.

So `confirmed=maybe` echoes `"maybe"` and puts `confirmed` in `ignored`.
The echo shows the wire (was this parameter mangled in transit?) and
`ignored` separately shows CQ's behavior (did CQ act on it?). Both
questions get answered without either field having to serve two masters.

A malformed `since` stays lenient rather than becoming a 422, because
rejecting it would break existing callers. It is no longer silent
though: it degrades to a full sync AND reports itself in `ignored`.

For the GP case this means a stripped `since` produces a response whose
`query.since` is null against a request that set it, which is a
one-line assertion in the three-way test.

Only the list endpoint echoes a query. The detail endpoint takes no
query parameters, so an empty echo there would be noise.

### 8c. Backfill plan, and a correction to Q6's premise

Q6 said appearances are reconstructible "from the ingest stream, which
preserves original payloads verbatim." That is true but **not sufficient
on its own**: `memory_updates` is a Redis Stream with no MAXLEN trim
policy settled (a known deferred item), currently 888 entries against
220 distinct meetings in the patch table. Stream-only backfill cannot be
proven complete.

Two tiers instead:

1. **Postgres-derived (complete, no retention risk).** Every patch
   carries `origin_id`, and 809 of them carry a `value.owner`. Joining
   owner strings and recorded aliases to person entities reconstructs
   appearances for all 220 meetings deterministically, no LLM call. High
   precision, and complete over all history.
2. **Stream-derived (fills the tail).** A person mentioned in a meeting
   who owned nothing produces no owner-bearing patch and is missed by
   tier 1. Scanning retained transcripts with the same entity-plus-alias
   matching recall already uses recovers those, bounded by whatever the
   stream still holds.

Dry-run default with `--apply`, reusing live matching logic, same shape
as the other `scripts/backfill_*.py`.

### 8d. Ledger collision: CQ commitments vs SS action items

Raised by SS and not previously addressed. SS builds its own action-item
ledger from meeting reports with its own owner strings. Who wins when
they disagree?

**The 5.2 projects treatment does not transfer, and it is worth saying
why.** `observed` and `stated` work there because CQ holds *both*
signals: appearances and `works_on` edges are both in CQ, so CQ can
return them side by side and let the client decide. For the ledger CQ
holds only one side. It cannot mark "SS also says this" about data it
has never seen, so mechanically copying the pattern would produce a flag
CQ cannot populate, which is the same failure 6.2 rejected.

**Doc 15 item 5 already governs and is not being reopened:** CQ wins on
state, SS wins on content. Applied here, CQ is authoritative for whether
an item is open, closed, overdue, and for the cross-meeting rollup; SS
is authoritative for the wording and for the per-meeting list.

**Reconciliation belongs on the SS side because only SS holds both
halves.** Every ledger item already returns `origin_id` and `patch_id`,
and with `owner` added (8b) SS can join `origin_id` to its own meeting,
diff CQ's owner attribution against its own, and decide what to render.
CQ's job is to give SS enough to do that, not to guess.

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
  exact name, then recorded alias. Creating "Lockridge C" after it merged
  into "Lockridge Chen" returns Lockridge Chen rather than reviving a duplicate.
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

### Process, and a correction owed to SS

Section 1 says shared surfaces lock with SS and GP before code lands on
either side. Section 5 and migrations 29 and 30 shipped on 07-31 against
that sentence, before SS had reviewed anything, while SS held and wrote
no People code at all, including the token layer that has no CQ
dependency.

Section 10 originally justified this as additive and shape-preserving.
That is a reason it did no harm, not a reason it was in bounds, and SS
correctly pointed out that this doc's own claim that field names and
nesting are "cheap to change now" gets less true the longer something
sits deployed. Nothing is being reverted, but the rule for the next
shared surface is **both hold or both move**, and CQ is the side that
broke it this time.

Applied immediately: the 8b deltas and the 6.5 fold were specified and
held rather than shipped. SS acked 8b on 2026-08-02 with conditions, and
it shipped after that ack, not before it. 6.5 is still held.

SS considers the process point closed and will not raise it again. It
stays written down here because the rule it produced outlives the
incident.

**Still open, tracked here so it is not lost:** SS notes their rendered
meeting count may be lower than CQ's `meeting_count`, because SS will
only show a count it can back with a tappable row and it holds meetings
CQ never ingested (pre-upgrade, imported recordings) and possibly the
reverse. Expected, not a defect on either side. `meeting_count` means
"meetings CQ knows this person appeared in", nothing more.
