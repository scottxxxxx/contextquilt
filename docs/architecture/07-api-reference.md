# 07: API Reference

## Authentication

All API calls require one of:
- **Bearer token** — JWT obtained via `/v1/auth/token`
- **X-App-ID header** — Legacy mode, app ID string

### Register an Application

```
POST /v1/auth/register
Content-Type: application/json

{"app_name": "my-app"}
```

**Response:** `{"app_id": "uuid", "app_name": "my-app", "client_secret": "one-time-secret", "created_at": "..."}`

Save the `client_secret` — it's only returned once.

### Get Access Token

```
POST /v1/auth/token
Content-Type: application/x-www-form-urlencoded

username={app_id}&password={client_secret}
```

**Response:** `{"access_token": "jwt...", "token_type": "bearer", "expires_in": 3600}`

---

## Memory Ingest (Write Path)

### POST /v1/memory

Queue content for cold path processing. Returns immediately.

```json
{
  "user_id": "string (required)",
  "interaction_type": "summary | query | sentiment | tool_call | trace | chat_log | meeting_summary",
  "content": "string — the main text content",
  "summary": "string — meeting summary text (for meeting_summary type)",
  "response": "string — LLM response to include (optional, for query type)",
  "metadata": {
    "meeting_id": "grouping key (optional)",
    "project": "project name (optional)",
    "any_key": "any_value"
  },
  "fact": "string — explicit fact (for tool_call type)",
  "category": "identity | preference | trait | experience (for tool_call type)"
}
```

**Response:** `{"status": "queued", "message": "Memory update received for async processing"}`

---

## Recall (Read Path)

### POST /v1/recall

Send text, get relevant context back. No LLM call — fast graph traversal.

```json
{
  "user_id": "string (required)",
  "text": "string — the query or transcript text to match against",
  "metadata": {
    "project": "optional hint to narrow recall"
  }
}
```

**Response:**
```json
{
  "context": "formatted text block with relevant facts and relationships",
  "matched_entities": ["entity names found in the text"],
  "patch_count": 9
}
```

### POST /v1/enrich

Template-based context injection with explicit placeholders.

```json
{
  "user_id": "string (required)",
  "template": "The user prefers [[communication_style|concise]] responses about [[current_project]].",
  "format": "text | json"
}
```

**Response (text):**
```json
{
  "enriched_prompt": "The user prefers concise responses about Widget 2.0.",
  "used_variables": ["communication_style (default)", "current_project"],
  "missing_variables": []
}
```

### GET /v1/profile/{user_id}

Retrieve the user's hydrated profile from cache.

**Query params:** `?keys=key1,key2` — filter to specific keys

**Response:** `{"variables": {"key": "value", ...}, "last_updated": "..."}`

---

## User Quilt (CRUD)

### POST /v1/memory — interaction_type "correction"

User corrections from chat (context-flow contract item 9). Body: `user_id`, `interaction_type: "correction"`, `content` (the user's correction text, question portion only — never the model's response), `metadata` with project scope, and optional `context_block` (the recall block that was injected when the user corrected it; candidate matching prefers what the user was looking at; never persisted). Async like all captures: returns queued; the worker supersedes the contradicted patch (archived + `replaces` connection from the new `declared` patch) seconds later, and delta sync converges devices. Acknowledgment wording for clients: "noted, updating the record" — queued is not applied.

### POST /v1/memory — interaction_type "completion"

