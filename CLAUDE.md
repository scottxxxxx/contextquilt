# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ContextQuilt** is a persistent cognitive memory layer for AI applications — a slide-in-place layer between applications and LLM providers that fixes stateless-LLM "goldfish memory" and cross-platform memory fragmentation.

## Core Architecture Principles

### The "Zero-Latency" Asynchronous Architecture

- **Read Path (Synchronous)**: live LLM calls query Redis "Working Memory" + fast Postgres lookups for instant context injection. No LLM call on the read path. Never block it with expensive operations.
- **Write Path (Asynchronous)**: background worker does cognitive consolidation (extraction, dedup, lifecycle) after the user already has their response.

### The Connected Quilt Data Model

Three memory tiers: **Factual** (Postgres `context_patches` — typed patches), **Episodic** (entities + relationships graph), **Working** (Redis, short TTL). Patches are typed (trait, preference, identity, role, person, org, project, deliverable, decision, commitment, blocker, takeaway, goal, constraint, event) and connected by stitching (roles: parent, depends_on, resolves, replaces, informs). Per-app taxonomy lives in registered manifests (`app_schemas`, see `init-db/11_shouldersurf_schema.json`); the schema-driven prompt builder generates extraction prompts from them. Manifest `ingest_mode` (extraction|structured) picks the ingest transformer and is ENFORCED by the worker when explicitly declared (undeclared → legacy routing; SS predates the key). Onboarding a new app = starter template (`templates/manifests/`, served at `GET /v1/schema/templates`) → lint via `POST /v1/schema/validate` → admin registration; see `docs/architecture/13-app-onboarding.md`.

## Key Technical Concepts

### Extraction Pipeline (Cold Path, src/worker.py)

