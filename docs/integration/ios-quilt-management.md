# Context Quilt — iOS Integration Guide

## What is the Quilt?

Context Quilt remembers things about a user across meetings. It extracts facts, decisions, commitments, and traits from meeting transcripts and makes them available as context in future conversations.

From the iOS app's perspective, a user's quilt is a small, manageable collection of patches — typically 15-30 at any given time. Each patch has a type and may be connected to other patches.

## What's in a User's Quilt

The quilt is organized around the user as the root. Here's what a typical quilt looks like:

```
                         ┌─────────┐
                         │   INA   │
                         │ (root)  │
                         └────┬────┘
                              │
           ┌──────────────────┼──────────────────┐
           │                  │                   │
     ┌─────┴─────┐    ┌──────┴──────┐    ┌──────┴──────┐
     │   TRAIT    │    │ PREFERENCE  │    │   ROLE      │
     │ "Tends to  │    │ "Prefers    │    │ "Product Mgr│
     │ over-      │    │  Nova 3"    │    │  at Acme"   │
     │ explain"   │    │             │    │             │
     └────────────┘    └─────────────┘    └─────────────┘
      (universal)       (universal)        (universal)

     ┌─────────────────────────────────────────────────┐
     │  PROJECT: "Florida Blue transcription"           │
     │                                                  │
     │  ├── DECISION: "Use Nova 3"                      │
     │  ├── COMMITMENT: "Deliver samples in 2 days"     │
     │  │   └── blocked_by → BLOCKER                    │
     │  ├── BLOCKER: "Waiting on Travis to upload"      │
     │  ├── PERSON: Travis (file uploads)               │
     │  ├── PERSON: Amanda (escalation)                 │
     │  └── TAKEAWAY: "No summary template yet"         │
     └─────────────────────────────────────────────────┘
```

**Universal patches** (trait, preference, role) — always present, never tied to a project. These are about who the user is.

**Project patches** — grouped inside a project container. When a project is done, everything inside it can be archived together. People patches survive since they're relationships the user has beyond any single project.

## Patch Types

| Type | Icon Suggestion | Editable? | Deletable? | Completable? | Decays? |
|------|----------------|-----------|------------|-------------|---------|
| trait | 🧠 | Yes | Yes | No | Never |
| preference | 💡 | Yes | Yes | No | Never |
| role | 👤 | Yes | Yes | No | Never |
| person | 🤝 | Yes | Yes | No | Never |
| project | 📋 | Yes | Yes | No | Never |
| decision | ✅ | Yes | Yes | No | Never |
| commitment | 🤝 | Yes | Yes | **Yes** | After deadline |
| blocker | 🚫 | Yes | Yes | **Yes** | When resolved |
| takeaway | 💭 | Yes | Yes | No | 14 days |

## API Endpoints

All calls go to `https://cq.shouldersurf.com`. Auth via GhostPour's JWT token passed as `Authorization: Bearer <token>`, or `X-App-ID` header.

### Get the User's Quilt

```
GET /v1/quilt/{user_id}
```

Optional query param: `?category=trait` to filter by type.

**Response:**

Returns all active patches for the user. The `facts` array contains every patch type (trait, preference, decision, commitment, blocker, person, project, etc.). The `action_items` array is a V1 holdover — it will be empty for new patches. Use `patch_type` to distinguish.

Each patch includes its `connections` — outgoing edges to other patches. Use `project` to group patches by project client-side.

```json
{
  "user_id": "user_abc123",
  "facts": [
    {
      "patch_id": "aaa-111",
      "fact": "Florida Blue transcription project",
      "category": "project",
      "patch_type": "project",
      "project": null,
      "source": "inferred",
      "created_at": "2026-03-25T14:32:10Z",
      "connections": []
    },
    {
      "patch_id": "aaa-222",
      "fact": "Tends to over-explain",
      "category": "trait",
      "patch_type": "trait",
      "project": null,
      "source": "inferred",
      "created_at": "2026-03-25T14:32:10Z",
      "connections": []
    },
    {
      "patch_id": "aaa-333",
      "fact": "Use Nova 3 for Florida Blue transcription",
      "category": "decision",
      "patch_type": "decision",
      "project": "Florida Blue",
      "source": "inferred",
      "created_at": "2026-03-25T14:32:10Z",
      "connections": [
        {"to_patch_id": "aaa-111", "role": "parent", "label": "belongs_to"},
        {"to_patch_id": "aaa-444", "role": "informs", "label": "motivated_by"}
      ]
    },
    {
      "patch_id": "aaa-444",
      "fact": "Prefers Nova 3 over Nova 2 for better noise handling",
      "category": "preference",
      "patch_type": "preference",
      "project": null,
      "source": "inferred",
      "created_at": "2026-03-25T14:32:10Z",
      "connections": []
    },
    {
      "patch_id": "aaa-555",
      "fact": "Deliver transcription samples within 2 days",
      "category": "commitment",
      "patch_type": "commitment",
      "owner": "Scott",
      "deadline": "2026-03-27",
      "project": "Florida Blue",
      "source": "inferred",
      "created_at": "2026-03-25T14:32:10Z",
      "connections": [
        {"to_patch_id": "aaa-111", "role": "parent", "label": "belongs_to"},
        {"to_patch_id": "aaa-666", "role": "depends_on", "label": "blocked_by"}
      ]
    },
    {
      "patch_id": "aaa-666",
      "fact": "Waiting on Travis to upload audio files",
      "category": "blocker",
      "patch_type": "blocker",
      "project": "Florida Blue",
      "source": "inferred",
      "created_at": "2026-03-25T14:32:10Z",
      "connections": [
        {"to_patch_id": "aaa-111", "role": "parent", "label": "belongs_to"}
      ]
    }
  ],
  "action_items": []
}
```

