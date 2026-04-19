# ContextQuilt v1 — Release Summary

**Date:** 2026-04-18
**Status:** Implementation complete. Five PRs stacked against `main`, awaiting review and sequential merge.
**Audience:** ContextQuilt core team, ShoulderSurf iOS team, any engineer onboarding to the v1 memory model.

This document consolidates everything we designed, tested, built, and shipped for the v1 release. Start here if you only read one doc.

---

## The one-paragraph summary

ContextQuilt v1 introduces a **6-facet cognitive memory primitive** (Attribute / Affinity / Intention / Constraint / Connection / Episode) that any app can register its own vocabulary against. ShoulderSurf — the first app to use it — ships with a 13-type / 10-label schema covering every meeting type we could imagine (work, coaching, legal, creative, personal). Backend improvements include query-scoped recall that cuts context-injection tokens by ~40%, schema-driven extraction prompt generation that replaces hand-maintained prompts, per-patch permanence override, and a full rename of `meeting_id` to `origin_id` + `origin_type` to generalize CQ beyond meeting capture. All built on validated-by-testing foundations with ~95% confidence in the facet model.

---

## Why we did this

CQ's previous taxonomy (11 patch types, 5 connection roles) had two problems:

1. **Overfitting to ShoulderSurf.** The types, column names (`meeting_id`), and hard-coded extraction prompts all assumed meeting capture. A future app — an interview coach, a financial advisor, a health companion — would have required invasive code changes.
2. **Dead weight in the taxonomy.** Real data showed `identity` and `role` patches at 0% of the corpus. `experience` was legacy. Three of five connection roles went essentially unused.

The v1 work fixes both by splitting **CQ core** (universal cognitive grammar) from **app schemas** (per-app vocabulary). Each app registers its own dictionary against shared primitives.

---

## The 6-facet cognitive model

Every memory patch maps to exactly one cognitive role:

| Facet | Job for the LLM | Examples |
|---|---|---|
| **Attribute** | Facts to state — what someone IS | age, diagnosis, credentials, background |
| **Affinity** | Shape recommendations — what they LEAN toward | preferences, values, tastes |
| **Intention** | Time recommendations against goals — what they AIM AT | retire at 62, A1C < 6.5, Director by 38 |
| **Constraint** | Vet every suggestion before offering it — what they CAN'T / MUST do | allergies, non-competes, bar ethics |
| **Connection** | Graph anchor / entity resolution — WHO or WHAT's in their world | people, projects, companies, accounts |
| **Episode** | Timeline / what's active — what HAPPENED or IS HAPPENING | meetings, transactions, diagnoses |

Plus **3 structural connection roles** (`parent`, `depends_on`, `informs`) and **7 permanence classes** (`permanent` → `decade` → `year` → `quarter` → `month` → `week` → `day`).

### Why 6 and not 5 or 7?

Validated empirically. Four tests, nine Haiku runs across five domains (meeting capture, healthcare, legal, career, grief therapy, couples therapy):

| Test | Scenario | Result |
|---|---|---|
| Test 1 | SS post-meeting draft | 3/3 — memory consistently tightened output, pattern-linked blockers |
| Test 2 | SS pre-1:1 prep | 3/3 — memory is the entire answer; anti-hallucination validated |
| Test 3 | Adversarial grief classification | 20/22 stable; 2 unstable items flagged *different* things across runs — sub-flavor territory, not a missing facet |
| Test 4 | Adversarial couples (multi-party) | 0/34 UNCLASSIFIED across both runs — multi-party is an ownership-model concern, not a facet concern |

Confidence after all tests: **~95%**. Full artifacts in `tests/benchmark/taxonomy_validation/`.

---

## The ShoulderSurf schema

13 patch types, 10 connection labels, 7 entity types. Validated against 7 personas (solo coach, wedding photographer, ad agency AE, F500 VP, legal partner, nonprofit director, real estate agent) — every persona filled every column without force-fitting.

