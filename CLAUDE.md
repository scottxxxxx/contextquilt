# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ContextQuilt** is an Intelligent AI Gateway that provides a unified, low-latency, persistent cognitive memory layer for AI agents. It solves the "goldfish memory" problem (stateless LLMs) and "memory fragmentation" problem (siloed AI platforms) by acting as a slide-in-place layer between applications and LLM providers.

## Core Architecture Principles

### The "Zero-Latency" Asynchronous Architecture

The system uses a decoupled read/write path design:

- **Read Path (Synchronous)**: Live LLM calls query an ultra-fast in-memory "Working Memory" cache (Redis) for instant context injection with zero added latency
- **Write Path (Asynchronous)**: Background workers perform expensive cognitive consolidation (summarizing, extracting facts) after the user receives their response

**Critical**: Any implementation must maintain this separation. Never block the synchronous read path with expensive operations.

### The "Hybrid Cognitive" Data Model ("The Quilt")

The system mimics human cognition through three memory types:

1. **Factual Memory** (K/V or SQL): Explicit user preferences and facts (e.g., "I don't like coffee")
2. **Episodic Memory** (Graph Database): Relationships between entities, events, and users using GraphRAG. This is the key differentiator - stores "threads" that connect context across conversations
3. **Working Memory** (Redis Cache): Short-TTL, in-session scratchpad context

**Implementation Note**: The episodic memory graph is what makes this more than a RAG system - it preserves relationship context across platform boundaries.

### Hub-and-Spoke RAG Integration

ContextQuilt acts as a central orchestrator (hub) with pluggable vector databases (spokes):

- Core system provides cognitive/episodic memory
- Plugin API allows any vector DB (Pinecone, Weaviate, Qdrant, etc.) to integrate as semantic memory
- The system doesn't compete with RAG; it orchestrates and enhances it

## Product Structure

### ContextQuilt Core (Open Source)
- Apache 2.0 licensed
- Core gateway features and RAG plugin framework
- Intended as a Python library (`contextquilt-py`)

### ContextQuilt Enterprise (Commercial)
- Self-hosted Docker appliance
- Adds RBAC, PII redaction, audit trails, management dashboards
- Cross-platform sync for enterprise memory fragmentation solution

## Key Technical Concepts

### Active Enrichment Methods
- Context compression (ACON, LLMLingua-2) to reduce token costs
- A/B testing capabilities at the gateway level
- Cost-based throttling and routing

### Cross-Platform Memory Sync
The "neutrality moat" - enables memory continuity across siloed AI platforms (AWS, OpenAI, ServiceNow, etc.)

## Development Considerations

### Patent Protection
This project has provisional patent protection for:
- The asynchronous zero-latency architecture
- The hybrid cognitive data model
- Specific active enrichment methods

When implementing or modifying core architectural components, preserve the novel aspects of these methods.

### Performance Requirements
- **Latency**: Synchronous read path must add minimal latency (target: <10ms overhead)
- **Scalability**: Design for high-throughput enterprise workloads
- **Memory Efficiency**: Working memory should use aggressive TTL and eviction policies

### Integration Patterns
When building integrations:
- Gateway sits between application and LLM provider
- Must be "slide-in-place" - minimal application changes required
- Support multiple LLM providers (OpenAI, Anthropic, AWS Bedrock, etc.)
- Maintain provider neutrality


# Documentation

## V3 Architecture (Current)
See `docs/architecture/` for the complete V3 specification:
- `00-overview.md` — What CQ is, core concepts
- `01-memory-model.md` — Three tiers, graph layer, entities/relationships
- `02-pipeline.md` — Extraction pipeline, four roles, model selection
- `03-queue-and-lifecycle.md` — Meeting queue, batching, context budgeting
- `04-recall.md` — Intelligent recall, entity matching, hot path
- `05-integration.md` — CloudZap/ShoulderSurf flow, capture points, metadata
- `06-configuration.md` — All settings, env vars, admin dashboard
- `07-api-reference.md` — Complete API endpoint documentation
- `08-connected-quilt-model.md` — Connected Quilt: extensible patch types, patch connections (role + label), app policy, lifecycle through connections
- `health-coach-quilt-diagram.md` — Design example: health coaching app with five connection roles (parent, depends_on, resolves, replaces, informs)

## Integration Guides
- `docs/integration/ios-quilt-management.md` — iOS team guide: patch types, API endpoints, UI recommendations, flow diagrams

