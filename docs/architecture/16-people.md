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
      "open_you_owe": null,
      "open_by_decay": {"live": 1, "aging": 1, "stale": 0}
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
  "insights": [
    {"patch_id": "…", "lens": "how_they_decide", "text": "…", "do": "…",
     "derived_at": "2026-08-11T…Z", "decay_state": "live", "facts": null,
     "evidence": [{"origin_id": "…", "ingested_on": "2026-06-17",
                   "date": "2026-06-17", "text": "…", "patch_ids": ["…"]}]}
  ],
  "insight_readiness": {
    "lenses": [
      {"lens": "how_they_follow_through", "state": "pending_evidence",
       "more_meetings_help": true,
       "items_observed": 2, "items_required": 4, "items_remaining": 2,
       "meetings_observed": 1, "meetings_required": 3, "meetings_remaining": 2}
    ]
  },
  "projects": [
    {"project_id": "…", "project": "Atlas Migration",
     "meeting_count": 5, "observed": true, "stated": false}
  ],
  "commitments": {
    "they_owe":  [{"patch_id": "…", "type": "commitment", "text": "…",
                   "deadline": "Friday", "deadline_date": "2026-08-04",
                   "overdue_since": null, "project_id": "…", "origin_id": "…",
                   "decay_state": "aging", "shelved_at": null,
                   "shelved_source": null}],
    "you_owe": null,
    "completed_they_owe": {
      "total": 14,
      "items": [{"patch_id": "…", "type": "commitment", "text": "…",
                 "completed_at": "2026-08-09T…Z", "completion_source": "app",
                 "completion_evidence": null, "decay_state": null, "…": "…"}]
    },
    "completed_you_owe": null
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
means CQ cannot tell. See 6.4. `insights` (5.8) carries the same
distinction, with a narrower null: `[]` whenever the pass has produced
nothing yet, INCLUDING for a person too thin to have a person patch,
and `null` only when the fetch failed. `insight_readiness` (5.8.2) says
why each lens is absent and whether waiting will fix it.

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

### 5.6a `POST /v1/people/{user_id}/{entity_id}/rename` (shipped 2026-08-09)

A display-name update, not an identity operation: the entity_id is
untouched and everything keyed on it (appearances, relationships,
separations) is unaffected. SS's ask, sized honestly: person patches
join to entities BY NAME, so one transaction does the whole set or the
patch orphans from its entity:

1. the old name becomes an alias (recall keeps matching it; a future
   transcript saying it resolves here instead of minting a duplicate),
2. the entity name changes,
3. every active person patch matching the old name or any alias is
   rewritten to the new name with an `updated_at` bump (rides the next
   delta; SS holds no person patch_ids and re-decodes),
4. the rename vouches for the person (same reasoning as merge and
   keep-separate: typing someone's actual name asserts who they are),
5. the Redis entity index rebuilds.

Deliberately untouched: `value.owner` strings on ledger items stay the
raw extracted surface form (section 8b); the ledger keeps matching them
through the alias. Refusals: 409 `NAME_TAKEN` when the name is another
person's name or alias (two people sharing a name is a merge question,
and answering it in rename would silently overturn a recorded
keep-separate); 422 for placeholders and for the user's own name
(checked with `is_user_reference`, the same predicate the sanitizers
and ledger share). Renaming to one of the person's OWN aliases promotes
it: alias becomes name, name becomes alias. Case-only renames skip the
alias insert (an alias equal to the name is dead weight and a LOWER()
collision).

### 5.9 The `people` manifest block (shipped 2026-08-10, slice 2 of the facet-runtime pass)

The People surface was born speaking SS's dialect as literals: patch
type `person`, entity type `person`, labels `owns` / `works_on` /
`owed_to`, in ~14 SQL sites. Nothing in a manifest marked those ROLES,
so a second app could never have People no matter what it declared
(TR's vocabulary shares zero of those names). The optional top-level
manifest block names them per app:

```json
"people": {
  "person_type": "contact",            // required; a declared patch type
  "person_entity_type": "contact",     // optional; defaults to person_type
  "ownership_label": "assigned_to",    // optional; default "owns"
  "works_on_label": "engaged_on",      // optional; default "works_on"
  "counterparty_label": "awaits_from"  // optional; NO default — see below
}
```

Rules, each carried by a test (`test_people_vocabulary.py`):

- **Absent block = the SS-default vocabulary.** Every manifest
  registered before the block existed (ShoulderSurf, GhostPour) behaves
  byte-identically with no re-registration. For an app with no `person`
  type the default vocabulary simply matches nothing, which is what
  having no people means.
- **`counterparty_label` never defaults.** An explicit block that omits
  it is the app stating "you owe them" is not tracked, and the
  `you_owe` capability stays off even if a label named `owed_to`
  happens to exist in its vocabulary. Same honesty rule as 6.4.
- **Referential integrity at registration**: every named role must
  resolve to a declared patch type / entity type / connection label, or
  validation fails. A block pointing at a type the extraction can never
  produce would be a capability that lies.
- The whole read surface and every identity write (merge,
  keep-separate, confirm, create, rename) resolves the CALLER'S
  vocabulary per request; `capabilities.you_owe` follows the caller's
  own counterparty label.

**Limit CLOSED by slice 4 (2026-08-10)**: both entity-storage lanes
(extraction and structured ingest) now pass the app's
`person_entity_type` into `store_entities`, so a custom-named person
entity type accumulates appearances. The one remaining literal is the
self-identity write-back (the SS voice-enrollment `display_name` flow),
which is SS-specific by nature and stays on the floor until a second
app has an equivalent flow.

Slice 4 also made corrections and completions ADAPTER-INDEPENDENT in
the ingest-mode gate (they never run extraction; each is its own
candidate-match plus one LLM call), so a structured-mode app's users
can correct and complete. An unmatched correction lands in the APP'S
vocabulary: the prompt offers the app's declared types, the parse
validates against them, and the landing type for a no-better-choice
correction is the manifest's optional `correction_fallback_type`
(validated as a declared type; absent falls back to `takeaway` even
off-manifest, because a slightly alien type beats losing a user-stated
fact, with a warning log naming the gap).

### 5.7 Triage: `decay_state`, shelve, vouch (shipped 2026-08-08)

Agreed in the 2026-08-07 turn-4 exchange with SS (full reasoning in the
private ops repo, `2026-08-07-cq-to-ss-turn4-ledger-decay.md`). SS's
"Let it go" wanted "archives, never deletes: recall still finds it", and
archiving does not behave that way: recall filters to active, so archive
and delete are the same disappearance from the user's seat. The state
that delivers the intent is **shelved**: out of the ledger, still known
to the assistant, reversible.

**`decay_state`** on every ledger item: `live | aging | stale`, open
vocabulary (unknown values must pass through). Two conditions CQ imposed
on itself, both enforced by tests (`test_decay_model.py`):

1. **Derived from the live decay parameters, never hardcoded.** The
   bands come from `services/decay_model.py`, the SAME module the
   worker's decay loop consumes for TTLs, anchors, salience multipliers
   and the access-exemption window. Band boundaries are fractions of the
   effective TTL remaining, so `stale` means "close to the archival CQ
   will actually perform" and moves when the parameters move.
2. **Bucketed to the UTC day**, the same discipline as recall scoring,
   because upstream prompt caching depends on byte-stable payloads.

`decay_state` is neglect, not age: recall access bumps the exemption
window, so an item can move from stale back to live because the user
happened to chat about that person. The shelf reordering itself is the
semantic working, not a bug.

**`open_by_decay`** on every person row: `{"live": n, "aging": n,
"stale": n}`, summed over the SAME rows `they_owe` carries, so the chip
and the card read one source and neither invents a number.

**Write paths** (GP forwards untyped bodies; DELETE for un-shelve):

    POST   .../patches/{patch_id}/vouch       "Still live"
    POST   .../patches/{patch_id}/shelve      "Let it go", reversible
    DELETE .../patches/{patch_id}/shelve      un-shelve
    POST   .../patches/{patch_id}/uncomplete  reverse a wrong completion

* Shelve stamps `value.shelved_at` + `value.shelved_source` and bumps
  `updated_at`. The patch STAYS active: recall still finds it, and it
  reaches clients as a normal patch update, never a tombstone. Archiving
  instead would flow it through the delta `deleted` array, the same
  array a DECAYED item uses, making a deliberate user act
  indistinguishable from the passage of time. The ledger arrays and
  every count derived from them exclude shelved rows; `/v1/quilt`
  action_items keep them, carrying the stamps, so the app can render or
  hide them by its own rule (ledger counts stay the authority).
* Decay still applies to a shelved item: shelving is the user declining
  to act, not asserting immortality. The `updated_at` bump gives it one
  final TTL window from the shelve, then it archives on its own.
* Vouch stamps `value.last_vouched_at` + `value.vouch_source` and bumps
  `updated_at`; the decay extension is the point of the tap. The
  explicit stamp is what separates "the user says this is live" from
  "this happened to surface in a recall". Vouch does NOT clear a shelf.
* Gates mirror `complete`: 404 wrong user, 400 non-completable type,
  409 already completed/archived, 409 shelving a shelved item or
  un-shelving an unshelved one, with the state predicate repeated in
  the UPDATE so races lose with a 409.
* **Uncomplete** (added 2026-08-09, after the first real device tap) is
  the correction verb for all three completion lanes: the app checkbox
  has a client-side regret window, but extraction auto-close and the
  chat-completion lane are LLM judgments with no window at all, and a
  wrong completion previously had no fix short of an operator editing
  prod. It restores `status = 'active'`, clears `completed_at`, and the
  item REAPPEARS through the next delta as a normal patch update (the
  tombstone stops being served because `deleted[]`/`completed[]` are
  computed from the current row). The original completion moves to
  `value.prior_completed_at` / `prior_completion_source` /
  `prior_completion_evidence` beside `uncompleted_at` +
  `uncompletion_source`, so "completed then reopened" never collapses
  into "never completed". 409 when the patch is not completed.
  Related fact worth keeping: dedup only matches ACTIVE patches, so a
  wrongly completed item restated in a later meeting spawns a fresh
  open copy rather than reviving the archived one. Uncomplete is the
  deliberate path; the dedup behavior is the accidental one.

**Completion history** (added 2026-08-09, detail route only):
`commitments.completed_they_owe` and `.completed_you_owe`, the answer to
"what did this person actually deliver". Shape is
`{"total": N, "items": [...]}` with items capped at 20 newest
completions, so the cap self-describes instead of a bare array reading
as "this is everything" (the quilt coverage-line rule). Items are the
open-item shape plus `completed_at` / `completion_source` /
`completion_evidence`; `decay_state` is null on them (decay no longer
applies, and null means not tracked, never a band). Completed-only by
construction: the population gates on `completed_at`, which all three
close lanes set and decay never does, so an expired item can never
appear as a delivery. `completed_you_owe` carries the same null
semantics as the open `you_owe` (null without the `owed_to` capability
or a person patch). List rows deliberately serve none of this: the list
would pay for the full completed population per call while rendering
none of it.

Why CQ must serve this rather than the app keeping it: SS's delta merge
keeps no tombstone memory (ids in `deleted` are removed and nothing
remembers them), so the client structurally cannot reconstruct history;
and recall filters to active, so chat cannot see it either. This leg is
currently the ONLY read surface for completion history. A chat-lane
answer ("what has Vijay completed?") is a separate design: it touches
recall's byte-stable output, so doc 15 + GP prompt composition territory,
proposed but not built.

**`value.archive_cause`** ships with this work: every archive site now
stamps why (`decay`, `replaced`, `corrected`, `merge`,
`project_archived`, `cleanup`, `dedup`, and since 2026-08-26 `lapsed`: a
derived trajectory card whose arithmetic fell under the hold floor with
nothing to succeed it, see 5.15), and completions already carry
`completion_source`. Before this, 864 archived-without-completion rows
on prod could not say whether a client deleted them or time did. Old
rows stay null = unknown; a new archive site that stamps nothing fails
`test_shelve_triage.py`.

### 5.8 Insights: the 16a lens stack (detail route only)

SS design 16a puts a stack of up to three lens cards above the
obligations ledger. CQ's half is the `insights` array on
`GET /v1/people/{user_id}/{entity_id}`. There is no insight on the list
row and there will not be: a stack of claims per row is a different
product than a directory.

```json
"insights": [
  {"patch_id": "…",
   "lens": "how_they_decide",
   "text": "Agrees in the room and reopens scope on the thread afterwards.",
   "do": "End the meeting with him restating the deliverable in his words.",
   "derived_at": "2026-08-11T…Z",
   "decay_state": "live",
   "evidence": [
     {"origin_id": "…",
      "ingested_on": "2026-06-17",
      "date": "2026-06-17",
      "text": "Agreed to the env strategy, reopened it the next morning.",
      "patch_ids": ["…", "…"]}
   ]}
]
```

**Where it comes from.** The worker's consolidation loop runs a
person-keyed pass (`_consolidate_user_people`, doc 14 has the machinery)
over the items connected to a person patch by the ownership edge. One
LLM call per person cluster returns a lens, a one-sentence claim, and one
imperative `do` line, or declines. The claim and the `do` are served
together or not at all: the parse rejects a claim with no actionable
line, because 16a renders both or neither.

**The lens vocabulary is CQ-side, not manifest-declared.**
`PROFILE_LENSES` in `services/consolidation.py` holds it, in two halves
that are produced by two different kinds of pass:

* `MODEL_CHOSEN_LENSES`: `how_they_decide` and `what_moves_them`. A model
  reads the observations and picks between them. A response naming
  anything else is declined, never coerced.
* `COMPUTED_LENSES`: `how_they_follow_through` (5.8.1). Arithmetic
  decides the verdict before any call happens, and the model only writes
  it up. It is never offered to the profile call, so that call cannot
  produce it and cannot decline it.

This is deliberate asymmetry: an app declares WHICH of its types and
labels carry People semantics (5.9), but it does not get to invent
lenses, because the lens is the thing the prompt, the card layout and the
suppression rule all agree on. Adding a lens is a CQ change, and it
reopens every person for one more derivation by design.

Two counts follow the vocabulary and must not be confused. The profile
call's candidate ceiling is `len(MODEL_CHOSEN_LENSES)`, because that is
what the call can produce; counting a computed stamp there would hold a
person in the candidate set forever, burning a cluster slot every cycle
to decline. Everything else (the readiness surface, the durable no, the
stack a person can carry) is the whole vocabulary. Neither is a literal
in the code, which is what let the vocabulary grow from two to three
without touching the gate.

**What a card can physically hold** lives in `services/insight_cards.py`,
one home shared by every lens so no two prompts can drift on it. The
claim is capped at 62 characters and the do line at 90, enforced in the
parse rather than requested in the prompt, because a served claim the UI
cannot render is worse than no claim. 62 is the 16a design's own number:
the collapsed capsule is one line, and iOS measured the visible budget
beside the lens chip at 30 to 37 characters on an iPhone. The first four
live insights ran 97 to 177 characters of claim and 94 to 148 of do,
because nothing in any prompt said otherwise.

A claim also may not OPEN with the person's name. Every shipped claim did
("Sukumar gates forward movement..."), on a page titled with that name,
spending six to eight characters of a thirty five character budget on the
one word the reader already has. Stripping it at render time was the
obvious fix and the wrong one: editing served words on the client is the
pattern this workstream has been retiring, so it is fixed at the
generator and checked in the parse.

The PROMPT asks for less than the parse allows (45 characters of claim,
70 of do). That gap is measured, not stylistic: asked for "at most 62
characters" the live model returned 65 on five identical calls, and
temperature is pinned, so an over-limit answer is not a lottery a retry
could win, it is a person who never gets a card. Anchoring the ask below
the ceiling puts the habitual overshoot inside it. After the change, four
of four live calls landed at 45 characters on the computed lens and 58 to
59 on the prose lens, none opening with the name. A rejected card costs
one call and no write, so the person keeps their candidacy and the next
cycle tries again; the rejection logs at info with its reason
(`profile_card_rejected`), because a run of format failures would
otherwise look exactly like a model that keeps declining.

**The receipts gate.** A claim about a person must be supported across at
least `min_meetings` DISTINCT meetings (default 3), or it is an anecdote
wearing a pattern's clothes. Checked in the cluster SQL and re-checked in
code against the fetched sources, sanitizer style.

**The durable no is per LENS.** Suppressing a card rides the existing
`DELETE /v1/patches/{id}` route, which archives. The pass's idempotency
check therefore ignores `status`: an archived insight is the record of a
lens the user said no to, and it is never re-derived. What changed on
2026-08-13 is the KEY. It used to match any prior insight for the person,
which meant the first card closed the person forever and a second lens
was structurally impossible, so the stack could never be a stack. The
gate now counts DISTINCT `value.lens` stamps for that person (archived
rows included) and admits them while that count is below the vocabulary
size. The lens itself cannot be in the pre-filter, because the model
picks it AFTER the call, so the authoritative refusal is a post-check:
the taken set is re-read once the model answers, and a repeat lens is
declined with no write. The prompt is told which lenses are taken, but
only to save a wasted call; a prompt is a hint and this is an invariant.

One consequence worth stating: a person missing both lenses gains at most
one card per consolidation cycle (one cluster row, one call), so the
second lands on the next 24h pass.

**Fields.**

* `lens` names which card this is. Clients should treat the vocabulary as
  open and skip a lens they do not render, the same posture as
  `decay_state`.
* `decay_state` is the INSIGHT patch's own band: `live | aging | stale`,
  from `services/decay_model.py`, the same module the worker's decay loop
  and the ledger items read, with the same UTC-day bucketing. Null means
  the type carries no TTL anywhere, so decay is not tracked for it, never
  a band CQ cannot stand behind. It is deliberately NOT a confidence
  float over the sources: the source types decay at wildly different
  rates (takeaway 14 days, blocker and commitment 30, and `decision` is
  pinned to never decay at all), so a per-source fraction would report a
  threshold the decay loop never acts on, which is the exact split brain
  `decay_model.py` exists to prevent. If the design wants "how much
  support is still inside the window", that is a new measurement to agree
  on, not a number to synthesize here.
* `evidence` is one row per DISTINCT meeting behind the claim. `text` is
  the source patch's OWN text. That does not cross the doc 15 line: a
  meeting TITLE is app content, a patch's text is CQ state. When several
  sources share a meeting the representative is the oldest by
  `created_at` with `patch_id` breaking ties, a total order, so two
  identical calls render identically. `patch_ids` carries all the live
  sources from that meeting so the client can join to patches it already
  holds.
* **`ingested_on` is an INGEST date, not a meeting date.** It is
  `min(created_at)` on the source patches: the day CQ stored them. CQ
  persists no meeting date and will not invent one. Join `origin_id` to
  the app's own meeting record for the real date and duration, the same
  split as everywhere else in this document. `date` is the original name
  of this same field and survives as an alias, because the served surface
  is additive only (doc 17 section 6); it is the same ingest date and
  carries no other meaning.

  **The same rule binds every dated field on the person surface, and
  the client's join is the PRIMARY path, not a nicety.** Stated in
  ShoulderSurf's own words (their `PERSON_INSIGHT_STACK.md`, adopted
  verbatim on 2026-08-21 so the two documents say one thing): "the date
  is the patch INGEST date, not the meeting date. CQ does not persist
  meeting dates at all. Join `origin_id` to the local MeetingStore and
  render OUR date. The served date is a labelled fallback only, rendered
  as 'ADDED JUN 02' in the muted token so a bulk import cannot dress its
  import day up as when the thing happened." This covers `ingested_on`,
  `described_as.history[].first_observed_at` / `last_observed_at`, and
  `stated_roles.items[].stated_at` alike.
* Archived sources are NOT served as evidence: a decayed or superseded
  patch is not a live receipt. This can drop the list below the
  `min_meetings` that created the insight. The honest list is served
  anyway and the count speaks. It is never padded back up.

**Null versus empty.** `insights` is `null` in ONE case: the fetch
failed and was swallowed, so the detail route never fails on this leg.
Everything else is `[]`, meaning the pass has produced nothing yet.

That includes a person with no person patch, which shipped on
2026-08-13 as null on the reasoning that CQ could "never" derive for a
patchless entity. The word never was wrong. An entity accumulates a
person patch as it is observed, so a patchless person is not a
cannot-tell, it is the thinnest possible NOT YET, and it is exactly the
case the not-yet card exists for: a user two meetings in, wondering why
someone has no card. Clients correctly render nothing for null, so
serving null there gave that user a blank screen. Same house rule as
`you_owe` (6.4), applied to the honest boundary. Whether the app can
EVER have insights is `capabilities.insights`.

**`facts`** carries the arithmetic a COMPUTED lens was written from, and
is null for a lens a model reasoned its way to (it counted nothing, so it
has no counts). Ints only, so nothing here can reach a strict serializer
as NaN or Infinity. It is served so the numbers behind a sentence stay
auditable: anyone can check the claim against the counts, and the counts
against the evidence rows.


### 5.8.1 The follow-through lens: a verdict the arithmetic owns

`how_they_follow_through` is the third lens and the first computed one.
The claim it produces is about delivery: "commits to a date and lands
most of them a week after it" rather than "responds well to charts".

**Why it is built the other way round.** The prose lenses ask a model to
read stored observations and name a behavioral pattern. That works for
well evidenced people (one person on prod carries `how_they_decide` and
`what_moves_them` at once), but it declines on the two best evidenced
people who have no card yet, with the same stated reason both times and
on both the oldest and the newest slice of their record: the stored
observations are task assignments and scheduling notes, so they describe
what a person DOES, and neither prose lens is about that. Arithmetic over
those same records cannot decline. So this lens reaches people the prose
lenses never will, and its verdict is never a model's opinion.

**What is computed, in `services/follow_through.py`.** For each item the
person owns that carried a due date which has come due:

| verdict | how it is decided |
| --- | --- |
| `on_time` | closed, and neither signal says otherwise |
| `late` | closed after the due date, OR carrying `value.overdue_since` |
| `open_past_due` | still open, active, unshelved, due date passed |

Everything else has NO verdict and is not counted: no due date (it cannot
be early or late), a due date still ahead (nobody has been asked the
question), shelved (the user released it, and holding a person to
something the user stopped tracking is not a delivery fact), archived
without a completion (expiry is CQ forgetting, not anyone failing, the
same rule that keeps decayed items out of the completion history).

`late` reads two independent signals and takes either. `overdue_since`
means the deadline sweep actually FOUND the item open past its date,
which survives the item closing later; the date comparison catches items
that closed before any sweep tick saw them. On the live corpus the two
agree exactly, and keeping both means neither the sweep's six hour
cadence nor a cleared stamp can turn a slip into an on time.

**What these numbers are, stated once so nothing overclaims.**
`completed_at` is when CQ LEARNED an item closed (a later meeting, an app
tap, a chat completion), not when the work landed, and `overdue_since` is
when the sweep first noticed. So every count is a fact about the RECORD,
not a stopwatch on a person, and the claim written from it says so. This
is also the first rule in the prompt: describe observed delivery
behavior, never character. "Slips twice before landing" is a claim a
reader can check against the receipts. "Unreliable" is a verdict about a
human being, it is not a fact about anything, and it is rejected in the
parse as well as forbidden in the prompt.

**Moved due dates.** `value.deadline_history` is the only place a
superseded due date survives: the dedup path records the displaced pair
when a re-observation carries a different date (shipped 2026-08-10 with
the 12a capture signals). Nothing else in CQ remembers that a date
changed, which makes the move count the one signal here that cannot be
reconstructed later. It is stated to the model only when it is non-zero,
because "0 moves" invites a sentence about an absence nobody asked about.
The store is empty today and fills forward.

**The gate, and why it is in code.** Arithmetic does not decline, so the
refusal has to be ours and it happens BEFORE a call is spent:
`MIN_JUDGED_ITEMS` (4) judged items across the rule's `min_meetings`
distinct meetings. Both thresholds are about volume. Nothing declines
because the numbers are unflattering or mixed, which is the whole point:
"commits confidently and slips twice before landing" is exactly the card
this lens is for.

**The receipts are the counted items.** `source_patch_ids` holds every
item the arithmetic counted, so the evidence rows a user taps through are
the meetings behind the number, and `value.facts` records the counts
beside the claim. The claim never states a number the arithmetic did not
produce: every integer in the served text has to be one of those counts,
checked in the parse, not merely asked for in the prompt.

**Everything else is shared.** Same write path, same lens stamp, same
per-lens durable no, same receipts read, same decay band. The two lens
families differ in how they reach a claim and in nothing else.

### 5.8.2 `insight_readiness`: why a lens is absent, and whether waiting helps

An absent lens used to be invisible: it simply was not in the array, and
on the wire "never derived" and "the user permanently rejected this" were
the same bytes. That is not a cosmetic gap. A client rendering an honest
empty state ("keep meeting Priya and this fills in") would say it after a
reinstall about the exact claim the user threw away, which is the product
arguing with a decision it promised to respect. It is also not something
a client can fix locally: local state does not survive a reinstall or
cross devices, and CQ is the authority on what it will and will not
derive. Same reason decay bands are not computed on device.

So the detail route serves, per lens in the whole vocabulary:

```json
{"lens": "how_they_follow_through",
 "state": "pending_evidence",
 "more_meetings_help": true,
 "items_observed": 2, "items_required": 4, "items_remaining": 2,
 "meetings_observed": 1, "meetings_required": 3, "meetings_remaining": 2}
```

| state | meaning | waiting helps |
| --- | --- | --- |
| `available` | the card is in `insights` | no, it is already there |
| `pending_evidence` | below the gate, and the numbers say by how much | yes |
| `pending_pattern` | gate met, no claim found yet, re-checked each cycle | yes |
| `suppressed` | the user rejected this card | NO, never invite waiting |
| `retired` | the system archived it | NO, never invite waiting |

`suppressed` and `retired` both mean the pass will not produce this card
again, because the durable no ignores status: any stamp closes the lens
forever. They are separated because they are not the same fact about the
user, and a client may want to word them differently. `suppressed` is an
archived stamp carrying `value.archive_cause = "user_delete"`, which is
what `DELETE /v1/patches/{id}` writes.

The vocabulary is OPEN, like `lens` and `decay_state`. A client meeting a
state it does not know must render NOTHING for that lens, never the
not-yet copy, because the not-yet copy is a promise. `more_meetings_help`
exists so a client never has to reason about the vocabulary to answer the
only question the empty card actually asks.

**The numbers are the gate the pass really runs on**, read from the
caller's manifest rule, not hardcoded: "two more meetings" is honest only
if it counts toward the threshold that gates the derivation. The two lens
families count different things, so their `items_observed` differ on the
same person: the model lenses count what their cluster SQL counts (ACTIVE
items carrying a meeting), while the computed lens counts items whose
date has come due, which is mostly items that already closed, and closing
archives the row.

Every entry carries every sibling key (doc 17 section 6) and every number
is an int, so nothing on this surface can reach GP's `allow_nan=False`
serializer as NaN or Infinity. `insight_readiness` is `null` in two
cases: the app declares no person-clustered rule (so no lens will ever be
derived and `capabilities.insights` already says so), or the readiness
fetch itself failed. It is present and meaningful for exactly the case
`insights: []` now covers, including a person with no person patch, who
reports zero observed against the real thresholds. That is the honest,
specific version of "not yet".

---

### 5.9a The primitive is not a commitment

Read this before the field names in 5.10, because the names are the
smaller half and they only make sense once this is settled.

The thing worth tracking is **an object that keeps coming back without
resolving**. A commitment is one kind of such object. A question nobody
answers, a decision that keeps getting revisited and a concern nobody
owns are others, and mechanically they are IDENTICAL: an object,
restated across meetings, its state unchanged. Nothing about the
machinery cares which it is.

Two independent findings forced this, and neither came from the design.

The single most valuable object in the test corpus is an ownership
question raised in three consecutive meetings and never assigned. It is
not a commitment. Nobody owes it. It has no due date. The USER raised it
himself. A ledger built on commitments would not hold it at all, and it
beat every commitment finding in testing.

Separately, the persona work landed on the same point: three of the four
people most likely to install this app do not have action items in any
meaningful sense. A fundraiser has a topic that keeps surfacing. A new
manager has a concern their report raises every fortnight. Neither is a
deliverable that slipped, and a product that only sees deliverables is
invisible to three quarters of its likely users.

So the contract is written for the general object. **Nothing in the wire
shape assumes a commitment**: the module is `services/item_ledger.py`,
the served blocks are `item_ledger` and `item_ledger_rollup`, every entry
carries its own `object_type`, and the one commitment-specific mode
(`re_dated`) is declared as such in a published vocabulary rather than
left for a client to discover. The contract does not need renegotiating
with the client when questions arrive.

**Day one coverage is still commitments and blockers only.** Extraction
is unchanged and no manifest declares anything new. The point of doing
this before merge is that the wire shape is the expensive thing to change
later, not the coverage.

#### Eligibility: `ledger_tracked`, and why completability was the wrong gate

Which types the ledger holds resolves from the facet runtime
(`TypeRuntime.ledger_tracked_types`), never from a list in CQ code, so a
manifest can widen coverage without a CQ deploy.

It is deliberately NOT the completable set alone. In this schema
`is_completable` means a type can be CLOSED by the completion machinery,
and it drags two other behaviors with it: deadline anchoring in the decay
loop (an item with a due date must never archive before it, see 5.7) and
membership of the People `commitments.they_owe` ledger. A recurring
question has no due date and is not something a person OWES, so declaring
its type completable to get it into the ledger would be wrong in both
directions at once.

So eligibility is a UNION of two declarations that say different things:

| half | says | who arrives through it |
| --- | --- | --- |
| `is_completable` | this can be FINISHED | SS's commitment and blocker, with no manifest change at all |
| `ledger_tracked: true` (migration 38) | this can be UNFINISHED | a question, an unowned concern, a decision that keeps being revisited |

Only the second generalises past commitments, which is why it could not
be folded into the first.

The write path and the read path consume the same set, so capture and
serving widen together: the worker records restatement history for
ledger-tracked types, and `_people_core` fetches the wider population
while every `commitments` array filters back down to completables
**explicitly**. That explicit filter is the load-bearing line. Day one the
two sets are equal, so it is a no-op, which is exactly the condition
under which an omission would go unnoticed until the first question
appeared on somebody's card as an outstanding obligation.

The runtime reads the new column through `to_jsonb` rather than as a bare
column, so a database that has not applied migration 38 returns "not
declared" instead of failing the whole snapshot. Losing project scoping
and completability because a NEW column is missing would be a wildly
disproportionate degradation.

### 5.10 The closure ledger: what happened to an item, not whether it is open

An action item tracker answers one question, open or closed, and that
question is blind to the failure mode that matters most. Some items are
never failed, they are MOLTED: the same object comes back at the next
meeting as a differently shaped fresh commitment, the language stays
strong ("Yeah, absolutely, end of next week, easy"), and the state never
changes. Motion reads as progress at every checkpoint, which is exactly
why it survives every accountability system the user already has. It is
visible only by holding one object across months, which is the one thing
CQ can do and a task list cannot.

Two halves, a write and a read.

**Write (worker, dedup re-observation path).** Re-observation already
detected a restatement: a trigram match over 0.6 bumps `updated_at`,
`last_observed_at` and the usage counters, and a different
`deadline_date` displaces the old one into `value.deadline_history`. What
it never recorded was WHAT was restated. It now appends, FOR
LEDGER-TRACKED TYPES ONLY (5.9a), to `value.restatements` (capped at 10, the deadline_history rule):
`observed_at`, the `text` as spoken in this meeting, the `owner` as
spoken, the `deadline` as spoken, `deadline_date`, and `origin_id`.
`value.restatement_count` is a monotonic counter that survives the cap,
so an item restated fourteen times reports fourteen while keeping ten
receipts. `value.text` is NOT rewritten: this is a record of an object's
life, not an edit to the fact. Neither is `value.owner`, which is what
the ledger matches on, so rewriting it would move an item off the ledger
of the person the user is owed by. A handover is stamped once as
`value.owner_restated_at`, and the restatement owners stay the single
source of truth for classifying it.

The same `origin_id` is the idempotency key: a meeting can restate an
item once. A re-ingest of one transcript, or a second extracted phrasing
of one sentence landing on the same patch, is not a second hop months
later, and an item first stated in this meeting has not come back yet.

**Read (`item_ledger` on the detail route).** A pure classifier,
`services/item_ledger.py`, no database, unit tested the way
`follow_through.py` is. Per item, one headline `mode` plus every other
mode that is also true in `modes`:

| mode | meaning |
| --- | --- |
| `resolved` | finished, with the existing completion stamps: delivery for a commitment, an answer for a question, a decision for an open decision |
| `absorbed_by_user` | the owner changed TO the user |
| `reassigned` | the owner changed to somebody else |
| `not_raised_since` | open, past its date, and not raised across the last 2 meetings the user actually had with that person |
| `re_dated` | open, and the due date has moved at least once |
| `restated` | open, restated with no date movement (the molt) |
| `open` | none of the above yet |

Precedence runs in that order and the tie breaks are deliberate.
Ownership modes outrank the rest because a change of hands is true
forever, while `not_raised_since` and `re_dated` describe the last few
weeks. `not_raised_since` outranks `re_dated` in the other direction: a
re-date is the item being managed against a calendar, and the point of
`not_raised_since` is that it stopped being managed OUT LOUD. Nothing is
hidden by the choice, because `modes` carries the rest and `by_mode`
counts each item exactly once, so the counts sum to `summary.items`.

#### The observation is about the conversation, never about the work

`not_raised_since` was called `silently_dropped` until review, and the
rename is the more important half of this section.

What CQ observes is that an item has not come up in the last two
presence-grade meetings with that person. What "dropped" asserts is that
it was abandoned. Those are different claims, and the gap between them is
where the worst false negative lives: **the item may have been finished
by email on the Tuesday and simply never mentioned again**, and CQ would
hold identical evidence either way.

Every other mode in the ledger is self-evidencing, because the receipt is
the person's own words in a meeting on a date. This is the only mode
where ABSENCE does the work, and absence is the one thing a meeting
cannot see. So the name states the observation, and the item carries
`meetings_since_last_statement` (peaked in the summary as
`max_meetings_not_raised`) so a client can render "has not come up in
your last 3 meetings with her" and be exactly right whatever happened
offline. A client must not render this mode as abandonment, neglect or a
stall.

The count itself uses MEETINGS WITH THAT PERSON, from
`person_appearances`, never elapsed days: a fortnight of silence while
the two of them were never in a room together is not evidence of
anything. The presence-grade predicate is shared with the 17a signals
(`people_signals.is_presence_grade`), so both surfaces count the same
meetings. Shelved items are excluded on the ledger's usual principle:
"Let it go" is the user releasing the item, so the silence afterwards is
the user's own decision. `MIN_MEETINGS_NOT_RAISED = 2` is a parameter,
not a constant, so it can be tuned against real data rather than
re-argued: two is the smallest number where "it did not come up" is a
pattern rather than one crowded agenda.

**The generalised rule, which now governs this whole surface:** a served
name may assert only what was observed. Where a name would assert a cause
and the evidence supports an observation, the observation wins, and where
inference is unavoidable it is published as a definition on the wire
rather than buried in a docstring (see `ADVANCE_DEFINITION` and
`CHASE_DEFINITION` in 5.12).

Per item CQ also serves `hop_count`, `deadline_moves`, `days_open` (first
statement to close or to today), `meetings_since_last_statement`, the
`owner_change` receipt, and the restatement array itself.
`object_regression` (did the OBJECT get vaguer, "a name" becoming "a
person to identify" becoming "a shortlist that still needs cleaning up")
is honestly `null`: no string comparison separates that from the same
object in different words, and the seam for a cold path judge is a
`regressions` map keyed by patch id, with no served field changing shape.

**The rules this surface is built on, which are not negotiable:**

- SHIP THE COUNT, NEVER THE CAUSE. "Six items assigned to this person
  have each been restated rather than closed, median three hops, zero
  closures" is checkable by the user AND by the subject. "He avoids
  accountability" is a verdict, it is unfalsifiable, and no mode name,
  no field and no string here may carry it.
- STORE INSTANCES, NEVER TRAITS. A stored trait about a named colleague
  is a defamation shaped object in a commercial engagement.
  `patch_ids_by_mode` makes every count openable into the dated, quoted
  items behind it.
- NO PERCENTAGE ON A DENOMINATOR UNDER FIVE. One to four observations
  per person per month means most thirty day ratios are statistically
  empty. This surface serves no ratio at all: counts, with `items` next
  to them, so the client can refuse to render.
- `median_hop_count` is `null` on an empty set, never NaN, and no other
  float is produced anywhere in the module (GP serializes with
  `allow_nan=False`).

### 5.11 `item_ledger_rollup`: the across-people read, which is about the user

On the person LIST route. The finding it exists for is about the person
running the meetings, not the people in them: follow up pressure can run
inversely to the delivery record, so the person generating risk gets
warmth and a re-date while the person who never misses gets interrogated.
Most of that falls out of the ledger already (`absorbed_by_user`, the
per person re-date and hop counts say who the user stops chasing and
whose work they end up holding). CQ serves those counts, their
denominators and the patch ids behind them, and writes NO interpretation:
no score, no ranking, no served string naming an asymmetry. The client
says what it means.

It is computed over the UNFILTERED population, so paging or a
`min_meetings` filter cannot move a user-level number, and over OPEN
items only, because the list route does not fetch the completed
population. `scope` states that on the wire rather than leaving the two
denominators to be assumed equal with the detail route's.

**Counts on the list, receipts on the detail.** `summarize()` produces
patch id arrays alongside its counts, and the list strips that whole
family (`item_ledger.RECEIPT_KEYS`) rather than one key by name, so
a receipt key added later cannot quietly start growing a browse payload
polled for every person the user has. The ids are only useful once the
user has chosen somebody, and the detail route serves them there next to
each item's own restatements.

### 5.12 `raised_without_advance`: the follow up metric, after two corrections

**Both corrections are worth keeping, because each was a claim that did
not survive contact.**

FIRST, the finding was that follow up pressure runs inversely to the
delivery record: the person generating risk gets warmth, the one who
never misses gets interrogated. The obvious metric was questions
RECEIVED. Computed by hand against the transcripts, it does not hold. The
volume is twelve questions to one person and ten to the other, nearly
level, and a card built on it would have asserted something the data
contradicts.

What separates the two sets is the KIND of question. One person's twelve
are items already in the ledger coming up again and not moving, three
times on one item across three meetings. The other's ten are substantive
probes to somebody already ahead of the user. Both kinds count as one
question each, which is exactly why volume cannot see the difference. So
the metric counts occasions that produced no advance, and **question
volume stays its own separate count** (`questions`, section 6.6).
Conflating them is what produced the false claim.

SECOND, the replacement was called `chases` until review, and it did not
survive the rule in 5.13 either. "Chase" asserts pursuit. What is
observed is thinner: a restatement records that THIS item came up in a
specific meeting (`origin_id`), and `person_appearances` records, for
that same meeting, how many questions the user asked THIS person. Both
halves were already stored, but **the join is MEETING level, not question
level**, because CQ holds no link from a question to an item. An occasion
where the item came up while the user asked about something else counts,
so a name asserting pursuit would report a motive off a coincidence of
timing. "Pressed" would have been the same assertion said more quietly.
RAISED is the observation with nothing added.

That rule biting its own author is the correct outcome of having a rule.

`RAISED_DEFINITION` ships on the wire
(`item_raised_in_a_meeting_where_the_user_asked_this_person_a_question`)
so the boundary travels with the number instead of living in a docstring.

Three outcomes per occasion, and the third is why this cannot collapse
into one number:

| outcome | meaning |
| --- | --- |
| `without_advance` | there was a later meeting with this person and the item had not closed by it |
| `with_advance` | it closed by that next meeting |
| `unresolved` | it was raised in the most recent meeting, so nothing has had a chance to happen |

Plus `unmeasurable`: the item came up in a meeting carrying no question
metric, so whether the user asked anything is unknowable. On the day this
ships that is every meeting there has ever been, and a client rendering
the count without this one is reporting a floor as a total.

`ADVANCE_DEFINITION` is published the same way
(`closed_by_the_next_meeting_with_this_person`) because it is
deliberately narrow. **A fresh restatement is not an advance and a moved
due date is not an advance.** Motion that reads as progress at every
checkpoint is the illusion this whole surface exists to break, so only
resolution counts. `unresolved` exists for the same reason pointed the
other way: counting an occasion from the latest meeting as a failure
would manufacture the finding out of recency.

The summary carries `raised_with_a_question`, `raised_without_advance`,
`items_raised_without_advance`,
`max_raised_without_advance_on_one_item` (the number behind "three of
them on the same item"), `raised_unmeasurable`, both definitions, and the
patch ids, which are receipts and therefore detail-route only.

### 5.12a The extraction seam, and what it would actually cost

**Explicit non-goal: CQ does not extract questions today, and nothing in
this pass built toward it beyond leaving the seam clean.** Day one
coverage is commitments and blockers, exactly as before.

The seam is the point of 5.9a: because eligibility is a manifest
declaration resolved through the runtime, turning on a recurring-question
ledger needs NO CQ code. What it needs, in order:

1. **A patch type in the app's manifest.** `open_question` (or whatever
   the app calls it) with `facet: Episode`, `project_scoped: true`,
   `ledger_tracked: true`, a permanence, and a `value_shape` carrying at
   minimum `text`. Registration writes the registry row and invalidates
   the runtime cache, and the ledger picks it up within the cache TTL.
   Zero CQ changes. This part is done and tested.
2. **Extraction guidance for it**, which is the real work and is entirely
   in the manifest's `extraction_rules` plus the schema prompt builder
   that already renders them. The hard part is not the prompt, it is the
   PRECISION: an extractor that emits every interrogative sentence would
   bury the ledger, because most questions in a meeting are answered in
   the next breath and are not objects at all. The target is the narrow
   case the corpus proved valuable: a question that is RAISED AND LEFT
   OPEN, with nobody named to answer it.
3. **Dedup thresholds for it.** This is the one place a question is
   genuinely harder than a commitment. The ledger's whole premise is that
   the same object restated in different words collapses onto one patch,
   and questions are restated much more loosely than commitments ("Who
   owns the vendor relationship?" then "We still have not sorted out
   ownership"). The trigram fast path will miss that pair; the gray zone
   judge is the mechanism that catches it, and its prompt is written for
   facts rather than for questions.

**Estimate.** Steps 1 and 3 are small: a manifest version bump and a
registration, plus a judge prompt variant and an eval over the existing
transcripts, call it two days including the eval. Step 2 is the whole
cost and it is not a coding cost: writing the extraction rule is perhaps
half a day, and measuring whether it fires on the right sentences needs
a labelled pass over the benchmark transcripts, which is where the time
goes. Three to four days end to end to a number worth trusting, with the
risk concentrated entirely in precision rather than in plumbing.

**What would make it worth doing now:** the recurring-question case beat
every commitment finding in testing, and three of the four likely
personas have no action items at all (5.9a). What would make it worth
waiting: the ledger's value on questions cannot be measured until
questions exist in the corpus, and nothing that ships before then can
tell us whether the extraction precision is good enough.

### 5.13 The vocabulary audit

Prompted by the `silently_dropped` finding, every name this surface
serves was checked against one rule: **a served name may assert only what
was observed.** Two names failed and one is on the line.

| name | verdict |
| --- | --- |
| `silently_dropped` | FAILED, renamed `not_raised_since` (5.10) |
| `chases` | FAILED, renamed `raised_without_advance`, boundary published as `RAISED_DEFINITION` (5.12) |
| `delivered` | kept through the naming audit, then renamed `resolved` when the primitive widened, see below |
| `re_dated`, `restated`, `reassigned`, `absorbed_by_user`, `open` | pass: each is a thing somebody said, on a date, in a meeting |
| `hop_count`, `deadline_moves`, `days_open`, `first_stated_on`, `last_stated_on`, `meetings_since_last_statement`, `owner_change` | pass: all counts of stored observations |
| `received_explicit` / `received_inferred` | pass, and the split is itself the honesty: the inferred half names its own uncertainty |
| `unresolved`, `unmeasurable` | pass: both exist precisely to keep an unknown out of a count |
| `object_regression` | pass by being null, and by asking about the restatement TEXTS (the conversation) rather than about the work |

`resolved` (which was `delivered` through the naming audit, and was
renamed when the primitive widened rather than because the audit failed
it) is worth stating rather than waving through. It does assert more
than the record strictly holds: `completed_at` is when CQ LEARNED an
item closed, not when the work landed, and the same caveat is already
written into `follow_through.py`. It is kept because it differs in KIND
from the failed name: PRESENCE does the work, not absence. A closure is
an affirmative act by a person or the user, and it arrives with
`completion_source` and `completion_evidence` attached, so the claim
carries its own receipt and a reader can check it. `not_raised_since` had
nothing to check, which is exactly why it could not keep a name that
asserted a cause. The later rename to `resolved` preserved that property
exactly: it is the same affirmative act with the same stamps, said in a
word that is still true when the object is a question somebody answered.

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

A merged entity keeps its name in the Redis entity index on purpose,
because the merge records that name as an alias of the canonical and
recall must still match it.

> **CORRECTION, 2026-08-07. This section previously said "recall is
> deliberately untouched" and that a dead row "seeds graph traversal with
> an empty neighborhood, which costs nothing and changes no output
> bytes." That was true of graph traversal and FALSE of the rendered
> output, and the difference was user-visible.**
>
> The entity fetch matched by name with no merge awareness, so a folded
> row came back alongside its survivor carrying its own name and its own
> description. After merging four spellings of one person, recall
> rendered `People: Vijay Rayudu (Participant...); Vijay R (Platform and
> product coordination); Vijay Rayud` and told the model one human was
> three. That is precisely the split brain a merge exists to resolve,
> surviving in the recall lane.
>
> The cause is the shape worth remembering: **only the WRITE path hopped
> the pointer** (`_resolve_merged_forward`). One concept, two
> implementations, one of them maintained. The same asymmetry produced
> the `owed_to` self-hole on the same day.
>
> Fixed by resolving forward in the recall entity fetch itself, so both
> formatters and the graph traversal all see canonical ids. It
> **resolves** rather than excluding folded rows: a merge usually records
> the loser's name as an alias on the survivor, so excluding would
> usually be enough, and "usually" silently drops the match when that
> alias is missing. Recursive with a depth cap of 8, matching the write
> path, so a corrupt cycle terminates instead of spinning.
>
> Verified against production (the four Vijay rows collapse to one) and
> against fabricated chain, cycle, missing-alias and unmerged rows.
> Output remains byte-stable: ordering is unchanged and two identical
> calls produce identical bytes. `tests/unit/test_recall_merge_aware.py`
> is a source-level guard, and it was checked to actually fail when the
> fix is reverted.

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

### 6.2b Ownership-grade presence lands at ingest (2026-08-13)

**A person who owns an action item out of a meeting has an
`ownership`-grade appearance for that meeting, written by the same
ingest that stored the item.** It used to be written by a script, when
somebody ran the script.

The failure, found in the field. A real meeting ingested cleanly, seven
patches including three commitments owned by two named people, no error
anywhere, ZERO entities and ZERO appearance rows. Both owners' cards
still said they were last met three days earlier. Nothing was broken in
the sense of throwing: the patches are correct and the owners are on
them. Presence was simply absent.

The mechanism is one seam. `enforce_person_ownership` is the structural
net for the model returning `value.owner` with no person patch behind
it, and its own docstring says compliance is unreliable and this is why
the net exists. It operates on `content["patches"]`. Appearances are
written from `content["entities"]`. Nothing did the equivalent there, so
a person could be a first-class owner of work in a meeting and never
exist as an entity in it, and an appearance references an entity.

`inject_ownership_entities` closes it in the sanitizer chain, between
`drop_placeholder_and_self_person_patches` and
`drop_placeholder_entities`. The position is load bearing in both
directions: late enough that the person names it reads are final (owner
edges pruned to real owners, prose cut out of names, self and
placeholder patches gone), early enough that the placeholder pass still
cleans up after it rather than ahead of it.

Three rules worth stating because each of them is a way to get this
wrong:

* **The capacity is `ownership`, never `speaker`.** Work gets assigned
  to people in absentia, so owning an item is not evidence of having
  spoken. `speaker` is computed at the sink from a resolved transcript
  label and is never accepted from an entity in any lane
  (`observed_capacities`). This is not pedantry: SS's duplicate veto
  reads the ownership-only-versus-speaker split to tell label drift from
  two humans (5.2), so a forged or inferred `speaker` is a merge
  proposal that never appears.
* **A name introduced by ownership is not stamped `mention`.** CQ did
  not observe anyone saying it; it observed an owner string. The
  backfill's ownership tier makes the same claim, so forward rows and
  repaired rows agree.
* **The row is dated by the ingest clock**, which for a row written
  during the ingest is the same clock its sibling mention and speaker
  rows get. This is the 6.2a trap in a different place: a naive `NOW()`
  from any path that is not the ingest would tell the People list the
  user met this person today.

The entity cap gets the patch backstop's exemption for the same reason
the patch backstop has one: the cap bounds LLM-output noise, and
truncating a structural injection deletes a person's presence in a
meeting.

**Consequence beyond presence**, worth noting because it was not the
target: project membership is presence-grade (`capacities` intersected
with `{speaker, ownership}`), so an owner who was only ever recorded as
a mention was being filtered OUT of the project they own work in. They
now carry `ownership` and count.

**What this does not do.** History is not repaired by shipping it. That
is `scripts/backfill_person_appearances.py --tier ownership`, which
derives the same rows from Postgres with no model call and no transcript
retention, and whose dry run now reports the delta against what is
stored rather than what it found. An owner who never became a person
entity at all cannot be repaired by it, because an appearance references
an entity and the script creates none. That population is the forward
fix's from here.

### 6.2a Presence follows a relabel: the reassignment, the speaker map, and the one null that stays ambiguous

**The invariant this section exists to state: a null
`signals.last_present_at` means NO PRESENCE WAS RECORDED, on every path
but one, and that one is named below.**

Why it matters more than it looks. The client fell back to its own local
speaker-label index whenever CQ served a null anchor, because a null
could mean two different things: "this person was not there" or "CQ
cannot tell, because an identity moved and the appearance did not follow
it". A client cannot separate those, so it could not delete the
fallback, and the fallback is NOT trustworthy: a stranger has worn the
user's enrolled name for an entire meeting on this app. Untrustworthy
local data was outranking the server because the server's null was
vague. Making the null mean one thing is what lets the fallback go.

#### What the client actually does, which changed the design

A post-save speaker rename does not call `rename-speaker`. It fires
`MeetingStore.onSpeakerRenamed(meetingId, oldLabel, newLabel)` from
three call sites, filters that MEETING's patches, string-replaces the
old label inside the fact text, and sends `PATCH /v1/quilt` per patch.
The meeting id is in the function signature and never leaves the device,
so the graph layer is never told anything and no appearance can follow.
A FOURTH lane, block-scoped segment relabelling, notifies CQ of nothing
at all, not even the text surgery.

Two things fall out of that. First, **a post-save rename is already
meeting scoped**, so nothing here needs to reason about a user's whole
history. Second, **the verb that should carry this is `reassign-speaker`,
not `rename-speaker`**: it already takes `from_labels: [{label,
meeting_id}]`, which is exactly the scope.

#### Form 1: `POST /v1/quilt/{u}/reassign-speaker`, one binding

Imperative and narrow: these labels, in these meetings, are this person.
Three targets, exactly one per request.

```json
{
  "from_labels": [{ "label": "Speaker 4", "meeting_id": "<uuid>" }],
  "to_person_id": "<uuid>"
}
```

```json
{
  "from_labels": [{ "label": "Speaker 4", "meeting_id": "<uuid>" }],
  "to_name": "Ramkumar"
}
```

```json
{
  "from_labels": [{ "label": "Speaker 4", "meeting_id": "<uuid>" }],
  "to_self": true
}
```

`to_name` is the new one and it is the whole reason a second route was
not needed for naming: CQ resolves the name (bind to the matching
person, create them if there is none) through the SAME path
`POST /v1/people` uses, so a name typed onto a speaker and the same name
typed into the "+" sheet cannot produce two different people. Server
side resolution on purpose: a client re-implementing name matching is
the duplication section 1 argues against. Placeholder names are refused
(422 `PLACEHOLDER_NAME`), same gate as create.

Response, additive:

```json
{
  "patches_updated": 3, "connections_updated": 0,
  "entities_merged": 0, "labels_skipped": 0,
  "appearances_recorded": 1,
  "presence_entity_id": "<uuid|null>",
  "resolved_person": {
    "entity_id": "<uuid>", "name": "Ramkumar",
    "patch_id": "<uuid>", "status": "created"
  }
}
```

`resolved_person` is null on the other two lanes, which already know
their target.

What it writes, and the reasoning behind each part:

* **Presence.** Utterances moving to a person is direct evidence that
  person SPOKE in that meeting, so a `speaker`-capacity appearance is
  upserted per meeting where anything actually moved, on the merge
  route's discipline (6.2): earliest `first_seen_at`, latest
  `last_seen_at`, capacities unioned, `turn_count` MAX with NULL never
  clobbering a known value. Several labels folding into one person in one
  meeting stay ONE appearance, because that is still one meeting.
* **A label that moved nothing records nothing.** The request named it;
  the meeting never used it. There is no evidence anyone spoke under it.
* **The timestamps are the MEETING's ingest anchor, never `NOW()`.**
  Appearances run on the ingest clock, so the anchor is the meeting's
  sibling appearance rows, or the meeting's own patches when it has no
  siblings, and a meeting with neither is skipped rather than dated.
  Stamping `NOW()` would tell the People list the user met this person
  today because they fixed a label today, which is the same failure
  `backfill_person_appearances.py` refuses in its own header.
* **The SOURCE label's appearance is left in place, and no measurement
  moves.** This form moves PATCHES, and patch ownership is not a
  partition of a meeting's turns, so carrying the label's turn count over
  would be inference. The source row also survives, and two rows each
  claiming the same 41 turns would credit one person's speech to two
  people. NULL is unknown, which is true. Form 2 states the whole meeting
  and can therefore move measurements; this one cannot.
* **The target is excluded from the label cleanup**, so a label wearing
  the target's own name can no longer delete the person just reassigned
  to, and cascade away the appearance with it.
* **A suppressed target accumulates no meeting history**, matching the
  ingest path exactly. The patches still move; only the presence write is
  withheld.
* **A pre-merge target id writes presence to the CANONICAL row**, since
  the list reads `merged_into IS NULL` and an appearance on a folded row
  is presence nobody can see. The owner STRING is unchanged, and the
  ledger still matches it because a merge leaves the folded name behind
  as an alias.

**`to_self` lands on the ego entity (migration 35) and nowhere else.**
The ego is a People row like every other, carrying the same `signals`
block, so leaving it out would make the user's own row the one place a
null anchor still means cannot-tell. The user saying "those were me" is
the same grade of presence evidence as saying "those were Marcus". The
guard is harder, though: no ego link stamped means nothing is written,
and this route never mints a stamp. The ego link is keep-first because a
moving ego silently reshapes every graph read, and a route about speaker
attribution does not get to decide who the user is.

#### Form 2: `POST /v1/quilt/{u}/speaker-map`, the resulting state

**The state, not the operation.** "What is the inverse of a
reassignment" has no answer as posed. An undo can mean "I mislabelled
that, it was never him", which makes the appearance a false statement,
or "I want the raw labels back on screen", which makes it a true one
that reverting would destroy. Neither CQ nor the client can tell those
apart from an undo signal, so any rule keyed on the OPERATION is wrong.

So CQ accepts the full label-to-person mapping for a meeting as it now
stands, diffs it against the appearances it holds, and adds or removes to
match. That dissolves the family: an undo is just the post-undo mapping,
a block-scoped edit is just the post-edit mapping (so segment ranges are
never modelled), a consolidation is a mapping with one fewer key.

```json
{
  "meeting_id": "<uuid>",
  "labels_are_complete": true,
  "labels": [
    { "label": "Speaker 1", "to_person_id": "<uuid>" },
    { "label": "Speaker 2", "to_name": "Ramkumar" },
    { "label": "Speaker 3", "to_self": true },
    { "label": "Speaker 4", "to_nobody": true }
  ]
}
```

Response:

```json
{
  "meeting_id": "<uuid>",
  "labels_received": 4,
  "appearances_recorded": 2,
  "capacities_reduced": 1,
  "appearances_removed": 1,
  "unresolved_labels": [],
  "labels": [
    { "label": "Speaker 1", "entity_id": "<uuid>", "name": "Priya",
      "patch_id": null, "status": "exists" },
    { "label": "Speaker 4", "entity_id": null, "name": null,
      "patch_id": null, "status": "nobody" }
  ]
}
```

Every `labels` entry carries every sibling key so a client decodes one
type. `status` is `exists | created | nobody | unresolved`.

**Idempotent, as a hard requirement rather than a nice property.** Only
necessary work is planned (`services/person_appearances.plan_speaker_map`
is pure and unit tested), so sending the same mapping twice writes
nothing the second time and moves no timestamp. That is what makes it
safe on relabel lanes nobody has found yet: an unwired lane then fails by
OMISSION rather than by writing something false.

**Removal, which is the half that makes undo work.** Capacities are a
set for exactly this reason, so the rule is graded:

* A row whose ONLY capacity is `speaker` is DELETED. Nothing but the
  label ever claimed this person was in the room. It is not left with an
  empty capacity set, because empty means pre-migration-31 unknown and
  unknown already counts as presence, so an emptied row would keep
  asserting the thing that was just retracted.
* A row standing on another capacity SURVIVES and loses only `speaker`.
  A person recorded by `ownership` was in that meeting whether or not a
  label still points at them.
* A row with no `speaker` capacity at all, including an EMPTY one, is
  never touched in either direction. This mapping speaks about speaker
  labels; it may not add or remove a claim of a grade it never described.
* Stripping `speaker` also NULLS the per-speaker measurements
  (`turn_count` and the four per-person question counts). A turn count is
  a claim about what this person SAID, and the mapping just said those
  words were somebody else's. They are not recoverable, which is the
  price of a corrected attribution; an honest unknown beats a confident
  misattribution. `meeting_questions_by_user` stays, because it counts
  what the user asked in the meeting and is a property of the meeting.
* **Removal requires a fully resolved target set.** If any label fails to
  resolve (today: a `to_self` with no ego link, or a `to_person_id`
  pointing at a suppressed row), absence stops meaning "did not speak",
  so that call adds and removes nothing and echoes the labels that cost
  it in `unresolved_labels`.
* **`labels_are_complete` must be literally true**, or the request is
  refused (422 `INCOMPLETE_MAPPING`). Removal works by absence and CQ
  cannot verify completeness from the outside, so the assertion is made
  at the call site: a half-wired lane fails loudly instead of quietly
  deleting presence it was never told about. `to_nobody` is explicit for
  the same reason, since a client that forgot to fill in a target must
  get a 422 rather than a deletion.

**Presence only, deliberately.** This form never rewrites `value.owner`
and never touches patch text. Speaking and owning are different claims
(work gets assigned in absentia, which is the argument the appearance
backfill's own header makes), so a map of who spoke must not silently
re-own anybody's commitments. Use form 1 for attribution and form 2 for
presence; they compose, and both are meeting scoped.

#### `POST /v1/quilt/{u}/rename-speaker` is unchanged, and stays

Not deprecated and not removed here: GP carries it, and a route removal
is a two-sided release. What is now pinned by test:

1. **The old name is already an entity.** The rename is IN PLACE:
   `UPDATE entities SET name`, so `entity_id` never changes.
   `person_appearances` is keyed on `entity_id`, so every appearance the
   person already had still points at them and the presence anchor is
   untouched. Verified, and pinned, because a future "fix" that turned
   this into a delete plus recreate would silently drop the person's whole
   meeting history.
2. **The old name was an unnamed placeholder.** A NEW entity is created,
   and `SpeakerRename` carries `old_name` and `new_name` and nothing
   else. There is no meeting id anywhere in the request, so there is no
   meeting to attach an appearance to. CQ will NOT guess by matching
   patch text or owner strings; that is inference wearing a record's
   clothes. The new entity starts with zero appearances and serves a null
   `last_present_at` until an extraction observes them.

The recommendation is NOT to add meeting ids to this route. Both of its
jobs are already expressible: `reassign-speaker` with `to_name` names an
unknown speaker in a meeting, and `speaker-map` states the meeting's
whole speaker set. Adding a third meeting-scoped dialect would mean
three ways to say the same thing, one of which cannot express removal.

#### The honest audit of a null anchor

| Path | Null anchor means |
| --- | --- |
| Mention-only person (17a presence grading) | Not present. Unambiguous. |
| Person from a chat turn or `POST /v1/people` (no origin) | No meeting recorded. Unambiguous. |
| Person whose meetings all predate migration 30 and were never backfilled | Not present as far as CQ can see. The `ownership` and `speakers` backfill tiers ran (8c); `mentions` is held, and it is not attendance anyway. |
| Reassigned speaker (form 1) | Present, and now recorded. |
| Speaker map (form 2) | Exactly what the mapping said, in both directions. |
| Merged identity | Present, and recorded since 6.2. |
| Renamed speaker, old name was an entity | Present, and recorded. Verified in place. |
| **Renamed speaker, old name was a placeholder** | **AMBIGUOUS. Could be genuinely absent, or an hour of real presence CQ has no meeting id for. Closes when the client routes that lane through either form above; no CQ contract change is needed.** |
| **A relabel lane that calls neither form** (today: the client's three text-only lanes and the block-scoped one) | **AMBIGUOUS until it is wired. This is the omission failure the design chose over a false write.** |
| MCP deployment whose Postgres lags migration 30 | Cannot tell. The write degrades silently by design there. |

#### Sequencing and one follow-up

`speaker-map` is a NEW route, so **GP carries it before CQ can call it
live**: the gateway declares exact paths, no prefix and no wildcard, and
CQ's socket cannot see a route-table miss. `to_name` is a new FIELD on a
route GP already carries, which is additive at the reader. Same word,
different mechanism.

**Follow-up, not bundled here: the patch TEXT rewrite.** The client
still does the string replacement itself and sends `PATCH /v1/quilt` per
patch, as `rename-speaker`'s docstring has always assigned. The
recommendation is to move it server side eventually, because the client
is rewriting fact text with a naive string replace (a label that appears
inside a sentence gets rewritten too) and because it is the last piece of
a relabel that CQ learns about only as anonymous patch edits. It is a
real behavior change to shared surfaces and deserves its own decision
rather than a bundle.

### 6.6 Question counts per appearance (shipped, migration 37)

The sibling of migration 34's `turn_count`, and it exists under the same
constraint: transcripts are derive-then-discard, so a signal not captured
at ingest is lost forever and every meeting that landed before this
column existed is permanently unmeasurable. That is the whole argument
for shipping it before the surface that reads it is finished.

`extraction_schema.question_attribution` parses the same normalized
transcript `speaker_turn_counts` does, in the same pass, with the same
hygiene: `(you)` stripped, diarization placeholders dropped, label keys
lowercased. Six nullable columns land on `person_appearances`:
`questions_asked`, `questions_received_explicit`,
`questions_received_inferred`, `questions_from_user_explicit`,
`questions_from_user_inferred`, `meeting_questions_by_user`.

**The two attribution grades are stored separately and must never be
summed by a reader.**

- EXPLICIT: the question names its addressee as a vocative, which is
  defined as comma delimited at an edge of the sentence ("Marcus, can you
  get me that?", "Can you get me that, Marcus?", or a question that is
  only a name). High confidence.
- INFERRED: no name, so the addressee is taken to be whoever speaks next,
  and only for the questions that TRAIL a turn (the ones after its last
  statement sentence). That last part is what keeps a rhetorical question
  the speaker answers themselves out of somebody's row. It is still a
  heuristic and it is wrong sometimes, which is the point of the column.
- UNATTRIBUTED: a question to the room with nothing to attribute it to.
  Counted, never dropped, because it says how much of the meeting the
  measurement missed.

The interesting false positive is a question that NAMES somebody who is
not the addressee: "Did Marcus ever send that?" asked of the person
across the table. A name inside a clause is a name being talked ABOUT, so
the edge rule leaves it unnamed and it falls into the inferred column.
Reading it as explicit would put the user's follow up pressure on the
wrong person's row wearing the high confidence label, which is the one
error this design cannot absorb. Known limit, recorded as a test: the
addressee vocabulary is built from SPEAKER LABELS, so a person who is in
the room and never speaks is not addressable and their vocative falls
through to the guess. Fixing that means matching spoken names against the
entity graph, which the pure function has no access to.

NULL is unknown everywhere: the meeting predates the metric, the ingest
carried no transcript, or no speaker label could be identified as the
user. Nulls are per FIELD, not per row, because a meeting can know how
many questions a person was asked and still not know which speaker was
the user; the `from_user` pair says so rather than reporting a zero the
transcript never supported. Re-ingesting one meeting keeps the MAX per
column, never sums, exactly as `turn_count` does.

`meeting_questions_by_user` is the denominator that travels with the
counts: two questions out of three asked all meeting and two out of forty
are not the same observation. CQ computes no ratio over them and serves
no string naming a pattern. Served per meeting in `meetings[].questions`
and aggregated per person as `questions`.

**Volume is not the follow up finding.** It is nearly level across people
whose follow up is nothing alike, because an item being raised again and
a substantive probe each count as one question. Section 5.12 is the
metric that carries it, and it CONSUMES these columns (the from_user
pair is what turns a restatement into a counted occasion) without
replacing them. Two counts, two questions, both served, never folded
together.

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

`insights` (added 2026-08-13) follows `you_owe` exactly: it reads from
the CALLER'S manifest, not from CQ's code. An app that declares no
person-clustered consolidation rule never runs the profile pass, so the
lens stack is not empty for it, it is unavailable, and the entry says so
with a reason. Two apps reading the same user can honestly get different
answers. Note the boundary: the capability answers "can this app ever
have insights", the per-person `null` answers "can CQ derive them for
THIS person". The `CQ_CONSOLIDATION_ENABLED` kill switch is deliberately
not folded in, because it is the worker's environment and the API process
cannot see the worker's copy of it; existing insights keep serving while
it is off.

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

The worker now stamps this capacity at ingest (6.2b), which changes what
the tier is FOR. It was the only writer, on the reasoning that ownership
is derivable from Postgres whenever anyone asks. True, and it still left
every meeting ingested between runs with owners who had no presence in
it. The tier is now the repair for history and the audit for the
forward path, not the mechanism.

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


## 5.14 The line under the name: `title`, `description`, `described_as`, `stated_roles` (2026-08-21)

Brian saw Suresh's description change between meetings (2026-08-18);
Scott saw "scrum master", stated by Suresh himself on 08-17, replaced
four meetings later by "Meeting facilitator and lead" (2026-08-21). The
description under a name was whatever the LAST meeting's extraction said
the person did, and nothing on the person surface consulted the role
patch that held what they SAID.

**Precedence rule, on the wire: a role the person STATED beats a
description a meeting INFERRED.** Person detail serves:

* `description`: unchanged, what the last meeting showed them doing.
  Kept as its own field because it is true and useful, just not the job.
* `described_as`: the `entity_descriptions` series (migration 39): one
  row per distinct perception with an `observation_count` of
  confirmations, never rewritten once appended. `{current,
  changed_from, iterations, history[], truncated}`, newest first, capped
  20. `changed_from` non-null is the "it moved" indicator. NOTE: this
  field was served `null` to every client from #286 (08-18) until #301
  (08-21) because the fetch ran on a released pool connection and the
  lagging-DB guard swallowed the error at debug level. A null here is now
  logged at WARNING. Null is the honest answer to a missing table and the
  same dishonest answer to a programming error; the log level is how the
  two are told apart.
* `stated_roles`: the active patches of the manifest's
  `people.stated_role_type` (SS floor `role`; an explicit manifest null
  means "not tracked" and the field serves null), matched by the
  person's name or alias as the opening of the text, or via a
  `describes` edge to the person's patch. Items keep the raw text, with
  project, origin and date as receipts. `title` is the newest item with
  the person's own name and copula stripped ("Suresh is scrum master on
  ABM project" serves as "scrum master on ABM project"); `title_source`
  is its receipt. The derivation never invents words: the output is a
  substring of the input or the input itself.
* `title` is also served top-level for the client's convenience.

Client rendering (contract sent to SS 2026-08-21): the line under the
name is `title` when non-null, else `description`; `description` stays
as a secondary "last meeting" line with a changed mark off
`changed_from`; tapping it opens `described_as.history` with the count
per row and a tap-through by `origin_id`; `stated_roles.items` renders
beneath as "what they told us" with the `title_source` row marked.
Instances, never summaries; counts, never confidence; null as absence.

Measured on prod the day it shipped: 24 of 498 people gain a `title`
from the 79 stated roles. The other 474 still show the last meeting's
inference until a role is stated. Synthesis of a better title across the
series is a model-bearing step and is deliberately NOT this; the 08-13
person-analyzer experiment showed cross-meeting synthesis needs Sonnet
and fails invisibly on Haiku. Decide it against that number.

## 5.15 `trajectory` and `working_with` (2026-08-22) — trajectory SERVED (2026-08-24); working_with NOT SERVED, and honestly cannot be yet

**Status: `trajectory` is live.** The worker's consolidation person
branch runs `_derive_trajectory` (after who_they_are, same budget), and
person detail serves the card as `trajectory` next to `who_they_are`,
filtered out of the `insights` stack. Kill switch
`CQ_TRAJECTORY_ENABLED`; model `CQ_TRAJECTORY_MODEL` (Sonnet default,
per the eval below). TWO of the three measures are wired:

- `closed_late`: dated completables owned by the person (owner resolved
  through the entity graph, never text), closed after their date, per
  span meeting.
- `speaking_turns`: `person_appearances.turn_count` per meeting; a
  meeting with a null count is an honest zero-denominator bucket.

**`questions_to_you` is in MEASURES and is deliberately NOT built**,
because the data does not exist: migration 37 attributes a question the
user RECEIVES to the aggregate user block, never to the asker's row, so
"questions they addressed to you" has no per-person source. Building it
from `questions_asked` (any addressee) would be a different claim
wearing this one's label. **`working_with.your_half` is unservable for
the same class of reason**: the user has no `person_appearances` rows,
so your-side turns and your-side received questions have no storage.
Both need CAPTURE-side changes (a per-asker to-user column; user-side
turn counts) and can never be backfilled; until then `working_with`
stays unserved (its moves writer prompt was also never authored). The
sections below remain the contract for when that lands.

This section exists because the contract previously lived only in
cross-session messages and a PR body. When ShoulderSurf's session
rotated on 2026-08-22 they reconstructed a plausible and wrong version of
it from notes, which is the failure mode the "record it in the artefact,
not the thread" rule exists to prevent, arriving on the team that had
been repeating the rule all day.

### `trajectory` — the hero card, "how they're changing"

The sibling of `what_stands_out`. That lens asks on what measure a person
is unlike EVERYBODY ELSE; this one asks on what measure they are unlike
THEMSELVES earlier, which is the question a user genuinely cannot answer
from inside the meetings. Code computes both windows and picks the
measure; the model only writes, and may state only numbers it was handed.

Served on `GET /v1/people/{user_id}/{entity_id}`. Object or null; null is
the common case, not the edge. **Every value is an integer.** There is
not one float in the payload, for two independent reasons: a served rate
is a number whose denominator the reader cannot see, and GhostPour's
proxy walks the body replacing non-finite floats with null and never
visits ints.

```
"trajectory": {
  "lens": "how_theyre_changing", "display_order": 5,
  "measure_key": "closed_late" | "speaking_turns" | "questions_to_you",
  "subject": <string>, "unit": <string>, "counted_noun": <string>,
  "pair_kind": "proportion" | "rate",
  "valence": "unflattering_up" | "neutral",
  "movement": "up" | "down",
  "direction": "worse" | "better" | "up" | "down",
  "span_meetings": <int>,
  "earlier": {"numerator","denominator","meetings","origin_ids"[]},
  "recent":  {"numerator","denominator","meetings","origin_ids"[]},
  "series":  [{"origin_id","sequence","numerator","denominator"}, ...],
  "supersedes": [<lens id>, ...],
  "about_person": <string>,
  "text": <=180, "narrative": <=320, "do": <=150
}
```

Four things the design asked for that this REFUSES to serve, each with
its reason, so nobody re-adds them from a mock:

- **A pre-divided percentage.** Both windows ship as integer pairs; the
  client divides if it wants a percent.
- **A slope.** Two windows support "it was this, it is now that". They
  do not support a rate of change and nobody measured one.
- **A duration.** CQ holds no timing at all. ShoulderSurf holds
  per-segment timing it CANNOT ATTRIBUTE TO A PERSON, because a diarized
  `speakerLabel` has no join to an `entity_id` unless that voice was
  enrolled. The honest statement is not "nobody has timing", it is "the
  side with the timing cannot say whose it is".
- **A TIME AXIS OF ANY KIND.** This was designed, written and caught in
  review before it shipped. **CQ NEVER PERSISTS A MEETING DATE**: one
  arrives at ingest as `payload.timestamp`, is spent resolving relative
  deadlines, and is dropped. Every surviving timestamp is an INGEST
  clock. A weekly sparkline keyed on those is a chart of when the
  importer ran. So windows split by MEETING SEQUENCE and `series` carries
  `origin_id`; the client holds the only real meeting dates in the system
  and is the only side that can draw a time axis.

**The pass has its OWN budget, and says so when it cannot run** (ruled
2026-08-27). It used to run last of the five person passes on the
remainder of `MAX_CLUSTERS_PER_USER_PER_CYCLE`, a pool also shared with
every cue rule for that user, and it logged nothing when the remainder
was zero. Two doors starved it: a cue rule could spend the pool before
the person rule was reached, so the branch never ran; and inside the
branch the four earlier passes could spend what was left. Between
2026-08-25 and 08-27 no hero card was created for anyone while Suresh
cleared the entry floor at +40.7 percent and was the only person on the
roster who did, and nothing in the log said so, because "no budget" and
"nobody qualified" produce identical silence. Now:
`MAX_TRAJECTORY_PER_USER_PER_CYCLE` is its own bound, hero cards are
reported in the cycle total but never charged to the shared pool, the
rule loop's break does not apply to a rule with its own budget, and both
starvation paths log `trajectory_budget_exhausted` at INFO with the
number of people left unexamined. That number is what was OBSERVED and
must never be read as a backlog that would have produced cards: most of
a roster never qualifies. The per-user ceiling is therefore this budget
ON TOP of the shared pool, which is the stated cost of the ruling.

**`pair_kind` is a PROHIBITION, not decoration.** A proportion's
numerator is a subset of its denominator ("8 of the 11 dated items he
closed"); a rate's is not ("214 turns across 8 meetings"). A proportion
MAY be rendered as a percentage. A RATE MAY NOT, EVER. Conflating them
put "214 out of 8" in a prompt, which both candidate writer models
correctly refused as impossible, and simultaneously pinned a rate flat
against the top of a 0..1 axis on the client. It also made the gap gate
vacuous on rates (2675 "points" against 1200 clears a 20 point floor), so
rates gate on relative change instead.

**`measure_label` says what was measured, and the SECTION stays general**
(ruled 2026-08-27). Scott: "if all we're ever doing is measuring their
interactions then why not be more specific?" The observation was right in
practice, since every card produced up to that day was `speaking_turns`,
and wrong as a rename: this lens also carries `closed_late`, which is
about delivering by a date and has nothing to do with how much somebody
talks, and there was data for one on prod that same day (128 closed dated
items). A section titled for engagement would be false the first time a
`closed_late` card shipped. So the lens keeps its general title and the
card names its own axis: `measure_label` is short, names WHAT was counted
and never the direction ("Speaking turns", "Closing late"), because
direction already rides `direction` and a grading label would put a
verdict on a neutral measure. It is ENGLISH and it is a FALLBACK: the
client localizes off `measure_key`, which is stable and present on cards
derived before this field existed, and renders `measure_label` only for a
key it does not recognise yet, so a new measure is never invisible on an
older client. Serving the words alone would put English on a Spanish
card.

**`valence` keeps neutral measures neutral.** `closed_late` has an
unflattering direction; speaking more, or asking fewer questions, does
not, and the compass spec is explicit that neither end of those axes is
good or bad. A neutral measure gets `up`/`down` and must not be tinted as
a judgement; the parse rejects "declined", "slipped", "disengaged" on
one, and a test pins that the same word is still allowed on a measure
entitled to say it.

**`supersedes` is served rather than inferred**, so the hero and a lens
built on the same arithmetic never render twice on one screen. Empty list
means nothing is superseded, which is not the same as absent.

**Hysteresis (ruled 2026-08-26).** The entry floors (40 percent relative
change on a rate, 20 points on a proportion, 5 counted meetings per
window) are what a card needs to APPEAR. A card that is live is judged
at lower HOLD floors (25 percent, 12 points, 4 counted) and only comes
down when even those fail. Why: on 2026-08-25 one new meeting slid every
window by one, Suresh's change landed at 39 percent and Pallavi's recent
window at 4 counted, and both cards vanished the day after they were
made, which reads as the system changing its mind rather than as the
sample moving. Structural gates (span, meetings per window, instances
behind an unflattering claim) never soften: they decide whether a
comparison exists, not how large it is. The sentence is regenerated from
the NEW numbers whenever the inputs move, so a held card never states
arithmetic the current windows do not support. A live card that fails
the hold floor is archived with `archive_cause = "lapsed"` (nothing
succeeds it, so not `replaced`), and lapsed is NOT a durable no: the
worker skips lapsed rows the way it skips replaced ones, and the person
earns a new card at the entry floor. Ranking is unchanged, so a held
card still yields to a different measure whose change is larger; it only
stops yielding to nothing. Constants: `HOLD_RATE_RELATIVE_CHANGE`,
`HOLD_GAP_POINTS`, `HOLD_WINDOW_DENOMINATOR` in `services/trajectory.py`.

### `working_with` — the coaching screen, and it is NARROWER than the design

The design's move cards want a "why it works" backed by observation
counts, and its own worked example needs proposal AUTHORSHIP, which
nothing on any hop records: `PersonCommitment` carries an owner, not a
proposer. The wrong branch is to keep the sentence shape and attach the
counts we DO have, which produces a non sequitur that reads as evidence
because there are numbers in it.

So the split, which is the whole design:

- **THE SITUATION IS OBSERVED.** Counts, subject, receipts, this person.
- **THE TECHNIQUE IS NOT.** General communication practice, offered
  BECAUSE OF the situation, never claimed to work ON this person.

`basis` carries that seam on the wire in two places, because a client
cannot infer it and a docstring does not travel. **There is deliberately
NO field that could hold "why this fits this person"**: nothing observed
could fill it and a served field invites a value. A test asserts the
absence.

```
"working_with": {
  "person": <string>,
  "moves": [ {                      # 0 to 3, one per technique
    "rank": <int>,
    "context": <=40, "headline": <=90, "say": <=220,
    "technique": "calibrated_question"|"accusation_audit"|"labelling"|"framing",
    "technique_label": <string>, "why": <string>,
    "basis": "general_practice",
    "situation": { "basis": "observed", "key": <string>,
                   "numerator","denominator","meetings",
                   "subject", "patch_ids"[] }
  } ],
  "your_half": { "basis": "observed", "stats": [ {
      "key","label","value","counterpart_label","counterpart_value","meetings"
  } ] }
}
```

The technique is chosen BY CODE from a fixed map, never by the model: a
model handed five facts and four techniques finds a reason for any
pairing and is fluent about it. Ranking is on EVIDENCE, not severity,
because severity puts the flimsiest most alarming thing at the top of a
screen whose job is to be actionable. One move per technique.

**A `say` may not RESTATE THE MEASUREMENT** (both of the situation's
numbers), because a script is a sentence said out loud to a colleague and
"you have gone quiet on 23 of your 46 open items" is accurate, checkable
and would end a working relationship. It MAY carry other numbers: the
first version of this rule banned any digit, which also bans the
reference design's own best script ("One blocker, one decision, 90
seconds"). A rule that is wrong in the safe direction still degrades,
because whoever hits it loosens it in whatever direction makes their case
pass rather than the direction that keeps the real prohibition.

**`your_half` replaces the design's unsourceable stat pair** with turns
and EXPLICIT question counts. NULL IS UNKNOWN, NEVER ZERO: migration 34
has no backfill because the transcripts are gone, so a null turn count
rendered as 0 says somebody sat silent through eight meetings, which is a
claim about a person built out of a missing column. Explicit and inferred
question columns are never summed (migration 37), and the labels say "by
name" because that is what the explicit column counts.

### Model

`CQ_TRAJECTORY_MODEL`. Measured 2026-08-22, 30 calls per model on
identical computed facts, writer varied: Sonnet 4.6 accepted 25/30 and
all 25 on the first attempt; Haiku 4.5 accepted 21/30 at 3.7x cheaper AND
shipped a unit conflation ("11 of the last 11 MEETINGS" on an ITEMS
denominator, where both 11s are permitted numbers so the invented-number
check is blind) plus forbidden do-line preambles on 4 of 21 accepted
cards. Sonnet: 0 and 0. **The recommendation rests on the invisible-error
gap, not the accept gap.** Both defect classes are now parse defects, so
they are caught whichever model runs.

## 5.16 `CONTESTED_NAME` (409): the wire contract (2026-08-23)

Written because ShoulderSurf asked to build against a file rather than a
message, which is the same reason 5.15 exists.

### Which routes return it

Both callers of `_resolve_or_create_person`:

- `POST /v1/people/{user_id}`
- **`POST /v1/quilt/{user_id}/reassign-speaker`** — the speaker-labelling
  path SS uses via `to_name`. This is the one that matters in practice;
  SS does not call the first at all.

### The body

```json
{
  "detail": {
    "code": "CONTESTED_NAME",
    "message": "<human sentence, varies by reason>",
    "name": "<the name the caller typed>",
    "reason": "bare_first_name",          // ONLY on the bare-first-name case
    "candidates": [ {
        "entity_id": "<uuid>",
        "name": "<stored display name>",
        "meetings": <int>,
        "last_met": "<ISO date or null>",
        "projects": ["<project_id>", ...],
        "present": <bool>,   // any presence-grade appearance (speaker, ownership, pre-31 unknown)
        "matched_by": "alias" | "name"
    } ],
    "total": <int>,       // counted BEFORE the cap
    "truncated": <bool>
  }
}
```

`total` is counted before the cap so a long tail is visible as a number
rather than silently dropped, the same reason `/v1/quilt` counts before it
truncates.

**Ranking is CQ's, and the client renders served order** (SS confirmed
2026-08-26 that `ContestedName.swift` neither sorts nor re-sorts). The
order, most significant first: `present` before mention-only (ruled
2026-08-26 after the live "Sam" run offered Sam Altman, an April article
with no meetings, beside Sam Wisco with one), then a project the meeting
being labelled belongs to, then most recently met, then most meetings,
then name. Mention-only people STAY in the list; present-first is an
ordering, never an exclusion. `present` uses the SAME predicate as
`people_signals.is_presence_grade` (in SQL, inside `_name_candidates`) so
the picker never calls someone "been here" whom the cadence never
counted.

### The three cases that raise it

| when | `reason` | candidates |
|---|---|---|
| the typed name matches **several** people | absent | all of them, ranked |
| the typed name matches **one** person, structurally, and is LONGER than that person's stored name | absent | the one |
| the typed name **exactly** matches an existing person and is a **single token** | `bare_first_name` | **every** person sharing that first token, not only the exact one |

The third is the two-Johns case. `person_candidates` short-circuits on an
exact token match and would offer a list of one while a second John sits
on the roster, which is the right answer to "who is named exactly this"
and the wrong answer to "who could this mean", so that path uses
`_name_candidates(all_sharing_first_token=True)` instead.

### What does NOT raise it

- an exact match at two or more tokens ("John Kirker" onto "John Kirker")
- a single structural candidate where the typed name has TWO OR MORE
  tokens and is SHORTER than the match ("Suresh M" onto "Suresh
  Muchakurti") — shorthand for somebody the system already knows more
  about
- a single candidate matched by a **user-authored alias** (`entity_aliases.source`
  in `user_edit`, `user_confirmation`), which is a question the user
  already answered
- anything with `create_new: true` (except the bare-taken case, which is
  `NAME_TAKEN` below)

### A bare first name always asks (ruled 2026-08-23)

Scott labelled a speaker "christina"; the roster held one "Christina
McAlpin" (13 meetings) with a recorded alias "Christina"; it resolved
with no prompt. Two rules produced that, and both were changed:

1. **The shorthand exemption** (#312: typed shorter than the match
   resolves) was written when SS had no 409 handler and every ask
   surfaced as "Memories could not be updated". With the picker live an
   ask is one tap on "Christina McAlpin · 13 meetings", and a first name
   alone is never sure which Christina. Now a single-token typed name
   with a single name-matched candidate ASKS (one candidate in the
   payload, same shape). Multi-token shorthand keeps the direction rule.
2. **The alias exemption** assumed an alias is the user's prior answer.
   On Scott's roster 128 of 145 alias rows were written by
   `merge_backfill` or `heuristic`. Now `matched_by: "alias"` is emitted
   only for user-authored rows; a machine alias still FINDS the person
   (it is a hit) but is reported as `"name"` and so asks.

Cost accepted by Scott: one extra tap per bare-name label.

### How the caller answers it

Retry with either `to_person_id` (or `entity_id`) set to the chosen
candidate, or `create_new: true` to assert this is somebody new. CQ
records what the caller says; it never hard-requires a distinguishing
surname, because refusing to record a real colleague for the sake of a
tidier graph is the wrong trade and sometimes you genuinely only know
"Mike".

### What `create_new` does on the bare-first-name case (2026-08-23, PR #315 then #316)

Found by checking CQ's half against SS's picker mechanism before the
first real 409: the flag skipped the ask and then fell into the
exact-match resolve, so "Someone new" for a second John landed on the
first John. The unit mirror had asserted "create" the whole time.

The first fix (#315) nulled the exact hit so a second entity named
"John" would be created. A smoke on the deployed build, in a rolled-back
transaction, hit `entities_user_id_name_entity_type_key`: **CQ cannot
hold two people with the same exact name**, and that uniqueness is
load-bearing (the ingest worker upserts entities on it; rename checks
collisions on it). Source-reading tests could not see a constraint.

**Scott's ruling, 2026-08-23: B, ask for more name** rather than drop
the uniqueness ingest depends on. **Extended 2026-08-26, "strict
everywhere":** the live labelling prompt on the device already refused a
bare first name whenever ANY roster person shared its first token
(`LiveLabelResolver.isDistinctEnough`), while this server, and so Review
> Rename, only refused a literal whole-name hit; two doors gave two
answers on a bare "Christina" beside a "Christina Lee". Now `create_new`
with a single-token name returns `NAME_TAKEN` when the name is literally
taken OR when any live person (mention-only included) shares its first
token; an uncollided bare name still creates. The message says which
("already someone's exact name" vs "Others are also called 'Christina'"),
the code and the payload are the same:

```json
{ "detail": {
    "code": "NAME_TAKEN",
    "message": "'John' is already someone's exact name. Add a last name or a nickname to record a different person.",
    "name": "John",
    "reason": "bare_first_name",
    "collision": "exact_name" | "first_name",   // which refusal; the client's alert branches on it
    "candidates": [ ...same shape and same first-token set as CONTESTED_NAME... ],
    "total": <int>, "truncated": <bool>
} }
```

Same family as `CONTESTED_NAME`, different code because this one needs
TEXT from the user, not a pick. SS hides "Someone new" on
`reason == "bare_first_name"` and (post ruling) puts a text field behind
it; the retry is `to_name: "John Smith", create_new: true`.

**When a `create_new` request CREATES** (the surname retry, or "Someone
new" on the two structural cases, where the typed name is nobody's exact
name), CQ:

1. creates the entity,
2. records a Keep separate (`entity_separations`) between it and
   **every person sharing the first token**, which is the list both
   409s carried, because "Someone new" is the user answering "none of
   these" and the merge endpoint must refuse to fold them later,
3. echoes those ids as `separated_from`.

```json
// reassign-speaker 200, inside the existing object
"resolved_person": {
  "entity_id": "<new uuid>", "name": "John Smith", "patch_id": "...",
  "status": "created",
  "separated_from": ["<uuid>", "..."]     // [] unless create_new created
}
// POST /v1/people 200, top level
{ "status": "created", "entity_id": "...", "patch_id": "...", "name": "John Smith",
  "separated_from": ["<uuid>", "..."] }
```

Why it is echoed rather than only stored: SS's duplicate scan vetoes a
proposed pair from a LOCAL record (`peopleDismissedMergePairs`,
UserDefaults, per device) and consults no CQ read surface, so a
separation stored only server-side is invisible to the client and the
list would ask "are these the same John?" right after the user said
no. The echo lets SS record the veto from the same call; the server row
makes the merge refuse. Both halves on the hop that can prove them.
Known limit, not made worse here: the client veto is per device.
`entity_separations` is still not served on any read surface; a
`separations` array on the list read is the durable fix if that ever
matters.

### 5.17 Live speaker labels: `metadata.speaker_identities` (2026-08-23)

Scott's first device test of the picker got no prompt, because labelling
a speaker in the LIVE recording view (the primary path) never calls
reassign-speaker: the typed name rides inside the capture POST as the
bracketed `[label]` in the transcript, and ingest then resolved a bare
"christina" through any recorded alias (LIMIT 1, any source) or the
unique-candidate heuristic, silently. The 1b contested guard fires only
when two people could match, and after a silent absorption there is
never a second. **Scott ruled: the question is asked live, on the
device.** There is no CQ meeting to ask yet, so the client asks from its
cached People roster (the one place the client builds the list; its
predicate mirrors CQ's: single token, every roster person sharing the
first token, "Someone new" with the last-name field under the NAME_TAKEN
rule), records the answer per label, and sends it on the capture:

```json
"metadata": { "speaker_identities": [
  {"label": "christina", "entity_id": "<uuid>"},
  {"label": "Speaker 2", "create_new": true, "name": "Christina Lopez"}
] }
```

**CQ's half is a REWRITE, not a second resolution path**
(`services/speaker_identities.py`, worker `_apply_speaker_identities`,
run after `normalize_owner_in_transcript` and before the LLM call):
`[label]` / `[label (you)]` becomes `[Canonical Name]` /
`[Canonical Name (you)]`, case-insensitive on the label. After that the
canonical name is what the model reads, what `store_entities` hits on its
exact match (step 1, before any alias or heuristic), what `value.owner`
carries, what the appearance row is written for and what turn counts key
on. The server never holds the bare label, so the device's local map and
CQ agree by construction. `entity_id` follows `merged_into` forward.
`create_new` follows 5.16's rules: an exact match on the stored name
resolves (a bare taken name resolves to the exact match and logs
`speaker_identity_bare_name_taken`, because a map cannot be asked);
otherwise the person is created (`confirmation_source='speaker_identity'`)
and Keep separate is stamped against every live person sharing the first
token (`entity_separations.source='speaker_identity'`). Re-ingest is
idempotent (19.4): the fuller name exists the second time and resolves.
A malformed entry, an unknown `entity_id`, or any failure leaves that
label untouched for today's matching and logs
`speaker_identity_unresolved`; a bad map never loses a meeting.
`speaker_identities_applied` logs each label with its replacement count,
so a key the transcript never used is visible as 0.

Reading the log after a run: `speaker_identities_applied` (`applied`,
`sent`) is a SUMMARY and cannot tell an unresolvable entry from a lost
one; the per-label `speaker_identity_unresolved` line is the receipt, so
keep exactly one per label that did not apply (GP, 2026-08-23).

Not served: a label -> entity_id map for the meeting. The canonical name
in every stored artefact plus the person's appearance for that
`origin_id` is the receipt; add a served map only if a client needs it.
GP must allowlist `metadata.speaker_identities` (rule 3); prove it on
GP's proxied path, not only CQ's socket.

### 5.18 `last_seen_in` on the People list (2026-08-23)

Scott's live test: labelling "Sam" offered "Sam Altman · 0 meetings" and
"Sam Wisco · 1 meeting", and what would have settled it is where Sam
Wisco was last seen. `top_project` cannot: it is presence-grade
(speaker/ownership) and Sam Wisco was only mentioned, so it is null for
exactly the person the badge is for. Every list row now carries

```json
"last_seen_in": {"project_id": "...", "project": "Agent Utilization",
                 "origin_id": "<meeting>", "last_seen_at": "<ISO>",
                 "capacities": ["mention"]}   // or null: no appearances
```

the newest appearance in ANY capacity, capacity stated, because
"mentioned in X" and "spoke in X" are different claims (5.13).
`project`/`project_id` null when that meeting had no project; the object
is still served. Mention-only people who predate migration 31 have no
appearance rows and get null, which is the same "0 meetings" the list
already shows. Same row object on list and detail. Intended render: a
badge under the candidate in the live and post-save pickers.

### 5.19 `separated_from` on every People row (2026-08-24)

Lost-phone recovery: SS's merge-proposal veto was a device-only
UserDefaults cache of a ruling CQ already holds in `entity_separations`.
Every list and detail row now carries `separated_from: [entity_id, ...]`
(sorted; `[]` when nothing was ruled; both directions of each pair), so
the client cache is derived and a fresh phone never re-proposes a pair
the user refused. Degrades to `[]` on a DB without the table.

### Known client gap as of 2026-08-23

**SS has no handler.** A 409 falls through `reassignSpeaker`'s switch into
`.httpFailure`, the transcript rename stands locally, and the user sees
"Renamed here. Memories could not be updated." Not destructive, and every
colliding bare-first-name label is silently unsynced until the picker
ships. Recorded here rather than only in a thread so the next person
reading this file knows the contract is served and unanswered.
