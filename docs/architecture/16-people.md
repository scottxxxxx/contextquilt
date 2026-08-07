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
> **6.5 person-patch fold SHIPPED 2026-08-03.** Merging now folds
> duplicate `person` patches too, so the Memory segment stops showing
> two Sarahs after a merge in People.
>
> **`owed_to` (4.2) BUILT, HELD FOR THE ACK.** The label, the sanitizer,
> the read surface and the backfill are written and runtime verified, on
> branch `feat/people-owed-to`. Nothing is merged and manifest v9 is not
> registered, because this is the one piece that changes the extraction
> prompt and the standing rule is that both sides hold or both move.
> Until v9 registers, `you_owe` stays `null` with a stated reason for
> every caller, which is exactly today's behavior.
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

**The fix, built and held:** a new connection label `owed_to`, role
`informs`, running from an action item to a person. A connection label
rather than a value field, because it then rides the existing manifest
from/to enforcement and `enforce_connection_vocabulary`, instead of
becoming free text the model gets wrong in a new way.

Schema note: `patch_connections` is `UNIQUE(from_patch_id,
to_patch_id, connection_role)`. No existing label runs commitment to
person, so `owed_to` occupies a fresh pair direction and cannot collide
with `owns` (which runs the other way).

Cost: a manifest bump to v9 plus prompt guidance, and per the ops rule
the v9 manifest must be re-registered on prod before the new prompt is
live.

#### 4.2.1 Deviation from this section: blockers too, not just commitments

This section proposed `commitment` to `person`. As built the label is
`commitment, blocker` to `person`, because `they_owe` already spans both
completable types. A `you_owe` that silently covered only half of them
would make "nothing outstanding either way" wrong on exactly the blocker
cases, which are the ones where somebody is most visibly waiting. The
live example from the owner-edge work (2026-07-28: Ellery supplying the
IP address a whitelisting commitment waits on) is a blocker-shaped
obligation, not a commitment-shaped one.

#### 4.2.2 What a wrong counterparty costs, and the four guards

`owed_to` is the only label that can assert an obligation the user has.
A wrong one does not read as missing data, it reads as a debt the user
does not owe, to a person they may not owe it to. So every path into it
is stated in the safe direction:

1. **`enforce_owed_to_counterparty`** (extraction sanitizer, runs after
   `enforce_connection_vocabulary` so directions are already normalized)
   drops three shapes: owed to the item's own owner, owed to the (you)
   speaker, owed to a diarization placeholder. The middle one matters
   most: *Lockridge owes you* is a real relationship carried by Lockridge's
   `owns` edge, and the (you) speaker has no person patch by design, so
   an edge pointing at them would dangle and Pass-2 stub synthesis would
   answer the dangle by re-creating the self person patch the self gate
   exists to prevent.
2. **The read side requires BOTH halves.** `you_owe` is items the USER
   owns that carry an `owed_to` edge here. The edge alone says who is
   waiting, not who is late: without the ownership gate, "Lockridge owes
   Marcus the shortlist" would surface on Marcus's card as something the
   user owes him. `is_self_owned` is an inclusion, so an owner string CQ
   cannot resolve stays out of the user's ledger. Absent understates,
   present overstates, and only one of those is affordable.
3. **The capability follows the app's manifest, not CQ's code.** Shipping
   the read logic does not make a counterparty exist for an app whose
   extraction never emits one, and `you_owe: []` for such an app reads as
   "nothing outstanding". `capabilities.you_owe.available` is therefore
   computed from whether the CALLER'S latest registered manifest declares
   the label. Two apps reading the same user can honestly differ.
4. **Per-person null on top of that.** An `owed_to` edge targets a person
   PATCH, so an entity with no patch behind it cannot be the target of a
   single edge and its `0` would be structurally guaranteed rather than
   measured. Those people keep `you_owe: null` even when the capability
   is on. On prod that is not a corner case: 332 person entities against
   175 person patches. `they_owe` degrades gracefully there because it
   also matches the free-text `value.owner` by name; `you_owe` has no
   such leg.