Chat completions (contract item 10, the sibling of corrections): the user says something is done. Body mirrors corrections: `content` (the user's statement), `metadata` scope, optional `context_block`. The worker matches OPEN completables only and closes via the standard machinery (`completed_at`, `completion_source: "user_chat"`, the statement as evidence) — the patch flows the delta `completed` array like tap-to-complete. Unmatched completions are dropped, never stored. Same acknowledgment wording: "noted, updating the record".

### GET /v1/quilt/{user_id}

View all facts and action items CQ knows about a user. This is the sync + dossier surface (the recall endpoint is the ranked prompt-injection surface — different tools for different jobs).

**Query params:**
- `category=<patch_type>` — filter by patch type
- `since=<ISO 8601>` — delta sync: only patches created/updated after this time, plus `deleted` (all removals) and `completed` (resolved subset) id arrays. Use the returned `server_time` as the next `since`.
- `origin_id=<meeting UUID>` — meeting view: that meeting's patches in capture order (no ranking)
- `group_by=origin` — adds a `meetings` array grouping origin-anchored patches by meeting (newest meeting first, capture order inside). Flat arrays unchanged.
- `project_id=<stable project id>` — project rundown view (context-flow contract): only patches carrying this id. Combine with `group_by=origin` for a complete per-meeting project dossier. Only returns what ingest stamped — meetings ingested without project metadata won't appear.
- `limit=<1..500>` — cap the patch set after ordering, for prompt-injection callers

**Response shape (stable — the gateway builds its injection formatter against this):**
```json
{
  "user_id": "string",
  "facts":        [ /* QuiltPatch — non-actionable patches */ ],
  "action_items": [ /* QuiltPatch — commitments/blockers */ ],
  "deleted":   ["patch_id"],
  "completed": ["patch_id"],
  "meetings":  [ { "origin_id": "uuid", "origin_type": "meeting", "patches": [ /* QuiltPatch */ ] } ],
  "server_time": "ISO 8601"
}
```

**QuiltPatch fields:** `patch_id`, `fact` (the text), `category`, `patch_type`, `participants[]`, `owner`, `deadline` (as spoken), `deadline_date` (YYYY-MM-DD when resolved), `source`, `created_at`, `project`, `project_id`, `origin_id`, `origin_type`, `permanence_override`, `permanence_override_source`, `connections[]` (`{to_patch_id, role, label, context}`).

### POST /v1/quilt/{user_id}/patches

Create a patch manually. Origin is `declared` (user-created, not extracted). Returns the new `patch_id` so callers can immediately create connections.

```json
{
  "type": "person",
  "text": "Maria Chen — design lead for the rebrand",
  "connections": [
    {"target_patch_id": "uuid-of-project", "role": "informs", "label": "works_on"}
  ]
}
```

**Response:** `{"status": "created", "patch_id": "uuid", "type": "person", "connections": [...]}`

### PATCH /v1/quilt/{user_id}/patches/{patch_id}

Correct a fact. Changes `origin_mode` to `declared` (user-verified).

```json
{
  "fact": "corrected text (optional)",
  "category": "new category (optional)"
}
```

**Response:** `{"status": "updated", "patch_id": "uuid"}`

### DELETE /v1/quilt/{user_id}/patches/{patch_id}

Remove a fact or action item.

**Response:** `{"status": "deleted", "patch_id": "uuid"}`

### POST /v1/origins/{user_id}/{origin_type}/{origin_id}/unassign-project

Clear project scope from one origin's patches — the mirror of assign-project (context-flow contract item 2). Optional body `{"project_id": "..."}` clears only patches currently scoped to that project (recommended: protects a meeting that was since reassigned). Returns `patches_updated`.

### POST /v1/projects/{user_id}/{project_id}/unscope

Project-deletion form: clears project scope from ALL of a user's patches carrying this project_id. Patches survive as unscoped memory — deleting a project container never deletes what was learned. Returns `patches_updated`.

### GET /v1/quilt/{user_id}/graph (REMOVED 2026-08-17)

Rendered the whole quilt as one force-directed graphviz image on the
request path. Deleted, along with the `graphviz` dependency and the
pango/cairo/gd stack the Dockerfile pulled in for it.

**Why, measured on prod before removal:** 3,550 nodes and 6,180 edges
took **60.3 seconds**, of which the database was 91ms and the `sfdp`
layout was 60.2s. Every caller timed out well before that, so CQ logged
`200 OK` while the device showed `504`, and the 6MB SVG was never once
delivered to a user. It also ran `dot.pipe()`, a blocking subprocess,
inside an `async def` with no thread offload, freezing one of four
uvicorn event loops and pegging one of the host's two cores for the
duration, next to a recall path budgeted in single-digit milliseconds.

It could not have been fixed by tuning. A tenfold speedup is still six
seconds for an image of 3,550 labels on a phone screen, which is a
texture rather than information.

**If you want a graph, the working pattern is
`GET /v1/people/{user_id}/network`:** the worker computes the snapshot
and the read path serves stored bytes, so nothing expensive happens
while a client is waiting. Scope any future graph to one project or one
person's neighborhood, where the node count is in the tens.

---

## Operations

### GET /health

Health check.

**Response:** `{"status": "healthy", "version": "3.9.0"}`

### POST /v1/prewarm

Trigger cache hydration for a user (pre-load their context into Redis).

**Body:** `{"user_id": "string"}`

**Response:** `{"status": "queued", "message": "Hydration requested"}`

---

## Admin Dashboard API

All `/api/dashboard/*` endpoints require the `X-Admin-Key` header.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/dashboard/verify-key` | GET | Verify admin key |
| `/api/dashboard/stats` | GET | Total users and facts count |
| `/api/dashboard/patches/recent` | GET | Recent patches with filters |
| `/api/dashboard/patches/history` | GET | Patch creation over time |
| `/api/dashboard/patches/distribution` | GET | Patch type/origin distribution |
| `/api/dashboard/users` | GET | User list with patch counts |
| `/api/dashboard/users/{user_id}/quilt` | GET | Full quilt for a user |
| `/api/dashboard/prompts` | GET | System prompts (active versions) |
| `/api/dashboard/prompts/{key}` | PUT | Update a system prompt |
| `/api/dashboard/schema` | GET | Memory schema definitions |
| `/api/dashboard/schema/candidates` | GET | Auto-discovered variables |
| `/api/dashboard/test-pipeline` | POST | Dry-run extraction (SSE streaming) |
