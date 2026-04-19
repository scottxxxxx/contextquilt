# ShoulderSurf Team — Work You Can Start Now, In Parallel

**Context:** While the CQ backend builds PR 1 (schema registration API + cross-app generalizations), the SS iOS team has substantial work they can start today against stable contracts.

**Date:** 2026-04-18
**Status:** Locked design — safe to build against.

---

## What's locked (will not change)

### The 13 patch types SS uses

See `init-db/05_shouldersurf_schema.json` for the authoritative definitions. Summary:

| Type | Facet | Permanence | Completable | What it captures |
|---|---|---|---|---|
| `trait` | Attribute | year | no | Self-disclosed user behavior pattern |
| `preference` | Affinity | year | no | User's lean/value/style |
| `goal` | Intention | year | no | Forward-looking target |
| `constraint` | Constraint | year | no | Hard rule, limit, boundary |
| `person` | Connection | decade | no | Named individual |
| `org` | Connection | decade | no | Any organizational entity |
| `project` | Connection | quarter | no | Any unit of ongoing work |
| `role` | Connection | year | no | Person's function on a project |
| `decision` | Episode | year | no | A call that was made |
| `commitment` | Episode | month | **yes** | Promise with a named owner |
| `blocker` | Episode | week | **yes** | Current impediment to progress |
| `takeaway` | Episode | week | no | Evaluative lesson |
| `event` | Episode | quarter | no | External occurrence affecting a project |

### The 10 connection labels

| Label | Role | From → To |
|---|---|---|
| `belongs_to` | parent | any scoped child → project (recursive for Connection→Connection) |
| `works_on` | informs | person → project |
| `owns` | informs | person → commitment/blocker/decision/goal |
| `blocked_by` | depends_on | commitment → blocker |
| `motivated_by` | informs | decision → preference/takeaway |
| `aims_for` | informs | commitment/decision/project → goal |
| `bound_by` | informs | commitment/decision/project → constraint |
| `describes` | informs | role → person |
| `member_of` | informs | person → org |
| `reports_to` | informs | person → person |

### The 7 permanence classes

`permanent` → `decade` → `year` → `quarter` → `month` → `week` → `day`

### Origin types SS registers

`meeting`, `imported_recording`, `typed_note`

---

## Work you can start immediately

### 1. Memory browser UI

Users will accumulate hundreds of patches. Build:

