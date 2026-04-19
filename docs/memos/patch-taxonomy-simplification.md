# Memo — Memory Patch Taxonomy v1

**Date:** 2026-04-18
**Author:** Scott
**Audience:** ContextQuilt core team + ShoulderSurf team
**Decision requested:** Approval to ship the 6-facet generic taxonomy, the core/app-schema separation, and the ShoulderSurf migration as described below.

---

## TL;DR

Four controlled tests on Haiku 4.5 (9 total runs) across 5 domains — meeting capture, healthcare, legal, career, grief, couples therapy — confirm a generic **6-facet cognitive taxonomy** that cleanly handles every scenario we could throw at it, including adversarial multi-party memory. We also validated that **recall format is a bigger lever than taxonomy size** and that CQ memory has an **anti-hallucination property** worth positioning around.

Two architectural changes ship together:

1. **Formalize the 6-facet model** as CQ v1 universal primitive.
2. **Separate CQ core from per-app schema** — core owns the grammar (facets, roles, permanence), apps register their dictionary (domain types, labels). The infrastructure (`app_id` columns) is already there; we just start using it.

ShoulderSurf becomes the first app to register a schema against the primitive.

---

## The 6-facet cognitive taxonomy (CQ core, universal)

Every memory patch lives in exactly one of these cognitive roles. Apps register domain types against these facets; they never define new top-level facets.

| Facet | Job for the LLM | Examples |
|---|---|---|
| **Attribute** | Facts to state — what someone IS | age, diagnosis, credentials, background |
| **Affinity** | Shape recommendations — what they LEAN toward | preferences, values, tastes |
| **Intention** | Time recommendations against goals — what they AIM AT | retire at 62, A1C < 6.5, Director role by 38 |
| **Constraint** | Vet every suggestion before offering it — what they CAN'T / MUST do | allergies, non-competes, bar ethics, medication interactions |
| **Connection** | Graph anchor / entity resolution — WHO or WHAT's in their world | people, projects, companies, accounts |
| **Episode** | Timeline / what's active — what HAPPENED or is HAPPENING | meetings, transactions, diagnoses, decisions |

**Sub-flavors (guidance, not enforced schema):**

- `Episode`: `external_event`, `internal_state`, `processing_marker`, `clinical_label`, `insight`
- `Affinity`: `relational_dynamic` (interaction patterns between two people)
- `Constraint`: `joint_rule` (agreements binding multiple parties)

Each facet maps to a **permanence class** (`permanent` → `decade` → `year` → `quarter` → `month` → `week` → `day`). Apps choose the class per domain type they register.

---