One LLM call per meeting extracts typed patches + entities + relationships + `resolved_commitments` (open commitments are injected into the prompt, overdue-first with deadline annotations). The `(you)` speaker marker identifies the app user. Input is prefixed with `Meeting date:` (anchors relative-deadline resolution) and `User language:` (from `metadata.language`, BCP-47; absent → auto-detect — output prose is written in the user's language, anchored by a required `output_language` schema field).

After the LLM call a fixed sanitizer chain (`src/contextquilt/services/extraction_schema.py`) enforces invariants the LLM is unreliable about, in order: `enforce_owner_gate`, `enforce_connection_requirements`, `enforce_person_ownership`, `enforce_connection_vocabulary` (manifest label from/to combos — flips reversed edges, drops invalid), `sanitize_you_marker_from_patches`, `strip_owner_on_self_typed_patches`, `strip_prose_from_person_names`, `drop_placeholder_and_self_person_patches`, `drop_placeholder_entities` (Speaker-N/Unknown names out of the entities array + their relationships; same predicate re-checked defensively in `store_entities` for the chat/structured lanes), `sanitize_cues` (normalizes `value.cues` associative-retrieval topic phrases; drops generic/entity-name duplicates), `sanitize_salience` (keeps only `value.salience` low|high; absent = normal), `sanitize_deadline_dates` (validates LLM-resolved `value.deadline_date` ISO dates), `strip_ephemeral_fields`. New sanitizers slot into this chain; each is unit-tested under `tests/unit/`. Backfill scripts in `scripts/backfill_*.py` reuse the live sanitizers — write new ones the same way (dry-run default, `--apply` writes).

**Cues (associative retrieval)**: extraction emits `value.cues` — short lowercase topic phrases naming what a patch is ABOUT ("pricing model") — stored in `patch_cues` (migration 27), popped from value before the JSONB is written. Dedup/longitudinal re-observation UNIONs new cues into the surviving patch. Manifest hooks: `extraction_prompt_guidance.cue_guidance` (replace default instruction), `cues_enabled: false` (kill), per-type `patch_types[].cue_guidance`. Recall-side consumption (cue index → fetch leg) ships separately.

**Corrections (contract item 9, `interaction_type=correction`)**: user chat corrections supersede stale patches — worker `handle_correction` builds a candidate set (patches whose text appears in the passed `context_block` first, then scoped recent), one LLM call picks the contradicted patch BY ID (resolved_commitments pattern; hallucinated ids downgrade to unmatched), then: new patch `origin_mode='declared'`/`source_prompt='correction'`, stale patch archived (+ `value.corrected_by`/`correction_source` stamps, flows delta sync), connected with role `replaces`. Unmatched corrections still land (never lose a user-stated fact). Pure logic in `services/corrections.py`. **Completions (item 10, `interaction_type=completion`)**: same matching against OPEN completables only; closes via the standard machinery (`completion_source='user_chat'` + evidence → delta `completed` array); unmatched completions DROPPED (closing needs a target; corrections land, completions don't).

**Storage dedup is two-tier** (`store_connected_patches`): trigram similarity > 0.6 → same fact (fast path); 0.35–0.6 gray zone → one batched LLM judge call per extraction (`services/semantic_dedup.py`, kill switch `CQ_SEMANTIC_DEDUP_ENABLED`); judge failure → insert, never lose a memory. Dedup hits merge deadline detail forward and bump `last_observed_at`. **Entity aliasing** (`services/entity_aliasing.py`, `entity_aliases` table): extracted entity names resolve exact → recorded alias → conservative unique-candidate heuristic before creating new entities; recall matches aliases and resolves to canonical.

**LLM client gotcha**: `AnthropicLLMClient.extract()` accepts `json_schema` for interface parity but does NOT enforce it on the wire — any prompt used with it must embed the exact raw-JSON output shape, or the model answers in prose and parsing silently fails.

### Deadline & Freshness Lifecycle

- `value.deadline` (as spoken) + `value.deadline_date` (LLM-resolved ISO). Recall renders `(OVERDUE | due today | due soon)` markers; scorer boosts overdue/imminent commitment/blocker.
- Decay (`worker.decay_loop`, type TTLs via `patch_type_registry` + defaults; per-patch `value.salience` stretches/shrinks effective TTL ×1.5/×0.5 — access-exemption window unmodified): self-typed types (trait/preference/goal/constraint) anchor on `COALESCE(last_observed_at, created_at)` (540d TTL); commitment/blocker anchor on `GREATEST(updated_at, deadline_date)` — never archived before their due date; others on `updated_at`. Recall bumps `patch_usage_metrics.last_accessed_at`, which exempts actively-recalled patches from decay.
- Consolidation (`worker.consolidation_loop`, 24h, kill switch `CQ_CONSOLIDATION_ENABLED`): synthesizes higher-order patches from cue-clustered sources per manifest `consolidation_rules` (no rules → inert). Derived patches: `origin_mode='derived'`, `source_patch_ids`, `value.source_cue` idempotency stamp, `informs` connections from sources. See docs/architecture/14-consolidation.md.
- `deadline_sweep_loop` stamps `value.overdue_since` on open completables past deadline (app-visible, flows into delta sync). Project-scoped recall guarantees up to 5 overdue completables surface regardless of recency windows.
- `last_observed_at` moves ONLY via the worker dedup re-observation path; admin edits move only `updated_at`. Recall scorer applies freshness penalty `max(0.30, exp(-days_stale/365))` to self-typed types, with `now` bucketed to the UTC day — **all recall output must stay byte-stable within a UTC day** (upstream prompt caching depends on it).
- Adding a self-disclosed type to the freshness model: update `FRESHNESS_TRACKED_TYPES` in `recall_scorer.py` AND `worker.decay_loop` AND the partial index in `init-db/20_preference_freshness.sql`.
- **Worker gotcha**: "constants" like `DECAY_INTERVAL_SECONDS` are local to their coroutine bodies, not module scope — verify before referencing from another loop (a NameError in any gathered loop crash-loops the whole worker).

### Database Migrations

`init-db/*.sql`, tracked by filename + sha256 in `schema_migrations`, applied by `scripts/run_migrations.py` from the deploy workflow. Editing an applied file aborts the deploy as drift — always add a new file.

### Recall (Hot Path, POST /v1/recall)

Redis entity index (names ∪ aliases, self-healing) + cue index (`cue_index:{user}`, patch_cues topics, degrades if table absent) → entity/cue match in text → recursive-CTE graph traversal → patch fetch (project-scoped + universal + overdue guarantee + cue-matched leg) → heuristic scoring (cue-fetched +75) → formatted block. Matched cues gate recall alongside entities and suppress metamemory gap claims. Scoped recalls that truncate append a coverage line `(showing N of M stored patches for this project)` — always on, contract commitment E. `metadata`: `project_id`/`project` (scope), `locale` (grouped-mode labels), `token_budget` (flat-mode size, default 700, clamped 100–2000; ~4 chars/token). 30s render cache keyed on the full request shape. Flat mode markers are deliberately English (LLM-facing).

### Quilt API (app-facing, JWT or X-App-ID)

- `GET /v1/quilt/{user_id}` — full sync or `since=` delta (`deleted` + `completed` arrays distinguish decayed from resolved); `origin_id=<meeting UUID>` meeting view (capture order, no ranking); `group_by=origin` adds a `meetings` array; `project_id=` project rundown filter + `limit=` cap (context-flow contract — GP's rundown route). Patches carry `deadline_date`, `origin_id/type`, connections.
- `POST /v1/quilt/{user_id}/patches/{patch_id}/complete` — app-initiated completion (commitment/blocker; 409 on race with worker auto-close). Both close paths stamp `value.completion_source` + `completion_evidence`.
- Triage siblings: `POST .../vouch` ("Still live": stamps `last_vouched_at`, bumps `updated_at` = the decay extension), `POST .../shelve` / `DELETE .../shelve` ("Let it go": stamps `value.shelved_at`, patch STAYS active so recall keeps it; ledger + counts exclude it, /v1/quilt keeps it with the stamps, never a tombstone), `POST .../uncomplete` (reverses a completion from ANY lane; restores active, moves completion stamps to `prior_*`, item reappears via normal delta). Person detail serves completion history (`commitments.completed_they_owe`/`_you_owe`, `{total, items}` capped 20, completed_at-gated so decay never appears as a delivery); detail route only. Ledger items carry `decay_state` (live|aging|stale, open vocab) derived from `services/decay_model.py` — the SAME module the worker decay loop consumes (bands = fractions of effective TTL, bucketed to the UTC day). Archive sites stamp `value.archive_cause` (decay|replaced|corrected|merge|project_archived|cleanup|dedup); doc 16 §5.7.
- Project scope lifecycle: `POST /v1/origins/{u}/{ot}/{oid}/assign-project` (rescope) and its mirrors `.../unassign-project` (optional project_id guard) + `POST /v1/projects/{u}/{pid}/unscope` (project-deletion form; patches survive unscoped). Cleanups archive, never hard-delete (delta-sync tombstone lesson); `scripts/cleanup_orphan_memory.py` (dry-run default).
- `GET /v1/schema` — caller's own latest registered manifest (launch refresh). Admin-gated variant: `GET /v1/apps/{app_id}/schema`.
- CQ authenticates apps, not end users; apps vouch for `user_id`. See `docs/architecture/10-security-and-authentication.md`.

### Cross-team note

ContextQuilt is consumed by ShoulderSurf (iOS) through the GhostPour gateway. **Verify additive API changes through GP's proxied path, not just CQ's socket** — GP middleboxes have eaten metadata keys and query params before. Coordination details live in the private ops dossier (see global CLAUDE.md pointer).

## Documentation

`docs/architecture/00-18` (overview, memory model, pipeline, queue, recall, integration, configuration, API reference, connected quilt, domain mapping, security, model selection, structured ingest, app onboarding/templates, consolidation, context-flow contract, people, prompt composition authority, app memory isolation) and `docs/openapi.yaml`. Four of these carry decisions rather than description, so read them before touching what they cover. **15** is the LOCKED three-team contract: changes to the project-chat context flow require the three-way test per its item 8. **16** is the People object type, now SHIPPED and live (identity write-back, person_appearances, the read surface, `owed_to` and manifest v9); its `capabilities` block is the contract that null means not tracked while an empty list means none open. **17** is the ratified companion to 15: GP owns the prompt composition recipe and SS executes it, so a placement change must never need an SS build. **18** is the app-isolation decision: each app gets its own subject space and there are no shared person objects, no `app_id` column and no per-app read filters; if you are about to add any of those, that doc explains what was measured and why it was declined. FastAPI auto-docs at `/docs`. NOTE: docs/openapi.yaml lags the June 2026 surface (meeting views, complete endpoint, token_budget, language, People); update when touched.

## Development

```bash
cp .env.example .env && docker-compose up -d   # API :8000, docs at /docs
.venv/bin/python -m pytest tests/unit/ -q      # unit suite (asyncpg/fastapi absent locally:
#   ignore test_run_migrations, test_split_compound_person_patches, test_update_key,
#   test_structured_ingest_db — the last needs TEST_DATABASE_URL + a live PG; run it in
#   docker/CI: TEST_DATABASE_URL=... pytest tests/unit/test_structured_ingest_db.py)
```

```
src/main.py        # FastAPI hot path — all API endpoints, recall
src/worker.py      # cold path — extraction, dedup, decay, deadline sweep
src/contextquilt/services/   # extraction_prompts, extraction_schema (sanitizers),
                             # schema_prompt_builder, recall_scorer/formatter,
                             # semantic_dedup, entity_aliasing, llm_client*
src/dashboard/     # admin dashboard (router + HTML/JS tabs incl. Memory Health)
init-db/           # migrations; scripts/ backfills + ops tools
```

Required env: `DATABASE_URL`, `REDIS_URL` (or host/port/password), `CQ_LLM_API_KEY`, `CQ_LLM_BASE_URL`, `CQ_LLM_MODEL`, `CQ_ADMIN_KEY`, `JWT_SECRET_KEY`. Anthropic-direct primary uses `CQ_ANTHROPIC_API_KEY` / Secret Manager (`CQ_GCP_PROJECT`); `CQ_LLM_PRIMARY_PROVIDER` flips anthropic↔openrouter. `CQ_SEMANTIC_DEDUP_ENABLED` kills the dedup judge.

Extraction quality: `CQ_LLM_API_KEY=... python tests/benchmark/test_extraction_dryrun.py [transcript] [--user "Name"]`.

Performance targets: recall <10ms cache-hit / <50ms miss; extraction 2–10s async; prewarm <50ms.

## Patent Notice

Provisional patent covers the asynchronous zero-latency architecture, hybrid cognitive data model, and active enrichment methods — preserve these when modifying core components.

## License & Contact

Apache 2.0. [contextquilt.com](https://contextquilt.com) · scott@contextquilt.com · [GitHub Issues](https://github.com/scottxxxxx/contextquilt/issues)
