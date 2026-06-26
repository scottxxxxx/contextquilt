# Design Doc: Structured Ingest Adapters + Longitudinal Patches (Step 1)

**Status:** Proposal · **Owner:** Scott · **Date:** 2026-06-22
**Scope:** The write-path refactor that lets Tech Rehearsal (TR) onboard onto ContextQuilt without going through transcript extraction, plus the schema for time-series ("longitudinal") memories. Read-path work (semantic retrieval, feedback loop, profile card) is out of scope here.

## 0. Why this exists

CQ's taxonomy is already pluggable (per-app manifests in `app_schemas`). The overfit to ShoulderSurf is one level down, in the **pipeline shape**: there is exactly one way into the store, and it assumes `transcript → one LLM extraction call → typed patches`. That is correct for SS (passive capture of a meeting that happened) and wrong for TR, which already produces structured coaching signals (`QuestionRating`, `GapAnalysis`, `StoryEntry`, `BehavioralAnalysis`) and needs them stored losslessly, with trend-over-time.

This doc generalizes the write path so extraction becomes **one ingest adapter among several** over the existing patch sink, and adds a longitudinal patch shape. TR is also the deliberate second manifest — the first real test that the system isn't secretly SS-shaped.

**Constraint that shapes every decision:** the only users today are SS users and Scott (sole TR dev). Backward compatibility is owed to **ShoulderSurf only**. TR is greenfield.

## 1. The refactor boundary

### 1.1 Where the cut goes

`store_connected_patches()` (`src/worker.py:301`) is already an app-agnostic sink: given a list of pre-formed patch dicts it writes `context_patches` + `patch_subjects` + `patch_usage_metrics` + `context_patch_acl` + `patch_connections`, plus dedup and connection resolution. Everything below this line is shared and stays shared.

`handle_meeting_summary()` is, in effect, **the extraction adapter**: build prompt from manifest → `llm.extract()` → sanitizer chain → `store_connected_patches()`. We name that concept and add a sibling.

```
                       ┌─ ExtractionAdapter  (SS)  transcript → LLM → sanitizers ─┐
POST /v1/memory ─►     │                                                          ├─► store_connected_patches() ─► dedup / connections / metrics / ACL ─► decay ─► recall
  dispatch by adapter  └─ StructuredAdapter  (TR)  pre-typed patches → validate ──┘
```

- **Above the line (pluggable, per-app):** how raw input becomes patch dicts. Extraction for SS; pass-through-with-validation for TR.
- **Below the line (shared, app-agnostic):** storage, dedup, connection vocab, metrics, ACL, decay, recall. No app branching here, ever. This is the anti-overfit invariant.

### 1.2 Dispatch change

`process_task()` (`src/worker.py:1506`) routes by `interaction_type`. Add one branch:

```python
# src/worker.py, process_task()
if task_type == "structured_patches":
    await self.handle_structured_ingest(payload)
    return
```

Adapter selection is driven by the manifest, not hardcoded: a new manifest field `"ingest_mode": "extraction" | "structured"`. The `structured_patches` interaction type is the structured entrypoint; SS's existing types (`meeting_summary`, `meeting_transcript`, `query`, …) keep routing to `handle_meeting_summary()` unchanged.

### 1.3 Request model

Extend `MemoryUpdate` (`src/main.py:128`) with carrier fields for pre-typed data (all optional, all `exclude_none` on enqueue, so SS payloads are byte-identical):

```python
# For 'structured_patches' (apps that already have typed signals, e.g. Tech Rehearsal)
patches:        Optional[List[Dict[str, Any]]] = None   # [{type, value, name?, connections?, series_key?}]
entities:       Optional[List[Dict[str, Any]]] = None
relationships:  Optional[List[Dict[str, Any]]] = None
```

`metadata` already carries `origin_id` / `origin_type` / `project` / `project_id`, which the structured path reuses as-is.

### 1.4 `handle_structured_ingest()` (new)