## Other Resources
- `GETTING_STARTED.md` — Setup instructions
- `README.md` — Project overview
- `docs/sales/` — Sales and marketing materials
- `docs/archive/` — Previous architecture versions (V1, V2, 3.7-3.10)

## Current Status (March 27, 2026)

**Live deployment:** `https://cq.shouldersurf.com`
**Admin dashboard:** `https://cq.shouldersurf.com/dashboard/` (protected by CQ_ADMIN_KEY)
**GCP VM:** `35.239.227.192` (shared with GhostPour via Project Bifrost)
**Version:** 3.12.0 (Connected Quilt + Projects)

### What's built and deployed:

**Connected Quilt Model:**
- Typed patches: trait, preference, role, person, project, decision, commitment, blocker, takeaway
- Patch connections with structural roles (parent, depends_on, resolves, replaces, informs) and semantic labels (belongs_to, blocked_by, motivated_by, works_on, owns, supersedes)
- Patch type registry and connection vocabulary tables for extensible, app-defined types
- Lifecycle through connections: project archival cascades to children, replaces auto-archives old patches
- Patch deduplication: same type + text for same user → reuses existing patch, bumps access count
- Cold path worker using Gemini 2.5 Flash via OpenRouter ($0.0017/extraction)
- Hard extraction caps: 12 patches, 10 entities, 10 relationships per meeting
- Trait priority: self-disclosed traits always extracted first
- Submitting user identity injected into extraction for correct trait attribution from diarized transcripts

**Projects & Meetings:**
- `projects` table with stable `project_id` (UUID from app) and renameable `name`
- `project_id` and `meeting_id` columns on `context_patches` for grouping
- `GET /v1/projects/{user_id}` — list projects with patch counts
- `POST /v1/projects/{user_id}` — register a project
- `PATCH /v1/projects/{user_id}/{project_id}` — rename or archive (archiving cascades to all patches)
- Worker auto-registers projects from `metadata.project_id`
- Renaming updates display name everywhere — project_id never changes

**Quilt API (iOS CRUD):**
- `GET /v1/quilt/{user_id}` — all active patches with connections, project_id, meeting_id
- `GET /v1/quilt/{user_id}?since=ISO8601` — delta sync: only patches created/updated since timestamp, plus `deleted[]` array of removed patch_ids. Returns `server_time` to use as `since` on next call.
- `GET /v1/quilt/{user_id}/graph?format=svg` — visual graph of entire quilt as SVG or PNG image. Force-directed layout, project clustering, user centered, color-coded by type.
- `PATCH /v1/quilt/{user_id}/patches/{patch_id}` — edit patch text/type
- `DELETE /v1/quilt/{user_id}/patches/{patch_id}` — delete single patch
- `DELETE /v1/quilt/{user_id}` — delete all patches for a user (testing/GDPR)
- Filters out archived/completed patches (status = 'active' only)

**Recall (Hot Path):**
- `POST /v1/recall` — entity matching + graph traversal + patch connection traversal
- Returns structured context grouped by: About you, Decisions, Open commitments, Blockers, Roles, Key facts
- Project-scoped: only returns patches for the requested project + universal traits/preferences

**Admin Dashboard (8 views, all functional):**
- Dashboard: KPI cards, ingestion chart, distribution chart, recent patches
- Settings: extraction config display, LLM config, prompt editor with versioning
- User Quilt: user directory, patch detail with edit/delete
- Patch Types: CRUD for patch_type_registry and connection_vocabulary
- Pipeline Playground: streaming dry-run extraction
- Extraction Costs: real cost tracking from extraction_metrics table (KPIs, charts, recent table)
- System Health: Postgres/Redis/Worker/LLM status cards with latency
- Application Access: app registration, auth toggle

**End-of-Meeting Transcript Capture:**
- GhostPour `POST /v1/capture-transcript` receives full transcript from ShoulderSurf at session end
- CQ extracts traits/facts from raw dialogue (not pre-summarized)
- Post-meeting chat queries get CQ recall (hot path) but skip capture (cold path) — no wasted tokens

### GhostPour integration:
- GhostPour calls `/v1/recall` before LLM queries when `context_quilt: true`
- GhostPour POSTs query+response to `/v1/memory` after LLM responds (async)
- GhostPour skips capture for `promptMode: "PostMeetingChat"` — read-only, no token cost
- GhostPour `POST /v1/capture-transcript` receives full transcript at meeting end
- Response headers `X-CQ-Matched` and `X-CQ-Entities` for iOS UI indicator
- Passes `display_name`, `email`, `project`, `project_id`, `meeting_id` in metadata

