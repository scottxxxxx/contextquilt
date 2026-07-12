# 13: Onboarding a New App — Templates & the Transformer Layer

ContextQuilt is a memory layer for *applications*, not for any one app.
A new app adapts CQ through configuration — a registered manifest — not
through CQ code changes. This doc is the path from zero to memory.

## The transformer layer

An app's manifest declares its **ingest_mode** — which transformer
(adapter) turns its input into typed patches:

| ingest_mode | You send | CQ runs | Archetypes |
|---|---|---|---|
| `extraction` | prose: transcripts (`meeting_transcript`), conversation text (`analysis`), chat messages (`chat_log`) | the LLM extraction pipeline, prompted from YOUR manifest's patch types | meeting capture, chat assistants |
| `structured` | pre-typed `patches[]` (`structured_patches`) | manifest validation + atomic storage — no LLM (doc §12) | coaching/assessment apps, anything that already produces signals |

**Declared mode is enforced.** When a manifest explicitly declares
`ingest_mode`, the worker rejects payloads whose `interaction_type`
belongs to the other adapter (logged as `ingest_mode_rejected`). This
prevents the silent-garbage failure: a structured app accidentally
sending prose would otherwise flow through LLM extraction with a
generic prompt and pollute the quilt with no error anywhere. Manifests
that predate the key (no `ingest_mode`) keep legacy unrestricted
routing.

Both modes share everything downstream of the adapter: dedup,
lifecycle/decay, connections, cue index, recall.

## The path

1. **Pick a starter template** — `GET /v1/schema/templates` lists the
   archetypes; `GET /v1/schema/templates/{name}` returns one verbatim.
   Also in-repo under `templates/manifests/`.
   - `meeting-capture` — extraction over spoken transcripts
   - `structured-coaching` — structured signals incl. a longitudinal
     (time-series) type
   - `chat-assistant` — extraction tuned for conversational memory
2. **Adapt it** — replace `app_id` (`REPLACE-WITH-YOUR-APP-ID` → your
   registered application UUID), rename domain types to your domain,
   tighten descriptions. Structural rules worth knowing:
   - connection `from_types`/`to_types` reference **patch** types, not
     entity types
   - `longitudinal: true` requires `series_descriptor_field`, and both
     it and `required_fields` must name real `value_shape` keys
   - per-type `cue_guidance` tunes associative-retrieval cue emission
     (doc §04)
3. **Lint it** — `POST /v1/schema/validate` (app auth) runs the exact
   registration validator and returns every error at once. Nothing is
   written. Iterate until `valid: true`.
4. **Register it** — operator action, admin-gated:
   `POST /v1/apps/{app_id}/schema`. Re-register (version bump) to
   change taxonomy later; the schema-driven prompt regenerates from
   the stored manifest.
5. **Send memory** — `POST /v1/memory` with the `interaction_type`
   your mode owns; **recall** via `POST /v1/recall`; **sync** via
   `GET /v1/quilt/{user_id}`.

## Design rule (the reason this doc exists)

Prefer structural generality; put per-app variation in the manifest;
per-app code paths are a last resort. If an app seems to need CQ code,
first ask whether the manifest vocabulary should grow instead — that's
how `ingest_mode`, longitudinal types, and `cue_guidance` happened.
