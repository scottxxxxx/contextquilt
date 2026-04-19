# Design — App Schema Registration

**Status:** Draft — pending sign-off
**Depends on:** `docs/memos/patch-taxonomy-simplification.md` (6-facet model)
**Supersedes behavior in:** `init-db/04_connected_quilt.sql` (hardcoded universal types)

---

## Purpose

Let any CQ-integrated application register its own vocabulary of patch types and connection labels without modifying CQ core. The 6-facet cognitive taxonomy (Attribute / Affinity / Intention / Constraint / Connection / Episode) stays universal; everything else becomes per-app.

ShoulderSurf, a future interview app, and any downstream vertical (healthcare, finance, social) all share the same storage, lifecycle, and recall infrastructure — they diverge only in their registered manifest.

---

## The manifest shape

An app's full schema is a single JSON document. The manifest declares which version of the CQ facet enum it's registered against — this lets the core evolve the facet set (adding a 7th facet, for example) without breaking existing app registrations.

```json
{
  "app_id": "shouldersurf",
  "version": 1,
  "facet_enum_version": 1,
  "patch_types": [
    {
      "domain_type": "commitment",
      "facet": "Episode",
      "permanence": "month",
      "display_name": "Commitment",
      "description": "A promise with a named owner and optional deadline.",
      "value_shape": {
        "text": "string",
        "owner": "string?",
        "deadline": "string?"
      },
      "completable": true,
      "project_scoped": true,
      "extraction_rules": {
        "required_fields": ["text"],
        "exclusion_examples": [
          "Generic participation without named owner",
          "Procedural logistics (scheduling, room booking)"
        ],
        "guidance": "Only emit when a specific named person is the owner. 'Someone should X' is a takeaway, not a commitment."
      }
    }
  ],
  "connection_labels": [
    {
      "label": "works_on",
      "role": "informs",
      "from_types": ["person"],
      "to_types": ["project"],
      "description": "Person is actively involved in a project."
    }
  ],
  "extraction_prompt_guidance": {
    "role_context": "You are extracting typed patches from a diarized meeting transcript.",
    "speaker_conventions": "Use the (you) marker convention to identify the submitting user.",
    "priority_order": [
      "Self-disclosed traits",
      "Project patches",
      "Person patches for named owners",
      "Commitments with owners",
      "Blockers",
      "Decisions",
      "Roles",
      "Takeaways (evaluative lessons only)",
      "Preferences"
    ],
    "hard_caps": {
      "total_patches": 12,
      "entities": 10,
      "relationships": 10,
      "per_type_caps": {
        "takeaway": 3
      }
    }
  }
}
```

### Field contracts

**`patch_types[]`:**
| Field | Required | Values | Notes |
|---|---|---|---|
| `domain_type` | yes | free string | Must be unique within the app |
| `facet` | yes | enum: `Attribute`/`Affinity`/`Intention`/`Constraint`/`Connection`/`Episode` | One of the 6 universal facets |
| `permanence` | yes | enum: `permanent`/`decade`/`year`/`quarter`/`month`/`week`/`day` | Drives default TTL via registry lookup |
| `display_name` | yes | free string | UI label |
| `description` | yes | free string | Human-readable what-it-means |
| `value_shape` | yes | JSON-schema-like | Fields the value object must/may have. `?` suffix = optional |
| `completable` | no (default false) | bool | Can be marked done |
| `project_scoped` | no (default false) | bool | Usually belongs to a project |
| `extraction_rules` | no | object | Extraction-time guidance |
| `self_only` | no (default false) | bool | Applies only to the submitting user (traits in SS) |

**`connection_labels[]`:**
| Field | Required | Values | Notes |
|---|---|---|---|
| `label` | yes | free string | Must be unique within the app |
| `role` | yes | enum: `parent`/`depends_on`/`informs` | CQ structural role |
| `from_types` | yes | string[] | Valid source domain_types |
| `to_types` | yes | string[] | Valid target domain_types |
| `description` | yes | free string | What this connection means |

**`entity_types[]`:** see the "entity_types" section above. Covers the graph-layer name index separately from patch types.

**`extraction_prompt_guidance`:** app-specific prompt tuning. CQ core auto-generates the extraction prompt from `patch_types` + `connection_labels`, then injects this guidance as contextual instructions. Apps control voice and priority; they don't hand-write the whole prompt.

**`extraction_prompt_override` (optional, advanced):** a full raw prompt string. If present, CQ uses it verbatim instead of generating one from structural rules + guidance. Apps opt into this when they have mature, tuned prompts they don't want CQ rewriting. Trade-off: CQ can't validate the override against the manifest, so the app takes full responsibility for keeping the prompt aligned with registered types. Not the default path.

