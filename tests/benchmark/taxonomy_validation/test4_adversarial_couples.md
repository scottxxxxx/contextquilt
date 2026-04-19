# Test 4 — Adversarial Couples-Therapy Scenario (Multi-Party Memory)

**Scenario:** Couples therapy app where the subject is not an individual but a *couple*. Memory includes facts about each partner, about the relationship as an entity, about the family unit, and about shared agreements.

**Purpose:** The deliberate adversarial twist — the 6-facet model's definitions assume "a person" as subject. This test checks whether the model breaks when the subject is inherently multi-party.

**Key design twist:** beyond facet classification, the LLM also marks **ownership** (`[A]` / `[M]` / `[A+M]` / `[FAM]`) and flags any fact where the ownership model feels awkward against the facet assignment.

**Runs:** 2 at temp 0.

---

## System message (excerpted — see source prompt for full version)

The system briefs the LLM on:
- The 6 facet definitions
- The instruction to assign each fact to exactly one facet
- The instruction to also mark ownership `[A]`, `[M]`, `[A+M]`, `[FAM]`
- The instruction to flag any fact where the facet × ownership combo strains
- A final-sentence verdict on whether 6 facets handle multi-party memory cleanly

## Raw memory (34 facts, unlabeled)

Personas: Alex (38, software mgr), Morgan (40, photographer), Mia (6, daughter). Together 8 years, married 5. In couples therapy with Dr. Vance.

Facts span individual characteristics, shared agreements, relationship history, therapist framings, rules of engagement, shared goals, and relational dynamics. Full fact list preserved in the original test prompt.

## User query

> Morgan and I are planning a kid-free weekend trip in 6 weeks. What should we think about as we plan it?

---

## Results

**Run 1:** 1,039 input / 968 output tokens

- UNCLASSIFIED: **0 facts**
- Facet counts: Attribute 6, Affinity 5, Intention 5, Constraint 6, Connection 3, Episode 5
- Ownership counts: [A] 8, [M] 5, [A+M] 16, [FAM] 2
- Awkwardness flags: Facts 20, 21, 24, 29 — "dyadic rules/commitments sit slightly askew in individual facet boxes"
- Suggested new facet: `ALLIANCE` — explicit dyadic agreements
- Verdict: *"The 6-facet model handles multi-party memory reasonably well, but shows stress at dyadic constraints, shared intentions, and family-affecting rules."*

**Run 2:** 1,039 input / 2,489 output tokens

- UNCLASSIFIED: **0 facts**
- All 34 facts classified with full ownership annotation
- Awkwardness flags: Facts 30, 33 — "Morgan's feeling but requires both to shift", "shared philosophy with constraint-like force"
- Suggested new facet: `DYNAMIC` — system-level relational pattern needing both parties to shift
- Verdict: *"The 6-facet model handles multi-party memory reasonably well. For couples therapy memory specifically, a 7th facet — DYNAMIC — would be useful."*

---

## Cross-run stability

| Aspect | Run 1 | Run 2 | Stable? |
|---|---|---|---|
| UNCLASSIFIED count | **0** | **0** | Yes — model survived |
| Ownership counts | A:8 M:5 A+M:16 FAM:2 | A:8 M:6 A+M:17 FAM:3 | Minor variance (±1) |
| Awkwardness flags | Facts 20, 21, 24, 29 | Facts 30, 33 | Different facts, same theme |
| Suggested 7th facet | `ALLIANCE` | `DYNAMIC` | Same concept, different name |
| Core verdict | Handles well, stress at dyadic items | Handles well, stress at relational patterns | Consistent |

---

## Verdict

**Outcome A (with footnote): 6-facet model survived the adversarial test.**

Both runs absorbed all 34 facts — including facts deliberately designed to challenge the single-subject assumption. The ownership dimension `[A]` / `[M]` / `[A+M]` / `[FAM]` does the heavy lifting.

### Critical diagnostic

Both runs independently suggested a 7th facet. But:
- They named it differently (`ALLIANCE` vs `DYNAMIC`)
- They flagged different specific facts
- Neither needed an UNCLASSIFIED bucket

If a real 7th facet existed, the suggestions would converge — same name, same flagged facts. They didn't. Same pattern as Test 3: real fuzziness, but sub-type territory within existing facets.

### The real finding

**Multi-party memory is an ownership-model problem, not a facet-model problem.**

The 6-facet taxonomy is about *cognitive roles* (what job does this memory do for the LLM at recall time). The `participants` dimension is about *whose memory it is*. These are orthogonal.

To support collaborative apps in v2, CQ needs:
- **Participant arrays on patches** (`participants: [user_id, ...]`) — a storage-model change
- **Connection-as-entity** for things like "the relationship" — already supportable today
- **Nothing new at the facet level**

### Sub-flavor documentation

Where runs flagged awkwardness:
- **Affinity** can carry a relational dynamic (e.g., "Morgan feels unheard when Alex plans alone") — document as `relational_dynamic` sub-flavor
- **Constraint** can be dyadic (e.g., "no fighting in front of Mia") — document as `joint_rule` sub-flavor

Both are sub-classifications, not new top-level facets.

**Confidence after this test:** 6-facet model holds across *all* tested domains, including adversarial multi-party memory. Final confidence: **~95%**.

**Known v2 item:** participant/ownership model generalization. Deferred until an app actually needs it.