```
0. Privacy gate (Decision 4): reject the request if it carries any transcript-shaped field
   (summary / content / messages / transcript). Structured-mode apps may send only
   patches / entities / relationships. Short text inside declared value_shapes is fine
   (distilled, app-authored); raw capture and full transcripts never reach CQ.
1. Load the caller's latest manifest from app_schemas (same fetch as GET /v1/schema).
2. Structural validation (NOT the LLM sanitizer chain), all pre-write:
   - each patch.type ∈ manifest patch_types
   - patch.value conforms to that type's value_shape (required_fields present, types match)
   - each connection uses a manifest connection_label with a valid (from_type → to_type) combo
     → reuse enforce_connection_vocabulary() from extraction_schema.py verbatim; it is already
       manifest-driven and app-agnostic. This is concrete reuse, not new code.
3. Reject the WHOLE batch atomically on any violation (Decision 3): validate everything first,
   then write inside a single transaction, so one bad patch never leaves a partial/inconsistent
   graph or a half-written trajectory. The app is the source of truth — a malformed batch is a
   client bug and should fail loudly, not be silently salvaged.
4. Map to the patch-dict shape store_connected_patches() expects; call it with longitudinal_types={patch_type: descriptor_field, …} built from the manifest's longitudinal patch types (see §3). A structured batch is mixed (ratings are longitudinal, gaps/stories are not), so this is a per-type mapping, not a single per-call flag.
```

The extraction-only sanitizers (`enforce_owner_gate`, `sanitize_you_marker_from_patches`, `strip_prose_from_person_names`, …) are **skipped** — they exist to clean up LLM output, and there is no LLM here. This is the point: structured ingest is cheaper and lossless.

### 1.5 What SS is guaranteed

- `ExtractionAdapter` == today's `handle_meeting_summary`, behavior unchanged; SS interaction types untouched.
- `store_connected_patches()` gains an optional `longitudinal_types` mapping (default `None`/empty); SS passes nothing → its path is byte-identical.
- New table (§3) is additive; SS never writes it. Recall reads `value` exactly as today.

## 2. The Tech Rehearsal manifest

Same structure as `init-db/11_shouldersurf_schema.json`, registered the same way (`scripts/register_ss_schema.py` pattern → `app_schemas`). Two new manifest capabilities: `ingest_mode` and a `longitudinal` flag on patch types. Illustrative core (not exhaustive — remaining types follow the pattern):

```jsonc
{
  "app_id": "techrehearsal",
  "version": 1,
  "display_name": "Tech Rehearsal",
  "description": "Practice for high-stakes conversations (interview flagship; negotiation, hard personal talks, pitches). Emits already-structured coaching signals; ingests them directly, no transcript extraction.",
  "ingest_mode": "structured",

  "origin_types": ["mock_run", "live_run", "clarify_session"],

  "entity_types": [
    { "entity_type": "person",   "display_name": "Counterpart", "indexed": true,
      "description": "Interviewer / negotiator / the other party in the rehearsed conversation." },
    { "entity_type": "company",  "display_name": "Organization", "indexed": true },
    { "entity_type": "job_role", "display_name": "Role",         "indexed": true,
      "description": "The role/position being rehearsed for; a recall anchor." }
  ],

  "patch_types": [
    {
      "domain_type": "rehearsal", "facet": "Connection", "permanence": "quarter",
      "display_name": "Rehearsal", "project_scoped": false, "self_only": false,
      "description": "Top-level container: one Job/conversation being rehearsed. CQ 'project' role.",
      "value_shape": { "text": "string", "scenario": "string?", "status": "string?" }
    },
    {
      "domain_type": "skill_rating", "facet": "Episode", "permanence": "year",
      "display_name": "Skill Rating", "project_scoped": true, "self_only": true,
      "longitudinal": true,
      "description": "QuestionRating over time. The series IS the value — Weak→Meets→Strong is a trajectory, not a fact to overwrite.",
      "value_shape": {
        "text": "string", "skill": "string",
        "rating": "string", "rating_ordinal": "number",
        "scale": "string?", "run_id": "string?"
      },
      "series_descriptor_field": "skill",
      "required_fields": ["skill", "rating", "rating_ordinal"]
    },
    {
      "domain_type": "gap", "facet": "Episode", "permanence": "quarter",
      "display_name": "Gap", "project_scoped": false, "self_only": true,
      "completable": true,
      "description": "GapAnalysis: a missing capability ('no failure story yet'). An open item with a first-flagged/closed lifecycle — reuses the commitment/blocker overdue machinery.",
      "value_shape": { "text": "string", "gap_kind": "string?", "first_flagged": "string?" }
    },
    {
      "domain_type": "story", "facet": "Affinity", "permanence": "decade",
      "display_name": "Story", "project_scoped": false, "self_only": true,
      "description": "StoryEntry / story bank. User-scoped (spans jobs). Surfaces in prep and live.",
      "value_shape": { "text": "string", "name": "string?", "works_for": "string?", "missing": "string?" }
    },
    {
      "domain_type": "behavioral_baseline", "facet": "Attribute", "permanence": "year",
      "display_name": "Behavioral Baseline", "project_scoped": false, "self_only": true,
      "longitudinal": true,
      "description": "BehavioralAnalysis baseline (filler density, latency variance, delivery flags). Longitudinal to track drift/improvement. Structured metadata only — no audio/video ever leaves device.",
      "value_shape": { "text": "string", "metric": "string", "value": "number", "unit": "string?" },
      "series_descriptor_field": "metric"
    },
    {
      "domain_type": "coaching_note", "facet": "Episode", "permanence": "month",
      "display_name": "Coaching Note", "project_scoped": true, "self_only": true,
      "description": "The single highest-leverage change for the next run. Links to the skill it addresses.",
      "value_shape": { "text": "string", "run_id": "string?" }
    }
  ],

  "connection_labels": [
    { "label": "belongs_to", "role": "parent", "from_types": ["skill_rating","gap","coaching_note"], "to_types": ["rehearsal"],
      "description": "Signal belongs to a specific rehearsal/Job. Cascades on archival." },
    { "label": "addresses",  "role": "informs", "from_types": ["coaching_note"], "to_types": ["skill_rating","gap"],
      "description": "A coaching note targets a specific weak skill or open gap — lets recall pair 'what to fix' with 'the fix'." },
    { "label": "about",      "role": "informs", "from_types": ["skill_rating","gap"], "to_types": ["job_role"],
      "description": "Ties a signal to the role being rehearsed for." }
  ],

  // forward hook for Step 2 (feedback loop), declared now, not consumed yet:
  "success_signal": { "events": ["gap_closed", "rating_improved"] }
}
```

