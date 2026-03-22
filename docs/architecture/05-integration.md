# 05: Integration Patterns

## The CloudZap / ShoulderSurf Reference Implementation

Context Quilt's first integration is with ShoulderSurf (iOS meeting copilot) via CloudZap (LLM API gateway). This serves as the reference implementation for how any app can integrate with CQ.

### Architecture

```
ShoulderSurf (iOS) ←→ CloudZap (gateway) ←→ LLM Provider
                            ↕
                      Context Quilt (memory)
```

- **ShoulderSurf** handles: audio capture, transcription, speaker diarization, camera, UI
- **CloudZap** handles: LLM routing, user auth, subscription tiers, CQ integration
- **Context Quilt** handles: memory storage, extraction, graph building, recall

ShoulderSurf never talks to CQ directly. CloudZap is the integration point.

### Capture Points

CloudZap captures information for CQ at these points:

| When | What CloudZap sends to CQ | CQ interaction_type |
|------|--------------------------|---------------------|
| Auto-summary generated (every 15 min) | Summary text | `summary` |
| User sends a query during meeting | Query + transcript context | `query` |
| LLM responds to a query | Query + response | `query` (with response field) |
| Meeting ends, final summary | Complete summary | `summary` |
| Post-meeting sentiment analysis | Sentiment score + label + reason | `sentiment` |
| User reviews meeting later, asks questions | Review query + response | `query` |

All events include `meeting_id` and optionally `project` in the metadata. CQ queues them and processes in batch (see [03-queue-and-lifecycle.md](03-queue-and-lifecycle.md)).

### Recall Point

Before CloudZap forwards a query to the LLM:

1. CloudZap sends the query text to `POST /v1/recall` with the user_id
2. CQ returns relevant context
3. CloudZap injects the context into the system prompt
4. CloudZap forwards the enriched prompt to the LLM

The user and ShoulderSurf are unaware this happened. The LLM simply has better context.

### What CloudZap Sends to CQ

```json
{
  "user_id": "apple-auth-user-id-from-jwt",
  "interaction_type": "query",
  "content": "What are the risks with the Widget 2.0 timeline?",
  "response": "Based on the discussion, the main risks are...",
  "metadata": {
    "meeting_id": "a1b2c3d4-e5f6-47g8-h9i0-j1k2l3m4n5o6",
    "project": "Widget 2.0"
  }
}
```

### Authentication Flow

```
User → Apple Sign In → ShoulderSurf → CloudZap JWT → CloudZap
                                                         ↓
                                              CloudZap calls CQ with:
                                              - CloudZap's app JWT (authenticates the app)
                                              - user_id (from CloudZap's own JWT)

                                              CQ trusts CloudZap to vouch for the user.
                                              CQ never sees Apple credentials.
```

CQ is auth-provider-agnostic. It authenticates apps, not users. The app authenticates users however it wants.

## Generic Integration Pattern

Any app can integrate with CQ using this pattern:

### Step 1: Register as an app

```
POST /v1/auth/register
{"app_name": "my-coding-assistant"}
```

Returns `app_id` and `client_secret`. Exchange for a JWT via `/v1/auth/token`.

### Step 2: Send events as they happen

```
POST /v1/memory
Authorization: Bearer <app-jwt>
{
  "user_id": "user-123",
  "interaction_type": "query",
  "content": "the user's message or content",
  "response": "the LLM's response (optional)",
  "metadata": {
    "session_id": "any-grouping-key",
    "any_key": "any_value"
  }
}
```

CQ queues and processes automatically.

### Step 3: Recall context before LLM calls

```
POST /v1/recall
Authorization: Bearer <app-jwt>
{
  "user_id": "user-123",
  "text": "the user's current query or context"
}
```

Returns a context block to inject into the prompt.

### Step 4 (Optional): Let users see their quilt

```
GET /v1/quilt/{user_id}
Authorization: Bearer <app-jwt>
```

Returns all facts and action items. Users can edit or delete via PATCH/DELETE.

## The Metadata System

CQ accepts arbitrary key-value metadata on every event. This metadata:

- Gets stored alongside extracted facts
- Can be used to filter recall results
- Is defined by the app, not CQ

**Examples by app type:**

| App | Metadata keys |
|-----|--------------|
| Meeting copilot | `meeting_id`, `project`, `participants` |
| Customer support | `ticket_id`, `customer_tier`, `product` |
| Coding assistant | `repo`, `branch`, `language` |
| Sales tool | `deal_id`, `company`, `stage` |

CQ doesn't know what these keys mean. It stores them and filters by them.
