# Domain Mapping Guide

ContextQuilt's API uses terms like "project" and "meeting" because its first integration was a meeting copilot. But these are **generic grouping concepts** — any app can map them to its own domain.

## Core Abstractions

| CQ Term | What It Really Is | Meeting App | Health App | CRM | Coding Assistant |
|---------|-------------------|-------------|-----------|-----|-----------------|
| **project** | Named container that groups related patches | Project | Treatment plan | Deal / Account | Repository |
| **meeting_id** | Session/event that produced patches | Meeting | Office visit | Sales call | PR review |
| **project_id** | Stable ID for the container (survives renames) | Project UUID | Plan UUID | Deal UUID | Repo UUID |
| **commitment** | Promise with a named owner | "Scott: deliver samples by Friday" | "Patient: walk 30 min daily" | "Send proposal by Tuesday" | "Fix the auth bug this sprint" |
| **blocker** | Something preventing progress | "Waiting on Travis to upload files" | "Insurance pre-auth pending" | "Legal review required" | "Blocked on API design decision" |
| **decision** | Something agreed upon | "Use Nova 3 for transcription" | "Switch from metformin to insulin" | "Discount approved at 15%" | "Use gRPC for internal services" |
| **takeaway** | Short-lived observation | "Deadline is June 28" | "A1C trending down" | "Competitor pricing is aggressive" | "Test coverage dropped to 60%" |
| **trait** | User's behavioral pattern | "Tends to over-explain" | "Morning person, prefers early appointments" | "Prefers email over phone" | "Writes detailed PR descriptions" |
| **preference** | What the user prefers | "Prefers Nova 3 over Nova 2" | "Prefers swimming over running" | "Prefers quarterly billing" | "Prefers TypeScript over JavaScript" |

## How to Use CQ for Your Domain

### 1. Map your concepts to CQ terms

Your app has its own vocabulary. Map it:

```
Your "patient"      → CQ "project" (a container for related patches)
Your "visit"        → CQ "meeting_id" (a session that produces patches)
Your "prescription" → Custom patch type registered via patch_type_registry
```

### 2. Register custom patch types

The built-in types (commitment, blocker, decision, etc.) work for project management. For other domains, register your own:

```bash
POST /api/dashboard/patch-types
{
  "type_key": "condition",
  "display_name": "Medical Condition",
  "persistence": "sticky",
  "project_scoped": true
}
```

Health app types: `condition`, `goal`, `metric`, `intervention`, `observation`
CRM types: `opportunity`, `objection`, `next_step`, `competitor_mention`
Coding types: `bug`, `tech_debt`, `architecture_decision`, `dependency`

### 3. Register custom connection labels

```bash
POST /api/dashboard/connections
{
  "label": "treats",
  "role": "resolves",
  "from_types": ["intervention"],
  "to_types": ["condition"]
}
```

### 4. Pass your domain's IDs as metadata

```bash
POST /v1/memory
{
  "user_id": "patient-123",
  "interaction_type": "meeting_transcript",
  "content": "Visit transcript...",
  "metadata": {
    "project_id": "treatment-plan-456",   # Your "treatment plan" ID
    "project": "Diabetes Management",      # Display name
    "meeting_id": "visit-789"              # Your "visit" ID
  }
}
```

CQ doesn't interpret these IDs — it uses them for grouping, filtering, and lifecycle management. Your app defines what they mean.

### 5. Customize extraction (future)

Currently, the extraction prompt is optimized for meeting transcripts. In a future release, apps will be able to register **domain profiles** that customize:
- Which patch types the extractor looks for
- Domain-specific relevance filters
- Custom recall formatting (how patches are grouped in the response)

## API Path Mapping

| CQ Endpoint | Your App Calls It | Purpose |
|-------------|-------------------|---------|
| `POST /v1/projects/{user_id}` | Register a treatment plan / deal / repo | Create a named container |
| `GET /v1/projects/{user_id}` | List treatment plans / deals / repos | List containers |
| `POST /v1/meetings/{user_id}/{id}/assign-project` | Assign a visit to a treatment plan | Retroactive grouping |
| `POST /v1/recall` with `metadata.project_id` | Recall for this treatment plan / deal | Scoped context retrieval |
| `GET /v1/quilt/{user_id}` | Get patient's full record / deal history | All patches for user |

## Future: Generic Aliases

We plan to add alias endpoints that use domain-neutral terminology:

```
/v1/scopes/{user_id}              → same as /v1/projects/{user_id}
/v1/sessions/{user_id}/{id}/scope → same as /v1/meetings/{user_id}/{id}/assign-project
```

The current endpoints will continue to work. Aliases are additive, not replacements.
