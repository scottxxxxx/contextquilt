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

**Amendment 1 (item 9, corrections — LOCKED 2026-07-17):**
GP detects correction intent (deterministic, fail closed) and sends
interaction_type=correction with the user's correction text, scope, and
the in-context recall block (context_block; never the model response).
CQ matches the contradicted patch from a candidate set (in-block
first), writes the corrected fact as a new origin_mode='declared'
patch inheriting the superseded patch's cues, archives the stale patch
(corrected_by/correction_source stamps), and connects them with role
'replaces'. Unmatched corrections land as declared patches. Client
acknowledgment wording: "noted, updating the record" — queued is not
applied. Locked on a green live correction from the user's device
(2026-07-17): both lanes exercised on real traffic — first fire landed
unmatched-but-stored because no target existed in the chat's project
scope (scope gating working as designed), re-fire matched and
superseded it with every stamp verified at the socket.

**Amendment 2 (item 10, completions — LOCKED 2026-07-17):**
GP detects declarative completion statements (deterministic, the
tightest fail-closed detector — a false positive closes a real
commitment) and sends interaction_type=completion with the user's
words, scope, and context_block. CQ matches against OPEN completables
only (in-block first) and closes through the standard machinery:
completed_at, completion_source='user_chat', completion_evidence (the
user's words). The closed patch id rides BOTH delta arrays — completed
is a strict subset of deleted by construction — so clients that only
decode deleted stay correct (the item leaves the list). Deliberate
asymmetry with corrections: unmatched completions are DROPPED
(log-and-no-op) — closing needs a real target, and inventing one
manufactures memory. SS ships nothing. Locked on a green live
completion from the user's device (2026-07-17): open commitment
matched and closed, all stamps verified at the socket.

**Operational validation — v1 lit end to end (2026-07-18):** the
build-749 three-way byte test lit the last lane, memory_signals from a
real device through GP's passthrough to CQ's renderer and back. Flag
confirmed on GP's outbound, scoped block returned with signal lines
and coverage line intact, each hop proven by byte capture against
frozen CQ reference renders. Procedural rule earned three times over:
**reference renders must use wire-true text** — GP's machinery
prefixes "User question: " on first turns and sends the entire
conversation history ending "Current question: …" on follow-ups, so a
reference rendered from the bare question never matches the wire.

**Known edges** (accepted, not defects): completed or corrected state
in the quilt does not annotate meeting content that rides inline, so
recent summaries can resurrect closed items until the summaries age
out of the inline window (observed live: a completed commitment
presented as a current priority from a summary). If this ever earns a
fix, the shape is item 11: "completed state annotates recent content"
— the quilt telling the renderer which inline facts it knows to be
stale — general enough to cover corrections too. Signal-line gap
claims are gap-gated: a question naming only covered topics produces
no signal lines with the flag on; that is correct behavior, not a
missing feature.

**Defect ledger from the road to lock** (why item 8 exists): GP output
budget + dossier timeout; CQ correction cue inheritance + action_items
bucketing (fix staged in PR #163, gated on SS's decoder release
reaching users — their decoder never read action_items, so an early
flip would have silently dropped all completables from field builds);
two relay drops (fixed by the shared status ledger practice); one
wrong straggler theory (resolved by stream replay). From the build-749
validation: CQ signal-line candidate selection flagged CamelCase and
markdown-bullet artifacts from prior answers while missing the real
gap in the live question (fixed, PR #166 — extraction scopes to the
current-question segment); GP's 200ms recall budget silently ate one
turn's block on CQ's long-text render tail (budget raised to 500ms,
ratified; CQ phase-timing + slow-render logging shipped in PR #167,
tail hunt open — slow mode is ~140-190ms in CQ's Python render path,
not the database).

**Resolved observation from the amendment live fires** (was: recalls
but no capture sends from a fresh chat on a second device): working as
designed. Ordinary ProjectChat turns are on GP's capture skip list
(echo-loop prevention, agreed design) — no device sends captures for
ordinary turns. The correction/completion lanes fire only on explicit
amendment phrasing against the fail-closed detectors; the distinguishing
axis was phrasing, never device, build, or chat age.