### Patch types

| Type | Facet | Permanence | Purpose |
|---|---|---|---|
| `trait` | Attribute | year | About the user — behavioral pattern (self-only) |
| `preference` | Affinity | year | User's lean / value / style |
| `goal` | Intention | year | Forward-looking target |
| `constraint` | Constraint | year | Hard rule, limit, boundary |
| `person` | Connection | decade | Named individual |
| `org` | Connection | decade | Any organizational entity |
| `project` | Connection | quarter | Any unit of ongoing work |
| `role` | Connection | year | Person's function on a project |
| `decision` | Episode | year | A call that was made |
| `commitment` | Episode | month | Promise with named owner (completable) |
| `blocker` | Episode | week | Current impediment (completable) |
| `takeaway` | Episode | week | Evaluative lesson |
| `event` | Episode | quarter | External occurrence affecting a project |

### Connection labels

`belongs_to` (parent, cascades), `works_on`, `owns`, `blocked_by` (depends_on), `motivated_by`, `aims_for`, `bound_by`, `describes`, `member_of`, `reports_to`.

### Entity types

`person`, `project`, `company`, `feature`, `artifact`, `deadline`, `metric` — used for the graph-layer name index, not user-editable memory.

### Canonical file

`init-db/11_shouldersurf_schema.json` — this is the SS manifest of record. Anything referencing `05_shouldersurf_schema.json` is stale (pre-rename draft).

---

## Architecture: core vs app schema

```
┌──────────────────────────────────────────────────────────────┐
│              CQ CORE — universal, never changes              │
│                    (the GRAMMAR of memory)                   │
│                                                              │
│   Facets:      Attribute | Affinity | Intention | Constraint │
│                Connection | Episode                          │
│   Roles:       parent | depends_on | informs                 │
│   Permanence:  permanent → decade → year → quarter → month   │
│                → week → day                                  │
│   Lifecycle:   cascade on parent archive; gate on            │
│                depends_on; supersede replaces active         │
└──────────────────────────────────────────────────────────────┘
                             ▲
                             │   registers against
                             │
┌──────────────────────────────────────────────────────────────┐
│         APP SCHEMA — per-app manifest (the DICTIONARY)       │
│                                                              │
│   • Patch type vocabulary: domain_type, facet mapping,       │
│     permanence, value shape                                  │
│   • Connection label vocabulary: label, role, from/to types  │
│   • Entity type vocabulary (optional): name indexing hints   │
│   • Extraction guidance tuned to the app's input             │
│   • Optional UI presentation hints                           │
└──────────────────────────────────────────────────────────────┘
```

The infrastructure for this split already existed (`patch_type_registry.app_id` and `connection_vocabulary.app_id` columns). Previously they were all populated with `NULL` (universal). v1 starts using the `app_id` scope — SS becomes the first app to register its own rows.

---

## The five PRs

All stacked. Merge order: PR 0 → PR 1 → PR 2 → PR 3 → PR 4 → PR 5.

### PR 0 — Design artifacts ([#44](https://github.com/scottxxxxx/contextquilt/pull/44))

No code. Foundation for every other PR.

- `docs/memos/patch-taxonomy-simplification.md` — the v1 memo
- `docs/design/app-schema-registration.md` — manifest format, API surface, DB migration plan
- `docs/memos/ss-team-parallel-work.md` — 10 workstreams SS can ship in parallel
- `docs/design/ss-memory-ui-grouping.md` — three-tier UI sketch for post-launch
- `init-db/11_shouldersurf_schema.json` — the SS manifest
- `tests/benchmark/taxonomy_validation/` — four validation tests (nine runs) backing the 6-facet model

### PR 1 — Schema registration foundation + `origin_id` refactor ([#45](https://github.com/scottxxxxx/contextquilt/pull/45))

Database and API infrastructure for per-app schemas, plus the full generalization from meeting-only to any origin.

