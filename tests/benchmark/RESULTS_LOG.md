# Extraction Benchmark Results Log

Persistent record of test runs across models, prompts, and transcripts.
Updated after each significant benchmark session.

---

## 2026-04-14 / 2026-04-15 — Gating + Schema + Enforcement Stack

### Context
Built and tested PRs #23-33: (you)-marker gating, JSON schema with 3-tier
fallback, Python post-filter, _reasoning scratchpad, connection enforcement,
owner-gate generalization, prompt genericization, default model switch.

### Prompt version
`MEETING_SUMMARY_SYSTEM` with STEP 0 (you-gate), STEP 1 (reason-then-extract),
identity as V2 type, genericized examples, second-person patch text convention.

### Synthetic transcript (embedded 3-speaker: Scott/Alan/Priya, payments project)

| Model | off | on | verdict | cost | lat | #patches | notes |
|---|---|---|---|---|---|---|---|
| mistralai/mistral-small-3.1-24b-instruct | 0 | 3 | PASS | $0.0004 | 75s | 12 | good connection hygiene |
| openai/gpt-4o-mini | 0 | 3 | PASS | $0.0010 | 6s | 3 | under-extracts |
| anthropic/claude-haiku-4.5 | 0 | 3 | PASS | $0.0164 | 13s | 12 | best quality |
| google/gemini-2.5-flash | 0 | 3 | PASS | $0.0070 | 13s | 12 | good but 2x Haiku cost |
| google/gemini-2.5-flash-lite | 0 | 3 | PASS | $0.0012 | 5s | 10 | fast but over-extracts |
| deepseek/deepseek-chat-v3-0324 | 0 | 3 | PASS | $0.0021 | 65s | 3 | decent, slow |

### Real transcript (475-line onboarding meeting, messy diarization)

| Model | off | on | verdict | cost | lat | #patches | notes |
|---|---|---|---|---|---|---|---|
| mistralai/mistral-small-3.1-24b-instruct | 0 | 0 | OVER-SUPPRESS | $0.0006 | 121s | 12 | good structure, slow |
| openai/gpt-4o-mini | 0 | 2 | PASS | $0.0023 | 7s | 4 | severe under-extraction |
| anthropic/claude-haiku-4.5 | 0 | 1 | PASS | $0.0291 | 27s | 12 | **best quality** |
| google/gemini-2.5-flash | 0 | 3 | PASS | $0.0099 | 14s | 12 | but ALL decisions/commitments orphaned |
| google/gemini-2.5-flash-lite | 0 | 3 | PASS | $0.0020 | 6s | 12 | 12-dup hallucination pre-fix, sloppy connections |
| deepseek/deepseek-chat-v3-0324 | 0 | 2 | PASS | $0.0034 | 62s | 6 | decent |

### Connection enforcement impact (real transcript, WITH marker)

| Model | before | after | dropped | surviving types |
|---|---|---|---|---|
| anthropic/claude-haiku-4.5 | 12 | 11 | 1 | project, 2 decisions, 4 commitments, blocker, takeaway, 2 persons |
| mistralai/mistral-small-3.1 | 12 | 12 | 0 | project, 3 commitments, 2 decisions, blocker, 2 takeaways, 3 persons |
| google/gemini-2.5-flash | 12 | 7 | 5 | 3 preferences + 4 persons only (all scoped patches orphaned) |
| google/gemini-2.5-flash-lite | 12 | 3 | 9 | identity + 2 traits only |
| deepseek/deepseek-chat-v3 | 7 | 7 | 0 | project, commitment, decision, 2 traits, 2 persons |
| openai/gpt-4o-mini | 4 | 4 | 0 | project, decision, 2 commitments |

### Key findings
- Claude Haiku 4.5 is the clear production winner on real messy transcripts
- Gemini Flash/Flash-Lite look good on synthetic but fail on real data (orphan patches, duplications)
- Connection enforcement reveals true quality: models that don't link patches to projects lose all operational context
- Prompt-echo bug (Mistral echoing "Florida Blue"/"Travis") fixed by genericizing examples
- Flash-Lite's 12-duplicate identity hallucination fixed by genericizing JSON example block
- Reasoning scratchpad unlocked GPT-4o-mini from OVER-SUPPRESS and fixed Haiku's trait/preference misclassification

### Decision
Production default switched to `anthropic/claude-haiku-4.5` (PR #30).

---

## 2026-04-16 — First E2E Test via Shoulder Surf

### Context
Scott recorded the test script via SS app (solo recording with noise breaks
for speaker separation). Enrolled as owner. Manually labeled Alan and Priya.

### Prompt version
Post-stack with second-person patch text convention (PR #35), genericized
examples (PRs #28/#29), connection enforcement (PR #31).

### Model
anthropic/claude-haiku-4.5 (production default)

### Results (from dashboard screenshot)

| # | Type | Text | Owner | Correct? |
|---|---|---|---|---|
| 1 | identity | "You are based out of Austin, Texas" | Scott | ✅ second-person, correct city |
| 2 | preference | "You prefer async communication over meetings" | Scott | ✅ second-person |
| 3 | role | "Scott is the backend lead on the payments project and coordinates the checkout redesign" | Scott | ✅ |
| 4 | project | "Checkout Redesign" | — | ✅ |
| 5 | decision | "The team will use Stripe Connect for the new payment flow" | Scott | ✅ |
| 6 | decision | "For the first milestone, the team will stay in Sandbox only with no production traffic" | Priya | ✅ (could also be takeaway) |
| 7 | commitment | "Priya will get compliance on the calendar this week" | Priya | ✅ |
| 8 | commitment | "Scott will write up the technical RFC by Friday" | Scott | ✅ |
| 9 | commitment | "Scott will start scaffolding the client once the RFC lands" | Scott | ❌ should be Alan |
| 10 | blocker | "The compliant interview for Stripe Connect has not been scheduled yet" | — | ✅ |

### Connections verified
- `role` → `belongs_to → [project] Checkout Redesign` ✅
- `commitment` (scaffolding) → `belongs_to → [project]` + `blocked_by → [commitment] RFC by Friday` ✅
- `decision` (sandbox) → `belongs_to → [project]` ✅

### Issues found
1. **Speaker misattribution**: Alan's scaffolding commitment attributed to Scott.
   Likely cause: solo recording — all speech came from one voice, diarization
   couldn't distinguish. The transcript may have labeled all segments as Scott.
   Not an extraction bug; a diarization/labeling issue.

### Gating checks
- ✅ Self-typed patches present (identity + preference in second person)
- ✅ No "(you)" in any patch text
- ✅ No prompt-echo content
- ✅ Connection enforcement working (no orphan scoped patches)

---

## Template for future entries

```
## YYYY-MM-DD — [Test name]

### Context
[What was being tested and why]

### Prompt version
[Which version of extraction_prompts.py / schema]

### Model
[Which model(s)]

### Results
[Table of patches with expected vs actual]

### Connections verified
[Which connections were checked]

### Issues found
[What went wrong, root cause, fix status]
```
