# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ContextQuilt** is a persistent cognitive memory layer for AI applications — a slide-in-place layer between applications and LLM providers that fixes stateless-LLM "goldfish memory" and cross-platform memory fragmentation.

## Core Architecture Principles

### The "Zero-Latency" Asynchronous Architecture

- **Read Path (Synchronous)**: live LLM calls query Redis "Working Memory" + fast Postgres lookups for instant context injection. No LLM call on the read path. Never block it with expensive operations.
- **Write Path (Asynchronous)**: background worker does cognitive consolidation (extraction, dedup, lifecycle) after the user already has their response.

### The Connected Quilt Data Model

Three memory tiers: **Factual** (Postgres `context_patches` — typed patches), **Episodic** (entities + relationships graph), **Working** (Redis, short TTL). Patches are typed (trait, preference, identity, role, person, org, project, deliverable, decision, commitment, blocker, takeaway, goal, constraint, event, behavior) and connected by stitching (roles: parent, depends_on, resolves, replaces, informs). Two per-type manifest keys govern the storage sink, both absent by default and absent means today's behavior: `collapse_duplicates: false` opts a type out of BOTH dedup tiers (two observations of one behavior are a trajectory, not a duplicate, and a collapse keeps one origin_id so it destroys a receipt), and `origin_scoped: true` stamps origin_id/origin_type on a type that is meeting-bound without being project-bound (without it such a type lands origin-null and the person cluster query, which requires an origin, can never see it). Neither is the `longitudinal` flag, which additionally asserts series identity and is wired only into structured ingest. Per-app taxonomy lives in registered manifests (`app_schemas`, see `init-db/11_shouldersurf_schema.json`); the schema-driven prompt builder generates extraction prompts from them. Manifest `ingest_mode` (extraction|structured) picks the ingest transformer and is ENFORCED by the worker when explicitly declared (undeclared → legacy routing; SS predates the key); `correction`/`completion` are ADAPTER-INDEPENDENT (allowed under any declared mode), and an unmatched correction lands as the manifest's `correction_fallback_type` (declared-type-validated; absent → takeaway). Onboarding a new app = starter template (`templates/manifests/`, served at `GET /v1/schema/templates`) → lint via `POST /v1/schema/validate` → admin registration; see `docs/architecture/13-app-onboarding.md`.

## Key Technical Concepts

### Extraction Pipeline (Cold Path, src/worker.py)

**Ingest makes FOUR calls per meeting, not one**, because the transcript is available exactly once and prompt real estate is zero sum (doc 19.5). The main extraction below; the **communication profile** (`_extract_communication_profile`, gated on the `(you)` marker, rolling average into `profiles.variables.communication_profile` — measured 2026-08-16 as converged on the midpoint of 5 of 6 dimensions at 167 samples, served as a top-level recall field that is never rendered into the context block, so verify a consumer exists before extending it); and **behavior observations** (`services/behavior_extraction.py`, inert unless the manifest declares a `behavior` type). The behavior call exists because inline it produced 4 observations across 8 meetings on Haiku and 0 on Sonnet against 48 from a dedicated call; measured at 7.0/meeting after the split vs a 0.5 inline baseline. It writes ONE type through the SAME `store_connected_patches` sink (a second writer with its own path would be a second source of truth about one person), runs AFTER the real extraction is stored, and never raises. Its output passes through `sanitize_behavior_observations` (the same rules as the main chain; the lane ran unsanitized until 2026-09-01 because the rules were wired into the main chain only, one rule on two carriers) and then through `services/behavior_classifier.py`: one batched judge call shown EVERY declared type with its manifest description (contrast, doc 19.8) that types each observation; a verdict of any type the main extraction owns DROPS the row (a commitment minted here would carry no deadline, no owed_to, no project scope, and land in a ledger), `preference` converts via `convert_to_preference` with `held_by`, and anything malformed KEEPS (fail open). Kill switch `CQ_BEHAVIOR_CLASSIFIER_ENABLED`, model override `CQ_BEHAVIOR_CLASSIFIER_MODEL`, drops logged with texts as `behavior_classifier_verdicts`; `scripts/backfill_behavior_classify.py` applies the same instrument to history and its dry run is the measurement. And the **semantic role signals** (`services/role_semantics.py`, doc 21 stage 2 second cut, kill switch `CQ_ROLE_SEMANTICS_ENABLED`): who assigned a follow-up to whom and whether it was taken, who moved the agenda, who was waiting on somebody upstream, and the directive-vs-responsive turn split. Doc 19.1 is the whole design: the model returns POINTERS AT TURN NUMBERS, never names and never counts, so WHO did it is read off that turn's own speaker label in the `transcript_turns` parse and a citation to a turn nobody took is dropped rather than counted. Unlike the behavior call it runs BEFORE `store_entities`, because these are columns on the appearance row and that row has exactly one writer with one set of re-ingest rules; a later UPDATE would be a second. Never raises, and an empty result is a NULL column, never a zero. (A fifth call, the alignment detector, is doc 20's and is inert without a project.)

