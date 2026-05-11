# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ContextQuilt** is a persistent cognitive memory layer for AI applications. It solves the "goldfish memory" problem (stateless LLMs) and "memory fragmentation" problem (siloed AI platforms) by acting as a slide-in-place layer between applications and LLM providers.

## Core Architecture Principles

### The "Zero-Latency" Asynchronous Architecture

The system uses a decoupled read/write path design:

- **Read Path (Synchronous)**: Live LLM calls query an ultra-fast in-memory "Working Memory" cache (Redis) for instant context injection with zero added latency
- **Write Path (Asynchronous)**: Background workers perform expensive cognitive consolidation (summarizing, extracting facts) after the user receives their response

**Critical**: Any implementation must maintain this separation. Never block the synchronous read path with expensive operations.

### The "Hybrid Cognitive" Data Model ("The Quilt")

The system mimics human cognition through three memory types:

1. **Factual Memory** (PostgreSQL): Explicit user preferences and facts stored as typed "patches"
2. **Episodic Memory** (Graph Layer): Relationships between entities, events, and users. Stores "threads" that connect context across conversations
3. **Working Memory** (Redis Cache): Short-TTL, in-session scratchpad context

### Connected Quilt Model

Memory is organized as typed **patches** (facts about users) connected by **stitching** (relationships):

**Patch types**: trait, preference, identity, role, person, project, decision, commitment, blocker, takeaway

**Connection roles**: parent, depends_on, resolves, replaces, informs

**Lifecycle**: Patches have type-specific TTLs. The decay worker archives stale patches. Connections drive cascading behavior (archiving a project cascades to children).

## Key Technical Concepts

### Extraction Pipeline (Cold Path)

The worker processes transcripts/conversations through a single LLM call that extracts:
- Typed patches with connections
- Named entities (person, project, company, feature, etc.)
- Relationships between entities

The extraction prompt uses the `(you)` speaker marker convention to identify the app user in diarized transcripts, enabling accurate trait attribution and project ownership.

After the LLM call, the worker runs a fixed chain of sanitizers (`src/contextquilt/services/extraction_schema.py`, invoked from `src/worker.py`) that enforce post-extraction invariants the LLM is unreliable about:

1. `enforce_owner_gate`, `enforce_connection_requirements`, `enforce_person_ownership` — structural enforcers (drop ungated patches, require parent connections, synthesize missing `person + owns` edges for named action owners)
2. `sanitize_you_marker_from_patches` — strip the literal `(you)` token from `value.text` / `value.owner`
3. `strip_owner_on_self_typed_patches` — force `owner=null` on trait/preference/goal/constraint
4. `strip_prose_from_person_names` — `person.value.text` must be a name, not a sentence
5. `drop_placeholder_and_self_person_patches` — drop diarization placeholders (`"Speaker N"`) and (you)-speaker self-references
6. `strip_ephemeral_fields` — final cleanup

If you add a new sanitizer, slot it in by editing this chain. Each sanitizer mutates `content` in place and is independently unit-tested under `tests/unit/`. The corresponding backfill scripts in `scripts/backfill_*.py` reuse the live sanitizer rather than duplicating logic — write new ones the same way.

### Database Migrations

Migrations live in `init-db/*.sql` and are tracked in a `schema_migrations` table by filename + sha256. The runner is `scripts/run_migrations.py`, invoked by `.github/workflows/deploy.yml` from a one-shot container built from the new image. Already-applied files are skipped; editing an applied file is detected as drift and aborts the deploy. To change behavior, add a new file — never rewrite history.

### Recall (Hot Path)

`POST /v1/recall` performs entity matching + graph traversal to return relevant context:
1. Redis entity index lookup (~1ms)
2. Postgres graph traversal via recursive CTE (~5-50ms)
3. Formatted context block grouped by type

No LLM call on the read path.

### App Integration Pattern

