# 2026-04-19 — v1 Prod Bootstrap (Drift Recovery + Healthcheck Fixes)

**Severity:** Low (no user-facing outage; caught during planned rollout)
**Duration:** ~1 hour of troubleshooting + ~2 minutes of MCP Redis downtime during recovery
**Detected by:** Attempt to register the ShoulderSurf schema against prod after v1 PR stack merged

---

## Summary

When running the ShoulderSurf schema registration script against the production API for the first time, the call returned `500 Internal Server Error`. Investigation revealed the production DB schema was multiple migrations behind what the running container expected. Root cause: the deploy workflow's `git pull || true` had been silently failing for an unknown period, leaving `/opt/contextquilt/` as a non-git directory and preventing migrations 08-12 from reaching the VM at all.

Four secondary bugs surfaced during recovery:
1. `patch_type_registry` primary key didn't allow per-app type scoping.
2. Both `contextquilt-worker` and `cq-mcp-worker` were inheriting the API container's `curl localhost:8000/health` healthcheck, which was always failing because workers don't serve HTTP.
3. `cq-mcp-worker`'s compose file did override the healthcheck but used `pgrep` which isn't in the slim Python base image.
4. Accidentally recreated `cq-mcp-redis` with the wrong `--env-file`, briefly breaking Redis config parsing (empty `requirepass` expansion).

All resolved. Prod is fully healthy.

---

## Timeline

| Time (UTC)* | Event |
|---|---|
| pre-incident | PR stack (#44-#53) merged to main earlier in day; GitHub Actions deploy "succeeded" — but `git pull` was silently failing on the VM |
| +0:00 | First attempt to register SS schema: `POST /v1/apps/{uuid}/schema` returns `500` |
| +0:05 | Container logs reveal `UniqueViolationError: duplicate key value violates unique constraint "patch_type_registry_pkey" — Key (type_key)=(trait)` — existing universal seed collides with SS's app-scoped types |
| +0:10 | DB inspection reveals `app_schemas`, `entity_type_registry` tables don't exist — migration 10 never ran |
| +0:15 | Further inspection: `/opt/contextquilt/` isn't a git repo; `init-db/` on disk has migrations 01-06 only (08, 09, 10, 12 missing); deploy workflow's `sudo git pull || true` silently failing for months |
| +0:20 | `scp` missing migrations to VM; apply in order 08 → 09 → 10 → 12. All clean. |
| +0:25 | Retry SS registration; still 500 — same PK collision. Diagnose: PK was `(type_key)` alone, doesn't allow per-app types with same names as universal seed. |
| +0:30 | Apply ad-hoc PK fix on prod (drop PK, add surrogate UUID PK, NULL-safe unique index on `(type_key, app_id)`) |
| +0:35 | SS schema registration succeeds (13 types, 10 labels, 7 entity types) |
| +0:40 | Run voice backfill — 19 patches rewritten to clean second-person voice. Idempotent on second pass. |
| +0:45 | Notice `contextquilt-worker` shows `(unhealthy)` — Dockerfile HEALTHCHECK was `curl http://localhost:8000/health`, wrong for workers |
| +0:50 | Fix main worker healthcheck, deploy live, verify healthy |
| +0:55 | Same fix for `cq-mcp-worker`; initial attempt used wrong `--env-file`, broke `cq-mcp-redis` for ~2 min |
| +1:00 | MCP stack fully recovered with correct env file; all 6 CQ containers `(healthy)` |
| +1:05 | All fixes committed as PRs #55, #56, #57 (migration 13 + healthcheck fixes) |

*Approximate; based on commit timestamps and SSH session.

---

## Root causes

### Primary: silent deploy drift

`.github/workflows/deploy.yml` had:

```bash
sudo git pull origin main --ff-only || true
# ...
for f in init-db/*.sql; do
  sudo docker exec -i ... psql ... < "$f" 2>&1 || true
done
```

The `|| true` on `git pull` swallowed the fact that `/opt/contextquilt/` wasn't a git repo. The same pattern on each migration swallowed any SQL failures. Net effect: for some unknown period, the container image was being updated (because `docker compose pull` uses GHCR tags, not the filesystem) but the on-disk `init-db/` files — the only thing the migration loop could see — stayed frozen at whatever version was there when /opt/contextquilt last had a working git checkout.

### Secondary: patch_type_registry PK design bug

Migration `04_connected_quilt.sql` made `type_key` the PRIMARY KEY of `patch_type_registry`. When PR 1 added `app_id` to the table to support per-app scoping, it didn't relax the PK. That blocked any app from registering a type with a name shared by the universal seed (which is the expected case — SS's `trait` is intentionally the same concept as the universal `trait`, just app-scoped).