Notes:
- **No `extraction_prompt_guidance`.** Structured mode never builds an extraction prompt, so that whole manifest section is absent — itself a useful signal that the SS prompt machinery isn't being dragged in.
- `gap` deliberately reuses `completable` so a closed gap flows through the existing `complete` endpoint / auto-close + `deleted`/`completed` delta-sync split. No new lifecycle. Per Decision 2 it is user-scoped by default (`project_scoped: false`) and may optionally `belongs_to` a rehearsal when job-specific; user-level gaps dedup across rehearsals via the §3.2 semantic match.
- `skill_rating` and `behavioral_baseline` are `longitudinal: true` with a `series_descriptor_field` naming which value field carries the stable series descriptor (the skill/metric name, not the changing rating). CQ groups observations into a series by **semantically matching** that descriptor (Decision 1), not by an exact app-supplied key.

## 3. Longitudinal patch schema

### 3.1 The problem with the existing model

The current dedup path (`store_connected_patches`) treats a near-duplicate as the *same fact* and re-observes it (bumps `last_observed_at`, merges deadline detail). For a `skill_rating`, "conflict: Weak" from Mock 1 and "conflict: Meets" from Mock 3 are **not the same fact to merge** — they are two points on a trajectory. Trigram dedup would wrongly collapse them and destroy the trend, which is the whole value for TR.

### 3.2 Design: identity row + observation history

- The `context_patches` row is the **stable series identity** (one row per resolved `(user, app, patch_type, skill)`), holding the **latest** observation in `value` for the hot path. Recall stays unchanged and byte-stable — it reads the latest snapshot exactly as today.
- A new `patch_observations` table holds the **full time series**. Trend/Review reads from here; the hot path never touches it.
- **Identity resolution is CQ-derived (Decision 1).** On each longitudinal ingest, CQ takes the value field named by `series_descriptor_field`, normalizes it (lowercase, strip the rating/measurement token), and **semantically matches** it against existing series identities for that same `(subject, app, patch_type)` — reusing the trigram dedup CQ already runs, upgrading to embedding match when pgvector lands. A match appends to that series; no match opens a new one. We accept the gray-zone merge/split risk this carries and contain it three ways: (a) the match is scoped to the same `(subject, app, type)` and rehearsal (`project_id`), so cross-skill and cross-rehearsal collisions are bounded; (b) a conservative similarity threshold biases toward opening a new series over a wrong merge; (c) the `series_descriptor_field` hint means we match on the skill descriptor, not the changing rating word. `patch_name` stores the normalized descriptor so `idx_patches_name` accelerates candidate lookup; no `context_patches` column change.

### 3.3 Migration `24_patch_observations.sql` (additive)

```sql
CREATE TABLE IF NOT EXISTS patch_observations (
    observation_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patch_id        UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    observed_at     TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    value           JSONB NOT NULL,           -- the point-in-time observation
    origin_id       TEXT,                     -- which run produced it
    origin_type     TEXT,
    source_app      UUID REFERENCES applications(app_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_patch_obs_series ON patch_observations(patch_id, observed_at);
```

