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
```json
{
  "user_id": "user_abc123",
  "facts": [
    {
      "patch_id": "550e8400-e29b-41d4-a716-446655440000",
      "fact": "Tends to over-explain",
      "category": "trait",
      "participants": [],
      "owner": null,
      "deadline": null,
      "patch_type": "trait",
      "source": "inferred",
      "created_at": "2026-03-25T14:32:10Z"
    },
    {
      "patch_id": "660f9500-f39c-52e5-b827-557766551111",
      "fact": "Deliver transcription samples within 2 days",
      "category": "commitment",
      "participants": ["Travis"],
      "owner": "Ina",
      "deadline": "2026-03-27",
      "patch_type": "commitment",
      "source": "inferred",
      "created_at": "2026-03-25T14:32:10Z"
    }
  ],
  "action_items": []
}
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
- "About You" = patches where `category` is trait, preference, or role (with no project)
- Project sections = group by project name from the `participants` or a future `project` field
- "Action Items" = patches where `category` is commitment or blocker

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

| Field | Type | Description |
|-------|------|-------------|
| `patch_id` | `string` (UUID) | Unique ID — use for edit/delete |
| `fact` | `string` | The patch text content |
| `category` | `string` | Patch type (trait, preference, commitment, etc.) |
| `participants` | `string[]` | People involved |
| `owner` | `string?` | Who's responsible (commitments, blockers) |
| `deadline` | `string?` | Due date if applicable |
| `patch_type` | `string` | Same as category |
| `source` | `string` | "inferred" (CQ extracted) or "declared" (user edited) |
| `created_at` | `string` (ISO 8601) | When this patch was created |

### PatchUpdate (for PATCH requests)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `fact` | `string` | No | New text for the patch |
| `category` | `string` | No | New type classification |
