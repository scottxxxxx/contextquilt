# ContextQuilt Operations Runbook

Everything you need to operate ContextQuilt in production without having to reverse-engineer it. If a routine operation is missing here, add it after you do it.

---

## Access

### Production VM

- **Host:** `35.239.227.192`
- **GCP project:** `weirtech-shared-infra`
- **Instance name:** `web-gateway` (`us-central1-a`)
- **Deploy path:** `/opt/contextquilt/` (now a real git checkout as of 2026-04-19)

### SSH — two paths

**Direct SSH (fastest):**
```bash
ssh -i ~/.ssh/id_ed25519 scottguida@35.239.227.192
```

The `id_ed25519` key matches the one stored in the instance's metadata `ssh-keys`. If that metadata entry is removed you need to re-add it or fall through to OS Login.

**gcloud (fallback):**
```bash
gcloud compute ssh web-gateway --zone=us-central1-a --project=weirtech-shared-infra
```

OS Login username is `gixxerscott_gmail_com`. If you hit `Permission denied (publickey)`, add your key:
```bash
gcloud compute os-login ssh-keys add \
  --project=weirtech-shared-infra \
  --key-file=$HOME/.ssh/google_compute_engine.pub
```

Repeated failed SSH attempts trip fail2ban on the VM (usually a 10-30 min cooldown). If you're bouncing off `Connection reset by peer` on port 22, wait.

---

## Environment layout on the VM

Two separate env files, two separate stacks:

| File | Purpose | Stack |
|---|---|---|
| `/opt/contextquilt/.env.prod` | Main CQ API, worker, Postgres, Redis | `docker-compose.prod.yml` |
| `/opt/contextquilt/.env.mcp` | MCP server stack (`cq-mcp-*` containers) | `docker-compose.mcp.yml` |

**Do not mix.** Using `--env-file .env.prod` against `docker-compose.mcp.yml` will recreate `cq-mcp-redis` with an empty `requirepass` and crash it. (This happened during the v1 rollout — see `docs/ops/incidents/2026-04-19-v1-prod-bootstrap.md`.)

Both files are `.gitignore`d and live only on the VM.

### Key variables

`.env.prod` contains (among others):
- `POSTGRES_PASSWORD`, `REDIS_PASSWORD`
- `CQ_ADMIN_KEY` — admin-key for `/v1/apps/{app_id}/schema` and dashboard
- `CQ_LLM_API_KEY`, `CQ_LLM_BASE_URL`, `CQ_LLM_MODEL`
- `JWT_SECRET_KEY`

`.env.mcp` contains:
- `MCP_POSTGRES_PASSWORD`, `MCP_REDIS_PASSWORD`
- `MCP_API_KEY`

---

## Container topology

After a normal deploy, `docker ps` should show every one of these `(healthy)`:

**CQ main stack (from `docker-compose.prod.yml` + `.env.prod`):**
- `contextquilt` — FastAPI API (port 8000, proxied)
- `contextquilt-worker` — cold-path extraction worker
- `cq-postgres` — primary Postgres
- `cq-redis` — working memory / queues

**MCP stack (from `docker-compose.mcp.yml` + `.env.mcp`):**
- `cq-mcp` — MCP server (port 8001, proxied)
- `cq-mcp-worker` — cold-path worker for the MCP instance
- `cq-mcp-postgres` — separate Postgres for MCP
- `cq-mcp-redis` — separate Redis for MCP

Any container in `(unhealthy)` for more than 2 minutes warrants investigation.

---

## Routine operations

### 1. Manual redeploy (skipping GitHub Actions)

```bash
ssh -i ~/.ssh/id_ed25519 scottguida@35.239.227.192
cd /opt/contextquilt
sudo git fetch origin main
sudo git reset --hard origin/main

# Main stack
sudo docker compose -f docker-compose.prod.yml --env-file .env.prod pull
sudo docker compose -f docker-compose.prod.yml --env-file .env.prod up -d

# MCP stack (different env file!)
sudo docker compose -f docker-compose.mcp.yml --env-file .env.mcp pull
sudo docker compose -f docker-compose.mcp.yml --env-file .env.mcp up -d
```

### 2. Apply missed / new migrations

Migrations live in `init-db/*.sql`. The normal deploy workflow applies them automatically. To apply manually:

```bash
ssh -i ~/.ssh/id_ed25519 scottguida@35.239.227.192
cd /opt/contextquilt
sudo git fetch origin main && sudo git reset --hard origin/main

for f in init-db/*.sql; do
  echo "Applying $f..."
  sudo docker exec -i cq-postgres psql -U postgres -d context_quilt \
    -v ON_ERROR_STOP=1 < "$f" || { echo "FAILED: $f"; break; }
done
```

All migrations are idempotent — re-running them on an up-to-date DB should be a no-op.

### 3. Register a new application

