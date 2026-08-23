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
  "text": "Let's discuss the WebSocket prototype timeline and whether Bramble can hit the April 5th deadline."
}
```

**What CQ does:**
1. Scans the text for known entity names for this user
2. Matches: "WebSocket prototype", "Bramble", "April 5"
3. Traverses the graph 1-2 hops from each matched entity
4. Collects connected facts, relationships, and action items
5. Formats into a compact context block

**Response:**
```json
{
  "context": "Project Widget 2.0: Bramble Martinez (VP Product) committed to WebSocket prototype by April 5. Prototype estimated at 3 weeks. Acme Corp requires real-time collaboration by June 15 (contractual). Budget capped at $150,000 (Lisa, finance). Team decided: offline mode as core, real-time collab as beta. Lockridge Chen scheduling demo with David Chen (Acme CTO).",
  "matched_entities": ["WebSocket prototype", "Bramble Martinez", "April 5"],
  "patch_count": 9
}
```

**How entity matching works (hot path):**

Entity names for each user are indexed in Redis as a sorted set. When text arrives, CQ checks which known entity names appear in the text. This is string matching against a cached index — no LLM call needed. Sub-10ms.

For fuzzy matching (e.g., "Bramble" matching "Bramble Martinez"), CQ stores both the full name and common short forms in the index.

**Cue matching (associative retrieval):**

Beside the entity index lives a per-user cue index (`cue_index:{user_id}`) of topic phrases attached to patches at extraction time ("pricing model", "visa paperwork" — see `patch_cues`). Request text that names a topic but no entity still recalls the right patches: matched cues gate recall (no more empty result for entity-less topical queries), drive a direct cue→patch fetch leg (up to 10 patches, any scope), and add a `+75` scoring boost (below the +100 entity boost, above the 60-point keyword-overlap cap — cue-recalled patches often share no words with the query, which is precisely their value). Matched cues also count as index coverage for metamemory signals: a mention covered by a cue is not reported as a memory gap. The response lists them in `matched_cues`. Same lazy-rehydrate + sliding-TTL + worker-rebuild lifecycle as the entity index; degrades to entity-only matching on a DB that lacks `patch_cues` (MCP lag).

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
- Starting from "Widget 2.0", CQ follows edges to find Bramble Martinez, Acme Corp, offline mode, real-time collab (1 hop)
- Then from each of those, finds David Chen (CTO of Acme), April 5 deadline (Bramble's commitment), June 15 (Acme's deadline) (2 hops)

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

## Metamemory Signals (opt-in)

By default recall stays silent about what it *doesn't* have — an empty or partial result is indistinguishable, to the downstream LLM, from memory never having been consulted, and the model fills that silence with confabulated context. Passing `"memory_signals": true` in `metadata` appends explicit gap lines to the context block:

```
(no stored memory about: Kinsley, Orion Initiative)
(no stored project memory for "Falcon")
(memory checked: nothing stored matched this request)
```

- **Unmatched mentions** — name-shaped mentions in the request text with zero word-level overlap against the entity index (names ∪ aliases). Overlap suppresses the claim: "Lockridge" is never reported missing while "Lockridge Abrams" is known. Conservative Latin-script heuristic, capped at 3 mentions.
- **Missing project scope** — emitted only when the scope has no project patch, no project-scoped rows, and no overdue completables.
- **Nothing matched** — replaces the silent-empty response when no entities matched and no scope was given.

**Coverage line (always on for scoped recalls — contract commitment E):** when a project-scoped recall renders fewer patches than the project holds, the block ends with `(showing N of M stored patches for this project)`. A correctly scoped block that silently omits most of a project's memory reads as complete, which is worse than absence; the coverage line tells the model (and teaches the user) that a full rundown exists via the quilt endpoint. Not gated by `memory_signals`.

Signal lines are deterministic functions of (text, entity index, scope) — byte-stable within a UTC day like the rest of the block — and ride inside the same `token_budget`. Like the flat-mode markers, they are deliberately English (LLM-facing). Implementation: `src/contextquilt/services/recall_signals.py`.

## Scoring

After patches are pulled for the recall set, they're ranked by `score_patches` in `src/contextquilt/services/recall_scorer.py` and trimmed to `max_patches` (default 15). The composite score per patch:

| Component | Range | Description |
| --- | --- | --- |
| Type priority | 5..50 | Actionable types (commitment, blocker) float above passive (preference, takeaway) |
| Salience | +20 / −10 | `value.salience` high/low, set at extraction from speaker signals (emphasis, surprise, stakes). A weight modifier — deliberately weaker than any relevance signal |
| Entity-match boost | +100 per match | A matched entity name appears in the patch text |
| Keyword overlap | +0..60 | Shared content words with the query (capped) |
| Recency | +0..10 | Newest patch in the batch gets +10, oldest gets +0 — anchored on `last_observed_at` for self-typed patches, `created_at` otherwise |
| **Freshness multiplier** | ×0.30..1.00 | **Self-typed patches only (trait, preference, goal, constraint).** `max(0.30, exp(-days_stale / 365))` applied as the final multiplicative step. Other types keep multiplier 1.0. |

The freshness multiplier is the recall-side half of the freshness model documented in `docs/architecture/08-connected-quilt-model.md#freshness-model-self-typed-patches`. A 540d-stale preference scores at 30% of a freshly re-affirmed one — still surfaced if nothing fresher exists, but never preferred over a more recent signal.

`now` is bucketed to the UTC day so back-to-back recall calls return byte-identical scores. This is load-bearing for upstream prompt caching (Anthropic `cache_control` + the 30s `RECALL_RENDER_CACHE_TTL`) — without it, the cache window would never hit.

## Performance

| Operation | Where it happens | Target latency |
|-----------|-----------------|----------------|
| Entity name matching | Redis sorted set | <1ms |
| Cache hit (pre-computed context) | Redis | <1ms |
| Cache miss (graph traversal) | PostgreSQL | <5ms |
| Cache rebuild | PostgreSQL → Redis | <50ms |

Total recall overhead target: **<10ms** on cache hit, **<50ms** on cache miss.


## The `excluded` block (2026-08-23)

GhostPour's upgrade nudges need the two real memory moments: a Plus
user whose question would have drawn on a meeting older than the tier
window, and a Free user whose people-scoped recall could not use the
project's memory. Only CQ can count either, so `/v1/recall` carries an
optional top-level `excluded` object, counted in MEETINGS because the
copy says meetings:

- `by_window` (present when `metadata.max_age_days` was sent):
  `{meetings, oldest, max_age_days, definition}`, the AGE predicate
  inverted over the project scope, universal self-disclosure types
  excluded because they are never windowed.
- `by_scope` (present when `metadata.recall_scope == "people"`):
  `{meetings, definition}`, meetings in the project holding memory.
  This is scope size, not "matches that scored": the people lane runs no
  memory leg, so no scored set exists to subtract from. The definition
  says so on the wire.

Rules: project-scoped requests only (the chat flow is project-scoped);
one indexed COUNT per condition, measured at ~5 ms warm on the largest
prod project (1745 rows), 43 ms cold once; never a second recall; absent
means not computed, zero means nothing excluded; byte-stable within a
UTC day; written into the render cache with the context so a cache hit
serves the same block. GP reads it off the raw JSON.