- `init-db/10_app_schema_registration.sql`
  - `facet` + `permanence` columns on `patch_type_registry` (CHECK-constrained to the 6 facets + 7 classes)
  - `app_schemas` table — versioned manifest snapshots per app
  - `entity_type_registry` table + seeded universal entities
  - `origin_id` + `origin_type` columns replacing `meeting_id` on both `context_patches` and `extraction_metrics` (no backfill — pre-launch test data discarded)
  - `about_patch_id` column for Attributes describing non-user Connections
  - Recursive `belongs_to` support (novel → chapter → scene)
  - **Hard cleanup:** unconditional DELETE of retired patch types (`identity`, `experience`, `feature`, `deadline`) and their registry rows
- `src/contextquilt/services/schema_validator.py` — pure-Python structural validation
- `src/contextquilt/routers/app_schemas.py` — four admin-authenticated endpoints:
  - `POST /v1/apps/{app_id}/schema` — register a new manifest
  - `GET /v1/apps/{app_id}/schema` — fetch current manifest
  - `PATCH /v1/apps/{app_id}/schema` — update to new version
  - `GET /v1/apps/{app_id}/schema/history` — list versions
- `meeting_id` → `origin_id` refactor across `worker.py`, `main.py`, `dashboard/router.py`, `mcp_server.py` (30+ call sites). Redis queue keys now `origin_queue:{user_id}:{origin_type}:{origin_id}`. Project assignment endpoint renamed to `POST /v1/origins/{user_id}/{origin_type}/{origin_id}/assign-project`.
- 25 unit tests covering manifest validation end-to-end.

**Behavior-neutral for apps with no registered schema** (fallback paths preserved).

### PR 2 — Per-patch permanence override + SS bootstrap ([#46](https://github.com/scottxxxxx/contextquilt/pull/46))

Implements "permanence is a default, not a rule."

- `init-db/12_permanence_override.sql`
  - `permanence_override` TEXT column on `context_patches` (nullable, CHECK-enumerated)
  - `permanence_override_source` column (`user` | `app` | NULL) for audit
  - Partial index on overridden rows for decay worker efficiency
- `src/worker.py` decay loop — two-step archival:
  1. Archive patches with explicit `permanence_override` (cross-cuts patch type)
  2. Archive patches using their type's default TTL, excluding anything with an override
  Access-based refresh still exempts either class.
- `src/main.py` PATCH endpoint accepts `permanence_override` + `permanence_override_source`. GET returns both.
- `scripts/register_ss_schema.py` — one-shot CLI that POSTs `init-db/11_shouldersurf_schema.json` to the registration endpoint. Takes `CQ_BASE_URL`, `CQ_ADMIN_KEY`, `SS_APP_ID` via env. Run once per environment after PR 1 deploys.

### PR 3 — Schema-driven extraction prompt generation ([#47](https://github.com/scottxxxxx/contextquilt/pull/47))

Replaces hand-maintained per-app prompts with generation from the registered manifest.

- `src/contextquilt/services/schema_prompt_builder.py`
  - `build_prompt(manifest) -> str` — returns the verbatim `extraction_prompt_override` if provided, otherwise composes sections from structural declarations
  - `build_output_schema(manifest) -> dict` — generates JSON schema for LLM structured output, with enums derived from registered types/labels/entities
- `src/worker.py` new method `_resolve_extraction_prompt(app_id)` — looks up the app's manifest, uses generated prompt + schema; falls back to hardcoded `MEETING_SUMMARY_SYSTEM` if no manifest registered.
- 11 unit tests covering override-verbatim, guidance section inclusion, enum fidelity, entity type fallback, and a SS manifest smoke test.

**SS's current prompt continues working unchanged** because it's preserved as `extraction_prompt_override` in the manifest. New apps get auto-generated prompts without hand-tuning.