```bash
ssh -i ~/.ssh/id_ed25519 scottguida@35.239.227.192
sudo docker exec contextquilt curl -s -X POST \
  http://localhost:8000/v1/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"app_name":"<app-name>"}'
```

Returns `{app_id, client_secret, ...}`. **Save the `client_secret` immediately — it's shown once and never again.** The `app_id` is a UUID you'll pass as `SS_APP_ID` (or equivalent) to the schema registration script.

### 4. Register an application's schema

Prerequisite: the manifest JSON must be committed to `init-db/` (e.g. `11_shouldersurf_schema.json`).

```bash
ssh -i ~/.ssh/id_ed25519 scottguida@35.239.227.192

# Preview before writing
sudo docker exec \
  -e CQ_BASE_URL=http://localhost:8000 \
  -e SS_APP_ID=<app-uuid> \
  contextquilt python /app/scripts/register_ss_schema.py --check

# Real registration
sudo docker exec \
  -e CQ_BASE_URL=http://localhost:8000 \
  -e SS_APP_ID=<app-uuid> \
  contextquilt python /app/scripts/register_ss_schema.py
```

Confirm with:
```bash
sudo docker exec contextquilt bash -c \
  'curl -s http://localhost:8000/v1/apps/<app-uuid>/schema -H "X-Admin-Key: $CQ_ADMIN_KEY"' \
  | python3 -m json.tool | head -20
```

### 5. List registered applications

```bash
sudo docker exec cq-postgres psql -U postgres -d context_quilt \
  -c "SELECT app_id, app_name, created_at FROM applications ORDER BY created_at;"
```

### 6. Run voice backfill (cleanup trait/preference patches)

Dry-run first:
```bash
sudo docker exec contextquilt python /app/scripts/backfill_voice_cleanup.py \
  --dry-run --user-id <user-id>
```

Apply:
```bash
sudo docker exec contextquilt python /app/scripts/backfill_voice_cleanup.py \
  --user-id <user-id>
```

Idempotent — re-running produces `Candidates identified: 0`.

---

## Diagnostics

### Container not healthy

```bash
# Status summary
sudo docker ps --format 'table {{.Names}}\t{{.Status}}'

# Healthcheck diagnosis
sudo docker inspect <container> --format '{{json .Config.Healthcheck}}' | python3 -m json.tool
sudo docker inspect <container> --format '{{range .State.Health.Log}}--- exit {{.ExitCode}}{{"\n"}}{{.Output}}{{end}}' | tail -30
```

Known past issue: the workers (`contextquilt-worker`, `cq-mcp-worker`) used to show `unhealthy` because they inherited the API's `curl localhost:8000/health` check from the Dockerfile. Both compose files now override with a `/proc/1/cmdline` check. If you see that symptom again, verify the override is still in place.

### Logs

```bash
sudo docker logs <container> --tail 100 -f
```

### Database shell

```bash
sudo docker exec -it cq-postgres psql -U postgres -d context_quilt
# or for MCP:
sudo docker exec -it cq-mcp-postgres psql -U postgres -d context_quilt_mcp
```

### Redis shell

```bash
sudo docker exec -it cq-redis redis-cli -a "$(sudo grep REDIS_PASSWORD /opt/contextquilt/.env.prod | cut -d= -f2)"
```

---

## Backups

Postgres volumes:
- `cq-postgres-data` — primary CQ DB
- `cq-mcp-pgdata` — MCP DB

Snapshot before risky operations:
```bash
ssh -i ~/.ssh/id_ed25519 scottguida@35.239.227.192
sudo docker exec cq-postgres pg_dump -U postgres context_quilt \
  > /tmp/context_quilt-$(date +%Y%m%d-%H%M%S).sql
```

(No automated backup job exists yet — known gap.)

---

## Common pitfalls (learned the hard way)

1. **`.env.prod` vs `.env.mcp`** — always pair the right env file with the right compose file. Mixing breaks Redis config parsing.
2. **`/opt/contextquilt` must be a git repo** — the deploy workflow now requires this. If it isn't, bootstrap via `git init && git remote add origin ... && git fetch && git reset --hard origin/main`.
3. **Migrations use `ON_ERROR_STOP=1`** in the deploy — a broken migration fails the deploy loudly instead of silently continuing. If you add a migration, test it is idempotent.
4. **`patch_type_registry` PK** is a NULL-safe composite via index, not a single-column PK. Don't add `ALTER TABLE ... PRIMARY KEY (type_key)` — that was the v1 bug. See migration `13_relax_patch_type_registry_pk.sql`.
5. **Workers don't serve HTTP** — their healthcheck reads `/proc/1/cmdline`, not a localhost port.
6. **SSH rate limiting** — fail2ban on the VM. If you lock yourself out, wait 10-30 min.

---

## References

- GitHub Actions deploy workflow: `.github/workflows/deploy.yml`
- Incident log: `docs/ops/incidents/`
- v1 design + release context: `docs/memos/v1-release-summary.md`
- Manifest format + API spec: `docs/design/app-schema-registration.md`
