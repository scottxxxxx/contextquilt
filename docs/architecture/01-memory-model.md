# 01: The Memory Model

## Three Memory Tiers

Context Quilt mimics human cognition through three complementary memory systems:

### Tier 1: Factual Memory (PostgreSQL)

Long-term storage for explicit knowledge. These are the "patches" of the quilt.

**What's stored:**
- Facts about people, projects, companies, features
- Action items with owners and deadlines
- Decisions made in meetings
- User preferences and constraints

**Patch categories:**
- `identity` — Who someone is (role, team, title, skills)
- `preference` — What someone prefers (tools, methods, constraints)
- `trait` — How someone behaves (communication style, work habits)
- `experience` — What happened (projects, decisions, events)

**Characteristics:**
- Persistent — survives restarts, retained for the configured retention period
- Mutable — users can view, edit, and delete their own patches via the Quilt CRUD API
- Categorized — every patch has a type and optional metadata from the originating app

### Tier 2: Episodic Memory (Graph Layer in PostgreSQL)

The "stitching" that connects patches into a quilt. This is what makes CQ more than a fact database.

**Entities** — Named things CQ has learned about:

| Type | Examples |
|------|----------|
| `person` | Bob Martinez, David Chen |
| `project` | Widget 2.0 |
| `company` | Acme Corp |
| `feature` | real-time collaboration, offline mode |
| `artifact` | WebSocket prototype, staging environment |
| `deadline` | June 15, April 5 |
| `metric` | $150,000 budget, 40% unreliable internet |

**Relationships** — Connections between entities:

```
Bob Martinez --committed_to--> WebSocket prototype
WebSocket prototype --has_deadline--> April 5
Acme Corp --requires--> real-time collaboration
real-time collaboration --has_deadline--> June 15
Widget 2.0 --includes--> offline mode (status: core)
Widget 2.0 --includes--> real-time collaboration (status: beta)
project budget --capped_at--> $150,000
David Chen --cto_of--> Acme Corp
Sarah Chen --leads--> Widget 2.0
```

**Why a graph, not just tags?** Tags tell you "this fact is related to Widget 2.0." The graph tells you "Widget 2.0 has a deadline from Acme Corp, which requires a feature that depends on a prototype Bob committed to delivering by April 5." The graph enables traversal — starting from any entity and following relationships to surface connected context.

**Implementation:** PostgreSQL with dedicated `entities` and `relationships` tables, queried via recursive CTEs. This handles hundreds to thousands of entities per user efficiently. If scale requires it, migration to a dedicated graph database (Neo4j) is possible without changing the API.

### Tier 3: Working Memory (Redis Cache)

Short-term, high-speed context for the hot path.

**What's stored:**
- Pre-computed context blocks per user/project (the output of graph traversal)
- Hydrated user profiles (flattened facts for template substitution)
- Entity name indexes for fast text matching during recall

**Characteristics:**
- Ephemeral — 1-hour TTL, rebuilt from Postgres on cache miss
- Read-optimized — the only store queried on the synchronous read path
- Invalidated — when the cold path stores new facts, it triggers cache rebuild

## How the Tiers Work Together

**Write flow (cold path):**
1. Meeting summary arrives
2. LLM extracts facts, action items, entities, relationships
3. Facts stored as patches in PostgreSQL (Tier 1)
4. Entities and relationships stored in graph tables (Tier 2)
5. Pre-computed context blocks cached in Redis (Tier 3)

**Read flow (hot path):**
1. App sends text to recall endpoint
2. CQ matches entity names against Redis entity index (Tier 3)
3. If cache hit: return pre-computed context block — done
4. If cache miss: traverse graph in PostgreSQL (Tier 2), build context block, cache it, return

## User Ownership

Users can view and edit everything CQ knows about them via the Quilt CRUD API:
- `GET /v1/quilt/{user_id}` — see all facts and action items
- `PATCH /v1/quilt/{user_id}/patches/{patch_id}` — correct a wrong fact
- `DELETE /v1/quilt/{user_id}/patches/{patch_id}` — remove a fact

When a user edits a fact, the `origin_mode` changes to `declared` (user-verified). CQ treats user corrections as ground truth. There are no confidence scores or reliability metadata — if it's in the quilt, it's trusted. If it's wrong, the user fixes it.