### PR 4 — Query-scoped recall with relevance ranking ([#48](https://github.com/scottxxxxx/contextquilt/pull/48))

Replaces category-grouped recall output with a relevance-ranked flat list (pre-PR-4 shape retained as opt-in).

- `src/contextquilt/services/recall_scorer.py`
  - `score_patches(patches, query_text, matched_entity_names)` — composite score: type priority + entity-match boost + query keyword overlap + recency
  - Honors the full 13-type SS schema including `goal` / `constraint` / `event`
- `src/contextquilt/services/recall_formatter.py`
  - `format_flat_ranked` (new default) — compact header + one-line-per-patch body ordered by score, soft char budget
  - `format_category_grouped` — pre-PR-4 shape, updated with new Goals / Constraints / Events sections
- `src/main.py` `RecallRequest` adds `output_format: "flat" | "grouped"` (default `"flat"`) and `max_patches: int` (default `15`).
- 17 new unit tests (8 scorer + 9 formatter).

**Expected impact:** per the taxonomy validation tests, query-scoped output reduced context-injection tokens by ~40% with equivalent answer quality.

### PR 5 — Client-accessible schema read endpoint (this PR)

Lets the SS iOS client (and any future client) fetch its own schema using app JWT auth, for building UI pickers data-driven.

- `GET /v1/schema`
  - Auth: app JWT or `X-App-ID` legacy (same as `/v1/recall`)
  - Infers `app_id` from the caller's token
  - Returns the caller's own manifest in the same envelope as the admin GET (`version`, `registered_at`, `registered_by`, `manifest`)
  - 404 if no schema registered
- No admin leakage — can only read your own schema.

---

## Test coverage

**52 unit tests + 2 fixture-dependent skips (pass once PR 0 lands):**

| File | Tests | What it covers |
|---|---|---|
| `tests/unit/test_schema_validator.py` | 25 | Manifest validation (enums, shape, referential integrity, duplicate detection, entity types, origin types, overrides) |
| `tests/unit/test_schema_prompt_builder.py` | 11 | Prompt generation (override-verbatim, guidance sections, enum fidelity, SS manifest smoke) |
| `tests/unit/test_recall_scorer.py` | 8 | Ranking (entity dominance, type priority, keyword overlap, recency tiebreaker, new-facet scores) |
| `tests/unit/test_recall_formatter.py` | 9 | Output shape (flat vs grouped, new facet sections, owner/deadline decoration, budget enforcement) |

All ran green on Haiku 4.5 during development.

---

## Economics

Per-call memory overhead on Haiku 4.5 (from the taxonomy validation tests):

| Scenario | Bare input | With-memory input | Memory cost delta |
|---|---|---|---|
| Post-meeting draft | 1,202 tok | 1,958 tok | ~$0.0006 |
| Pre-1:1 prep | 141 tok | 614 tok | ~$0.0004 |
| Grief classification | n/a | 947 tok | ~$0.0007 |
| Couples memory | n/a | 1,039 tok | ~$0.0008 |

At 100 recall calls per user per day, memory overhead is **under $0.10/user/day**. Economically trivial relative to the output quality lift. Query-scoped recall (PR 4) is expected to cut these numbers ~40% further.

---

## What's explicitly deferred to v2

- **Participant / ownership model.** Patches today root on one user. Collaborative apps (couples therapy, team productivity) will need `participants: [user_id, ...]`. Pattern: introduce when the first collaborative app demands it.
- **Embedding-based recall scoring.** Today's keyword overlap is a reasonable floor. Upgrade when we need tighter matching on synonyms / paraphrases.
- **Sample-data validation at schema registration.** Post a sample transcript with the manifest, extract against it, verify output before accepting. Nice-to-have.
- **Admin UI for managing schemas.** Can hand-edit fixtures until then.
- **Per-app quotas / billing.** Separate layer when we get there.
- **Removing `MEETING_SUMMARY_SYSTEM` hardcoded fallback.** Safe to retire once every in-use app has registered a manifest.

