# 04: Recall — How Context Gets Retrieved

## The Core Idea

When an app sends a query to its LLM, CQ can enrich that query with relevant context from its memory. The app doesn't need to know what context exists or how to ask for it. CQ reads the text, recognizes entities it knows about, and returns relevant context.

## Two Recall Modes

### Intelligent Recall (POST /v1/recall)

The app sends raw text — a query, a transcript snippet, anything. CQ figures out what's relevant.

**Request:**
```json
{
  "user_id": "scott-001",
  "text": "Let's discuss the WebSocket prototype timeline and whether Bob can hit the April 5th deadline."
}
```

**What CQ does:**
1. Scans the text for known entity names for this user
2. Matches: "WebSocket prototype", "Bob", "April 5"
3. Traverses the graph 1-2 hops from each matched entity
4. Collects connected facts, relationships, and action items
5. Formats into a compact context block

**Response:**
```json
{
  "context": "Project Widget 2.0: Bob Martinez (VP Product) committed to WebSocket prototype by April 5. Prototype estimated at 3 weeks. Acme Corp requires real-time collaboration by June 15 (contractual). Budget capped at $150,000 (Lisa, finance). Team decided: offline mode as core, real-time collab as beta. Sarah Chen scheduling demo with David Chen (Acme CTO).",
  "matched_entities": ["WebSocket prototype", "Bob Martinez", "April 5"],
  "patch_count": 9
}
```

**How entity matching works (hot path):**

Entity names for each user are indexed in Redis as a sorted set. When text arrives, CQ checks which known entity names appear in the text. This is string matching against a cached index — no LLM call needed. Sub-10ms.

For fuzzy matching (e.g., "Bob" matching "Bob Martinez"), CQ stores both the full name and common short forms in the index.

### Template Enrichment (POST /v1/enrich)

The app sends a prompt template with explicit placeholders. CQ fills them from the user's profile.

**Request:**
```json
{
  "user_id": "scott-001",
  "template": "The user's role is [[job_title]]. They prefer [[communication_style|concise]] responses."
}
```

**Response:**
```json
{
  "enriched_prompt": "The user's role is Tech Lead. They prefer concise responses.",
  "used_variables": ["job_title", "communication_style (default)"],
  "missing_variables": []
}
```

**When to use which:**
- **Recall** — app doesn't know what context is relevant (meeting copilots, open-ended assistants)
- **Enrich** — app knows exactly what variables it wants (structured workflows, customer support templates)

Both modes read from the hot path (Redis cache). Neither triggers LLM calls.

## Context Block Formatting

The recall endpoint returns a `context` string — a pre-formatted block of text that the consuming app can inject into its LLM prompt. CQ formats this as natural language, not JSON, because it will be read by an LLM, not parsed by code.

The format groups information by relevance:

```
[Project context — what's the project, who's involved]
[Open action items — what's pending, who owns it, what's the deadline]
[Key constraints — budgets, deadlines, blockers]
[Recent decisions — what was agreed, what changed]
```

This format is opinionated but generic — it works for meeting copilots, project management tools, and coding assistants alike.

## Graph Traversal

When recall matches entities, it traverses the relationship graph to build the context block.

**Traversal depth:** 2 hops by default. This means:
- Starting from "Widget 2.0", CQ follows edges to find Bob Martinez, Acme Corp, offline mode, real-time collab (1 hop)
- Then from each of those, finds David Chen (CTO of Acme), April 5 deadline (Bob's commitment), June 15 (Acme's deadline) (2 hops)

Two hops captures the immediate network around the matched entities without pulling in the entire graph.

**Implementation:** PostgreSQL recursive CTE query. For a user with hundreds of entities, this completes in <5ms.

**Pre-computation:** After the cold path stores new entities/relationships, CQ pre-computes context blocks for the most active projects/entities and caches them in Redis. The hot path serves these directly.

## Metadata Filtering

If the app provides metadata hints (e.g., `"project": "Widget 2.0"`), CQ uses them to narrow the recall before doing text matching. This is optional — CQ works without hints, but hints improve precision.

```json
{
  "user_id": "scott-001",
  "text": "What's the status?",
  "metadata": {"project": "Widget 2.0"}
}
```

Without the hint, "What's the status?" has no entity names to match. With the hint, CQ knows to start from Widget 2.0.

## Performance

| Operation | Where it happens | Target latency |
|-----------|-----------------|----------------|
| Entity name matching | Redis sorted set | <1ms |
| Cache hit (pre-computed context) | Redis | <1ms |
| Cache miss (graph traversal) | PostgreSQL | <5ms |
| Cache rebuild | PostgreSQL → Redis | <50ms |

Total recall overhead target: **<10ms** on cache hit, **<50ms** on cache miss.