**`facet_enum_version`:** which version of CQ's facet enum this manifest is registered against. v1 = the 6-facet model (Attribute, Affinity, Intention, Constraint, Connection, Episode). Future facet additions bump the version; older manifests continue to work under their declared version.

**`entity_types` (optional array of objects):** types of entities the app wants indexed for graph traversal during recall. Entities are NOT user-editable memory — they're an internal name-matching index that makes recall fast. An entity is something like a person name, a project name, a company, an artifact — any identifier that appears across multiple patches and needs fast lookup.

Fields per entity type:
- `entity_type` (required, string): the type key used in extraction (e.g., `"person"`, `"project"`, `"company"`, `"artifact"`)
- `display_name` (required, string)
- `description` (required, string)
- `indexed` (optional, bool, default true): keep a Redis name index for fast recall
- `extraction_rules` (optional, object): extraction guidance for when to emit entities of this type

Apps that don't register `entity_types` fall back to CQ's default set (`person`, `project`, `company`, `feature`, `artifact`, `deadline`, `metric`).

**Patch types vs. entity types — when to use which:**
- User-editable memory shown in UI → patch type.
- Internal name-matching index for fast recall → entity type.
- Many concepts appear in both (a `person` is both a patch type AND an entity type). That duplication is intentional: the patch carries user-editable fields; the entity is just a fast-lookup index. The extraction pipeline emits both automatically when configured.

---

## API surface

All admin-key-protected. Not exposed to end users.

```
POST   /v1/apps/{app_id}/schema
       Body: full manifest JSON
       - Validates manifest against CQ core rules (facet enum, role enum, etc.)
       - Checks no cross-app collisions
       - Writes to patch_type_registry and connection_vocabulary with app_id set
       - Writes version to new app_schemas table
       Returns: { "status": "registered", "version": 1, "types_added": N, "labels_added": M }

GET    /v1/apps/{app_id}/schema
       Returns: current manifest JSON

PATCH  /v1/apps/{app_id}/schema
       Body: partial manifest (add types/labels)
       - Validates additions
       - Updates registry rows
       - Bumps schema version
       Returns: { "status": "updated", "version": 2, "diff": {...} }

GET    /v1/apps/{app_id}/schema/history
       Returns: [{ version, registered_at, manifest }, ...]
```

**Breaking changes not supported via PATCH.** Removing or renaming a type requires explicit migration via a versioned manifest replacement and a coordinated data migration — handled as a separate workflow.

---

## DB changes

Three migrations. Each additive; existing data keeps working.

### Migration 1 — Add facet and permanence to patch_type_registry

```sql
ALTER TABLE patch_type_registry
  ADD COLUMN facet TEXT,
  ADD COLUMN permanence TEXT;

-- Backfill universal types with facet/permanence mapping
UPDATE patch_type_registry SET facet = 'Attribute',  permanence = 'year'
  WHERE type_key IN ('trait');
UPDATE patch_type_registry SET facet = 'Affinity',   permanence = 'year'
  WHERE type_key IN ('preference');
UPDATE patch_type_registry SET facet = 'Connection', permanence = 'decade'
  WHERE type_key IN ('person');
UPDATE patch_type_registry SET facet = 'Connection', permanence = 'quarter'
  WHERE type_key IN ('project');
UPDATE patch_type_registry SET facet = 'Connection', permanence = 'year'
  WHERE type_key IN ('role');
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'year'
  WHERE type_key IN ('decision');
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'month'
  WHERE type_key IN ('commitment');
UPDATE patch_type_registry SET facet = 'Episode',    permanence = 'week'
  WHERE type_key IN ('blocker', 'takeaway');

ALTER TABLE patch_type_registry
  ALTER COLUMN facet SET NOT NULL,
  ALTER COLUMN permanence SET NOT NULL;

-- Constraints
ALTER TABLE patch_type_registry
  ADD CONSTRAINT chk_facet CHECK (facet IN (
    'Attribute', 'Affinity', 'Intention', 'Constraint', 'Connection', 'Episode'
  )),
  ADD CONSTRAINT chk_permanence CHECK (permanence IN (
    'permanent', 'decade', 'year', 'quarter', 'month', 'week', 'day'
  ));
```

### Migration 2 — New `app_schemas` table

```sql
CREATE TABLE app_schemas (
  schema_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  app_id         UUID NOT NULL REFERENCES applications(app_id) ON DELETE CASCADE,
  version        INT NOT NULL,
  manifest       JSONB NOT NULL,
  registered_at  TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
  registered_by  TEXT,  -- admin or service identifier
  UNIQUE(app_id, version)
);

CREATE INDEX idx_app_schemas_current ON app_schemas(app_id, version DESC);
```