So the client rule is unchanged from 6.4 and now applies at two levels:
**null means not tracked, an empty list means none open.** Check
`capabilities.you_owe.available` for whether CQ can answer at all, and
check the per-person value for whether it can answer for that person.

#### 4.2.3 The backfill, and why it is not optional

`owed_to` fills forward only. Everything already stored was extracted
under a vocabulary with no counterparty, so on the day v9 registers the
capability flips to available and every person card says the user owes
them nothing. `scripts/backfill_owed_to.py` closes that: one batched LLM
call per user over the user's OWN open completables, answering from a
CLOSED list of that user's known people or null. Dry run by default.

Three properties worth keeping: the candidate list is closed so a
backfill can never create a person patch; null is the documented default
answer and every malformed or off-list verdict resolves to it; and every
proposed edge is run through the live `enforce_owed_to_counterparty`
before it is written, so the backfill cannot produce a shape the forward
path would have refused.

#### 4.2.4 What the first production dry run found

Run against prod on 2026-08-06 (dry, nothing written). 53 of Scott's 230
open completables are his own; the judge proposed 10 counterparties.
Reviewing them by hand found two real bugs, neither of which any unit
test would have caught, because both needed real transcript language.

**Bug 1, the self hole.** "Scott to obtain feature request number",
`value.owner` "Scott Guida", proposed `owed_to` "Scott". The sanitizer's
self check required an exact display-name match while the read side's
ownership check used the first token, so the write path allowed the edge
and the read path then counted it as the user owing themselves. There is
a real person patch named "Scott" for this user, so it would have been
written. Fixed by `is_user_reference`, now shared by both sides with a
test asserting they agree on every form.

**Bug 2, the direction inversion, four of ten.** Every one was a blocker
with a null owner: "awaiting Pemberly feedback", "blocked waiting on routing
configuration from Pemberly", "depends on Pemberly for threshold setup", "awaiting
Fenwyck decision". All four mean that person owes the USER, which is the
exact opposite of what `owed_to` records. Fixed with an explicit
anti-inversion rule in both the judge prompt and the manifest label
description, since the extractor will meet the same sentences.

Worth keeping: this does NOT argue for dropping `blocker` from
`from_types` (4.2.1). The four were rejected on their direction, not
their type, and a blocker whose owner is the one somebody is waiting on
is still a real `owed_to`. It does argue that "waiting on a person" is
the dominant blocker shape in this data, and that shape is `they_owe`.

After both fixes the same run proposes 3, of which 2 are unambiguous
("keep Garrick updated", "reply to Cranmore's email") and 1 is a judgment
call ("confirm Ashby updated the ticket": she is the subject of the
check, not the recipient). Prompt tuning stopped there deliberately. A
fourth pass against one user's three remaining items is overfitting, and
adjudicating a meeting the operator attended is what the dry run is for.

#### 4.2.5 The lock covers the DATA, not just the code

Found by applying two adjudicated edges to prod on 2026-08-06 and then
looking at what would carry them (rolled back within minutes; prod is
byte-identical to before, verified against a pre-write snapshot).

