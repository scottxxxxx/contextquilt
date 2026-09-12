# Duplicate undertakings: the 18 pairs to mark

**This is the gate for doc 23.** Nothing is built and nothing ships until
these are marked. Run of 2026-09-10, Sonnet, 51 judge calls over the last
30 days, zero failures.

Self-agreement was measured because a suggestion that comes and goes
between loads cannot be attached to a tap that closes a real commitment:
run 1 flagged 19, run 2 flagged 19, **18 of 20 distinct pairs appeared in
both (90%)**. Only the stable 18 are below. The earlier, broken harness
scored 42% on the same measure; that instability was the instrument.

Recall on the known set: both known positives found AND stable. The
adversarial control (`a6f4792c`, "Steven's mother will send over the
psychological evaluation forms", which shares heavy vocabulary with the
Steven pair and is a different undertaking by a different person) was
NOT flagged.

## How to mark

Put an `x` in one box per pair. The question is **"is this one piece of
work described twice"**, not "are these related".

Doc 23's rule for the hard case: **one being a step toward the other,
each with its own outcome, is NOT the same undertaking.** Several of
these are exactly that shape and they are the ones that decide the
result.

If most are wrong, doc 23's falsification section applies: the
affordance trains people to ignore it and the sixth per-meeting LLM call
buys nothing. **Marking these all `no` is a real and useful outcome.**

## Scott's rulings, given verbally on 2026-09-10

**The per-row boxes below are still unmarked.** These are what he said
while reading the sheet, recorded here because otherwise they live only
in a conversation. Anything not covered stays open.

**Merge policy depends on WHOSE item it is, and that is about tolerance
rather than truth.** For other people's items he would err toward
merging even when arguably separate, PROVIDED the merge combines the
descriptions into something more verbose rather than keeping one and
discarding the other. For his own items he prefers to stay granular. In
his words that "might just be a personal preference so we could change
this in the future". The reason it works: on somebody else's item he
needs the gist, on his own he has to act on it, so an over-merge costs
him a real task.

**"Everything that didn't mention Scott I thought we could consider them
the same undertaking."** That is 11 of the 18.

**The underlying discriminator is not owner.** Asked what separates the
Portuguese pair from the Cigna cluster, with both rows in front of him:
Portuguese is **one deliverable** and the Cigna items are **separate
work**. Portuguese names the same deliverable twice; the Cigna rows name
P1 against P2 and P3, different use cases, different documents. Sharing
a customer and a spreadsheet is not sharing an undertaking.

**Pair 10** (`fb83be81` / `7b1d6919`): unsure, left open.

**Pairs 6 and 7:** rather than merging the Aug 14 row with the Aug 17
rows, he would consider combining the two Aug 17 scenario rows
(`3ddab0d8` and `ce9ceed2`) into one, IF their descriptions could be
combined in a way that makes sense. Those two are from the SAME MEETING
(`EED21245`), which doc 23's design structurally cannot propose, since
it judges a new commitment only against open items from OTHER meetings.
**The one merge he might accept is the one the current design can never
offer.** Unresolved against his separate statement that the Cigna items
are separate work; recorded as a tension rather than settled.

**Not yet folded into doc 23.** The amendment was drafted and never
applied.

---

### 1. pallavi / 10437AFE

- **NEW** `417ff0ff` (2026-08-17, due before Monday)
  > Pallavi to continue communication with Jaffer to get clarifications needed for CTS asset management feature implementation.
- **OLD** `3d2d1601` (2026-06-29)
  > Reach out to Jaffar to ask clarifying questions on CTS endpoint email and speed up response

Judge's reason: *Both involve reaching out to Jaffar/Jaffer with clarifying questions related to CTS, restating the same outreach task.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 2. vijay / 10437AFE

- **NEW** `46fc867d` (2026-08-17, due 8 PM today)
  > Vijay to send JWT token generation code and coordinate with Srikanth and Sukumar on ServiceNow token integration changes
- **OLD** `74535bb4` (2026-08-15, due tomorrow or later today)
  > Coordinate with Pallavi to get JWT token from Jaffer for token validation testing in ServiceNow