### Tertiary: workers inheriting the API healthcheck

Dockerfile has an image-level `HEALTHCHECK` curling `localhost:8000/health`. Both workers run from the same image but neither serves HTTP. The prod compose file didn't override for `contextquilt-worker`; the MCP compose did override but used `pgrep` which is absent from the slim base.

### Compose env-file mix-up during recovery

When reapplying the MCP compose to pick up the healthcheck fix, ran it with `--env-file .env.prod` instead of `.env.mcp`. Compose re-interpolated `${MCP_REDIS_PASSWORD}` as empty, recreating `cq-mcp-redis` with `redis-server --requirepass --maxmemory 128mb` — two of those tokens bled into Redis's config parse. Redis crashed on startup. Restart with `--env-file .env.mcp` resolved it.

---

## Fixes landed

| Change | PR | Landing commit |
|---|---|---|
| Migrations 08/09/10/12 applied live to prod (one-off) | n/a | manual psql |
| Migration 13 — `patch_type_registry` PK relaxation | [#55](https://github.com/scottxxxxx/contextquilt/pull/55) | `34d4ff9` |
| `contextquilt-worker` healthcheck | [#56](https://github.com/scottxxxxx/contextquilt/pull/56) | `a7ef5ac` |
| `cq-mcp-worker` healthcheck | [#57](https://github.com/scottxxxxx/contextquilt/pull/57) | `611e4b2` |
| `/opt/contextquilt/` bootstrapped as a real git repo | n/a | manual `git init` + `reset --hard` |
| **Deploy workflow hardened** (this PR) | #58 (this) | — |
| **Operations runbook** (this PR) | #58 (this) | — |
| **Incident log established** (this PR) | #58 (this) | — |

---

## Preventive actions (this PR)

1. **Deploy workflow uses `set -euo pipefail`** — every step fails loudly. `|| true` on migrations is replaced with `-v ON_ERROR_STOP=1` + explicit break-on-failure logic.
2. **Deploy workflow auto-bootstraps git** — if `/opt/contextquilt/` isn't a git repo, it gets `git init` + remote configured before the first `fetch`. Subsequent deploys `git reset --hard origin/main` to guarantee on-disk state matches the commit being deployed.
3. **Deploy workflow now syncs the MCP stack too** — previously only touched the main compose; MCP drift wouldn't have been caught at all.
4. **`.env.mcp` added to `.gitignore`** — explicit rather than relying on the `.env.prod` pattern + hoping.
5. **Runbook (`docs/ops/runbook.md`)** — SSH, env files, migrations, new app/schema registration, voice backfill, diagnostics, common pitfalls.

---

## What we'd do differently

- The original `|| true` on the migration loop was probably added to handle the case where an old migration couldn't be re-run idempotently. The right answer was to make migrations idempotent (now the case via `IF NOT EXISTS` / DO-block guards), not to suppress errors.
- The healthcheck bug would have been caught earlier if we'd ever looked at `docker ps` post-deploy and noticed `(unhealthy)`. Worth adding to the deploy runbook: always eyeball the container table after a deploy.
- `/opt/contextquilt` not being a git repo was invisible because the `|| true` hid the error. Any deploy verification that `git status` cleanly runs on the VM would have caught this immediately.

---

## Metrics

- **Patches rewritten (voice backfill):** 19 (user `fa4d903c-...`)
- **Types registered for SS:** 13 patch types + 10 connection labels + 7 entity types
- **Migrations applied during recovery:** 4 (08, 09, 10, 12) + 1 ad-hoc PK fix (subsequently codified as 13)
- **MCP Redis downtime:** ~2 minutes (recovery-induced)
- **PRs opened during recovery:** 3 (#55, #56, #57)
- **End-state container health:** 6/6 green

---

## References

- PR stack that merged: [#44](https://github.com/scottxxxxx/contextquilt/pull/44), [#45](https://github.com/scottxxxxx/contextquilt/pull/45), [#50](https://github.com/scottxxxxx/contextquilt/pull/50), [#51](https://github.com/scottxxxxx/contextquilt/pull/51), [#52](https://github.com/scottxxxxx/contextquilt/pull/52), [#53](https://github.com/scottxxxxx/contextquilt/pull/53)
- Related recovery PRs: [#55](https://github.com/scottxxxxx/contextquilt/pull/55), [#56](https://github.com/scottxxxxx/contextquilt/pull/56), [#57](https://github.com/scottxxxxx/contextquilt/pull/57)
- v1 release summary: `docs/memos/v1-release-summary.md`
- Runbook: `docs/ops/runbook.md`
