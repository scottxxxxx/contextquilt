# Test 1 — Post-Meeting Message Draft

**Scenario:** Scott just finished a meeting about ServiceNow integration blockers on the Atlas App MVP. Chandran (the MVP owner) was not on the call. Scott wants to brief him before bed.

**Query class:** Outgoing-message drafting from a meeting transcript.

**Primary types exercised:** trait, person, project, blocker, decision.

---

## Shared user message (both variants)

```
I just finished this meeting. Transcript below.

=== TRANSCRIPT ===
[full meeting transcript — see harborview_servicenow_transcript.txt]
=== END TRANSCRIPT ===

Draft a short message I can send to Chandran before I go to bed
tonight. He was NOT on today's call but he owns the Atlas App MVP.

Tell him:
 1) What actually happened in the meeting.
 2) How this changes what's already on his plate.
 3) The ONE decision I need from him before the 10 AM Eastern demo
    tomorrow.

Keep it under 150 words. Make it sound like me, not a bot.
```

---

## Variant A — no memory

**System:**
```
You are a concise assistant helping Scott turn a meeting he just had
into an actionable next step. Be specific, name names, and don't hedge.
```

**Input tokens:** 1,202
**Output tokens:** 197

### Raw outputs (3 runs)

**A-1:**
> Hey Chandran— Quick heads up on today's meeting. The team hit a blocker with ServiceNow integration... I need you to decide by morning whether we demo the full chatbot flow tomorrow (and risk showing broken integrations) or pivot to just showing the UI/logic without live ServiceNow calls.

**A-2:**
> Subject: ServiceNow Integration Blocker - Need Your Input
>
> Chandran, Quick heads up on today's meeting. We're blocked on the ServiceNow integration—Nandini's hitting permission errors when trying to create tickets via API, even with valid credentials... Can we demo the chatbot core functionality tomorrow at 10 AM ET without the full ServiceNow integration live?

**A-3:**
> Hey Chandran, Quick heads up on today's sync: we're blocked on ServiceNow API integration—Nandini's hitting authorization errors on ticket creation and get requests... by tomorrow morning, confirm whether the Atlas App MVP demo should proceed independently or wait for ServiceNow clarity.

---

## Variant B — with query-scoped memory

**System:**
```
You are a concise assistant helping Scott turn a meeting he just had
into an actionable next step. Be specific, name names, and don't hedge.

Use the MEMORY CONTEXT below to ground the message in Scott's
ongoing work.

=== MEMORY (relevant to this question) ===

About Scott:
- Tends to over-explain and be repetitive. Prefers tight, direct writing.

About Chandran:
- Backend lead; owner of Atlas App MVP production-readiness, story
  creation, Agent Bot integration, and omni-channel context retention.
- Already on the hook for: continuous MVP testing, scheduling the MVP
  demo, defining MVP evaluation ground rules.

About the Atlas App MVP and Harborview Mutual:
- MVP completion is the single gate holding back the rest of the
  Harborview Mutual backlog.
- Harborview Mutual's new platform must be live by July to support the
  October rollout to 7M new members.
- V2 demos are scoped to voice/limited-feature only.

Existing related blocker:
- Voice models are unavailable across Code AI platform environments
  (dev/stage/test/Korea); Chandran is aware.

=== END MEMORY ===
```

**User message:** Same as A.

**Input tokens:** 1,958 (+756 vs A)
**Output tokens:** 193
**Cost delta:** ~$0.0006 on Haiku

### Raw outputs (3 runs)

**B-1:**
> Chandran, Just wrapped a meeting on the MVP integration work. Quick status: ServiceNow API auth is broken — Nandini's getting "not authorized" errors even with valid credentials... **This stacks on top of the voice model unavailability we already knew about. Both are now blocking the demo.** Here's what I need from you tonight: Can the V2 demo still move forward tomorrow with voice/voice features temporarily removed, using only the non-ServiceNow flows? Or do we need to push the demo until ServiceNow auth is fixed? **The July deadline is real.** Let me know which way we're going so I can set expectations with the business before 10 AM.

**B-2:**
> Chandran— ServiceNow auth is broken. Nandini can't create tickets or pull data; permissions issue tied to API configuration... **This stacks on top of the voice model unavailability we already flagged. Two integration blockers, same timeline.** Should we demo voice-only tomorrow and explicitly scope out ServiceNow integration as post-MVP, or do we need to push the demo?

**B-3:**
> Chandran— ServiceNow integration hit a permissions wall today... **Voice models are already offline across environments—this is the second blocker stacking up. Know your constraints before the call.**

---

## Scorecard

| Dimension | A (no memory) | B (with memory) |
|---|---|---|
| Names Chandran naturally | 3/3 (from query) | 3/3 |
| Frames Chandran as established MVP owner | 0/3 | 3/3 |
| Connects new blocker to existing voice-models blocker | **0/3** | **3/3** |
| References July/7M rollout stakes | 0/3 | 1/3 |
| Decision framing specificity | Generic ("demo or pivot") | Specific ("voice-only vs push") |
| Tone tightness | Chattier ("Hey Chandran—") | Tighter ("Chandran—") |

**Takeaway:** Three patches carried the lift — trait (tone), voice-models blocker (pattern link), V2-voice-only decision (option framing). Two patches underperformed — July deadline (used 1/3), Chandran's existing commitments (scaffolding only).

**Utilization:** ~3 of ~8 supplied patches actively shaped output. Improved over the earlier category-dumped version which used 3 of ~25.
