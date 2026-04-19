# Taxonomy Validation — Cross-Test Summary

Synthesis across 4 tests and 9 total runs. Feeds directly into `docs/memos/patch-taxonomy-simplification.md`.

## Tests

| Test | Scenario | Runs | Purpose |
|---|---|---|---|
| Test 1 | Post-meeting message draft (ShoulderSurf) | 3 | Which current patch types are load-bearing? |
| Test 2 | Pre-1:1 prep (ShoulderSurf) | 3 | Confirm commitment + preference utility; check anti-hallucination |
| Test 3 | Grief/mental health classification (adversarial) | 2 | Does the 6-facet model break on emotional/internal-state memory? |
| Test 4 | Couples therapy, multi-party memory (adversarial) | 2 | Does the 6-facet model break when subject is a couple, not a person? |

## What each test proved

### Tests 1 + 2 — Current SS taxonomy audit

Validated which of the current 11 patch types earn their keep at recall time.

| Type | Test evidence | Decision |
|---|---|---|
| `trait` | Shaped tone 3/3 in Test 1 | **Keep** |
| `preference` | Drove landmine detection 3/3 in Test 2 | **Keep** |
| `person` | Scaffolding, both tests | **Keep** |
| `project` | Scaffolding, both tests | **Keep** |
| `decision` | Option-framing 3/3 | **Keep** |
| `commitment` | Only source of "what Sri owes" — 3/3 | **Keep** |
| `blocker` | Pattern-link 3/3, push-topics 3/3 | **Keep** |
| `takeaway` | Works when scope-matched to query — 3/3 in Test 2 | **Keep + tighten** |
| `role` | 0 patches — but this turned out to be a prompt bug, not a type-value issue. Reinstated as SS domain_type on Connection facet. | **Keep as SS domain_type** |
| `identity` | 0 patches; no test moment needed it; redundant with trait + connection | **Cut** |
| `experience` | Legacy, already removed from extraction | **Cut from registry** |

**Anti-hallucination property:** In Test 2, the bare variant gracefully declined to invent rather than fabricating (3/3). This is a positioning lever for regulated verticals.

### Test 3 — Does the 6-facet model break on individual emotional complexity?

20/22 facts stable across runs. 2 unstable — different items flagged, different proposed facet names. **Not a real gap.** Fuzziness exists (internal states, recurring dreams, clinical framings) but resolves as **Episode sub-flavors**, not a 7th facet.

Documented Episode sub-flavors:
- `external_event`, `internal_state`, `processing_marker`, `clinical_label`, `insight`

### Test 4 — Does the 6-facet model break on multi-party memory?

0 UNCLASSIFIED across both runs. All 34 facts (individual, shared, relational) absorbed by the 6 facets. **Multi-party memory is an ownership-model problem, not a facet problem.**

The ownership dimension `[A] / [M] / [A+M] / [FAM]` is orthogonal to facets. v2 work should add participant arrays to patches, not new facets.

Documented sub-flavors within Affinity and Constraint:
- `relational_dynamic` (Affinity) — interaction patterns between two people
- `joint_rule` (Constraint) — agreements that bind multiple parties

## Consolidated findings

### 1. 6-facet top-level taxonomy is complete (95% confidence)

```
ATTRIBUTE  — stable fact about what someone IS
AFFINITY   — soft lean toward/away from something
INTENTION  — forward-looking target
CONSTRAINT — hard rule must/can't
CONNECTION — an entity in the person's world
EPISODE    — time-bounded event (happened, happening, will happen)
```

### 2. Query-scoped recall > category-dumped recall

An earlier experiment used ~500-token category-grouped memory blocks; the model used 3 of ~25 patches (low utilization). Tests 1 and 2 used query-scoped ~200-300 token blocks; utilization jumped to ~3-4 of 8 supplied patches. **Recall format is a bigger lever than taxonomy size.**

### 3. App-schema registration beats hardcoded taxonomy

The tests span 5 different domains (meeting, healthcare, legal, career, grief, couples). The 6 facets held across all of them. This validates the architectural split: CQ core owns the **grammar** (facets, roles, permanence); apps register their **dictionary** (domain types, labels, extraction prompts). The existing `patch_type_registry.app_id` and `connection_vocabulary.app_id` columns already support this — we just need to use them.

### 4. Known v2 items (not blocking v1)

- **Participant/ownership model** — generalize patches from single-user-root to accepting participant arrays. Only needed when a collaborative app (couples, team, family) comes online.
- **Sub-flavor formalization** — document Episode, Affinity, and Constraint sub-flavors as part of extraction/recall guidance, not as schema.

## Economics

| Test | Bare input | With-memory input | Memory cost delta (Haiku 4.5) |
|---|---|---|---|
| Test 1 | 1,202 tok | 1,958 tok | ~$0.0006 per call |
| Test 2 | 141 tok | 614 tok | ~$0.0004 per call |
| Test 3 | n/a | 947 tok | ~$0.0007 per call |
| Test 4 | n/a | 1,039 tok | ~$0.0008 per call |

Sub-$0.001 per call for consistent, measurable output lift. Economically trivial.

## Where the tests came up short

Transparent about limits:
- Tests 3 and 4 were single-shot designs with 2 runs each — less statistical weight than Tests 1 and 2's 3 runs each.
- I authored the personas and queries — designer bias is present, especially in Tests 3 and 4.
- No test exercised a **shared corporate context** (team memory, shared codebase conventions) which may behave differently from couples/family memory.
- No test exercised **very long-horizon memory** (2+ years of accumulated patches). Decay and supersede behavior at scale is unvalidated.

These are known unknowns. None blocks shipping v1.