**Grouping logic for iOS:**
```swift
// Group by project
let universals = facts.filter { $0.project == nil }  // traits, preferences
let byProject = Dictionary(grouping: facts.filter { $0.project != nil }, by: \.project)
```
```

### Edit a Patch

User taps a patch and changes the text or type.

```
PATCH /v1/quilt/{user_id}/patches/{patch_id}
Content-Type: application/json

{
  "fact": "Tends to over-explain — prefers concise responses",
  "category": "trait"
}
```

Both fields are optional — send only what changed.

**Response:** `{"status": "updated", "patch_id": "..."}`

### Delete a Patch

User swipes to delete something CQ got wrong.

```
DELETE /v1/quilt/{user_id}/patches/{patch_id}
```

**Response:** `{"status": "deleted", "patch_id": "..."}`

## iOS UI Recommendations

### View: "Your Memory" / "Your Quilt"

Group patches into sections the user can scan quickly:

```
┌─────────────────────────────────────┐
│  About You                      3 ▸ │
│  🧠 Tends to over-explain          │
│  💡 Prefers Nova 3                  │
│  👤 Product Manager at Acme        │
├─────────────────────────────────────┤
│  Florida Blue                   6 ▸ │
│  ✅ Use Nova 3 for transcription    │
│  🤝 Deliver samples in 2 days      │
│  🚫 Waiting on Travis to upload    │
│  🤝 Travis — file uploads          │
│  🤝 Amanda — escalation            │
│  💭 No summary template yet        │
├─────────────────────────────────────┤
│  Action Items                   1 ▸ │
│  🤝 Deliver samples (by Mar 27)    │
└─────────────────────────────────────┘
```

**Section logic:**
- "About You" = patches where `project_id` is null and `patch_type` is trait, preference, or role
- Project sections = group by `project_id`, display using `project` (the label). Within each project, optionally sub-group by `meeting_id`.
- "Action Items" = patches where `patch_type` is commitment or blocker

### Interactions

| Gesture | Action | API Call |
|---------|--------|----------|
| Tap | View detail / edit | — |
| Edit + save | Update patch text | `PATCH /v1/quilt/{user_id}/patches/{patch_id}` |
| Swipe left | Delete | `DELETE /v1/quilt/{user_id}/patches/{patch_id}` |
| Swipe right (commitment/blocker) | Mark complete | `PATCH` with status update (future) |

### CQ Indicator in Chat

During a meeting or post-meeting chat, GhostPour returns headers indicating CQ activity:

```
X-CQ-Matched: 3
X-CQ-Entities: Travis,Florida Blue,Nova 3
```

The iOS app can show a subtle indicator on the response bubble:
- "CQ matched 3 items" or a small quilt icon
- Tapping it could show which entities were matched

### What the User Should Know

The quilt is **transparent**. Users should understand:
- CQ learns from their meetings automatically
- They can see everything CQ knows about them
- They can edit or delete anything
- Traits and preferences travel across all meetings
- Project-specific patches stay scoped to that project
- Old projects and completed tasks naturally fade away

### Quilt Graph Visualization

Render the user's entire quilt as an SVG image — patches color-coded by type, grouped by project, with connections drawn between them.

```
GET /v1/quilt/{user_id}/graph
GET /v1/quilt/{user_id}/graph?format=png
```

Returns `image/svg+xml` (default) or `image/png`. The graph includes all active patches across all projects, with the user's person patch at the center.

**iOS usage:** Load the SVG in a `WKWebView` for native pan/zoom, or render as a static image. GhostPour can proxy this endpoint or ShoulderSurf can call CQ directly.

```swift
// Load quilt graph in a WKWebView
let url = URL(string: "\(cqBaseURL)/v1/quilt/\(userId)/graph?format=svg")!
var request = URLRequest(url: url)
request.addValue("cloudzap", forHTTPHeaderField: "X-App-ID")
webView.load(request)
```

## How It All Flows

```
During a meeting:
  ShoulderSurf → GhostPour → [CQ recall injects context] → LLM → response
                                                          ↓
                                              GhostPour captures query+response
                                                          ↓
                                              CQ extracts patches async (cold path)