(Per the migration convention: new file, never edit an applied one; tracked by filename + sha256 in `schema_migrations`, applied by `scripts/run_migrations.py`. 24 is the next free number — 11 is the SS manifest JSON, 07 is genuinely unused.)

### 3.4 Storage behavior (the longitudinal branch)

```
For a longitudinal patch:
  descriptor = normalize(patch.value[series_descriptor_field])
  identity   = semantic_match(active same-(subject, app, type, rehearsal) series, descriptor, threshold)
  if identity is None:
    create the identity row (patch_name = descriptor, value = this observation, status active)  # first point
    insert patch_observations(point)
  else:
    insert patch_observations(point)                                    # append, never collapse
    UPDATE context_patches SET value = <this observation>,              # refresh hot-path snapshot
                              updated_at = NOW(), last_observed_at = NOW()
    WHERE patch_id = identity
```

`semantic_match` is the existing trigram dedup (embedding-based once pgvector lands), scoped to the same `(subject, app, type)` and rehearsal (`project_id` via `IS NOT DISTINCT FROM`), biased toward opening a new series on uncertainty.

Non-longitudinal structured patches (`gap`, `story`, `coaching_note`) go through the **existing** dedup path unchanged — they are facts, not series. User-level `gap` dedup across rehearsals rides that same path (Decision 2).

### 3.5 Decay interaction

`skill_rating`/`behavioral_baseline` anchor freshness on `last_observed_at` (already the `FRESHNESS_TRACKED_TYPES` behavior), which the append branch bumps — so an actively-practiced skill stays fresh, a dropped one decays. If we want these exempt from archival entirely while a rehearsal is active, set `permanence: year` (already in the manifest) and rely on the access-based exemption. No change to `decay_loop` required for step 1; revisit only if a TR type needs a custom anchor (then follow the three-place update rule in CLAUDE.md).

## 4. Step-1 deliverables (checklist)