### Migration 3 — Remove `identity` and `experience` from registry

```sql
-- Only remove if no patches reference them (should be zero per taxonomy-validation tests)
DELETE FROM patch_type_registry
  WHERE type_key IN ('identity', 'experience')
    AND app_id IS NULL
    AND NOT EXISTS (
      SELECT 1 FROM context_patches
        WHERE patch_type IN ('identity', 'experience')
    );
```

---

## How extraction uses the schema

Today's pipeline: the extraction prompt is hand-maintained in `src/contextquilt/services/extraction_prompts.py` with SS types baked in.

New pipeline:

```
1. Extraction worker receives a meeting for app X.
2. Reads app X's current manifest from app_schemas.
3. Generates the extraction prompt dynamically:
   - Patch-type section: iterate manifest.patch_types,
     emit one block per type with description + value_shape + examples.
   - Connection-label section: iterate manifest.connection_labels,
     emit the from→to combinations and guidance.
   - Injects manifest.extraction_prompt_guidance (priority order,
     hard caps, exclusion examples) as system-level instructions.
4. Calls the configured LLM.
5. Validates the LLM's output against the schema:
   - Every emitted patch's domain_type must exist in manifest.patch_types
   - Every emitted connection's label must exist in manifest.connection_labels
   - from_types/to_types must match
   - value_shape fields must validate
6. Rejects/logs non-conforming patches rather than silently storing them.
```

This means the extraction prompt is no longer maintained by hand. Changing SS's takeaway rules means updating SS's manifest — not editing Python.

---

## Ship plan — four PRs

Break this into reviewable pieces:

### PR 1 — DB + schema registration API + cross-app generalizations
- Add `facet` and `permanence` columns with backfill
- Create `app_schemas` table
- Build `POST/GET/PATCH /v1/apps/{app_id}/schema` endpoints
- Remove `identity` and `experience` rows
- **Allow recursive `belongs_to`** (Connection → Connection)
- **Add optional `about_patch_id` column** on `context_patches`
- **Rename `meeting_id` → `origin_id` + add `origin_type` column**; update manifest schema to include `origin_types` array
- Integration tests for all of the above

**Safe to merge without disrupting production.** Existing extraction still uses the hardcoded prompt (which populates `origin_type="meeting"` and leaves `about_patch_id` NULL).

### PR 2 — ShoulderSurf schema registration + per-patch permanence override
- Add SS manifest JSON fixture (`init-db/05_shouldersurf_schema.json`)
- Bootstrap script that posts it on first deploy
- Migrate universal built-in types to `app_id=shouldersurf`
- Register the full 13-type SS schema (including new `goal`, `constraint`, `event`)
- Add `permanence_override` column (nullable) on `context_patches`
- Update decay worker to `COALESCE(patch.permanence_override, type.default_permanence)`
- Expose `permanence_override` in patch POST/PATCH API schemas
- Optional: add `override_source: 'user' | 'app'` audit column

**Principle documented:** permanence is a default, not a rule. Apps and users can override up (pin) or down (shorten) without schema changes.

**Still safe.** Extraction reads SS's manifest but the prompt generation isn't live yet.

### PR 3 — Schema-driven extraction prompt generation
- Build prompt generator in `src/contextquilt/services/schema_prompt_builder.py`
- Wire extraction worker to use it for app_id-tagged meetings
- Keep hardcoded prompt as fallback behind feature flag
- Compare outputs side-by-side for 1 week (A/B on real SS meetings)
- Validate role patches now actually emit (currently 0 in production)

**Feature-flagged.** Can roll back instantly if quality regresses.

### PR 4 — Query-scoped recall
- Separate from schema work but depends on facet information being available
- Rank patches against the query (entity match + graph traversal + cosine similarity)
- Return a narrow flat list, not a category-grouped block
- Feature-flagged with category-dumped fallback
- Measure utilization improvement on the existing SS benchmark suite

**Biggest engineering lift (~2 weeks). Highest output-quality payoff per the taxonomy tests.**

---

## Design decisions (resolved)

1. **Facet enum IS versioned.** `facet_enum_version: 1` on every manifest. Future facet changes bump the version; older manifests continue to work.

2. **Full-prompt override IS available, but opt-in only.** Default is structural scaffold + guidance tuning. Apps with mature prompts can supply `extraction_prompt_override` and take full responsibility for alignment.

3. **Sample-data validation at registration DEFERRED to v2.** Not required for v1. Registration validates structural correctness only (enum values, referential integrity of label types).