Judge's reason: *Both involve Vijay coordinating JWT token work with Sukumar in the context of ServiceNow integration.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 3. sukumar gurugubelli / 5A5D8433

- **NEW** `38d1589b` (2026-08-15, due Monday)
  > Complete phase one of model endpoint development and have endpoint ready by Monday
- **OLD** `ad7ca2e5` (2026-08-15, due Monday)
  > Integrate model endpoint with core system

Judge's reason: *Both refer to completing the model endpoint development phase one, ready by Monday.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 4. sukumar gurugubelli / 5A5D8433

- **NEW** `7ea27b3d` (2026-08-17, due end of week / Monday)
  > Sukumar to deliver model endpoint development phase one by end of week, with endpoint ready by Monday.
- **OLD** `ad7ca2e5` (2026-08-15, due Monday)
  > Integrate model endpoint with core system

Judge's reason: *Sukumar delivering model endpoint phase one by Monday is the same undertaking restated with an assignee.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 5. sukumar / 10437AFE

- **NEW** `f58fd96a` (2026-08-15, due Monday)
  > Schedule HR intake discussion call for Monday around 4 PM to walk through requirements and determine core vs. standalone approach
- **OLD** `340305a4` (2026-08-15, due Monday)
  > Discuss HR intake end state and interim plan on Monday after CTS items are completed.

Judge's reason: *Both refer to a Monday discussion about the HR intake approach and plan.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 6. scott / B77FF5E3

- **NEW** `3ddab0d8` (2026-08-17)
  > Create test scenarios and demo scripts for P2 and P3 Cigna use cases (RX Management, Pharmacy Retail Location) to help Karthik scope work
- **OLD** `9ab83c51` (2026-08-14, due Monday)
  > Build a new spreadsheet with Cigna demo test scenarios (priority 1 and 2 intents) paired with sample test data, with explanations at appropriate detail level for dev team to build from.

Judge's reason: *Both involve building Cigna demo test scenarios, with the new item extending scope to P2/P3 use cases beyond the existing P1/P2 focus.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 7. scott / B77FF5E3

- **NEW** `ce9ceed2` (2026-08-17)
  > Provide script for P1 Cigna scenarios (Eligibility, Pricing, Order, Status) in the Cigna scenarios document
- **OLD** `9ab83c51` (2026-08-14, due Monday)
  > Build a new spreadsheet with Cigna demo test scenarios (priority 1 and 2 intents) paired with sample test data, with explanations at appropriate detail level for dev team to build from.

Judge's reason: *Providing P1 Cigna scenario scripts is part of the same spreadsheet/document of Cigna demo test scenarios already committed to.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 8. srikanth / 10437AFE

- **NEW** `85179c77` (2026-08-17)
  > Refactor attachment download button to pass sufficient context (message ID and source file ID) for proper file retrieval.
- **OLD** `7bb2fffb` (2026-07-16)
  > Fix attachment download issue in production

Judge's reason: *Both address fixing the attachment download issue, with new item specifying the context refactoring needed.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 9. srikanth / 10437AFE

- **NEW** `825a0d3d` (2026-08-17)
  > Update attachment download implementation to use message ID and source file ID in correct order (message ID / source file ID) instead of file ID.
- **OLD** `7bb2fffb` (2026-07-16)
  > Fix attachment download issue in production

Judge's reason: *Both address the same attachment download fix, with new item detailing the correct parameter order to use.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 10. scott / 10437AFE

- **NEW** `fb83be81` (2026-08-17, due tomorrow)
  > Provide timeline feedback on the history display issue (showing data from multiple environments) in tomorrow's feature review call.
- **OLD** `7b1d6919` (2026-08-15, due next week)
  > Continue following up with product team on history/environment isolation feature request scheduling; expect update by next week at earliest

Judge's reason: *Both concern following up on the history/environment display issue feature request, with the new item specifying feedback at tomorrow's feature review call.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 11. scott / B77FF5E3

- **NEW** `b585a844` (2026-08-17, due this week)
  > Draft and refine sample demo scenarios in a Google Doc, including hard scenarios that will impress CIGNA, with focus on scenarios like refill-too-soon, vacation fill, address change, proxy calls, and complex multi-intent handling