```
Your App ←→ Your LLM Gateway ←→ LLM Provider
                    ↓
              Context Quilt
```

Apps authenticate via JWT (required). CQ authenticates apps, not end users — apps are responsible for their own user auth and pass `user_id` to CQ. See [Security & Authentication](docs/architecture/10-security-and-authentication.md) for the full threat model.

## Documentation

### Architecture (docs/architecture/)
- `00-overview.md` — Core concepts
- `01-memory-model.md` — Three tiers, graph layer, entities/relationships
- `02-pipeline.md` — Extraction pipeline, model selection
- `03-queue-and-lifecycle.md` — Meeting queue, batching, context budgeting
- `04-recall.md` — Intelligent recall, entity matching, hot path
- `05-integration.md` — Integration flow, capture points, metadata
- `06-configuration.md` — All settings and env vars
- `07-api-reference.md` — API endpoint documentation
- `08-connected-quilt-model.md` — Patch types, connections, lifecycle
- `09-domain-mapping.md` — How to map CQ concepts to your domain (health, CRM, etc.)
- `10-security-and-authentication.md` — Auth model, threat model, GDPR, defense in depth
- `11-model-selection.md` — Why Claude Haiku 4.5, benchmark results, alternatives

### API Reference
- `docs/openapi.yaml` — OpenAPI 3.0 specification
- FastAPI auto-docs available at `/docs` when running locally

## Development

### Running Locally

```bash
cp .env.example .env  # Edit with your API keys
docker-compose up -d  # Starts API, worker, Postgres, Redis
# API at http://localhost:8000
# Docs at http://localhost:8000/docs
```

### Project Structure

```
src/
  main.py                           # FastAPI app — all API endpoints
  worker.py                         # Cold path worker — extraction pipeline
  auth.py                           # JWT + app authentication
  dashboard/                        # Admin dashboard (router, HTML, JS)
  contextquilt/
    services/
      extraction_prompts.py         # LLM extraction prompts
      llm_client.py                 # OpenAI-compatible LLM client
init-db/                            # PostgreSQL migration scripts
tests/benchmark/                    # Extraction quality benchmarks
docs/architecture/                  # Architecture documentation
```

### Environment Variables

Required:
- `DATABASE_URL` — PostgreSQL connection string
- `REDIS_URL` or `REDIS_HOST`/`REDIS_PORT`/`REDIS_PASSWORD`
- `CQ_LLM_API_KEY` — API key for LLM extraction (any OpenAI-compatible provider)
- `CQ_LLM_BASE_URL` — LLM endpoint (default: OpenRouter)
- `CQ_LLM_MODEL` — Model for extraction (default: anthropic/claude-haiku-4.5)
- `CQ_ADMIN_KEY` — Admin dashboard access key
- `JWT_SECRET_KEY` — JWT signing secret

### Testing Extraction Quality

```bash
# Dry-run extraction on a transcript (no database writes)
CQ_LLM_API_KEY=... python tests/benchmark/test_extraction_dryrun.py [transcript_file] [--user "Name"]

# Run full benchmark across models and test cases
CQ_LLM_API_KEY=... python tests/benchmark/run_benchmark.py
```

### Performance Targets
- **Hot path (recall)**: <10ms on cache hit, <50ms on cache miss
- **Cold path (extraction)**: 2-10s per meeting (async, non-blocking)
- **Pre-warm**: <50ms to hydrate Redis cache

## Patent Notice

This project includes innovations protected by provisional patent application. When modifying core architectural components, preserve the novel aspects of the asynchronous zero-latency architecture, hybrid cognitive data model, and active enrichment methods.

## License

Apache 2.0 — See [LICENSE](LICENSE) for details.

## Contact

- **Website**: [contextquilt.com](https://contextquilt.com)
- **Email**: scott@contextquilt.com
- **Issues**: [GitHub Issues](https://github.com/scottxxxxx/contextquilt/issues)
