# 03: Queue and Lifecycle Management

## The Problem

During a 1-hour meeting, an app might send 8+ events through CQ — auto-summaries every 15 minutes, user queries, query responses, and a post-meeting sentiment analysis. Running the cold path extraction on each event individually wastes tokens, costs more, and produces worse results because each extraction only sees a fragment of the meeting.

## The Solution: Meeting Queue

CQ buffers incoming events and processes them in consolidated batches. Events are grouped by a `meeting_id` (or any grouping key the app provides in metadata). When the buffer is ready to process, CQ concatenates all queued events and runs a single extraction call.

## How Events Flow

```
App → CloudZap → CQ /v1/memory
                      ↓
              Redis queue (grouped by meeting_id)
                      ↓
              Wait for trigger...
                      ↓
              Consolidate queued events
                      ↓
              Single cold path extraction
                      ↓
              Store facts, entities, relationships
                      ↓
              Update Redis cache (hot path)
```

## Triggers

The cold path runs when **either** trigger fires (whichever comes first):

### 1. Time Trigger

**Default: 60 minutes of quiet.** If no new events arrive for a meeting queue within 60 minutes, CQ consolidates and processes everything in that queue.

This handles:
- Normal meetings (events stop arriving when the meeting ends)
- Post-meeting review queries (user asks questions hours later, then stops)
- The case where no explicit "session end" signal is sent

**Configuration:** `CQ_QUEUE_MAX_WAIT_MINUTES=60`

### 2. Context Budget Trigger

**Default: 80% of the model's context window.** If the queued content exceeds this threshold, CQ processes immediately rather than waiting for the time trigger.

This handles:
- Long meetings with many summaries and queries
- Small context window models (e.g., a self-hosted 8K model fills up fast)
- Prevents content from being silently truncated

**How the budget is calculated:**

```
context_window = configured model context window
prompt_overhead = ~800 tokens (extraction system prompt)
output_reserve = ~2000 tokens (room for extraction output)
available = context_window - prompt_overhead - output_reserve

As events queue:
  event 1: 600 tokens  → total: 600 / available
  event 2: 400 tokens  → total: 1000 / available
  ...
  when total > 0.8 * available → trigger cold path now
```

**Multi-role pipeline note:** When multiple models are configured with different context windows, CQ uses the smallest window as the budget. This ensures the content fits every model in the pipeline.

**Configuration:**
```
CQ_LLM_CONTEXT_WINDOW=128000    # Auto-detected for known models
CQ_QUEUE_BUDGET_THRESHOLD=0.8   # Trigger at 80% capacity
```

## What Gets Queued

Every event sent to `/v1/memory` with a `meeting_id` in metadata gets queued:

| Event Type | What CQ captures | Example |
|-----------|-----------------|---------|
| `summary` | Meeting summary text | Auto-summary at 15-min intervals, final summary |
| `query` | The user's question + transcript context | "What did Bob say about the budget?" |
| `query` with `response` | The question AND the LLM's response | Query + synthesized answer (may contain insights not in transcript) |
| `sentiment` | Post-meeting sentiment analysis | Score, label, reasoning |

## What Happens Without a Meeting ID

If an event arrives without a `meeting_id` in metadata, CQ falls back to per-user queuing with the same 60-minute window. Events are grouped by `user_id` instead. This handles apps that don't have a meeting/session concept.

## Consolidated Extraction

When a trigger fires, CQ:

1. Reads all events from the queue for that meeting
2. Concatenates them in chronological order with type labels:

```
[SUMMARY 1] Team discussed Widget 2.0 timeline...
[QUERY] User asked: "What are the risks with the timeline?"
[RESPONSE] The main risks are: 1) WebSocket prototype has no buffer...
[SUMMARY 2] Bob agreed to April 5 deadline for prototype...
[SENTIMENT] Score: 0.7 (positive-collaborative). Team aligned on scope.
```

3. Sends this consolidated text to the extraction pipeline (single call or multi-role)
4. Stores all extracted facts, entities, relationships, action items
5. Updates the Redis cache
6. Clears the queue

## Queue Persistence

Queues are stored in Redis as lists keyed by `meeting_queue:{meeting_id}`. Each entry is a JSON object with the event content, type, and timestamp.

If CQ restarts, pending queues survive (Redis persistence). On startup, the worker checks for any queues that have exceeded their time trigger and processes them.

## Configuration Summary

| Setting | Default | Description |
|---------|---------|-------------|
| `CQ_QUEUE_MAX_WAIT_MINUTES` | 60 | Minutes of quiet before processing a queue |
| `CQ_QUEUE_BUDGET_THRESHOLD` | 0.8 | Fraction of context window that triggers immediate processing |
| `CQ_LLM_CONTEXT_WINDOW` | Auto-detected | Override for custom/unknown models |
