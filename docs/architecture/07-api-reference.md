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

### GET /v1/quilt/{user_id}

View all facts and action items CQ knows about a user.

**Query params:** `?category=identity|preference|trait|experience` — filter by category

**Response:**
```json
{
  "user_id": "string",
  "facts": [
    {
      "patch_id": "uuid",
      "fact": "Bob Martinez committed to WebSocket prototype by April 5",
      "category": "experience",
      "participants": ["Bob Martinez"],
      "source": "meeting_summary",
      "created_at": "2026-03-22T19:33:02Z"
    }
  ],
  "action_items": [
    {
      "patch_id": "uuid",
      "fact": "Have WebSocket prototype ready",
      "owner": "Bob Martinez",
      "deadline": "April 5",
      "source": "meeting_summary",
      "created_at": "2026-03-22T19:33:02Z"
    }
  ]
}
```

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

### GET /v1/quilt/{user_id}/graph

Render a visual graph of a user's entire quilt — all patches and connections displayed as a colorful, quilt-like diagram. Returns an image directly.

**Query params:** `?format=svg|png` (default: `svg`)

**Response:** `image/svg+xml` or `image/png`

The graph uses a force-directed layout with:
- **Project clustering** — patches grouped into labeled sub-boxes by project
- **User centering** — the submitting user's person patch is pinned at the center
- **Color-coded types** — each patch type has a distinct color (project=blue, commitment=amber, blocker=red, decision=purple, person=green, trait=pink, preference=cyan, takeaway=lime)
- **Connection edges** — colored by structural role (parent=blue, depends_on=red, resolves=green, replaces=orange, informs=purple)

The user's person patch automatically connects to all project patches via `works_on` edges, even if the extraction didn't create them explicitly.

**Example:**
```
GET /v1/quilt/fa4d903c-24c0-45d5-9fdb-b5496e32501b/graph?format=svg
→ image/svg+xml (full quilt visualization)
```

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
