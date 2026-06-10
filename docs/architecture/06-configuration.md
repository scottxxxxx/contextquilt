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
| `CQ_LLM_API_KEY` | OpenRouter API key (fallback path) | `sk-or-...` |
| `CQ_LLM_BASE_URL` | OpenAI-compatible API endpoint (fallback path) | `https://openrouter.ai/api/v1` |
| `CQ_LLM_MODEL` | Model name for the OpenRouter fallback path | `anthropic/claude-haiku-4.5` |
| `CQ_ANTHROPIC_API_KEY` | Anthropic API key (primary path) — or resolved from Secret Manager | `sk-ant-...` |

`CQ_ANTHROPIC_API_KEY` may be left unset if `CQ_GCP_PROJECT` is set and the `anthropic-api-key` secret exists in Secret Manager — see [Secret Management](#secret-management) below.

## Extraction Model Settings

### LLM Provider Routing

The worker runs Anthropic native as the primary extraction path and falls back to OpenRouter on transient failures (auth/timeout/5xx, narrow allowlist). The routing is wired in `src/contextquilt/services/llm_client_fallback.py` and selected at worker startup by `_build_default_llm_client` in `src/worker.py`. See [11-model-selection.md](11-model-selection.md) for the rationale and the fallback failure-type matrix.

| Variable | Default | Description |
|----------|---------|-------------|
| `CQ_LLM_PRIMARY_PROVIDER` | `anthropic` | Either `anthropic` (Anthropic-direct primary, OR fallback wrapper) or `openrouter` (OR-only, no fallback wrapper). |
| `CQ_ANTHROPIC_API_KEY` | (from Secret Manager when `CQ_GCP_PROJECT` set) | Anthropic API key. Env wins over Secret Manager. |
| `CQ_ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Bare native Anthropic model ID, not the OR slash form. |
| `CQ_LLM_API_KEY` | (required for fallback) | OpenRouter API key. |
| `CQ_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | OpenAI-compatible endpoint for the fallback path. |
| `CQ_LLM_MODEL` | `anthropic/claude-haiku-4.5` | Model name for the OR fallback path (slash form). |
| `CQ_LLM_CONTEXT_WINDOW` | Auto-detected from model | Override when running an unknown model. |

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

**Production default (Anthropic primary, OpenRouter fallback, SM-backed key):**

```env
CQ_LLM_PRIMARY_PROVIDER=anthropic
CQ_ANTHROPIC_MODEL=claude-haiku-4-5-20251001
CQ_GCP_PROJECT=contextquilt              # CQ_ANTHROPIC_API_KEY resolved from SM
CQ_LLM_API_KEY=sk-or-your-key            # OR fallback key
CQ_LLM_BASE_URL=https://openrouter.ai/api/v1
CQ_LLM_MODEL=anthropic/claude-haiku-4.5
```

**Anthropic-direct only (no fallback, env-only keys):**

```env
CQ_LLM_PRIMARY_PROVIDER=anthropic
CQ_ANTHROPIC_API_KEY=sk-ant-your-key
CQ_ANTHROPIC_MODEL=claude-haiku-4-5-20251001
# Still set CQ_LLM_* so the fallback wrapper has a valid OR client to construct;
# leave CQ_LLM_API_KEY empty if you genuinely don't want the wrapper.
```

**OpenRouter only (legacy path, no Anthropic-direct):**

```env
CQ_LLM_PRIMARY_PROVIDER=openrouter
CQ_LLM_API_KEY=sk-or-your-key
CQ_LLM_BASE_URL=https://openrouter.ai/api/v1
CQ_LLM_MODEL=anthropic/claude-haiku-4.5
```

**OpenAI direct via the OR-compat client (legacy / test):**

```env
CQ_LLM_PRIMARY_PROVIDER=openrouter
CQ_LLM_API_KEY=sk-your-openai-key
CQ_LLM_BASE_URL=https://api.openai.com/v1
CQ_LLM_MODEL=gpt-4.1-nano
```

**Local model via Ollama (free, no API key):**

```env
CQ_LLM_PRIMARY_PROVIDER=openrouter
CQ_LLM_API_KEY=ollama
CQ_LLM_BASE_URL=http://localhost:11434/v1
CQ_LLM_MODEL=qwen2.5:7b
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
| `CQ_KEY_ENCRYPTION_KEY` | (none — plaintext storage in dev) | Symmetric key for encrypting BYOK third-party API keys at rest. |

## Secret Management

Managed provider keys (`CQ_ANTHROPIC_API_KEY`, `CQ_LLM_API_KEY`) can be sourced from GCP Secret Manager instead of `.env`. Set `CQ_GCP_PROJECT` and `src/contextquilt/secrets.py:ensure_secrets_in_env` will populate `os.environ` from SM before pydantic Settings builds at process startup.

| Variable | Default | Description |
|----------|---------|-------------|
| `CQ_GCP_PROJECT` | (empty — env-only mode) | GCP project ID holding the secrets. Empty means no SM lookup. |

**SM resolution order** (`get_secret` in `secrets.py`):

1. If the env var is set non-empty, return it. Local dev and operator overrides win.
2. Otherwise, fetch `projects/{CQ_GCP_PROJECT}/secrets/{secret_name}/versions/latest`.
3. On any failure (no project, missing SDK, RPC error), return `""` and log a warning. Callers decide if that's fatal.

The mapping from env var to SM secret ID is in `_SECRET_MANAGER_MAPPINGS`:

| Env var | SM secret ID |
|---|---|
| `CQ_ANTHROPIC_API_KEY` | `anthropic-api-key` |
| `CQ_LLM_API_KEY` | `openrouter-api-key` |

**Rotation:** Use the dashboard **Providers** tab. The `POST /api/dashboard/update-key` handler does the three-step GP-parity live mutation:

1. `object.__setattr__` on the frozen Settings (in-process key swap, no validation thrash)
2. `os.environ[env_var]` aligned so subprocess Python sees the same value
3. `persist_to_secret_manager(secret_name, value)` writes a new SM version, auto-creating the secret on first write

`get_secret` cache is then busted so the next caller resolves the fresh value. SM persist failures are logged but do **not** roll back the in-memory mutation — the running process IS using the new key; the operator just needs to fix SM or re-paste after the next restart.

**IAM:** The VM Compute SA needs `secretmanager.secrets.create`, `secretmanager.versions.add`, and `secretmanager.versions.access` on the project. A custom role with exactly these three (`projects/{project}/roles/runtimeSecretWriter`) is the minimum-grant pattern; `roles/secretmanager.admin` also works but is broader than the handler actually uses.

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
