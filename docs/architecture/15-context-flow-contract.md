# 15: Context Flow Contract (v1 — LOCKED 2026-07-16)

The agreed, three-team behavior for the flow: user opens Shoulder Surf,
enters a project or meeting chat, asks a question, memory context
arrives. Confirmed by SS, GP, and CQ; verified by the three-way test
(SS device sends / GP diff / CQ socket) on both shapes — one scoped
chat query, one rundown query — before locking. **Any future change to
this flow gets the same test before it ships (item 8).**

1. **Scoping.** SS sends the project display name and the stable
   project_id on every chat call. GP forwards project, project_id,
   locale, and owner_speaker_label on POST /v1/recall, adds
   memory_signals as a client passthrough key, and sets token_budget
   1200 on project chat recalls. The GP allowlist is the extension
   point for new metadata keys. CQ treats project_id as primary and the
   display name as a load-bearing fallback (dedup convergence can
   retire a project UUID).
2. **Ingest scoping.** Every user-initiated path that changes a
   meeting's project, plus CloudKit dedup convergence, fires the CQ
   rescope. Orphan heal at load stays deliberately unwired; historical
   stragglers are healed server-side by CQ (stream replay is the tool —
   the ingest stream preserves original payloads verbatim). The unscope
   endpoints (per-origin unassign with project guard; project-wide
   unscope) clear scope on removal; patches always survive unscoped.
3. **Rundown routing.** GP detects rundown intents deterministically on
   the question portion, failing open to normal recall. On a hit, GP
   calls GET /v1/quilt/{user_id}?project_id&group_by=origin&limit=150
   and injects the meeting-grouped dossier in place of the recall
   block. Response shape per doc 07, stable; CQ versions any change.
4. **Truthfulness.** memory_signals is on for project chats (model
   facing). CQ's coverage line — "(showing N of M stored patches for
   this project)" — is always on for scoped recalls that truncate.
   Deadline/overdue markers render where the data carries dates; the
   overdue guarantee fires when overdue items exist. GP forwards the
   block verbatim.
5. **Source split.** CQ wins on state (open, closed, overdue,
   lifecycle, cross-meeting rollups); SS wins on content (what was
   said, recent verbatim). SS folds older meeting summaries out of chat
   context deliberately now that this is locked.
6. **Caching.** CQ recall output is byte-stable within a UTC day; GP
   caches the injected block on that basis.
7. **Historical heal & cleanup.** Completed: Kore healed (1 meeting
   recovered via stream replay; 1 legitimately empty; 1 device-side
   duplicate; 5 never sent — SS re-sends if they matter). Orphan
   project patches and Speaker-N entities cleaned; all cleanup is
   archive-based so removals flow the delta `deleted` array. The orphan
   population self-heals through Pass-2 auto-parenting; no bulk pass
   needed.
8. **Verification and lock.** The standing three-way test on both
   shapes gates any future change to this flow.

**Amendment 1 (item 9, corrections — active, locks on live fire):**
GP detects correction intent (deterministic, fail closed) and sends
interaction_type=correction with the user's correction text, scope, and
the in-context recall block (context_block; never the model response).
CQ matches the contradicted patch from a candidate set (in-block
first), writes the corrected fact as a new origin_mode='declared'
patch inheriting the superseded patch's cues, archives the stale patch
(corrected_by/correction_source stamps), and connects them with role
'replaces'. Unmatched corrections land as declared patches. Client
acknowledgment wording: "noted, updating the record" — queued is not
applied. Locks on one green live correction from the device.

**Defect ledger from the road to lock** (why item 8 exists): GP output
budget + dossier timeout; CQ correction cue inheritance + action_items
bucketing (fix pending SS ack); two relay drops (fixed by the shared
status ledger practice); one wrong straggler theory (resolved by
stream replay).