- **OLD** `3ddab0d8` (2026-08-17)
  > Create test scenarios and demo scripts for P2 and P3 Cigna use cases (RX Management, Pharmacy Retail Location) to help Karthik scope work

Judge's reason: *Both involve creating demo/test scenarios for Cigna use cases including complex pharmacy scenarios like refill-too-soon and vacation fill.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 12. scott / B77FF5E3

- **NEW** `b585a844` (2026-08-17, due this week)
  > Draft and refine sample demo scenarios in a Google Doc, including hard scenarios that will impress CIGNA, with focus on scenarios like refill-too-soon, vacation fill, address change, proxy calls, and complex multi-intent handling
- **OLD** `ce9ceed2` (2026-08-17)
  > Provide script for P1 Cigna scenarios (Eligibility, Pricing, Order, Status) in the Cigna scenarios document

Judge's reason: *Both involve drafting scenario scripts in a Cigna scenarios document covering P1 intents.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 13. suresh / 10437AFE

- **NEW** `dd4ca408` (2026-08-17)
  > Share step-by-step latency details with Sukumar for review and optimization
- **OLD** `0ecea7d8` (2026-08-15)
  > Review response latency details shared by Joy on QA environment and identify which of the 6-7 items taking 10-30+ seconds can be optimized

Judge's reason: *Both involve sharing/reviewing latency details with Sukumar for optimization.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 14. joy / 10437AFE

- **NEW** `95a1852c` (2026-08-21, due today)
  > Validate CTS ticket creation feature changes in QA after regression testing confirms fix
- **OLD** `de7cafa1` (2026-08-15)
  > Continue validation testing on CTS IT Service Desk in QA and begin testing asset management POC once invited

Judge's reason: *Both involve validating CTS features in QA environment, with the new item being a continuation/narrowing of the existing ongoing validation work.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 15. scott / B77FF5E3

- **NEW** `83db4300` (2026-08-26, due today)
  > Test all scenarios in the Excel sheet and provide feedback on conversational experience and bot behavior.
- **OLD** `551d6866` (2026-08-17, due today)
  > Test Happy Path feature and share feedback on conversational intelligence, language, and behavior

Judge's reason: *Both involve testing bot scenarios and providing feedback on conversational behavior.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 16. suresh / 10437AFE

- **NEW** `ad6abe6f` (2026-08-31, due after this meeting)
  > Suresh will call Sukumar to discuss environment strategy and dependency resolution approach, including whether multiple agents or alternative solutions are needed.
- **OLD** `30ac8df5` (2026-07-23, due Monday)
  > Schedule and conduct 15-minute dedicated discussion on Monday to debate cross-environment issue solution and finalize environment strategy before proceeding.

Judge's reason: *Both involve scheduling a call with Sukumar to debate and finalize environment strategy and dependency resolution.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 17. scott / 10FF20F9

- **NEW** `84b99a40` (2026-09-01, due end of weekend)
  > Complete Portuguese language support implementation and Apple archive submission
- **OLD** `db45b39d` (2026-08-28, due end of weekend)
  > Deliver Portuguese language support by end of weekend.

Judge's reason: *Both refer to the same Portuguese language support deliverable, with the new item adding archive submission as part of completing the same work.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

### 18. steven williams / 10FF20F9

- **NEW** `23721690` (2026-09-08, due this weekend)
  > Steven will visit his mother this weekend to test ShoulderSurf in a real interview call
- **OLD** `b133913f` (2026-08-28)
  > Steven will travel to his mother's location in the next 2 weeks to conduct interviews and gather data for app testing.

Judge's reason: *Both describe Steven visiting his mother to conduct real interview testing, now with a specific weekend timeframe.*

**Same undertaking?**  `[ ] yes`   `[ ] no`   `[ ] unsure`

Notes:

---

## Tally (fill in after marking)

- yes: ___ / 18
- no: ___ / 18
- unsure: ___ / 18

**Precision on the stable set = yes / (yes + no).**

Doc 23 open question 1 said "ship it, gated on question 3". This is
question 3. A high number means build it; a low number means the
falsification case fired and the feature does not get built.