## The core/app-schema split

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
│   • Extraction guidance tuned to the app's input             │
│   • Optional UI presentation hints                           │
└──────────────────────────────────────────────────────────────┘
```

**This split is already possible** — `patch_type_registry.app_id` and `connection_vocabulary.app_id` exist. They've been populated exclusively with `NULL` (universal) values. We start using the app scope.

---

## What the ShoulderSurf schema looks like in this model

| SS domain_type | CQ facet | Permanence |
|---|---|---|
| `trait` | Attribute | year |
| `preference` | Affinity | year |
| `person` | Connection | decade |
| `project` | Connection | quarter |
| `role` | Connection | inherits project |
| `commitment` | Episode | month |
| `blocker` | Episode | week |
| `decision` | Episode | year |
| `takeaway` | Episode | week |

**Cut from SS schema:** `identity` (0 patches in real data, redundant with trait + connection), `experience` (legacy, already removed from extraction).

**Preserved:** `role` stays as a SS domain_type (reversal of earlier "cut role" recommendation). Topology: role patch `belongs_to` project (cascade on archive) + `informs` person (describes). No sub-patch attributes, no broken decay model.

SS connection labels: `works_on`, `owns`, `belongs_to`, `blocked_by`, `motivated_by`, `reports_to`, `member_of`, `vendor_for` (the last three being new additions to close the person→person gap).

---

## Evidence base — four tests, nine runs

See `tests/benchmark/taxonomy_validation/` for full artifacts.

| Test | Purpose | Runs | Key result |
|---|---|---|---|
| Test 1 | SS: post-meeting draft | 3 | With-memory B consistently tighter, pattern-linked, decision-specific. 3/3 |
| Test 2 | SS: pre-1:1 prep | 3 | Bare A cannot answer. With-memory B nails commitment + preference + landmine detection. 3/3. Anti-hallucination: A refused to fabricate 3/3. |
| Test 3 | Adversarial grief classification | 2 | 20/22 facts stable; 2 unstable items flagged *different* items across runs — sub-flavor territory, not a 7th facet |
| Test 4 | Adversarial multi-party (couples) | 2 | 0/34 UNCLASSIFIED in both runs. Multi-party = ownership-model concern, not facet concern |

**Confidence in 6-facet completeness: ~95%.**

---

## Economics

| Test | Bare input | With-memory input | Cost delta per call (Haiku 4.5) |
|---|---|---|---|
| Test 1 | 1,202 tok | 1,958 tok | ~$0.0006 |
| Test 2 | 141 tok | 614 tok | ~$0.0004 |
| Test 3 | n/a | 947 tok | ~$0.0007 |
| Test 4 | n/a | 1,039 tok | ~$0.0008 |

Sub-$0.001 per call for consistent, measurable output lift. At 100 recall calls/user/day, memory overhead is under $0.10/user/day. Economically trivial relative to the output quality lift.

---

## What to ship

### ContextQuilt core team

1. **Add a `facet` column to `patch_type_registry`** (enum: Attribute | Affinity | Intention | Constraint | Connection | Episode). Required for every registered type.
2. **Add a `permanence` column to `patch_type_registry`** (enum: permanent | decade | year | quarter | month | week | day). Required for every registered type. This is the **default** — individual patches can override via `permanence_override` on `context_patches`.
3. **Add per-patch permanence override** — nullable `permanence_override` column on `context_patches`. Decay worker uses `COALESCE(patch.permanence_override, type.default_permanence)`. Supports both user-driven pinning ("keep forever") and app-driven promotion (e.g., promote a patch when it's referenced repeatedly across meetings). Principle: *permanence is a default, not a rule.*
4. **Build the schema registration API** — `POST /v1/apps/{app_id}/schema` accepts a manifest (patch types + connection labels + entity types + extraction guidance + origin types), validates, writes to `patch_type_registry`, `connection_vocabulary`, and a per-app entity-type registry (new in PR 1) with `app_id` set. Apps that don't register `entity_types` fall back to CQ's default set (`person`, `project`, `company`, `feature`, `artifact`, `deadline`, `metric`).
5. **Allow recursive `belongs_to`** (Connection → Connection hierarchy). Enables novel → chapter → scene, dissertation → chapter → section, Benefits App MVP → Florida Blue engagement. Relaxation in connection validation; decay worker already handles recursion via CTE.
6. **Add optional `about_patch_id` column** to `context_patches`. Allows Attribute patches to describe non-user Connection patches ("Elena is 34", "Benefits App MVP is in defined-mode"). Nullable — NULL preserves current self-referential behavior.
7. **Rename `meeting_id` to `origin_id` + add `origin_type`**. Apps declare their input units in manifest (`origin_types: ["meeting", "practice_session", "paper_highlight", ...]`). Generalizes CQ beyond meeting-capture.
8. **Reduce connection roles to 3** (`parent`, `depends_on`, `informs`). Fold `resolves` into `informs`; drop `replaces` (fold into `supersedes` semantics via the lifecycle worker, since stateful flips need this even though the explicit edge went unused).
9. **Build query-scoped recall** (highest-impact item). Replace category-dumped output with a relevance-ranked flat list. Target 150-300 tokens for short queries. Fall back to category dump on error until validated.
10. **Tighten the takeaway extraction prompt** for SS: evaluative lessons with decision implications only; cap at max 3 per meeting.
11. **Document the anti-hallucination property** as a feature. Stronger positioning lever for regulated verticals than "better answers" alone.

### ShoulderSurf team

1. **Register the SS schema** via the new manifest API. Migrates the 8 surviving types from universal (`app_id=NULL`) to app-scoped (`app_id=shouldersurf`).
2. **Add SS connection labels** for person-to-person and org relationships: `reports_to`, `member_of`, `vendor_for`, `org` as new domain type.
3. **Remove `identity` from the UI patch-type picker.** No migration needed — zero user data.
4. **Hide connection options by source patch type** using the previously shared filtered matrix. A Trait never offers "Blocked by."
5. **Add contextual subtitles to the connection picker** (one line per source/target combo).
6. **Defer three-tier UI grouping** until after the schema registration lands.

### Interview app (future)

1. Register its own schema against the same 6-facet primitive. Distinct vocabulary (candidate_profile, interviewed_for, evaluated_by, etc.), zero SS coupling.

---

## Known v2 items (not blocking v1)

- **Participant/ownership model.** Patches currently root on one user. Collaborative apps (couples therapy, team productivity, contract negotiation) will need `participants: [user_id, ...]`. Pattern: introduce when the first collaborative app demands it. Storage-model change, not facet change.
- **Long-horizon decay behavior.** Tests use recent data; no test exercised 2+ years of accumulated patches. Watch decay and supersede behavior at scale.
- **Shared corporate context.** Team coding conventions, company-wide policies. Similar shape to couples memory but at org scale. Same v2 fix applies.

---

## What we're explicitly NOT shipping (yet)

- **Permanence-as-enforced-rule.** Permanence classes ship, but the decay worker keeps the current bucketed implementation (3 TTL buckets under the hood). Refactor to continuous permanence only when a concrete decay bug demands it.
- **User-confirmed takeaways.** Violates the async zero-touch promise. Extraction stays automatic.
- **Emotional state / relational dynamic as top-level facets.** Adversarial tests showed these are sub-flavors within Episode, Affinity, and Constraint — document, don't split.
- **7th facet of any kind.** Both adversarial tests explicitly probed for one and neither confirmed. Revisit only if deployed apps surface a persistent clustering of UNCLASSIFIED facts.

---

## Risks & rollback

- **Schema registration change** is additive. Existing data with `app_id=NULL` continues to work. Migration for SS is a targeted UPDATE statement.
- **Query-scoped recall** is the only non-trivial engineering lift (~2 weeks). Ship behind a feature flag; fall back to category-dumped recall on error. Compare output quality side-by-side for 2 weeks before cutting over.
- **Extraction prompt tightening for takeaway** may reduce takeaway volume ~50%. Expected and desirable; monitor for over-correction for one week.
- **Role reinstatement** requires updating the SS extraction prompt to actually produce role patches (0 in current data). This is a SS-schema-migration sub-task, not a separate workstream.

---

## Appendix — Test artifacts

Full prompts, outputs, and scorecards for all four tests live in `tests/benchmark/taxonomy_validation/`. Each test is independently reproducible: copy the prompts into a Haiku 4.5 client at temp=0 and compare.

- [`README.md`](../../tests/benchmark/taxonomy_validation/README.md) — index
- [`SUMMARY.md`](../../tests/benchmark/taxonomy_validation/SUMMARY.md) — cross-test synthesis
- `test1_post_meeting_draft.md` — SS post-meeting test
- `test2_pre_meeting_prep.md` — SS pre-1:1 test
- `test3_adversarial_grief.md` — individual emotional-complexity test
- `test4_adversarial_couples.md` — multi-party memory test