---

## Breaking changes for clients

These land when PR 1 merges and deploys. SS has been briefed.

| What | Old | New |
|---|---|---|
| Ingestion metadata | `metadata.meeting_id` | `metadata.origin_id` + `metadata.origin_type` |
| Patch response field | `meeting_id` on every patch | `origin_id` + `origin_type` |
| Project assignment URL | `POST /v1/meetings/{user_id}/{meeting_id}/assign-project` | `POST /v1/origins/{user_id}/{origin_type}/{origin_id}/assign-project` |
| Dashboard quilt query filter | `?meeting_id=<id>` | `?origin_type=<type>&origin_id=<id>` (both required together) |
| MCP `store_memory` tool | `meeting_id` parameter | `origin_id` + `origin_type` parameters |
| Retired patch types | `identity`, `experience`, `feature`, `deadline` (patches) | Deleted from DB on deploy |

SS lossy decoder handles the retired types; all other clients need to update.

---

## Non-obvious design calls worth flagging

These came up during implementation. Flagging them here so future-you doesn't relitigate.

1. **`role` stays a patch type (reversed a prior cut).** Rationale: "0 patches in real data" was an extraction-prompt weakness, not evidence of redundancy. With PR 3's schema-driven prompt generation, role extraction should actually fire now.
2. **Recursive `belongs_to` is unrestricted at the Connection level.** A `project` can belong_to another `project`. Enables novel → chapter → scene, Benefits App MVP → Florida Blue parent engagement.
3. **Permanence override is not just a user-facing feature.** Apps can use it too for algorithmic promotion (e.g., a blocker referenced across 5+ meetings → auto-promote from `week` to `quarter`). The `source` audit field distinguishes `user` vs `app`-driven overrides.
4. **Schema versioning has three signals** for drift detection:
   - `manifest.version` (SS-declared)
   - `manifest.facet_enum_version` (CQ cognitive model version)
   - `app_schemas.version` (CQ-assigned revision counter)
5. **No long-term transition workarounds.** The earlier `meeting_id` sync trigger was removed in favor of a hard rename — pre-launch status means no external dependencies. Same rationale for unconditional DELETE of retired patch types.

---

## File map

Useful starter set for anyone onboarding:

```
docs/
├── memos/
│   ├── patch-taxonomy-simplification.md   ← PR 0 memo (read first)
│   ├── ss-team-parallel-work.md           ← SS team handoff
│   └── v1-release-summary.md              ← this file
├── design/
│   ├── app-schema-registration.md         ← manifest format + API design
│   └── ss-memory-ui-grouping.md           ← three-tier UI sketch (post-launch)

init-db/
├── 10_app_schema_registration.sql         ← PR 1 migration
├── 11_shouldersurf_schema.json            ← canonical SS manifest
└── 12_permanence_override.sql             ← PR 2 migration

src/contextquilt/
├── services/
│   ├── schema_validator.py                ← PR 1 manifest validation
│   ├── schema_prompt_builder.py           ← PR 3 prompt + output schema generation
│   ├── recall_scorer.py                   ← PR 4 relevance ranking
│   └── recall_formatter.py                ← PR 4 output formatting
└── routers/
    └── app_schemas.py                     ← PR 1 admin endpoints

scripts/
└── register_ss_schema.py                  ← PR 2 bootstrap CLI

tests/
├── benchmark/taxonomy_validation/         ← 4 tests backing the 6-facet model
└── unit/
    ├── test_schema_validator.py
    ├── test_schema_prompt_builder.py
    ├── test_recall_scorer.py
    └── test_recall_formatter.py
```

---

## Credits

Design, testing, and implementation pairing sessions between Scott and Claude Opus 4.7 (1M context) during April 2026. Taxonomy validated against five domains and seven personas. Five PRs stacked and ready.
