# Test 3 — Adversarial Grief / Mental-Health Scenario

**Scenario:** Complicated grief, strained sibling relationship, therapy context. A classification test — the LLM is given raw unlabeled facts and must classify them into the 6 facets itself.

**Purpose:** Pressure-test whether the 6-facet model has a gap around emotional/internal states, clinical framings, or narrative identity. Designed to *break* the model, not confirm it.

**Key design twist:** rather than pre-assigning facts to facets (which risks designer bias), the LLM classifies each fact itself and flags anything that doesn't fit. If 3+ items cluster into the same UNCLASSIFIED theme across runs, a 7th facet is real.

**Runs:** 2 at temp 0.

---

## System message

```
You are a supportive AI companion (not a therapist). The user's memory
is supplied as raw facts below — UNCLASSIFIED.

The 6 cognitive facets are:

1. ATTRIBUTE  — stable fact about what the person IS
2. AFFINITY   — soft LEAN toward/away from something
3. INTENTION  — forward-looking TARGET the person is aiming at
4. CONSTRAINT — hard RULE the person must/can't do
5. CONNECTION — an ENTITY in the person's world
6. EPISODE    — a TIME-BOUNDED thing that happened or is happening

INSTRUCTIONS:
- STEP 1: Read all supplied facts.
- STEP 2: Classify each fact into exactly ONE facet. If a fact doesn't
          fit any facet cleanly, put it in UNCLASSIFIED with explanation.
          Honest flagging is more valuable than tidy classification.
- STEP 3: Answer the user's question using the classified memory.
- STEP 4: Report (a) facets used, (b) UNCLASSIFIED facts and what
          they would need.
```

## Raw memory (22 facts, unlabeled)

1. 38 years old, female, mother of Mia (5)
2. Type 1 diabetes since age 9
3. Takes Wellbutrin 150mg daily
4. Mother (Eleanor) died 8 months ago after long illness
5. Has one sister — Allison, 34 — estranged since a fight 3 months ago over their mother's will
6. Partner: Sam (married 7 years)
7. Remote UX designer; flexible hours
8. Therapist: Dr. Martinez, weekly Tuesday sessions
9. Dr. Martinez's clinical framing: user is experiencing "complicated grief," NOT clinical depression
10. Currently feels hopeless about ever repairing the relationship with Allison
11. Prefers journaling over group therapy; values one-on-one processing
12. Promised Allison she would not touch their mother's jewelry until both ready
13. Working on: setting firmer boundaries with Allison without cutting her off entirely
14. Cannot drink alcohol — Wellbutrin interaction
15. Was raised by a mother who was emotionally distant and withholding
16. Has a deep fear of becoming emotionally distant with Mia the way her mother was with her
17. Has a recurring dream about her mother's final hospital visit
18. Been avoiding most social plans for ~6 months
19. Wedding anniversary is in 3 weeks; Sam wants to celebrate; user feels guilty enjoying anything right now
20. Sister Allison's birthday dinner is next Saturday; user is invited; hasn't responded yet; flipping between going and not going all week
21. Long-term goal: be the emotionally present parent her own mother never was
22. Recent insight from therapy: user tends to apologize first to end conflict, even when she doesn't agree

## User query

> Should I go to Allison's birthday dinner next Saturday? I keep flipping. Part of me says I should — she's my sister. Part of me dreads it and doesn't want to pretend things are fine.

---

## Results

**Run 1:** 947 input / 1,352 output tokens

- UNCLASSIFIED: **1 fact** — Fact 10 ("currently feels hopeless about repairing Allison relationship")
- LLM's explanation: "This is an emotional state tied to a specific relationship goal. It doesn't cleanly fit AFFINITY (a lean/preference) or INTENTION (forward-looking). Closer to a temporary emotional register that may need its own micro-facet."
- Suggested new facet: `EMOTIONAL_REGISTER` — time-bound feeling tied to an active episode.

**Run 2:** 947 input / 1,197 output tokens

- UNCLASSIFIED: **1 fact** — Fact 17 ("recurring dream about mother's final hospital visit")
- LLM's explanation: "Fits no single facet; is symptom/processing marker that bridges grief (EPISODE) + emotional state."
- Suggested new facet: `SOMATIC/PROCESSING STATE` — symptoms of grief that don't fit narrative facts.

---

## Cross-run analysis

| Fact | Run 1 | Run 2 | Stable? |
|---|---|---|---|
| 9 (clinical framing) | EPISODE | EPISODE | Yes |
| 10 (feels hopeless) | **UNCLASSIFIED** | Absorbed into EPISODE | **Unstable** |
| 12 (promise re: jewelry) | CONSTRAINT | CONSTRAINT | Yes |
| 15 (raised by distant mother) | ATTRIBUTE | ATTRIBUTE | Yes |
| 16 (fear of becoming distant) | AFFINITY | AFFINITY | Yes |
| 17 (recurring dream) | EPISODE | **UNCLASSIFIED** | **Unstable** |
| 19 (guilty about anniversary) | EPISODE | EPISODE | Yes |
| 22 (therapy insight) | Used in answer, not classified | EPISODE | Soft |

**20 of 22 facts stable across runs. 2 unstable — and they flagged *different* facts.**

---

## Verdict

**Outcome B: soft signal, not a real gap.**

Key diagnostic: if a genuine 7th facet existed, both runs would flag the *same class* of items. The fact that Run 1 flagged #10 and Run 2 flagged #17 — and named the proposed new facet differently (`EMOTIONAL_REGISTER` vs `SOMATIC/PROCESSING STATE`) — means Episode *can* absorb these, just uncomfortably. It's a fit-quality problem, not a missing-category problem.

**Resolution:** keep the 6-facet taxonomy. Document that **Episode has sub-flavors**:
- `external_event` — things that happened in the world
- `internal_state` — time-bounded subjective states (hopelessness, anxiety windows)
- `processing_marker` — recurring signals of ongoing processing (recurring dreams, rumination)
- `clinical_label` — framings applied by a professional
- `insight` — epistemic shifts the person had

All share Episode's lifecycle and recall job. Sub-classification, not a new facet.

**Confidence after this test:** 6-facet model holds for individual-subject memory across emotionally complex domains. Moved from 90% → ~92%.