> **Status (2026-06-23):** items 1–5 implemented in the working tree (uncommitted). #1 `init-db/24_patch_observations.sql`; #5 the `longitudinal_types` branch in `store_connected_patches`; #2 `MemoryUpdate` carrier fields (`patches`/`entities`/`relationships`) + `structured_patches` interaction type; #3 `process_task` dispatch branch (immediate, never buffered); #4 `handle_structured_ingest()` — privacy gate, mandatory-manifest load, whole-batch pre-write validation (type ∈ manifest, required fields, longitudinal descriptor present, `enforce_connection_vocabulary` drop = reject), atomic write via one acquired connection + transaction. Both files compile; unit suite green (380 passed). Remaining: #6 TR manifest + `register_tr_schema.py`, #7 DB-harness tests.
>
> **Status (2026-06-25):** item #6 done. `init-db/25_techrehearsal_schema.json` (6 patch types, 2 longitudinal, 3 entities, `ingest_mode: structured`) + `scripts/register_tr_schema.py` (clone of the SS register script, `TR_APP_ID` env). Three corrections surfaced while wiring TR as the second manifest — exactly the SS-overfit checks this manifest was meant to flush out:
> 1. **Validator gap (had to fix).** `schema_validator.py` does strict unknown-key rejection, so `ingest_mode` / `success_signal` (top-level) and `longitudinal` / `series_descriptor_field` / `required_fields` (patch-type) were never in its allow-lists — registration would have 400'd. Extended the validator to accept and structurally check them (`ingest_mode ∈ {extraction,structured}`; longitudinal ⇒ `series_descriptor_field`; descriptor + required_fields must name real `value_shape` keys). 11 new tests; SS manifest still validates.
> 2. **`text` is mandatory on every type.** The shared sink (`store_connected_patches`, worker.py:619-621) does `if not text: continue` *before* the longitudinal branch — a rating with no `value.text` would pass `handle_structured_ingest` validation and then silently vanish. So `skill_rating`/`behavioral_baseline` list `text` in `required_fields`, not just the descriptor. Per §2's illustrative manifest this was missing.
> 3. **`about → job_role` dropped.** Connection from/to types must be declared *patch* types; `job_role` is an entity, so that edge fails the validator's referential check. Dropped it (consistent with §5's "TR entity index is intentionally thin in Step 1"); signal↔role ties wait for the relationships graph / semantic retrieval.
>
> Also note: `handle_structured_ingest` rejects falsy required values (`if not value.get(field)`), so `rating_ordinal` must be **1-indexed** (Weak=1) — a 0 ordinal would be refused. Documented in the manifest. Unit suite green (390 passed). Remaining: #7 DB-harness tests.
>
> **Status (2026-06-25, #7):** `tests/unit/test_structured_ingest_db.py` authored — 5 `TEST_DATABASE_URL`-gated integration tests: longitudinal append (2 same-skill ratings → 1 identity + 2 `patch_observations`, latest snapshot), distinct-skill series stay separate, privacy-gate reject (transcript field → 0 written), invalid-patch whole-batch reject (0 written), and a happy-path `handle_structured_ingest` (rehearsal + 2 ratings + gap → 3 patches, 2 observations). Applies the real `init-db` schema via `run_migrations` into a throwaway DB; each test provisions its own app + registers the TR manifest. **Not yet executed** — local box has no Docker/asyncpg/PG; needs a CI/docker run (`TEST_DATABASE_URL=… pytest tests/unit/test_structured_ingest_db.py`). Added to the CLAUDE.md local-ignore list (imports asyncpg at module load → collection error in the bare suite). With that, **Step 1 is code-complete pending the CI DB run.**


1. `init-db/24_patch_observations.sql` — table + index (§3.3).
2. `MemoryUpdate`: add `patches` / `entities` / `relationships` carrier fields (§1.3).
3. `process_task`: add `structured_patches` dispatch branch (§1.2).
4. `handle_structured_ingest()`: privacy gate + manifest load + pre-write structural validation (reusing `enforce_connection_vocabulary`) + atomic batch write to the sink (§1.4).
5. `store_connected_patches(..., longitudinal_types=None)`: add the series identity + observation-append branch (§3.4).
6. `init-db/`-registered TR manifest + a `register_tr_schema.py` (clone of `register_ss_schema.py`). **DONE** — also required extending `schema_validator.py` to allow the structured-mode manifest keys (see 2026-06-25 status note).
7. Tests: unit for the structural validator and the longitudinal append (assert two ratings → one identity row + two observation rows, not a collapsed merge); one end-to-end TR ingest test (safe — sole user is the dev). **AUTHORED** — `tests/unit/test_structured_ingest_db.py` (5 `TEST_DATABASE_URL`-gated tests). Pending a CI/docker DB run (no local PG); see 2026-06-25 status note.

## 5. Non-goals / follow-ons

- **Read path** (semantic/pgvector retrieval, feedback-driven ranking, the always-injected profile card) — separate docs. The `success_signal` manifest field is declared here but not consumed until Step 2.
- **Trend query API** (`GET` the series for Review 360) — needs `patch_observations` to exist first; spec it once data is flowing.
- **Entity index for TR** — TR recall is mostly skill/gap-shaped, not entity-name-shaped, so this is intentionally thin until semantic retrieval lands. Flagged so it's a conscious choice, not a silent gap.
- **Manifest-driven project scoping** — the worker's `project_scoped_types` set is hardcoded to SS's episode types, so app-specific types (incl. TR's longitudinal ones) aren't covered. Step 1's `_insert_series_identity` attaches rehearsal context (`project`/`project_id`/`origin`) explicitly as a contained workaround; generalizing scoping to every manifest-declared `project_scoped` type is a follow-up. This is exactly the kind of SS overfit the second-manifest generality test was meant to surface.

## 6. Decisions (locked 2026-06-23)

1. **Series identity — CQ-derived (semantic).** CQ resolves which series an observation joins by semantically matching the `series_descriptor_field`, scoped to `(subject, app, type)`, trigram now / embedding when pgvector lands. Chosen over an app-supplied key to keep the layer flexible for any client, not just a disciplined one — accepting the gray-zone merge/split risk and containing it per §3.2.
2. **Gap scope — both.** User-scoped by default (cross-job hero cards) with optional `belongs_to` a rehearsal for job-specific gaps. Reuses CQ's universal-vs-project-scoped distinction; user-level gaps dedup across rehearsals via the §3.2 match.
3. **Validation — reject whole batch, atomically.** Validate everything pre-write, then write in one transaction; any violation rejects the batch and writes nothing. The app is authoritative, so malformed input fails loudly instead of being salvaged.
4. **Privacy — hard enforced contract.** Structured-mode ingest accepts only `patches`/`entities`/`relationships` and rejects transcript-shaped fields; short text inside declared value_shapes is permitted as distilled signal. CQ structurally cannot receive raw audio/video or full transcripts for these apps.

### Consequence to track
- Decision 1 couples reliable series grouping to embeddings. Step 1 ships on trigram match (coarser); revisit grouping quality when the pgvector read-path work lands. Flagged so this is a conscious sequencing choice, not a silent gap.
```
