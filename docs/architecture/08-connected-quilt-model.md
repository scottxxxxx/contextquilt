# Connected Quilt Data Model

## Overview

The quilt is a connected graph of typed patches rooted on a person. Each patch has a type that defines its shape and lifecycle, and patches are connected via typed edges that carry both structural meaning (for CQ's automation) and semantic meaning (for the app's domain).

## Design Principles

1. **The quilt is a person** — everything hangs off the user as the root
2. **Patches are typed** — each type has persistence rules, TTL, and whether it's completable
3. **Patches are connected** — directed edges with a structural role (CQ uses) and semantic label (app defines)
4. **Types are extensible** — apps can register custom types; CQ provides 11 built-in types
5. **Connections drive lifecycle** — project archival cascades to children, replaces auto-archives
6. **CQ doesn't editorialize** — any topic the user records is a valid project

## Patch Types (Built-In)

All 11 types are registered in `patch_type_registry` with `app_id = NULL` (universal).

| type_key | persistence | TTL (days) | completable | project_scoped | description |
|----------|------------|------------|-------------|----------------|-------------|
| `trait` | sticky | never | no | no | Self-disclosed behavioral pattern (submitting user ONLY) |
| `preference` | sticky | never | no | no | What the user prefers |
| `identity` | sticky | never | no | no | Who the user is (role, org) |
| `role` | sticky | never | no | optional | Someone's function on a project |
| `person` | sticky | never | no | no | A named participant |
| `project` | sticky | never | no | no | An initiative the user tracks across sessions |
| `decision` | sticky | never | no | yes | Something agreed upon |
| `commitment` | completable | 30 | yes | yes | A promise with a named owner |
| `blocker` | completable | 30 | yes | yes | Something preventing progress |
| `takeaway` | decaying | 14 | no | yes | A notable observation, short-lived |
| `experience` | decaying | 30 | no | yes | Legacy type — general observations |

### Type Rules

- **Traits** apply ONLY to the submitting user. "Speaker 3 is meticulous" is NOT a trait.
- **Commitments** require a specific NAMED owner. "Someone should finalize the deck" (no owner) = takeaway.
- **Projects** are any topic the user tracks — CQ doesn't decide what's "real work" vs "casual."
- **Unnamed speakers** (Speaker 1, Speaker 4) must NOT become entities or person patches.

## Connections (Role + Label)

Connections are directed edges stored in `patch_connections`. Each has:
- **role** — structural, CQ uses for lifecycle automation (5 roles)
- **label** — semantic, app-defined vocabulary (unlimited)

### The Five Roles

| Role | CQ Behavior | Example Labels |
|------|------------|----------------|
| `parent` | Archive parent → cascade to children | belongs_to |
| `depends_on` | Can't complete until dependency clears | blocked_by |
| `resolves` | Completing this can satisfy target | unblocks, fulfills |
| `replaces` | Archive the old, keep the new | supersedes, updates |
| `informs` | Traversal only — no lifecycle side effects | motivated_by, works_on, owns |

### Connection Direction

Connections go FROM → TO. Direction matters:
- `person → project`: "works_on"
- `person → commitment/blocker/decision`: "owns" (person is responsible)
- `commitment/blocker/decision → project`: "belongs_to" (item is inside project)
- `commitment → blocker`: "blocked_by"
- `decision → preference`: "motivated_by"

**Never**: `commitment → person` with "owns" (backwards — the worker normalizes this automatically).

### Connection Vocabulary

Registered in `connection_vocabulary` table. Built-in labels:

| label | role | from_types | to_types |
|-------|------|-----------|----------|
| belongs_to | parent | decision, commitment, blocker, takeaway, role | project |
| works_on | informs | person | project |
| owns | informs | person | commitment, blocker, decision |
| blocked_by | depends_on | commitment | blocker |
| unblocks | resolves | blocker | commitment |
| motivated_by | informs | decision | preference, takeaway |
| supersedes | replaces | decision | decision |

## Projects & Meetings

Projects are first-class entities with stable IDs. Names can be renamed without breaking patch associations.