At meeting end:
  ShoulderSurf sends full transcript → GhostPour → CQ
                                                    ↓
                                        CQ extracts traits, decisions,
                                        commitments, blockers from raw dialogue

Post-meeting review:
  ShoulderSurf → GhostPour → [CQ recall injects context] → LLM → response
                              (hot path only, no cold path capture)

User managing their quilt:
  ShoulderSurf → CQ directly (GET/PATCH/DELETE /v1/quilt)
```

## API Field Reference

### QuiltPatchResponse

> **Note on `category` vs `patch_type`:** Both fields return the same value. `patch_type` is the canonical field — it maps directly to the DB column and the patch type registry. `category` is a legacy alias kept for backward compatibility. **Model only `patch_type` in your Swift types.** The `category` field may be removed in a future version.

| Field | Type | Description |
|-------|------|-------------|
| `patch_id` | `string` (UUID) | Unique ID — use for edit/delete |
| `fact` | `string` | The patch text content |
| `patch_type` | `string` | The patch type: trait, preference, role, person, project, decision, commitment, blocker, takeaway |
| `category` | `string` | **Legacy alias for `patch_type`** — same value. Do not model separately in Swift. |
| `participants` | `string[]` | People involved |
| `owner` | `string?` | Who's responsible (commitments, blockers) |
| `deadline` | `string?` | Due date if applicable |
| `source` | `string` | "inferred" (CQ extracted) or "declared" (user edited) |
| `created_at` | `string` (ISO 8601) | When this patch was created |
| `project` | `string?` | Display name of the project (renameable) |
| `project_id` | `string?` | Stable project UUID — never changes on rename. Group by this. |
| `meeting_id` | `string?` | Meeting UUID — sub-group within a project |
| `connections` | `PatchConnection[]` | Outgoing edges to other patches |

### PatchConnection

| Field | Type | Description |
|-------|------|-------------|
| `to_patch_id` | `string` (UUID) | The patch this connects to |
| `role` | `string` | Structural role: parent, depends_on, resolves, replaces, informs |
| `label` | `string?` | Semantic label: belongs_to, blocked_by, motivated_by, works_on, owns |
| `context` | `string?` | Optional explanation |

### QuiltResponse

| Field | Type | Description |
|-------|------|-------------|
| `user_id` | `string` | The user |
| `facts` | `QuiltPatchResponse[]` | All active patches (all types) |
| `action_items` | `QuiltPatchResponse[]` | Legacy V1 — always empty for new patches. Ignore. |
| `deleted` | `string[]` | Patch IDs removed since `since` timestamp (delta sync only) |
| `server_time` | `string` (ISO 8601) | Use as `since` on next request for delta sync |

### PatchUpdate (for PATCH requests)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `fact` | `string` | No | New text for the patch |
| `category` | `string` | No | New type classification |

## Delta Sync

Avoid fetching all patches every time. Use `since` for incremental updates.

**First launch (full sync):**
```
GET /v1/quilt/{user_id}
→ Store all patches locally + save server_time
```

**Subsequent opens (delta sync):**
```
GET /v1/quilt/{user_id}?since=2026-03-26T05:30:00Z
→ facts: [only new/updated patches]
→ deleted: ["patch-id-1", "patch-id-2"]  ← remove from local cache
→ server_time: "2026-03-26T06:15:00Z"   ← save for next request
```

**iOS logic:**
1. Store `server_time` from each response (UserDefaults or CoreData)
2. First launch or cleared cache: call without `since` (full sync)
3. Subsequent opens: call with `since` = last saved `server_time`
4. Merge `facts` into local store (upsert by `patch_id`)
5. Remove any `patch_id` found in `deleted`

## Project Management

Projects have stable IDs. Names can change without breaking the quilt.

**ShoulderSurf sends in metadata to GhostPour:**
```json
{
  "project_id": "proj-uuid-123",
  "project": "Florida Blue",
  "meeting_id": "meet-uuid-456"
}
```

**List projects:**
```
GET /v1/projects/{user_id}
→ [{"project_id": "...", "name": "Florida Blue", "status": "active", "patch_count": 9}]
```

**Rename a project:**
```
PATCH /v1/projects/{user_id}/{project_id}
{"name": "FL Blue - Benefits"}
→ Updates display name on all patches. project_id never changes.
```

**Archive a project:**
```
PATCH /v1/projects/{user_id}/{project_id}
{"status": "archived"}
→ Archives the project and cascades to all patches inside it.
   Archived patches disappear from the quilt view.
```

## Meeting Grouping

Within a project, patches can be grouped by `meeting_id`:

```swift
// Group patches: project → meeting → patches
let byProject = Dictionary(grouping: facts.filter { $0.projectId != nil }, by: \.projectId)
for (projectId, projectPatches) in byProject {
    let byMeeting = Dictionary(grouping: projectPatches, by: \.meetingId)
    // Render: Project header → Meeting sections → Patch cards
}
```
