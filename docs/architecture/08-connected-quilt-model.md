# Connected Quilt Data Model

## The Problem

Today, patches are flat and isolated. Four hardcoded types (identity, preference, trait, experience), no connections between them. The quilt metaphor implies patches stitched together — but right now it's a pile of fabric.

This matters because:
- We can't represent "this commitment is blocked by that action item"
- We can't auto-archive a project and everything connected to it
- We can't tell ShoulderSurf "show me the Florida Blue quilt" as a connected graph
- A different app (CRM, helpdesk) can't define its own patch types

## Design Principles

1. **Patches are typed** — each type has a shape (required fields, lifecycle rules)
2. **Patches are connected** — typed edges between patches form the quilt
3. **Types are extensible** — apps register custom types; CQ provides built-in universals
4. **The graph is the quilt** — recall traverses connections, not just flat lists
5. **Connections drive lifecycle** — when a project archives, its children archive; when all blockers clear, a commitment completes

## Data Model

### Layer 1: Patch Type Registry

Apps register the types of patches they'll create. CQ ships with built-in types that all apps share.

```sql
CREATE TABLE IF NOT EXISTS patch_type_registry (
    type_key TEXT PRIMARY KEY,            -- "trait", "commitment", "decision"
    app_id UUID REFERENCES applications(app_id) ON DELETE CASCADE,
                                          -- NULL = built-in (available to all apps)
    display_name TEXT NOT NULL,           -- "Commitment", "Decision"
    description TEXT,                     -- "A promise to deliver something by a deadline"

    -- Shape: what fields this type expects in value JSONB
    schema JSONB NOT NULL DEFAULT '{}',   -- JSON Schema for value validation
                                          -- e.g., {"owner": "string", "deadline": "string?", "status": "string"}

    -- Lifecycle rules
    persistence TEXT DEFAULT 'sticky',    -- "sticky", "decaying", "completable"
    default_ttl_days INTEGER,             -- NULL = no expiry. 30 = archive after 30 days idle
    is_completable BOOLEAN DEFAULT FALSE, -- Can this patch be marked "done"?
    project_scoped BOOLEAN DEFAULT TRUE,  -- Does this type belong to a project context?

    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

**Built-in types** (app_id = NULL, available to all apps):

| type_key | persistence | completable | project_scoped | schema |
|----------|------------|-------------|----------------|--------|
| `trait` | sticky | no | no | `{text}` |
| `preference` | sticky | no | no | `{text}` |
| `identity` | sticky | no | no | `{text, role?, org?}` |
| `experience` | decaying | no | yes | `{text, participants?}` |

**ShoulderSurf custom types** (registered by the app):

| type_key | persistence | completable | project_scoped | schema |
|----------|------------|-------------|----------------|--------|
| `commitment` | completable | yes | yes | `{text, owner, deadline?, status}` |
| `decision` | sticky | no | yes | `{text, rationale?, participants?}` |
| `action_item` | completable | yes | yes | `{text, owner, deadline?, status}` |
| `requirement` | completable | yes | yes | `{text, priority?, status}` |
| `project` | sticky | no | no | `{text, status}` |

A CRM app might register completely different types: `deal`, `objection`, `contact_note`.

### Layer 2: Patches (Existing Table, Extended)

The `context_patches` table stays mostly the same. The `patch_type` column now references the registry instead of being a free-form string.

```sql
-- Existing columns remain unchanged
-- New columns:

ALTER TABLE context_patches ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active';
    -- "active", "completed", "archived"
    -- Only meaningful for completable types, but tracked universally

ALTER TABLE context_patches ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP WITH TIME ZONE;
    -- When this patch was marked complete (action items, commitments)
```

The `value` JSONB column already stores structured data. With the type registry, we can validate that a `commitment` patch has `{text, owner, deadline, status}` while a `trait` patch just has `{text}`.

### Layer 3: Patch Connections (New — The Stitching)

This is the new primitive. Typed, directed edges between patches.

```sql
CREATE TABLE IF NOT EXISTS patch_connections (
    connection_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_patch_id UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    to_patch_id UUID NOT NULL REFERENCES context_patches(patch_id) ON DELETE CASCADE,
    connection_type TEXT NOT NULL,         -- "belongs_to", "blocked_by", "motivated_by", etc.
    context TEXT,                          -- Optional explanation
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(from_patch_id, to_patch_id, connection_type)
);

