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

#### Context Injection Contract

The `context` string returned by `/v1/recall` is a formatted block structured by section headers. The headers are **load-bearing** — they anchor pronouns and categorize content for the LLM. Integrators MUST preserve them when injecting.

**Canonical output shape:**

```
About you:
- You prefer async communication over meetings.
- You tend to elevate your game and push others.

Open commitments:
- Vijay will import the agents (by Friday)
- Nandini will retest once the scope is corrected

Decisions:
- Use Nova 3 for transcription on the Harborview Mutual project.

Key facts:
- You are based out of Austin, Texas.
```

**Pronoun anchoring.** Trait, preference, and identity patches are emitted in **second person** ("You prefer X", "You are based in Y"). The `About you:` header tells the LLM the following bullets describe the user of the query — not the assistant. Without the header, "You" becomes ambiguous against the assistant's own "You are a helpful assistant" framing.

**Do's:**

- Inject the block verbatim into your system prompt, either via a template placeholder (e.g. `{{context_quilt}}`) or as a prepended block with a clear anchor (`[CONTEXT FROM PREVIOUS MEETINGS]\n{context}\n\n{system_prompt}`).
- Preserve all section headers (`About you:`, `Open commitments:`, etc.) exactly as emitted.
- Send verbatim — no truncation, re-wrapping, reordering, or header renaming.

**Don'ts:**

- Don't flatten the block into a single paragraph. The headers are the anchors.
- Don't strip or rename headers. Even `"About you"` → `"About the user"` loses the pronoun-to-subject binding.
- Don't try to rewrite pronouns client-side ("You" → "Scott"). CQ owns the voice contract; see [02-pipeline.md](02-pipeline.md) for how patches are generated.

**Timeout guidance.** The recall endpoint targets <50ms on cache miss and <10ms on cache hit. A 200ms client-side timeout with graceful degrade (empty context on timeout) is a reasonable default. For best results, call `POST /v1/working-memory/prewarm` at session start to warm the user's entity index in Redis before the first live query.

**Do not transform at render time.** If you find yourself adding post-processing to fix voice, pronouns, or stray markers in CQ's output, file an issue — the fix belongs in the extraction pipeline, not a client-side sanitizer. Sanitizers drift when new edge cases appear and produce subtly wrong text (e.g., replacing `"Scott (you) wants"` → `"You wants"` without verb agreement).

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

## Reserved Metadata Keys

Some `metadata` keys are interpreted by CQ rather than passed through opaquely. Apps may send them, but the values must match the contracts below or CQ will drop them with a warning (graceful degrade — the rest of the event still ingests).

| Key | Type | Purpose |
|-----|------|---------|
| `user_label` | string | Hard claim from the app's identity layer of which transcript speaker is the submitting user. Used together with `user_identified` and `identification_source`. |
| `user_identified` | bool | `true` only when the app has a hard claim (e.g., voice enrollment match). |
| `identification_source` | enum string | `"enrollment"` \| `"user_confirmation"` \| `"none"`. CQ honors the claim when this is non-`"none"`. |
| `owner_speaker_label` | string | Legacy server-side marker injection. New integrators should use `user_label` + `user_identified`. |
| `display_name` | string | Fallback for user-context when no `(you)` marker is present. |
| `subscription_tier` | enum string | Lowercase: `"free"` \| `"plus"` \| `"pro"`. Forwarded on every write for cost-by-tier analytics. |
| `previous_tier` | enum string | Same shape; set only when a tier boundary was crossed in the previous 24h. |
| `user_attribution_hint` | object | Soft-signal attribution when no hard claim is available. See below. |

### `user_attribution_hint`

Soft attribution signal for cases where the app's identity layer has a best-guess speaker mapping but not enough confidence to set `user_identified = true`. Wire shape:

```json
{
  "user_attribution_hint": {
    "speaker_label": "Speaker 3",
    "confidence": 0.42,
    "confidence_basis": "combined",
    "secondary_candidate": {
      "speaker_label": "Speaker 5",
      "confidence": 0.31
    }
  }
}
```

**Field contract:**

| Field | Type | Required | Constraint |
|-------|------|----------|------------|
| `speaker_label` | string | yes | Must match a `Speaker N` label that appears in the transcript chunks of this request. |
| `confidence` | float | yes | `[0.0, 1.0]` inclusive. |
| `confidence_basis` | enum | yes | One of: `enrolled_similarity`, `cumulative_seconds`, `embedding_consistency`, `combined`. |
| `secondary_candidate` | object | no | Same shape as parent, depth-1 only. `confidence` must be `≤` primary confidence. |

**`confidence_basis` semantics:**

- `enrolled_similarity` — raw cosine to the user's enrolled voice centroid. Lowest signal-to-noise; weighted least in gating.
- `cumulative_seconds` — most-talkative-speaker heuristic. Strong on long meetings, weak on short ones.
- `embedding_consistency` — within-cluster embedding variance, normalized. High = stable cluster.
- `combined` — sender-side weighted blend. CQ treats this as opaque and trusts the number.

**CQ gating behavior (pre-calibration thresholds; revisable):**

| Confidence | CQ treatment |
|------------|--------------|
| `≥ 0.70` | Functionally-confirmed identity. Extract personal types (trait, preference, identity) from the speaker's content. |
| `0.40 – 0.70` | Soft prior. Requires corroboration (cross-meeting agreement, explicit `(you)` markers, etc.) before extracting personal types. Safe to use for non-personal extraction. |
| `< 0.40` | Treated as noise. Personal-type gating falls through to cross-meeting heuristics or the legacy markerless path. |

**When the hint is absent:**

The producer should omit `user_attribution_hint` entirely (not send it as `null`) when:

- No enrolled profile exists for the signed-in user.
- The diarizer mode does not compute embeddings (e.g., Sortformer / Apple Foundation Model mode).
- The meeting has zero confidently-attributed single-speaker segments.
- Multiple candidates are within ~5% of each other (genuinely ambiguous — better to send nothing than mislead).

CQ falls through to the existing markerless path when the field is absent.

**Multi-speaker chunks:** Chunks tagged `[Multiple]` (auto-detected or user-tagged as overlap) should be excluded by the producer from the input to the hint computation — their embeddings are mixed and would pull the similarity / consistency signals in misleading directions.

**Not in v1:** per-segment confidence breakdowns, confidence intervals, negative-attribution hints (`"definitely NOT this user"`). The wire shape is stable; iOS senders may ship against this contract.