### Projects Table
```
projects: project_id (stable UUID from app), user_id, name (renameable), status, created_at
```

### On Patches
```
context_patches: ... project (display name), project_id (stable UUID), meeting_id (UUID)
```

### APIs
- `GET /v1/projects/{user_id}` — list projects with patch counts
- `POST /v1/projects/{user_id}` — register a project
- `PATCH /v1/projects/{user_id}/{project_id}` — rename (cascades to all patches) or archive (cascades: archives all patches inside)

### Grouping Hierarchy
```
Person (root)
 ├── Universal patches (trait, preference — no project)
 ├── Project A
 │    ├── Meeting 1 (meeting_id)
 │    │    ├── decision, commitment, blocker, takeaway
 │    │    └── person patches (works_on → project)
 │    └── Meeting 2
 │         └── ...
 └── Project B
      └── ...
```

## Lifecycle

### Decay Worker
Runs every 6 hours. Archives patches that exceed their type's TTL and haven't been accessed recently:
- Takeaways: 14 days
- Experience: 30 days
- Commitments: 30 days
- Blockers: 30 days
- Sticky types (trait, preference, decision, project, person, role): never

TTLs are configurable via `patch_type_registry.default_ttl_days`. Access-based exemption: if `patch_usage_metrics.last_accessed_at` is recent, the patch survives even past TTL.

### Lifecycle Through Connections
- **Project archival**: `PATCH /v1/projects/{user_id}/{project_id}` with `status: "archived"` → cascades to all patches with that `project_id`
- **Decision supersedes**: when a `replaces` connection is created, the target patch is auto-archived
- **Direction normalization**: worker automatically flips `commitment → owns → person` to `person → owns → commitment`

### Patch Status
- `active` — visible in quilt, included in recall
- `completed` — marked done by user (commitments, blockers)
- `archived` — hidden from quilt and recall (decayed, cascaded, or manually removed)

## Deduplication

Before creating a new patch, the worker checks for existing patches with the same type and similar text using `pg_trgm` trigram similarity:
- Threshold: SIMILARITY() > 0.6
- If found: reuses existing patch_id, bumps `access_count` and `updated_at`
- Catches: "Deliver samples in 2 days" vs "Deliver transcription samples within 2 days"

## Delta Sync (iOS)

`GET /v1/quilt/{user_id}?since=ISO8601` returns:
- `facts[]` — only patches created/updated after the timestamp
- `deleted[]` — patch_ids archived/completed since then
- `server_time` — use as `since` on next request

First call without `since` = full sync. Subsequent calls = delta only.

## Speaker Rename

When ShoulderSurf renames "Speaker 4" → "SriDev":
1. App updates patch text via `PATCH /v1/quilt/{user_id}/patches/{patch_id}`
2. App calls `POST /v1/quilt/{user_id}/rename-speaker` with `{old_name, new_name}`
3. CQ creates entity (unnamed speakers are never stored as entities) and rebuilds Redis index

## Extraction

### Prompt (V2 — Connected Quilt)
Produces `patches[]` with `connects_to[]` instead of flat `facts[]` + `action_items[]`. Entities and relationships arrays unchanged (feed the entity name index for hot-path recall).

### Hard Limits
- 12 patches per meeting
- 10 entities
- 10 relationships

### Priority Order
1. Self-disclosed traits
2. Project patches
3. Person patches for owners
4. Commitments with owners
5. Blockers
6. Decisions
7. Roles
8. Takeaways
9. Preferences

### Extraction Cost
Model: Gemini 2.5 Flash via OpenRouter (~$0.0017/extraction). Metrics tracked in `extraction_metrics` table and visible in admin dashboard.

## Recall (Hot Path)

1. Match entity names from Redis index (text search)
2. Get entity rows from Postgres
3. Recursive CTE on relationships table (entity graph)
4. Flat patch query (filtered by project, status = active)
5. Traverse `patch_connections` from project patches (parent role)
6. Merge and deduplicate
7. Format structured context block: About you, Decisions, Open commitments, Blockers, Roles, Key facts

Target: <10ms, no LLM call.
