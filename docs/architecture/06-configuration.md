# 06: Configuration

## Overview

Context Quilt is configured via environment variables. All CQ-specific variables use the `CQ_` prefix. Settings can also be managed through the admin dashboard's Settings page.

## Configuration Methods

1. **Environment variables** — set in `.env` or `.env.prod` files, or in Docker Compose
2. **Admin dashboard** — Settings page at `/dashboard/` (requires `CQ_ADMIN_KEY`)

Both methods are equivalent. Dashboard changes write to the database and take effect immediately. Environment variables are read at startup.

## Required Settings

| Variable | Description | Example |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | `postgresql://postgres:pass@cq-postgres:5432/context_quilt` |
| `REDIS_URL` or `REDIS_HOST`+`REDIS_PORT`+`REDIS_PASSWORD` | Redis connection | `redis://:pass@cq-redis:6379/0` |
| `CQ_LLM_API_KEY` | API key for the extraction model provider | `sk-or-...` |
| `CQ_LLM_BASE_URL` | OpenAI-compatible API endpoint | `https://openrouter.ai/api/v1` |
| `CQ_LLM_MODEL` | Model name for extraction | `mistralai/mistral-small-3.1-24b-instruct` |

## Extraction Model Settings

### Single-Call Mode (Default)

| Variable | Default | Description |
|----------|---------|-------------|
| `CQ_LLM_API_KEY` | (required) | API key for the LLM provider |
| `CQ_LLM_BASE_URL` | `https://api.openai.com/v1` | Provider endpoint |
| `CQ_LLM_MODEL` | `gpt-4.1-nano` | Model for all extraction |
| `CQ_LLM_CONTEXT_WINDOW` | Auto-detected | Override for unknown models |

### Multi-Role Mode

| Variable | Default | Description |
|----------|---------|-------------|
| `CQ_PIPELINE_MODE` | `single` | Set to `multi_role` to enable |
| `CQ_PICKER_MODEL` | Falls back to `CQ_LLM_MODEL` | Model for fact extraction |
| `CQ_PICKER_BASE_URL` | Falls back to `CQ_LLM_BASE_URL` | Picker endpoint |
| `CQ_STITCHER_MODEL` | Falls back to `CQ_LLM_MODEL` | Model for profile organization |
| `CQ_STITCHER_BASE_URL` | Falls back to `CQ_LLM_BASE_URL` | Stitcher endpoint |
| `CQ_DESIGNER_MODEL` | Falls back to `CQ_LLM_MODEL` | Model for communication profiling |
| `CQ_DESIGNER_BASE_URL` | Falls back to `CQ_LLM_BASE_URL` | Designer endpoint |
| `CQ_CATALOGER_MODEL` | Falls back to `CQ_LLM_MODEL` | Model for episode summarization |
| `CQ_CATALOGER_BASE_URL` | Falls back to `CQ_LLM_BASE_URL` | Cataloger endpoint |

### Provider Examples

```env
# OpenRouter (recommended — access to 200+ models)
CQ_LLM_API_KEY=sk-or-your-key
CQ_LLM_BASE_URL=https://openrouter.ai/api/v1
CQ_LLM_MODEL=mistralai/mistral-small-3.1-24b-instruct

# OpenAI direct
CQ_LLM_API_KEY=sk-your-key
CQ_LLM_BASE_URL=https://api.openai.com/v1
CQ_LLM_MODEL=gpt-4.1-nano

# Google Gemini
CQ_LLM_API_KEY=AIza-your-key
CQ_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
CQ_LLM_MODEL=gemini-2.5-flash-lite

# Local model via Ollama (free, no API key)
CQ_LLM_API_KEY=ollama
CQ_LLM_BASE_URL=http://localhost:11434/v1
CQ_LLM_MODEL=qwen2.5:7b

# Local model via vLLM / LiteLLM / LM Studio
CQ_LLM_API_KEY=not-needed
CQ_LLM_BASE_URL=http://localhost:8000/v1
CQ_LLM_MODEL=your-model-name
```

## Queue Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `CQ_QUEUE_MAX_WAIT_MINUTES` | `60` | Minutes of inactivity before processing a queue |
| `CQ_QUEUE_BUDGET_THRESHOLD` | `0.8` | Fraction of context window that triggers immediate processing |

## Security Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `CQ_ADMIN_KEY` | (none — open in dev) | Admin dashboard access key. Required for public deployments. |
| `JWT_SECRET_KEY` | `dev_secret_key...` | JWT signing secret for app authentication. Must change in production. |

## Memory Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_RETENTION_DAYS` | `90` | Days before facts auto-expire (0 = no expiry) |
| `CACHE_TTL_SECONDS` | `3600` | Redis cache TTL (1 hour) |

## Application Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_QUILT_API_PORT` | `8000` | API server port |
| `CONTEXT_QUILT_HOST` | `0.0.0.0` | API bind address |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `WORKERS` | `4` | Uvicorn worker count |

## Admin Dashboard

The dashboard is accessible at `/dashboard/` and provides:

- **System Overview** — KPIs, memory counts, cache hit rates
- **Insights Stream** — Recent patches with type/origin filters
- **The User Quilt** — Browse users, view their full quilt with timeline
- **Schema & Discovery** — View and manage memory schema
- **Pipeline Playground** — Test extraction with live results
- **Settings** — Configure all settings above via web UI

Access is controlled by `CQ_ADMIN_KEY`. Same key as your CloudZap admin for convenience.

## Settings Page (Admin Dashboard)

The Settings page in the admin dashboard allows changing configuration without editing environment files or restarting services. Changes are stored in the database and take effect immediately.

**Sections:**

1. **Extraction Model** — Provider, model name, API key (masked), context window
2. **Pipeline Mode** — Single call vs multi-role, per-role model config
3. **Queue Behavior** — Max wait time, budget threshold
4. **Memory Retention** — Retention period, cache TTL
5. **Security** — Admin key (change), JWT secret (rotate)
