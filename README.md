# ContextQuilt

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-purple.svg)](https://modelcontextprotocol.io)
[![Live MCP Server](https://img.shields.io/badge/MCP_Server-Live-green.svg)](https://mcp.contextquilt.com/health)

**Persistent cognitive memory for AI applications.**

ContextQuilt gives your AI app memory that persists across sessions, platforms, and conversations. It sits between your application and LLM provider, automatically extracting and recalling user context with zero added latency on the read path.

## The Problem

LLMs are stateless. Every conversation starts from scratch. Users repeat themselves. Context is lost. If you use multiple AI platforms, memory is siloed in each one.

## How ContextQuilt Solves It

ContextQuilt acts as a **cognitive memory layer** that:

- **Remembers** user preferences, decisions, commitments, and relationships across sessions
- **Recalls** relevant context in <10ms when your app queries the LLM
- **Extracts** structured knowledge from conversations asynchronously (never blocks the user)
- **Connects** facts into a knowledge graph — "who committed to what, blocked by whom"

```
Your App ←→ Your Gateway ←→ LLM Provider
                 ↓
           ContextQuilt
              ↓           ↓
         [Recall]    [Extract]
         (sync)      (async)
           ↓           ↓
         Redis ←── PostgreSQL
```

## Quick Start

```bash
git clone https://github.com/scottxxxxx/contextquilt.git
cd contextquilt

# Configure
cp .env.example .env
# Edit .env: add your LLM API key (any OpenAI-compatible provider)

# Run
docker-compose up -d

# API available at http://localhost:8000
# Interactive docs at http://localhost:8000/docs
```

## Core Concepts

### Patches

A **patch** is a typed fact about a user:

| Type | Example | Persistence |
|------|---------|-------------|
| `trait` | "Tends to over-explain technical details" | Permanent |
| `preference` | "Prefers Nova 3 over Nova 2 for transcription" | Permanent |
| `commitment` | "Scott: deliver samples by Friday" | 30-day TTL |
| `blocker` | "Waiting on Travis to upload files" | 30-day TTL |
| `decision` | "Use AWS S3 for file storage" | Permanent |
| `takeaway` | "Production deadline is June 28" | 14-day TTL |

### Connections

Patches are **stitched together** with typed connections:

```
[Scott] --owns--> [Deliver samples by Friday]
                        |
                  --belongs_to--> [Florida Blue Project]
                        |
                  --blocked_by--> [Waiting on Travis]
```

Connection roles: `parent`, `depends_on`, `resolves`, `replaces`, `informs`

### Three-Tier Memory

| Tier | Store | Purpose | Latency |
|------|-------|---------|---------|
| Working Memory | Redis | Pre-computed context for fast recall | <1ms |
| Factual Memory | PostgreSQL | Patches, entities, relationships | <50ms |
| Episodic Memory | Graph (PostgreSQL CTEs) | Multi-hop relationship traversal | <50ms |

## API Overview

### Recall Context (Hot Path)

```bash
curl -X POST http://localhost:8000/v1/recall \
  -H "X-App-ID: your-app" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-123",
    "text": "What did we decide about the API?",
    "metadata": {"project_id": "proj-456"}
  }'
```

Returns a formatted context block ready to inject into your LLM prompt:

```json
{
  "context": "Project: Widget API\n\nDecisions:\n- Use REST for external, gRPC for internal\n\nOpen commitments:\n- Scott: API schema review (by Friday)\n...",
  "matched_entities": ["Widget API", "Scott"],
  "patch_count": 8
}
```

### Store Memory (Cold Path)

```bash
curl -X POST http://localhost:8000/v1/memory \
  -H "X-App-ID: your-app" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "user-123",
    "interaction_type": "meeting_transcript",
    "content": "Full meeting transcript here...",
    "metadata": {
      "project_id": "proj-456",
      "meeting_id": "mtg-789"
    }
  }'
```

Returns immediately (`{"status": "queued"}`). The worker asynchronously extracts patches, entities, and relationships.

### Manage the Quilt

```bash
# View all patches for a user
GET /v1/quilt/{user_id}

# Incremental sync (only changes since last fetch)
GET /v1/quilt/{user_id}?since=2024-01-01T00:00:00Z

# Edit a patch
PATCH /v1/quilt/{user_id}/patches/{patch_id}

# Delete a patch
DELETE /v1/quilt/{user_id}/patches/{patch_id}

# Visual graph of the entire quilt
GET /v1/quilt/{user_id}/graph?format=svg
```

### Authentication

```bash
# Register your app
POST /v1/auth/register  {"app_name": "my-app"}
# Returns: app_id + client_secret

# Get a JWT token
POST /v1/auth/token  (OAuth2 password flow with app_id:client_secret)

# Use the token
curl -H "Authorization: Bearer {token}" ...

# Or use the simpler header auth
curl -H "X-App-ID: {app_id}" ...
```

Full API documentation: [docs/openapi.yaml](docs/openapi.yaml) | Interactive docs at `/docs` when running.

## MCP Server

ContextQuilt is available as an [MCP](https://modelcontextprotocol.io) server. Any MCP-compatible client (Claude Desktop, Claude Code, Cursor, etc.) can use CQ for persistent memory.

**Connect from Claude Code:**

```bash
claude mcp add contextquilt --transport sse \
  --url https://mcp.contextquilt.com/sse \
  --header "Authorization: Bearer YOUR_API_KEY"
```

**Or add to your project's `.mcp.json`:**

```json
{
  "mcpServers": {
    "contextquilt": {
      "type": "sse",
      "url": "https://mcp.contextquilt.com/sse",
      "headers": {
        "Authorization": "Bearer YOUR_API_KEY"
      }
    }
  }
}
```

**Available MCP tools:**
- `recall_context` — retrieve relevant memory for a query (<50ms)
- `store_memory` — save a transcript or conversation for extraction
- `get_quilt` — view all patches for a user
- `delete_patch` — remove a specific patch

**Self-hosted:** The MCP server is included in `docker-compose.yml` on port 8001. Set `MCP_API_KEY` in your `.env` to protect it.

To get an API key for the hosted server, email [scott@contextquilt.com](mailto:scott@contextquilt.com).

## Architecture

### Zero-Latency Design

The read path (recall) never calls an LLM. It's pure Redis + PostgreSQL graph traversal, targeting <10ms.

The write path (extraction) runs asynchronously after the user gets their response. A background worker processes transcripts through a single LLM call to extract patches, entities, and relationships.

### Connected Quilt Model

Unlike flat fact stores, ContextQuilt preserves **relationships** between facts. A commitment connects to a project, depends on a blocker, and is owned by a person. This enables queries like "what's blocking the Florida Blue project?" to return the full connected context.

### Extensible Patch Types

Apps can register custom patch types and connection labels via the patch type registry. A health coaching app might add `condition`, `goal`, `metric`, `intervention`. A CRM might add `deal`, `contact`, `interaction`.

### Not Just for Meetings

The API uses terms like "project" and "meeting" but these are generic grouping concepts. A "project" is any named container (a treatment plan, a deal, a repository). A "meeting_id" is any session that produces patches (an office visit, a sales call, a code review). See [Domain Mapping Guide](docs/architecture/09-domain-mapping.md) for how to map CQ to your domain.

## Configuration

See [docs/architecture/06-configuration.md](docs/architecture/06-configuration.md) for all settings.

Key environment variables:

```bash
# Required
DATABASE_URL=postgresql://user:pass@localhost:5432/contextquilt
REDIS_URL=redis://localhost:6379
CQ_LLM_API_KEY=sk-...          # Any OpenAI-compatible provider
CQ_LLM_BASE_URL=https://openrouter.ai/api/v1
CQ_LLM_MODEL=mistralai/mistral-small-3.1-24b-instruct
CQ_ADMIN_KEY=your-admin-key    # Dashboard access
JWT_SECRET_KEY=your-jwt-secret

# Optional
CQ_QUEUE_MAX_WAIT_MINUTES=60   # Meeting queue consolidation window
MEMORY_RETENTION_DAYS=90       # Default patch retention
CACHE_TTL_SECONDS=3600         # Redis cache TTL
```

## Admin Dashboard

ContextQuilt includes a built-in admin dashboard at `/dashboard/`:

- **Overview**: KPI cards, ingestion charts, patch distribution
- **Users**: Browse users, view quilts, edit/delete patches
- **Communication Profiles**: Per-user style scoring (verbosity, directness, etc.)
- **Patch Types**: Manage the type registry and connection vocabulary
- **Pipeline Playground**: Dry-run extraction with streaming output
- **Extraction Costs**: Real cost tracking per model
- **System Health**: Postgres/Redis/Worker/LLM status

## Contributing

We welcome contributions! Please open an issue first to discuss what you'd like to change.

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Run tests: `pytest tests/ -v`
5. Open a Pull Request

## License

Apache 2.0 - See [LICENSE](LICENSE) for details.

## Contact

- **Website**: [contextquilt.com](https://contextquilt.com)
- **Email**: scott@contextquilt.com
- **Author**: [Scott Guida](https://github.com/scottxxxxx)

---

Built by [Scott Guida](https://github.com/scottxxxxx). Patent pending.