CREATE INDEX IF NOT EXISTS idx_connections_from ON patch_connections(from_patch_id);
CREATE INDEX IF NOT EXISTS idx_connections_to ON patch_connections(to_patch_id);
CREATE INDEX IF NOT EXISTS idx_connections_type ON patch_connections(connection_type);
```

**Connection types:**

| connection_type | meaning | example |
|----------------|---------|---------|
| `belongs_to` | child → parent grouping | action_item → project |
| `blocked_by` | this can't complete until that completes | commitment → action_item |
| `fulfills` | completing this satisfies that | action_item → commitment |
| `motivated_by` | this decision was driven by that preference/trait | decision → preference |
| `supersedes` | this replaces that (auto-archives the old one) | decision → decision |
| `related_to` | general association | experience → experience |

### Layer 4: App Policy (Per-App Configuration)

```sql
ALTER TABLE applications ADD COLUMN IF NOT EXISTS policy JSONB DEFAULT '{}'::jsonb;
```

```json
{
  "extraction": {
    "max_facts_per_event": 5,
    "max_action_items_per_event": 3,
    "max_entities_per_event": 10
  },
  "budget": {
    "max_active_patches": 30,
    "overflow_strategy": "archive_oldest_decaying"
  },
  "decay": {
    "check_interval_hours": 24,
    "rules": {
      "experience": { "idle_days": 30 },
      "action_item": { "idle_days": 14 },
      "commitment": { "idle_days": 60 }
    }
  }
}
```

No policy = no limits (platform default behavior for apps that don't need constraints).

## How It All Connects — The Florida Blue Example

After a meeting, the extraction produces these patches and connections:

```
PATCHES:
  [project]     "Florida Blue transcription project"     status: active
  [decision]    "Use Nova 3 for transcription"           status: active
  [commitment]  "Deliver samples in 2 days"              owner: Scott, status: active
  [action_item] "Travis uploads audio files via FTP"     owner: Travis, status: active
  [requirement] "Summary capped at 1000-5000 chars"      status: active

CONNECTIONS:
  decision:Nova3        --belongs_to-->    project:FloridaBlue
  decision:Nova3        --motivated_by-->  preference:latest_tech
  commitment:samples    --belongs_to-->    project:FloridaBlue
  commitment:samples    --blocked_by-->    action_item:Travis_uploads
  requirement:summary   --belongs_to-->    project:FloridaBlue
```

Plus the user's universal patches (no project, no connections needed):

```
  [trait]       "Tends to over-explain"                  sticky, never expires
  [preference]  "Prefers to understand problem fully"    sticky, never expires
  [preference]  "Prefers latest technology"              sticky, never expires
```

**Total: 8 patches, 5 connections.** That's the entire Florida Blue quilt.

## How Recall Changes

Today: flat query, return 20 most recent patches.

With connections: **graph traversal starting from the project patch.**

```
User asks: "catch me up on Florida Blue"

1. Match "Florida Blue" → project patch
2. Traverse belongs_to connections → find decision, commitment, requirement
3. Traverse blocked_by on commitment → find Travis action item
4. Check statuses → filter out completed/archived
5. Also include universal patches (traits, preferences) — no traversal needed

Result: structured context, not a flat list
```

## How Lifecycle Works Through Connections

**Completing an action item:**
1. Travis uploads files → `action_item:Travis_uploads` marked `completed`
2. System checks: anything `blocked_by` this patch? → Yes: `commitment:samples`
3. Are ALL blockers for that commitment now complete? → Yes
4. `commitment:samples` becomes unblocked (still active — Scott hasn't delivered yet)

**Archiving a project:**
1. Florida Blue ships → `project:FloridaBlue` marked `archived`
2. System traverses: find all patches with `belongs_to → project:FloridaBlue`
3. Archive all of them (decisions, commitments, requirements, action items)
4. Traits and preferences are NOT archived — they have no project connection

**A decision supersedes another:**
1. "Switch from Nova 3 to Whisper" → new decision patch
2. Connected: `decision:Whisper --supersedes--> decision:Nova3`
3. Old decision auto-archived

## How This Stays Flexible for Other Apps

A CRM app registers its own types:

```
deal:       { value, stage, close_date, owner }     completable, project_scoped
objection:  { text, response_strategy }             decaying
contact:    { text, sentiment }                     decaying
```

And its own connections:

```
objection --raised_in--> deal
contact   --belongs_to--> deal
deal      --owned_by-->  (links to identity patch)
```

CQ doesn't interpret these. It stores them, traverses them on recall, and applies the lifecycle rules the app defined in its type registry. The platform is generic; the meaning comes from the app.

## Migration Path

This doesn't break anything that exists today:

1. **patch_type_registry** — new table, seed with the four built-in types
2. **patch_connections** — new table, empty until extraction starts producing connections
3. **status column** — added to context_patches, defaults to "active" for all existing patches
4. **app policy** — added to applications table, defaults to empty (no constraints)

Existing patches keep working. The flat extraction prompt keeps working. We evolve the prompt to produce connections over time, and the recall path learns to traverse them.

## What the Extraction Prompt Becomes

Instead of four flat arrays, the LLM returns patches with their connections:

```json
{
  "patches": [
    {
      "type": "commitment",
      "value": { "text": "Deliver samples in 2 days", "owner": "Scott" },
      "connects_to": [
        { "target_text": "Florida Blue transcription project", "connection": "belongs_to" },
        { "target_text": "Travis uploads audio files", "connection": "blocked_by" }
      ]
    }
  ],
  "entities": [...],
  "relationships": [...]
}
```

The worker resolves `target_text` references to actual patch IDs when storing (similar to how entity names get resolved to entity IDs today).