4. **SS migration ships full scope in one coordinated change** — migrations for existing 8 types AND new types (`org`) AND new labels (`reports_to`, `member_of`, `vendor_for`, `describes`). Ripped bandaid, single SS change to absorb.

---

## Cross-app generalizations (added in PR 1 to avoid SS overfit)

These three additions make CQ core genuinely multi-app by design. Each is minor in implementation and large in payoff — they turn "moderate-fit" app categories (creative projects, academic research) into strong fits without compromising the SS path.

### 1. Recursive `belongs_to` (Connection → Connection hierarchy)

**Change:** allow `belongs_to` edges from any Connection-facet patch to any other Connection-facet patch, not just child → project.

**Why:** real hierarchies exist across app types:
- Creative: novel → chapter → scene
- Research: dissertation → chapter → section; experiment → sub-experiment
- SS: Benefits App MVP → Florida Blue engagement (previously flagged gap)
- Interview tool: round → loop → company

**Implementation:** relax the validation rule in `connection_vocabulary` that restricts `belongs_to` target types. The decay worker's cascade query already uses a recursive CTE, so depth handles itself.

### 2. Attributes about non-user Connections

**Change:** Attribute-facet patches can optionally include an `about_patch_id` pointing to any Connection-facet patch. The attribute is then understood to describe *that entity*, not the submitting user.

**Why:** many stable facts describe things other than the user:
- "Elena is 34" (character, creative)
- "The 2023 Smith paper uses RCT methodology" (citation, research)
- "Benefits App MVP is in defined-mode until finalized" (project, SS)
- "Patient's current insurance plan covers infusions" (plan, healthcare)

**Implementation:** nullable `about_patch_id` column on `context_patches`. When set, the patch applies to that target; when NULL, the patch applies to the submitting user (current behavior preserved). Recall traversal naturally surfaces Attributes when their `about` target is in scope.

### 3. Generic `origin_id` + `origin_type` (replaces `meeting_id`)

**Change:** rename `meeting_id` on `context_patches` to `origin_id`, add `origin_type` string column, and let each app declare its valid origin_types in its manifest.

**Why:** "meeting" is SS's input unit. Other apps have different input units:
- Interview tool: `practice_session`, `live_interview`, `debrief`
- Research: `paper_highlight`, `typed_note`, `lit_review`
- Creative: `writing_session`, `editor_feedback`, `read_through`
- Health: `visit`, `checkin`, `device_reading`

**Implementation:**
1. Rename column in migration (since no one has shipped on it yet)
2. Add `origin_types` section to manifest schema — array of strings per app
3. Validate patches have an `origin_type` matching the app's registered set

SS manifest adds:
```json
"origin_types": ["meeting", "imported_recording", "typed_note"]
```

---

## Per-patch permanence override (shipping in PR 2)

The schema declares per-type permanence defaults. Individual patches can **override** their permanence via a nullable column on `context_patches`:

```sql
ALTER TABLE context_patches
  ADD COLUMN permanence_override TEXT;
  -- NULL = use the type's default permanence from patch_type_registry
  -- non-NULL = use this value instead (must be a valid permanence-class enum value)
```

**Decay worker:**
```
effective_permanence = COALESCE(patch.permanence_override, type.default_permanence)
```

**Two driver modes:**

1. **User-driven.** App UI offers "keep forever" / "remember this" / "let it fade" controls that map to permanence classes. User choice wins.
2. **App-driven.** App logic promotes or demotes based on usage signals (blocker referenced across 5+ meetings → promote from week to quarter). No user interaction needed.

**Behavioral rules:**

- Override can be shorter or longer than type default; both directions honored.
- Access-based refresh (`last_accessed_at`) still applies on top of permanence.
- Project cascade, supersede, and status transitions ignore permanence and run regardless — these are structural lifecycle paths.
- Override value must be one of the valid permanence classes (same enum as type defaults).

**Principle:** *permanence is a default, not a rule.* Types declare how long patches of this kind usually matter; individual patches deviate based on actual importance.

---

## What this design does NOT cover

- **Participant/ownership model** (v2) — patches root on one user. Collaborative apps need this eventually.
- **Schema diff/migration tools** — removing or renaming a type still requires a coordinated data migration. v2 concern.
- **Sample-data validation at registration** — verifying a manifest produces reasonable output against a sample transcript. Deferred to v2.
- **UI for managing schemas** — admin dashboard additions. Not blocking; can hand-edit fixtures until then.
- **Per-app quotas/billing** — if we bill by app usage, that's a separate layer.
- **Permanence lockdown per app** — a manifest-level flag saying "this app doesn't allow overrides." Add only if a future app needs it.