One LLM call per meeting extracts typed patches + entities + relationships + `resolved_commitments` (open commitments are injected into the prompt, overdue-first with deadline annotations). The `(you)` speaker marker identifies the app user. Input is prefixed with `Meeting date:` (anchors relative-deadline resolution) and `User language:` (from `metadata.language`, BCP-47; absent → auto-detect — output prose is written in the user's language, anchored by a required `output_language` schema field).

After the LLM call a fixed sanitizer chain (`src/contextquilt/services/extraction_schema.py`) enforces invariants the LLM is unreliable about, in order: `enforce_owner_gate`, `enforce_connection_requirements`, `sanitize_behavior_observations` (guardrail 12b at capture time: drops a `behavior` patch whose text states character rather than conduct, and strips edges targeting it so Pass-2 cannot resynthesize it as a stub; English denylist only, shared with `follow_through`), `enforce_person_ownership`, `enforce_connection_vocabulary` (manifest label from/to combos — flips reversed edges, drops invalid), `sanitize_you_marker_from_patches`, `strip_owner_on_self_typed_patches`, `strip_prose_from_person_names`, `drop_placeholder_and_self_person_patches`, `inject_ownership_entities` (a person who OWNS an item in this meeting gets an entity, so presence is recorded: appearances are written from the entities array and `enforce_person_ownership` only ever fixed the patches array, so an owner could come out of a meeting with three commitments and no presence in it at all; capacity is `ownership`, never `speaker`, and never `mention` for a name introduced here; position in the chain is load bearing both ways, see doc 16 §6.2b), `drop_placeholder_entities` (Speaker-N/Unknown names out of the entities array + their relationships; same predicate re-checked defensively in `store_entities` for the chat/structured lanes), `sanitize_cues` (normalizes `value.cues` associative-retrieval topic phrases; drops generic/entity-name duplicates), `sanitize_salience` (keeps only `value.salience` low|high; absent = normal), `sanitize_deadline_dates` (validates LLM-resolved `value.deadline_date` ISO dates), `strip_ephemeral_fields`. New sanitizers slot into this chain; each is unit-tested under `tests/unit/`. Backfill scripts in `scripts/backfill_*.py` reuse the live sanitizers — write new ones the same way (dry-run default, `--apply` writes).

**Cues (associative retrieval)**: extraction emits `value.cues` — short lowercase topic phrases naming what a patch is ABOUT ("pricing model") — stored in `patch_cues` (migration 27), popped from value before the JSONB is written. Dedup/longitudinal re-observation UNIONs new cues into the surviving patch. Manifest hooks: `extraction_prompt_guidance.cue_guidance` (replace default instruction), `cues_enabled: false` (kill), per-type `patch_types[].cue_guidance`. Recall-side consumption (cue index → fetch leg) ships separately.

**Corrections (contract item 9, `interaction_type=correction`)**: user chat corrections supersede stale patches — worker `handle_correction` builds a candidate set (patches whose text appears in the passed `context_block` first, then scoped recent), one LLM call picks the contradicted patch BY ID (resolved_commitments pattern; hallucinated ids downgrade to unmatched), then: new patch `origin_mode='declared'`/`source_prompt='correction'`, stale patch archived (+ `value.corrected_by`/`correction_source` stamps, flows delta sync), connected with role `replaces`. Unmatched corrections still land (never lose a user-stated fact). Pure logic in `services/corrections.py`. **Completions (item 10, `interaction_type=completion`)**: same matching against OPEN completables only; closes via the standard machinery (`completion_source='user_chat'` + evidence → delta `completed` array); unmatched completions DROPPED (closing needs a target; corrections land, completions don't).

**Storage dedup is two-tier** (`store_connected_patches`): trigram similarity > 0.6 → same fact (fast path); 0.35–0.6 gray zone → one batched LLM judge call per extraction (`services/semantic_dedup.py`, kill switch `CQ_SEMANTIC_DEDUP_ENABLED`); judge failure → insert, never lose a memory. Dedup hits merge deadline detail forward and bump `last_observed_at`. **Entity aliasing** (`services/entity_aliasing.py`, `entity_aliases` table): extracted entity names resolve exact → recorded alias → conservative unique-candidate heuristic before creating new entities; recall matches aliases and resolves to canonical.

**LLM client gotcha**: `AnthropicLLMClient.extract()` accepts `json_schema` for interface parity but does NOT enforce it on the wire — any prompt used with it must embed the exact raw-JSON output shape, or the model answers in prose and parsing silently fails.

### Deadline & Freshness Lifecycle

- `value.deadline` (as spoken) + `value.deadline_date` (LLM-resolved ISO). Recall renders `(OVERDUE | due today | due soon)` markers; scorer boosts overdue/imminent commitment/blocker.
- **Facet runtime** (`services/facet_runtime.py`, slice 1 of the platform pass): type behavior (completable, project_scoped, freshness-tracked, decay inventory) resolves from `patch_type_registry` (written at manifest registration) through a cached snapshot, SS floor as fallback. SS's own vocabulary is PINNED to shipped behavior (live discrepancy: manifest says `role` project_scoped, shipped write path scopes it conditionally — settle with SS before unpinning). A completable is never freshness-anchored (deadline wins). Registry down → floor, never a crash. Registration invalidates the cache.
- Decay (`worker.decay_loop`, iterates the runtime's decaying inventory — registry-TTL'd types INCLUDED, they previously never decayed; type TTLs via `patch_type_registry` + defaults; per-patch `value.salience` stretches/shrinks effective TTL ×1.5/×0.5 — access-exemption window unmodified): self-typed types (trait/preference/goal/constraint) anchor on `COALESCE(last_observed_at, created_at)` (540d TTL); commitment/blocker anchor on `GREATEST(updated_at, deadline_date)` — never archived before their due date; others on `updated_at`. Recall bumps `patch_usage_metrics.last_accessed_at`, which exempts actively-recalled patches from decay.
- Consolidation (`worker.consolidation_loop`, 24h, kill switch `CQ_CONSOLIDATION_ENABLED`): synthesizes higher-order patches from cue-clustered sources per manifest `consolidation_rules` (no rules → inert). Derived patches: `origin_mode='derived'`, `source_patch_ids`, `value.source_cue` idempotency stamp, `informs` connections from sources. See docs/architecture/14-consolidation.md.
- `deadline_sweep_loop` stamps `value.overdue_since` on open completables past deadline (app-visible, flows into delta sync). Project-scoped recall guarantees up to 5 overdue completables surface regardless of recency windows.
- `last_observed_at` moves ONLY via the worker dedup re-observation path; admin edits move only `updated_at`. Recall scorer applies freshness penalty `max(0.30, exp(-days_stale/365))` to self-typed types, with `now` bucketed to the UTC day — **all recall output must stay byte-stable within a UTC day** (upstream prompt caching depends on it).
- Adding a self-disclosed type to the freshness model: update `FRESHNESS_TRACKED_TYPES` in `recall_scorer.py` AND `worker.decay_loop` AND the partial index in `init-db/20_preference_freshness.sql`.
- **Worker gotcha**: "constants" like `DECAY_INTERVAL_SECONDS` are local to their coroutine bodies, not module scope — verify before referencing from another loop (a NameError in any gathered loop crash-loops the whole worker).

### Database Migrations

`init-db/*.sql`, tracked by filename + sha256 in `schema_migrations`, applied by `scripts/run_migrations.py` from the deploy workflow. Editing an applied file aborts the deploy as drift — always add a new file.

### Recall (Hot Path, POST /v1/recall)

Redis entity index (names ∪ aliases, self-healing) + cue index (`cue_index:{user}`, patch_cues topics, degrades if table absent) → entity/cue match in text → recursive-CTE graph traversal → patch fetch (project-scoped + universal + overdue guarantee + cue-matched leg) → heuristic scoring (cue-fetched +75) → formatted block. Matched cues gate recall alongside entities and suppress metamemory gap claims. Scoped recalls that truncate append a coverage line `(showing N of M stored patches for this project)` — always on, contract commitment E. `metadata`: `project_id`/`project` (scope), `locale` (grouped-mode labels), `token_budget` (flat-mode size, default 700, clamped 100–2000; ~4 chars/token), `max_age_days` (tier recall window: serve meeting-bound patches whose `COALESCE(last_observed_at, created_at)` falls within the last N UTC days; universal self-disclosure types exempt; ONE predicate on every leg incl. the overdue guarantee, cue leg and coverage denominator; absent/malformed = no window, never 4xx; the number is the gateway's per-tier dial, CQ holds no default). 30s render cache keyed on the full request shape. Flat mode markers are deliberately English (LLM-facing).

### Quilt API (app-facing, JWT or X-App-ID)

- `GET /v1/quilt/{user_id}` — full sync or `since=` delta (`deleted` + `completed` arrays distinguish decayed from resolved); `origin_id=<meeting UUID>` meeting view (capture order, no ranking); `group_by=origin` adds a `meetings` array; `project_id=` project rundown filter + `limit=` cap (context-flow contract — GP's rundown route); `max_age_days=` the tier recall window on the rundown leg, same predicate as recall's (the chat flow has TWO CQ legs and a window on one is a window with a hole), `total_available` counts inside it, sync callers never pass it. Patches carry `deadline_date`, `origin_id/type`, connections.
- `POST /v1/quilt/{user_id}/patches/{patch_id}/complete` — app-initiated completion (commitment/blocker; 409 on race with worker auto-close). Both close paths stamp `value.completion_source` + `completion_evidence`.
- Triage siblings: `POST .../vouch` ("Still live": stamps `last_vouched_at`, bumps `updated_at` = the decay extension), `POST .../shelve` / `DELETE .../shelve` ("Let it go": stamps `value.shelved_at`, patch STAYS active so recall keeps it; ledger + counts exclude it, /v1/quilt keeps it with the stamps, never a tombstone), `POST .../uncomplete` (reverses a completion from ANY lane; restores active, moves completion stamps to `prior_*`, item reappears via normal delta). Person detail serves completion history (`commitments.completed_they_owe`/`_you_owe`, `{total, items}` capped 20, completed_at-gated so decay never appears as a delivery); detail route only, as are `described_as` (the entity_descriptions series, migration 39: `current`, `changed_from`, `history`; was served NULL for everyone from #286 until 2026-08-21 because the fetch ran on a released pool connection and the lagging-DB guard swallowed it, so a null here is now a WARNING-level log, never debug) and `stated_roles` + `title` (PRECEDENCE RULE: a role the person STATED, a `people.stated_role_type` patch matched by name/alias prefix or a `describes` edge, BEATS the description a meeting INFERRED; `title` = newest stated role with the name and copula stripped, `title_source` is its receipt; SS floor type `role`, explicit manifest null = not tracked) and `who_they_are` (the SYNTHESIS across stated roles + the description series, `services/who_they_are.py` + worker `_derive_who_they_are` in the person-cluster pass: arithmetic in code, the model writes from inputs it was handed, the parse REFUSES a summary that drops the newest stated role's title phrase, invents a number, opens with the name or uses a dash; its own model `CQ_WHO_THEY_ARE_MODEL` default claude-sonnet-4-6 because the 08-13 experiment showed Haiku fails cross-meeting synthesis invisibly; kill switch `CQ_WHO_THEY_ARE_ENABLED`; regenerates ONLY when the inputs fingerprint changes, prior card archived `replaced`; served as its own field with typed receipts, filtered OUT of the `insights` card stack because it is a paragraph not a capsule); as is the 16a `insights` lens stack (worker profile pass; the durable no is PER LENS so a suppressed card never bars the person's other lenses; evidence rows are one per distinct meeting carrying the source patch text and an INGEST date, never a meeting date; the insight's own `decay_state`, never a synthesized confidence float; `capabilities.insights` follows the caller's manifest). Ledger items carry `decay_state` (live|aging|stale, open vocab) derived from `services/decay_model.py` — the SAME module the worker decay loop consumes (bands = fractions of effective TTL, bucketed to the UTC day). Archive sites stamp `value.archive_cause` (decay|replaced|corrected|merge|project_archived|cleanup|dedup); doc 16 §5.7.
- **Item ledger (doc 16 §5.9a-§5.13)**: THE PRIMITIVE IS NOT A COMMITMENT, it is a thing that keeps coming back without resolving (a question nobody answered, a decision that keeps being revisited, an unowned concern are mechanically identical to a molted commitment). Nothing in the wire shape may assume a commitment: `services/item_ledger.py`, served as `item_ledger` (person detail) and `item_ledger_rollup` (person list, unfiltered population, open items only), every entry carries `object_type` (open vocab) and the block publishes `vocabulary.modes_by_object_type` so a client never guesses which modes apply. Modes: resolved|absorbed_by_user|reassigned|not_raised_since|re_dated|restated|open, in that precedence, one headline `mode` + all applicable `modes`; `re_dated` is the ONLY commitment-specific one (needs a date). ELIGIBILITY resolves from `TypeRuntime.ledger_tracked_types` = every completable UNION every type declaring `ledger_tracked: true` (migration 38), NOT completability alone, because that also means deadline-anchored AND "a person owes this", and a recurring question is neither. Day one = commitments/blockers only; `commitments.they_owe` filters back to completables EXPLICITLY so a question can never arrive as an obligation. The dedup path records restatements for ledger-tracked types (`value.restatements` capped 10 + monotonic `value.restatement_count`, `origin_id` as the same-meeting idempotency key; `value.text`/`value.owner` NEVER rewritten, a handover stamps `value.owner_restated_at`). Rules that bind anything built on top: ship the count never the cause, instances never traits, no ratio at any denominator, `median_hop_count` null never NaN, every count traceable via `patch_ids_by_mode`, COUNTS ON THE LIST / RECEIPTS ON THE DETAIL (list strips `item_ledger.RECEIPT_KEYS` as a family). `object_regression` is honestly null (seam: `classify_items(regressions=...)`).
- **A served name may assert only what was OBSERVED (doc 16 §5.10/§5.13)**: `silently_dropped` was renamed `not_raised_since` because absence does the work there and a meeting cannot see an email (the item may have been finished offline and never mentioned); clients render off `meetings_since_last_statement`, never as abandonment. Where inference is unavoidable, publish the definition ON THE WIRE (`ADVANCE_DEFINITION`, `CHASE_DEFINITION`), never in a docstring. Full audit of every served name in §5.13.
- **Follow-up pressure, doc 16 §5.12**: the metric is `raised_without_advance` (a restatement's `origin_id` joined to the same meeting's `questions_from_user_*`, resolved against whether the item CLOSED by the next meeting with that person), NOT questions received. Measured: volume is near level (12 vs 10) across people whose follow up is opposite, so a claim built on volume contradicts the data. Keep the two counts SEPARATE. It was `chases` until the naming rule bit its own author (the join is MEETING level, not question level, so pursuit is not observed). `RAISED_DEFINITION` and `ADVANCE_DEFINITION` are on the wire: a re-date and a restatement are NOT advances; `unresolved` (chased in the latest meeting) and `unmeasurable` (meeting predates migration 37) are their own numbers, never folded into failure.
- **Question counts (doc 16 §6.6, migration 37)**: `extraction_schema.question_attribution` parses the transcript in the SAME derive-then-discard pass as `speaker_turn_counts` (no backfill is ever possible) and writes six nullable columns on `person_appearances`. Explicit (comma-delimited vocative at a sentence edge) and inferred (trailing question + who spoke next) are SEPARATE columns and must never be summed; `meeting_questions_by_user` is the denominator. A name mid-clause is a name talked ABOUT, never an addressee.
- **Observed role signals (doc 21 stage 2, migrations 42 + 43)**: `person_appearances` carries `opened_meeting`/`closed_meeting`/`answers_given`, exact from the transcript, plus `follow_ups_assigned`/`follow_ups_accepted`/`agenda_moves`/`upstream_deferrals`/`directive_turns`/`responsive_turns` from the semantic call. NULL is unknown throughout. A FALSE in 42 is an exact parse; A ZERO IN 43 IS WEAKER, it is a model finding none, so nothing may read it as "they never do this". `follow_ups_accepted` is a subset of `follow_ups_assigned` and the gap between them IS NOT REFUSALS (silence is neither); `directive_turns + responsive_turns` is NOT `turn_count`, because an unclear turn stays unclassified on purpose. None of it is ever backfillable.
- **Speaker attribution and presence (doc 16 §6.2a)**: two meeting-scoped forms, both writing a `speaker`-capacity `person_appearances` row dated by the MEETING's ingest clock (sibling rows, else the meeting's patches), never `NOW()`. `POST .../reassign-speaker` is IMPERATIVE (`from_labels[{label,meeting_id}]` → exactly one of `to_person_id|to_name|to_self`; `to_name` resolves-or-creates through the same path as `POST /v1/people`, `to_self` lands on the ego entity or writes nothing when no ego link is stamped; the source label's row is left alone and no measurement moves, because moving patches is not a partition of a meeting's turns). `POST .../speaker-map` is DECLARATIVE and idempotent: the meeting's whole label mapping (`labels_are_complete` must be true, `to_nobody` is explicit), diffed against held appearances, so an undo/segment edit/consolidation is just the resulting state. Removal is graded: strip `speaker` + null the per-speaker metrics when the row stands on another capacity, DELETE only when `speaker` was the sole capacity, never touch a row without it (empty capacities = pre-31 unknown = presence), and skip the removal half entirely if any label failed to resolve. A null `signals.last_present_at` now means NOT PRESENT everywhere except a rename-speaker placeholder create and lanes that call neither form. Ownership-grade presence is written at INGEST (doc 16 §6.2b, `inject_ownership_entities`): the backfill's ownership tier is now the repair for history, not the mechanism, and `observed_capacities` is the single place that decides what one ingest may stamp (`speaker` is never accepted from an entity, in any lane).
- Project scope lifecycle: `POST /v1/origins/{u}/{ot}/{oid}/assign-project` (rescope) and its mirrors `.../unassign-project` (optional project_id guard) + `POST /v1/projects/{u}/{pid}/unscope` (project-deletion form; patches survive unscoped). Cleanups archive, never hard-delete (delta-sync tombstone lesson); `scripts/cleanup_orphan_memory.py` (dry-run default).
- `GET /v1/schema` — caller's own latest registered manifest (launch refresh). Admin-gated variant: `GET /v1/apps/{app_id}/schema`.
- CQ authenticates apps, not end users; apps vouch for `user_id`. See `docs/architecture/10-security-and-authentication.md`.

### Cross-team note

ContextQuilt is consumed by ShoulderSurf (iOS) through the GhostPour gateway. **Verify additive API changes through GP's proxied path, not just CQ's socket.** GP middleboxes have eaten metadata keys and query params before. Coordination details live in the private ops dossier (see global CLAUDE.md pointer).

### How the three teams talk to each other

Identical wording lives in CQ's, SS's and GP's CLAUDE.md, because shared
literal text is itself drift resistance. Every rule below was paid for in
the week of 2026-08-11, and each carries its receipt. Add one only after
it has cost something.

1. **Send the mechanism, not the summary.** A merge relocated identity
   because CQ's API could not express a name choice while SS's client
   expressed it by moving the surviving row. Both bugs produced the
   identical observable, so from either side the other was invisible and
   more private rigour would not have found it. What found it was one
   team describing a fix in enough detail that the other could check
   their half against it. When both parties hold a COHERENT account,
   nobody is confused, so nobody asks.

2. **Prove the test can fail.** GP verified their passthrough tests by
   making the proxy behave like a middlebox (dropping 4xx bodies,
   re-sorting arrays, flattening nested ones) and confirming each test
   went red. SS sabotaged a survivor rule to confirm three tests caught
   the exact bug shape. Both found real defects. A test that cannot fail
   on the bug it was written for is decoration.

   And prove the SABOTAGE worked, not merely that something went red. It
   failed repeatedly in one evening across both teams; five of the ways:
   a mutation that never reached the file; one that reached it but sat
   on a branch the test could not take; a module break that turned
   everything red and read as caught; the same break leaving a
   source-reading test green and reading as uncaught; and a grep
   matching a warning URL containing "error". So confirm the mutation
   reached the file, then that the test you EXPECTED failed, and only
   that one. A diff proves the edit and coverage proves the line ran;
   NEITHER proves the branch you changed was taken, because a
   short-circuit inside one expression compiles to a real jump with
   every instruction on one source line, invisible to any tool reasoning
   in lines.

   A sixth way, worse than the five because every check above passed:
   GP inverted a predicate, expected six tests red, saw two, and the
   honest reading of that is "the filter is more robust than I thought".
   It was a STALE BYTECODE CACHE after several rapid mutations, so the
   run had used the pre-mutation module while the file on disk showed
   the mutation. The mutation reached the file, the branch was
   reachable, the test was correct, and the result was still fiction.
   Nothing in the diff, the file or the test could have revealed it,
   because the thing that was stale was none of those. So when a
   sabotage says a test is more robust than you expected, treat the
   surprise as the finding and re-run it in ISOLATION with the cache
   cleared, rather than as evidence you built better than you knew. CQ
   hit the same shape from the other end the same evening: a mutation
   that landed in the file, changed nothing semantically, and went
   green, which is indistinguishable from a coverage gap unless you
   check what the edit actually did.

3. **A response-side test cannot see a request-side hole.** `to_name`
   was sent by SS and silently dropped by an unmodelled field in GP's
   schema. SS saw a correct send; CQ saw a complete request that simply
   lacked a name, so neither endpoint held evidence that anything was
   wrong. It lived only on the middle hop, and only a request-side test
   there could find it. (How long it sat is unmeasured. An early draft
   said "about a week", which nobody had counted; GP caught it, which is
   rule 6 working before this text had even shipped.)

4. **Check the echo, not the status.** A 200 says the request was
   processed, never that it did what the caller meant. A merge reported
   success while the chosen name never travelled, and an endpoint logged
   `200 OK` for sixty seconds while every device saw a 504. Where a
   write has an outcome worth confirming, serve it back and have the
   caller compare.

5. **Name which side each claim was proved on.** Additive at the writer,
   at the gateway and at the reader are three different claims, and each
   side can prove its own half while the failure lives on another. This
   is doc 19.9 stated as a habit rather than a ruling.

6. **Say what you have NOT done, and correct your own numbers out
   loud.** "I have not started it tonight and I am not going to pretend
   otherwise" is worth more than a hedge. A cost estimate that was 4x
   wrong, a scope of 41 meetings that was really 150, and a "13 of 167
   close cleanly" that was really an abstention rate were all corrected
   by the team that produced them, which is the only way any of them
   could have been.

7. **Verify the property you assert, do not just name it.** SS wrote
   "the BIGGER relationship survives, always" in a comment above code
   that chose the survivor by how many words were in the NAME, and
   shipped it; the comment was true of the intent and false of the code.
   CQ wrote `getattr(body, ...)` for a parameter actually named `req`,
   which passed a syntax check and would have been a NameError on the
   first real call. Two teams, one day, one shape: a name that sounded
   right and was never opened. A comment cannot be wrong out loud.

8. **Wherever two systems each apply a CORRECT filter, the intersection
   is invisible to both.** Scott asked why Suresh, his highest-data
   person at 140 meetings, showed FEWER insight lenses than a colleague
   at 104. CQ's `one_card_per_lens` collapse kept one card per lens,
   correctly. SS dropped any claim with fewer than three receipts,
   correctly, because three receipts is what separates a pattern from a
   coincidence. The collapse happened to keep the cards with TWO rows,
   so the client dropped them and the page starved. CQ saw three cards
   shipped, SS saw one rendered, and the deciding number lived inside a
   card neither team was inspecting. No error, no log, no failing test,
   both halves behaving exactly as written. This predicts WHERE to look
   rather than describing the damage afterwards: when a user reports
   "less than I expected" and both sides look correct, stop hunting for
   a fault and go find the INTERSECTION of two filters. Then make each
   side's drop audible, because the other team cannot build your half.

## Documentation

`docs/architecture/00-21` (overview, memory model, pipeline, queue, recall, integration, configuration, API reference, connected quilt, domain mapping, security, model selection, structured ingest, app onboarding/templates, consolidation, context-flow contract, people, prompt composition authority, app memory isolation, recurring rulings, alignment layer, role assertions) and `docs/openapi.yaml`. Five of these carry decisions rather than description, so read them before touching what they cover.

**19 IS THE ONE TO READ FIRST, AND THE SHORTEST.** Every rule in it was learned twice or more, in different code, by someone who did not know it had already been learned. Read it before designing an LLM call, adding a field the model must produce, writing a timestamp at ingest, or trusting a check. The headlines, each with its receipts in the doc: the model may identify but may not count (19.1); a contract with exactly one carrier disappears silently (19.2); an unstated field is an unemitted field (19.3); a re-ingest is the same observation arriving twice (19.4); prompt real estate is zero sum, so a new type may deserve its own call (19.5); check what your instrument resolves through (19.6); a pause enforced by convention is not a pause (19.7); a model describes the SHAPE of what it is given, so a claim true of the whole population carries no information even when accurate, and CONTRAST is the fix rather than phrasing (19.8); a failure invisible from your side of a boundary stays invisible, so name which side each claim was proved on (19.9); an absence is evidence only if the contradicting result had a channel to arrive through, so a machine decision nobody sees is unexamined rather than uncontested (19.10); when a carrier disappears, enumerate everything riding on it, because the fix lands on the one field somebody looked at and the same carrier was holding up several (19.11); and a check that never watches the outcome passes by construction, so a source-reading test gets an executing sibling and a client reads the result before it changes what the user sees (19.12). New entries go in ONLY after a rule has cost something twice; the receipts are the point, since a rule without them gets argued with. **15** is the LOCKED three-team contract: changes to the project-chat context flow require the three-way test per its item 8. **16** is the People object type, now SHIPPED and live (identity write-back, person_appearances, the read surface, `owed_to` and manifest v9); its `capabilities` block is the contract that null means not tracked while an empty list means none open. **17** is the ratified companion to 15: GP owns the prompt composition recipe and SS executes it, so a placement change must never need an SS build. **21** is the gap analysis for the Memory Layer Spec (the Role Evolution sheet): what the `RoleAssertion` record needs that CQ does not have, what CQ already satisfies, and the three requirements that COLLIDE with rulings already in force (a meeting date CQ deliberately does not persist, a served confidence float doc 16 forbids, and transcript spans CQ cannot keep). Read it before designing anything on a time axis. **18** is the app-isolation decision: each app gets its own subject space and there are no shared person objects, no `app_id` column and no per-app read filters; if you are about to add any of those, that doc explains what was measured and why it was declined. FastAPI auto-docs at `/docs`. NOTE: docs/openapi.yaml lags the June 2026 surface (meeting views, complete endpoint, token_budget, language, People); update when touched.

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

Required env: `DATABASE_URL`, `REDIS_URL` (or host/port/password), `CQ_LLM_API_KEY`, `CQ_LLM_BASE_URL`, `CQ_LLM_MODEL`, `CQ_ADMIN_KEY`, `JWT_SECRET_KEY`. Anthropic-direct primary uses `CQ_ANTHROPIC_API_KEY` / Secret Manager (`CQ_GCP_PROJECT`); `CQ_LLM_PRIMARY_PROVIDER` flips anthropic↔openrouter. `CQ_SEMANTIC_DEDUP_ENABLED` kills the dedup judge; `CQ_ROLE_SEMANTICS_ENABLED` kills the semantic role-signal call.

Extraction quality: `CQ_LLM_API_KEY=... python tests/benchmark/test_extraction_dryrun.py [transcript] [--user "Name"]`.

Performance targets: recall <10ms cache-hit / <50ms miss; extraction 2–10s async; prewarm <50ms.

## Patent Notice

Provisional patent covers the asynchronous zero-latency architecture, hybrid cognitive data model, and active enrichment methods — preserve these when modifying core components.

## License & Contact

Apache 2.0. [contextquilt.com](https://contextquilt.com) · scott@contextquilt.com · [GitHub Issues](https://github.com/scottxxxxx/contextquilt/issues)