**Worker improvements (v3.13):**
- Stub patches now include `project_id` and `meeting_id` (previously missing, caused orphan connections)
- Auto-connects submitting user's person patch to all project patches via `works_on` after each extraction
- Graphviz added to Docker image for server-side graph rendering

### Next priorities:
1. End-to-end test: ShoulderSurf → GhostPour → CQ → connected quilt → recall
2. Implement decay worker (scheduled job to archive stale patches per app policy)
3. Build domain description interface (YAML or natural language → schema generation)

## CI/CD and Deployment

### Deployment workflow
Push to `main` triggers GitHub Actions (`.github/workflows/deploy.yml`):
1. Builds Docker image → pushes to `ghcr.io`
2. SSH into GCP VM (`35.239.227.192`)
3. Pulls latest image, restarts CQ containers via `docker-compose.prod.yml --env-file .env.prod`
4. Only restarts CQ containers — does NOT touch CloudZap/Bifrost

### Production environment
- **On the GCP VM at `/opt/contextquilt/`**
- `.env.prod` contains all production secrets (CQ_ADMIN_KEY, JWT_SECRET_KEY, CQ_LLM_API_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD)
- `docker-compose.prod.yml` defines: `context-quilt` (API on port 8000), `context-quilt-worker` (cold path), `cq-postgres`, `cq-redis`
- LLM calls go through OpenRouter (`CQ_LLM_BASE_URL`), default model configured via `CQ_LLM_MODEL`
- Nginx Proxy Manager (Project Bifrost) handles TLS termination and routes `cq.shouldersurf.com` → port 8000

### GitHub Secrets required
`GCP_HOST`, `GCP_USERNAME`, `GCP_SSH_KEY`, `GCP_SSH_PASSPHRASE`, `GITHUB_TOKEN` (auto)

## Testing and Data Seeding

### How data gets into CQ (production flow)
1. User uses ShoulderSurf on iPhone → ShoulderSurf sends audio/transcript to GhostPour (CloudZap)
2. GhostPour POSTs to `POST /v1/memory` with `user_id` (Apple Sign-In UUID), `interaction_type: "meeting_summary"`, transcript in `summary` field, and metadata (`meeting_id`, `project`, `display_name`)
3. CQ queues to Redis stream → cold-path worker picks it up → LLM extracts patches → stored to Postgres
4. On next query, GhostPour calls `POST /v1/recall` with `user_id` → CQ returns matching context

### User identity
- CQ does NOT authenticate end users — it authenticates **apps** (via JWT or X-App-ID header)
- Apps are responsible for user auth (Apple Sign-In, etc.) and pass `user_id` to CQ
- `user_id` is the Apple Sign-In subject identifier (UUID format, e.g., `FA4D903C-24C0-45D5-9FDB-B5496E32501B`)
- CQ's internal `subject_key` format is `user:{user_id}`

### Verifying patches for a user
- **Admin dashboard:** `https://cq.shouldersurf.com/dashboard/` → Users tab → click user → see all patches
- **API:** `GET /api/dashboard/users/{user_id}/quilt` (requires `X-Admin-Key` header)
- **Quilt API:** `GET /v1/quilt/{user_id}` (requires app JWT or X-App-ID)

### Dry-run extraction (no database writes)
```bash
OPENROUTER_API_KEY=sk-... python tests/benchmark/test_extraction_dryrun.py [transcript_file] [--user "Display Name"]
```
Runs the transcript through the extraction prompt and shows what patches would be created. Uses the Florida Blue transcript by default. See also `tests/benchmark/run_pipeline_benchmark.py` for multi-model comparison.

### Test scripts
- `scripts/generate_seed_patches.py` → generates synthetic patches to `data/seed_patches.json`
- `scripts/load_seed_data.py` → loads seed patches into DB for demo users
- `scripts/dump_patches.py` → dump all patches for inspection
- `tests/benchmark/test_summaries.py` → ground truth test cases for benchmarking extraction quality

## Related Projects

- **GhostPour** (formerly CloudZap) (`/Users/scottguida/cloudzap/`) — LLM gateway, CQ integration point
- **ShoulderSurf** (`/Users/scottguida/ShoulderSurf/`) — iOS meeting copilot, first CQ consumer
- **Project Bifrost** (`/Users/scottguida/bifrost/`) — Nginx Proxy Manager on shared GCP VM