- **List view** grouped by facet (About Me / Your World / What You're Working On / Recent Activity)
- **Filter by type** (commitments, blockers, etc.)
- **Search** (text-based, across patch content)
- **Sort** (recency, permanence, relevance)
- **Project/container view** — show all patches `belongs_to` a given project in a nested tree (since `belongs_to` is now recursive, show hierarchy)

The patch shape is stable. Build against mocked data.

### 2. Patch editor UI

When a user views or edits an individual patch:

- Show patch text, owner (if applicable), deadline (if applicable)
- Show connections: who/what this patch is connected to, with labels like "belongs to Benefits App MVP" / "owned by Ravikanth"
- Allow editing text, owner, deadline
- **Connection editor** — use the filtered matrix (see below) to constrain which labels are available for a given source type

### 3. Connection picker (the filtered matrix)

When the user adds a connection to a patch, filter the available labels by the source patch's type:

| If source is... | Show only these labels |
|---|---|
| `commitment` | belongs_to, owns (inverse), blocked_by, aims_for, bound_by |
| `blocker` | belongs_to, owns (inverse) |
| `decision` | belongs_to, owns (inverse), motivated_by, aims_for, bound_by |
| `goal` | belongs_to |
| `constraint` | belongs_to |
| `takeaway` | belongs_to |
| `event` | belongs_to |
| `role` | belongs_to (to project), describes (to person) — both required |
| `person` | works_on, owns, member_of, reports_to |
| `project` | aims_for, bound_by, belongs_to (can now nest under another project) |

Labels that make no sense for the source type are hidden entirely.

### 4. Permanence override UX

Build a simple three-choice control on each patch:

- **Keep forever** → sets `permanence_override = "permanent"`
- **Remember this** → sets `permanence_override = "year"`
- **Let it fade naturally** → clears override, returns to type default

Ship this as an opt-in "Pin this memory" affordance, not a default prompt. Most patches should use the type default.

### 5. Onboarding flow

First-time users need to understand the memory model without jargon. Sketch:

- "ShoulderSurf builds memory from your meetings"
- "It captures 13 kinds of things — people, projects, decisions, action items, what you're working toward, what's getting in the way"
- "You can browse, edit, or remove anything"
- Keep facet and permanence terminology out of the user-facing copy. "Trait" and "preference" are fine — those are intuitive.

### 6. Meeting capture pipeline (iOS side)

Independent of CQ — record audio, transcribe, diarize, upload. The only contract with CQ is:
- POST transcript + `origin_type: "meeting"` + `origin_id: <your meeting UUID>`
- CQ handles extraction and storage

### 7. Delta sync logic

Already works: `GET /v1/quilt/{user_id}?since=ISO8601` returns patches created/updated/deleted since last sync. Build against this contract.

### 8. Rename-speaker flow

SS-specific feature: when user renames "Speaker 4" → "SriDev", call `POST /v1/quilt/{user_id}/rename-speaker`. Already supported. Ship UI for this.

### 9. Offline/local caching

Whatever iOS-native caching layer you want. Purely app-side. No coordination needed.

### 10. Privacy / data export / delete

Users will want to export all their memory or delete specific patches. Build against:
- `GET /v1/quilt/{user_id}` (full export)
- `DELETE /v1/quilt/{user_id}/patches/{patch_id}` (targeted delete)

---

## API contracts you can build against

All stable. These endpoints either exist or will be delivered in PR 1.

| Endpoint | Purpose | Status |
|---|---|---|
| `POST /v1/memory` | Ingest a transcript for extraction | Exists |
| `POST /v1/recall` | Query-time context retrieval | Exists; getting smarter in PR 4 |
| `GET /v1/quilt/{user_id}` | Full memory browse | Exists |
| `GET /v1/quilt/{user_id}?since=T` | Delta sync | Exists |
| `PATCH /v1/quilt/{user_id}/patches/{id}` | Edit a patch | Exists |
| `DELETE /v1/quilt/{user_id}/patches/{id}` | Delete a patch | Exists |
| `POST /v1/quilt/{user_id}/rename-speaker` | Rename a speaker | Exists |
| `GET /v1/projects/{user_id}` | List container projects | Exists; may consolidate into `/quilt` API in v2 |

**What's changing in PR 1 (breaking where noted):**

- **Patches will have `origin_type` and `origin_id` fields** (replacing `meeting_id`). Existing SS payloads should add `origin_type: "meeting"` to all patch POSTs. The rename is non-breaking if you add the new fields; `meeting_id` will be deprecated with a grace period.
- **Patches can optionally have `about_patch_id`** (nullable) — SS likely never uses this, but it's additive.
- **Patches can optionally have `permanence_override`** (nullable) — only set when user pins a patch.
- **`belongs_to` is now recursive** — a project can `belongs_to` another project. Your project-hierarchy UI can rely on this.

---

## What you DON'T need to decide yet

- Query-scoped recall format — PR 4 concern. Current recall output works for now.
- Extraction prompt tuning — CQ handles this.
- Schema registration — CQ will bootstrap SS's schema automatically on deploy.

---

## What we need from you in return

A few things that will unblock downstream work:

1. **Confirm the 3 origin_types are right.** Is `imported_recording` a real flow? Is `typed_note` how you'd want a user to hand-enter a memory? Or do you have other input flows in mind?
2. **Sanity-check the 13 types** for intuitive UI copy. Does "event" read well in the iOS interface? Or do we need friendlier names? Types are the schema identifier; the display name can be whatever you want.
3. **Flag any iOS-specific constraints** that affect CQ. Apple's diarization models? Transcript chunking? Privacy requirements around data export?
4. **Target app-store ship date.** Helps us prioritize PR sequence — if you're 8 weeks out, PR 4 (query-scoped recall) should be live in time.

---

## What to read first

If you only read one doc: **`docs/memos/patch-taxonomy-simplification.md`** — the v1 design and why.

For the full architecture: **`docs/design/app-schema-registration.md`** — how SS sits on top of the generic CQ primitive.

For the test evidence behind the design: **`tests/benchmark/taxonomy_validation/SUMMARY.md`**.

For SS's specific manifest: **`init-db/05_shouldersurf_schema.json`**.

---

## One-sentence summary you can tell your team

> *"CQ is building a generic memory primitive; SS registers a 13-type schema against it; our UI, ingestion pipeline, and all iOS-native work can proceed against stable contracts starting today — we only need CQ to be ready for the schema registration endpoint and the origin_id/about_patch_id additions, which are in PR 1."*