Holding the PR is not enough. `/v1/quilt` fetches outgoing connections
with **no label filter** (`src/main.py`, "Fetch all outgoing connections
for these patches"), and the backfill bumps `updated_at` on the item,
which is the from-side. So writing a single `owed_to` row puts a
connection label ShoulderSurf has never been told about into their very
next delta, ahead of the ack, whatever the code is doing.

If SS decodes `label` as a plain string this is harmless. If it decodes
into a closed enum, an unannounced value is the classic strict-decoder
failure, and it is the same class of additive change the standing rule
says to verify through GP's proxied path rather than assume. That is not
a bet to take unilaterally on somebody else's client.

**Rule: a new connection label is live to clients the moment the first
row exists, not the moment the reader ships.** Any vocabulary addition
under a lock has to hold the backfill too, not just the endpoint.

Question this adds for SS (section 9): does the connection decoder
tolerate an unknown `label`, and if so, since which build? The answer
also settles whether every future vocabulary addition needs this dance.

#### 4.2.6 The bump, and the decay it costs

The backfill bumps `updated_at` on the ITEM, which is the from-side of the edge,
because quilt connections are fetched outgoing-only and that is the only
channel the new fact has (the standing rule from the 2026-08-05
propagation fix). That does extend commitment decay, which anchors on
`GREATEST(updated_at, deadline_date)`. Accepted deliberately: these are
the user's own open obligations, they really were modified, and quietly
decaying something the user still owes somebody is the worse failure.

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

### 6.3 Built and held: manifest v9

Manifest v9 adds the `owed_to` label (4.2) and its extraction guidance,
plus `scripts/backfill_owed_to.py` so existing commitments gain a
counterparty. All of it is written, unit tested and runtime verified
against a local stack; none of it is merged or registered.

It is held rather than shipped because this is the one piece of the
People work that touches the extraction prompt, and the standing rule
after the section 5 process miss is that **both sides hold or both
move**. What SS and GP owe back is in section 9.

**Deploy invariant while it is held, and after it merges but before v9
registers:** no registered manifest on prod declares `owed_to`, so
`owed_to_available` is False for every caller, the counterparty query is
never issued, and `you_owe` stays null everywhere. Merging the code
should move no number. If one moves, something reads the label from
somewhere other than the caller's manifest.

### 6.3a SHIPPED: `top_project` on list rows

> **Shipped 2026-08-07**, on SS ask 5. The design puts a project name in
> the list-row subtitle ("Atlas Migration" under a person). The list
> carried `project_count` but no names, so that subtitle cost a detail
> fetch per row.

`top_project` is the highest-signal project for that person, in the SAME
object shape as an entry in the detail route's `projects` array
(`project_id`, `project`, `meeting_count`, `observed`, `stated`), or
`null` when CQ knows of none.

**It is `projects[0]`, not a separately computed maximum, and that is the
design rather than a shortcut.** `merge_project_rollups` already orders by
meeting_count descending then name. Reusing that ordering means the list
row and the detail route can never disagree about which project leads. A
second `max()` would be a second source of truth for one claim, which is
the same disease as `value.owner` versus the `owns` edge.

Two properties worth keeping, both tested:

- **Deterministic under a tie.** The tie break is alphabetical, not
  arbitrary, because a browse surface polled twice must not swap its
  subtitle.
- **Observed outranks stated.** A `works_on` edge is somebody SAYING they
  are on a project, and carries `meeting_count` 0, so a project the person
  was actually in the room for leads. A stated-only project still produces
  a subtitle, because "Atlas Migration" beats nothing when that is
  genuinely all CQ knows, and the `observed` / `stated` flags let the
  client tell the two apart.

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

### 6.5 SHIPPED: a merge folds duplicate person patches

> **Shipped 2026-08-03.** A merge now folds duplicate `person` patches,
> not just entities.
>
> It shipped because SS reported the deferral condition was already met.
> This section said the gap "stops being deferrable if the quilt view ever
> renders people directly", and it does: `person` is a first-class
> rendered patch type in the Memory segment. Verified against the code
> rather than taken on trust, `VALID_PATCH_TYPES` includes `person`,
> `GET /v1/quilt` applies no type exclusion, and merge touched `entities`
> only. So merging two Sarahs in People and switching one segment over
> showed two Sarahs in Memory: the same split brain this document exists
> to prevent, reappearing inside the same screen.
>
> **How the survivor is chosen.** A patch whose text already IS the
> canonical name wins, so the surviving fact needs no rewrite. Otherwise
> the OLDEST patch wins and its text is rewritten to the canonical name.
> Oldest rather than newest on purpose: the newest is the extractor's
> most recent guess, the oldest is what the rest of the quilt has been
> pointing at.
>
> Connections repoint onto the survivor with the same NOT EXISTS guard
> the relationship repoint uses, since `patch_connections` has the same
> UNIQUE(from, to, role) collision. Duplicates collapse, self loops are
> swept.
>
> Losers are **archived, never deleted**, which is what puts them in the
> delta-sync `deleted` array. SS confirmed `QuiltService` has decoded
> that array since delta sync shipped, so this needed no SS change at
> all. Archived rows carry `value.merged_into_patch` and
> `value.merge_source` so the fold is auditable after the fact. The merge
> response also names them in `folded_patch_ids`, so a caller does not
> have to diff a sync to find out what happened.
>
> Verified end to end: `/v1/quilt?category=person` goes from two Sarahs
> to one, an unrelated person is untouched, the folded id appears in
> `deleted`, a unique connection on the loser repoints while a duplicate
> collapses, and the rename branch rewrites the survivor's text when no
> patch matched the canonical name.

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

### 8c. Backfill: three tiers, two applied

`person_appearances` only fills forward, so without a backfill every
person shows a zero meeting count on day one and the feature reads as
knowing nothing.

**`meeting_count` renders to a user as "9 meetings", which is a claim
about ATTENDANCE.** That sentence decides which tiers may feed it.

**ownership (APPLIED).** Postgres-derived, complete over all patch
history with no retention dependency. A person appears if they own
something anchored to that meeting, via a raw `value.owner` string or an
`owns` edge. Deterministic, no LLM call. 436 appearances, 114 people,
157 meetings.

**speakers (APPLIED).** Transcript speaker labels resolved against known
person entities. The strongest attendance signal CQ holds: having spoken
in a meeting is better evidence of being there than having owned an
action item out of it, since work gets assigned in absentia. Took the
table to 971 rows, and for the primary user took `min_meetings=1` from
93 people to **118**.

Only labels resolving to a known person entity are recorded, which drops
caption-scanner noise without a blocklist. SS's screen-capture reader
hands on-screen text to a name-shape extractor, so strings like "Ask
Gemini" and "BUILD SUCCEEDED" arrive shaped exactly like two-word human
names. They were never entities, so they never land. SS owns that defect
and is fixing it at source.

**mentions (BUILT, NOT APPLIED).** Anyone named anywhere in a transcript.
Not attendance. It counts people discussed in absentia and took the
busiest person from 37 meetings to 179 in a dry run. It IS the right
number for the provenance line in design 1e ("named in 11 transcripts"),
which needs the `source` column below before it can land anywhere useful.

**Still proposed: a `source` column** (`ownership` | `speaker` |
`mention`) so `meeting_count` can stay attendance-grounded while
provenance exposes mentions separately. Needs a migration and an SS ack.

Script is `scripts/backfill_person_appearances.py`. `--tier attendance`
(default) runs ownership plus speakers; `mentions` and `all` are
explicit opt-ins because the wrong one is destructive to trust.

#### The lesson underneath it (SS, 2026-08-03)

SS planned to show a local count in the signed-out CTA and CQ's count
after sign-in, under the rule that the signed-out number must be a lower
bound so it can only grow. Good rule. It did not save them, because the
two numbers came from different sources: SS parses transcript speaker
labels, CQ counted ownership-grounded appearance rows. Measured against
CQ's own copy of the transcripts, SS's predicate yields 189 speakers
against CQ's 93, so signing in would have SHRUNK the number, which is
precisely what the rule existed to prevent.

Adding the speaker tier narrows that (93 to 118) but cannot close it:
107 of 189 speaker labels resolve to no CQ person entity, part scanner
noise and part real people who spoke but were never extracted. **CQ
cannot record a person it never learned**, so no backfill makes CQ's
number a superset.

The generalisation, which is worth more than the fix:

> A number shown before an auth or tier boundary has to be computed from
> the same source as the number shown after it, or it should not be a
> number.

No threshold on either side could reconcile a different-source problem.
SS dropped the count and uses GP's `body` string instead of
`body_with_count`.

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
before any SS code existed.

**Review status, 2026-08-02.** SS has reviewed and acked round 1: the
section 1 `entity_id` rule, all six section 8 questions, and the three
8b deltas with their conditions. Those calls are settled and SS is
building against them. **GP has not reviewed this doc at all**, which
matters because section 2 and open question 4 both land on GP: the tier
gate is entirely theirs, and the six `/v1/people/*` routes need explicit
allowlisting before SS can reach any of this through the gateway.

Not settled and explicitly still open: `owed_to` (4.2), the person-patch
fold (6.5), the appearance `source` column (8c), and the GP proxied-path
pass (9).

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
