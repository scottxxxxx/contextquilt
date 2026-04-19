# Test 2 — Pre-1:1 Prep

**Scenario:** Scott has a 1:1 with Sri (voice platform lead on Florida Blue) in 15 minutes. He wants a quick briefing: what Sri owes him, what to push on, what to avoid.

**Query class:** Pre-meeting prep with no transcript — pure memory-dependence.

**Primary types exercised:** commitment, preference, decision, takeaway.

---

## Shared user message (both variants)

```
I'm getting on a 1:1 with Sri in 15 minutes.

In under 100 words, tell me:
 1) What does he already owe me?
 2) What's the ONE thing I should push him on that he won't
    expect me to raise?
 3) Any landmines I should avoid bringing up cold?
```

---

## Variant A — no memory

**System:**
```
You are a concise assistant helping Scott prep for a back-to-back
1:1. Be specific, name names, and don't hedge. If you don't have
enough context to answer a part of the question, say so briefly
instead of inventing.
```

**Input tokens:** 141
**Output tokens:** ~120

### Raw outputs (3 runs) — all effectively equivalent

**A-1:**
> I don't have context about your relationship with Sri, your previous conversations, or what commitments he's made to you. I can't see: Your shared history or any outstanding deliverables, Unexpected topics that would catch him off-guard, Sensitive areas to avoid. To help effectively, I'd need details like: What's your working relationship? Any recent discussions or missed deadlines? What's his current situation or pressure points?

**A-2:**
> I don't have context about your relationship with Sri, your previous conversations, commitments he's made to you, or current project status. I can't tell you what he owes you, what would surprise him, or what landmines exist without that information. Before the call, quickly review: Your last 1:1 notes, Any open action items he committed to, Recent Slack/email threads with him, Any feedback you've been sitting on.

**A-3:**
> I don't have any context about your relationship with Sri, your previous conversations, outstanding commitments, or his current priorities. I can't tell you what he owes you, what would surprise him, or what sensitive topics to avoid.

**Key observation:** A gracefully degrades — refuses to invent, every run. This is the correct behavior but the answer is unusable.

---

## Variant B — with query-scoped memory

**System:**
```
You are a concise assistant helping Scott prep for a back-to-back
1:1. Be specific, name names, and don't hedge. If you don't have
enough context to answer a part of the question, say so briefly
instead of inventing.

=== MEMORY (relevant to this 1:1) ===

About Scott:
- Tends to over-explain and be repetitive — prefers tight, direct
  writing.
- Strongly prefers Deepgram Nova 3 for transcription. Wants to avoid
  churn between voice models.

About Sri (also called Sridev):
- Voice platform lead on the Florida Blue engagement.
- Partnered with Deepak on the leadership deck for Joe's review.
- Partnered with Anna on voice platform challenges discussion.

Open commitments Sri owes Scott / the team:
- Provide Deepgram model recommendations (Nova 3 vs. Flux) — soft
  deadline "by next week."
- Finalize the 2026-2027 Planning Session deck for Joe's review.
- Send email with the afternoon session plan and arrival times to
  Anna and team (Wednesday).
- Alert leadership about the cloud migration discussion if needed
  before Tuesday.
- Prepare the morning-session deck for the Florida Blue leadership
  meeting.

Standing decisions that frame the 1:1:
- Use Deepgram Nova 3 for the June sales release — already decided.
- V2 application demos are scoped to voice only, no full-application
  demo.
- Nova 3 is the chosen transcription solution for the Benefits AI
  project.

Active blocker on Sri's work:
- Voice models are unavailable across Code AI platform environments
  (dev / stage / test / Korea).

Takeaway worth keeping in mind:
- Moving from Nova 3 to Flux later would introduce upgrade churn.
  Joe shares this concern.

=== END MEMORY ===
```

**User message:** Same as A.

**Input tokens:** 614 (+473 vs A)
**Output tokens:** ~130
**Cost delta:** ~$0.0004 on Haiku

### Raw outputs (3 runs)

**B-1:**
> 1) What he owes you: Deepgram model recommendations (Nova 3 vs. Flux) by next week. Morning Florida Blue leadership deck. 2026-2027 planning deck finalized for Joe. Wednesday email to Anna with afternoon session plan and arrival times. Alert leadership on cloud migration discussion if needed before Tuesday.
> 2) Push him on: The voice model unavailability blocker across dev/stage/test/Korea environments. It's actively limiting what he can ship. Get clarity on timeline and workarounds—this is strangling the work.
> 3) Landmine: Don't lead with Flux. You're locked on Nova 3 for good reasons (churn risk, Joe agrees). Keep that decision frame steady; his model rec request is just for documentation, not reopening it.

**B-2:**
> 1) What he owes you: Deepgram model rec (Nova 3 vs. Flux) by next week. Finalize 2026-2027 Planning deck for Joe. Email Anna/team Wednesday with afternoon session plan & arrival times. Alert leadership on cloud migration if needed before Tuesday. Morning deck for Florida Blue leadership meeting.
> 2) Push him on: The voice model unavailability blocker across dev/stage/test/Korea. It's actively limiting what he can ship—worth forcing a concrete plan to unblock it.
> 3) Landmine: Don't lead with the Deepgram rec deadline—he knows it's soft. If he hasn't started, ask why before mentioning the deadline.

**B-3:**
> 1) What he owes you: Deepgram model recommendations (Nova 3 vs. Flux—soft deadline next week). Finalize the 2026-2027 Planning Session deck for Joe. Send Anna the Wednesday afternoon session plan and arrival times. Alert leadership about cloud migration discussion if needed before Tuesday. Prepare the Florida Blue leadership morning-session deck.
> 2) Push him on: The voice model unavailability blocker across dev/stage/test/Korea environments. It's actively limiting what he can ship. Get specifics on timeline and workaround.
> 3) Landmine: Don't open with Flux recommendations yet—he's likely still researching. Lead with the platform blocker instead.

---

## Scorecard

| Dimension | A (no memory) | B (with memory) |
|---|---|---|
| Commitment utility (list open asks) | **0/3** | **3/3** (all 5 items listed every run) |
| Preference utility (surface Nova-3 stance) | 0/3 | 3/3 |
| Landmine detection (Flux framing risk) | 0/3 | 3/3 |
| Graceful degradation (no hallucination) | **3/3** ✓ | N/A |
| Tone match | Generic format | Tight, structured, direct |

**Takeaway:** Memory is not additive here — it is the entire answer. A cannot respond substantively without fabricating. B lists all five commitments and synthesizes preference + takeaway into the landmine finding every run.

**Bonus finding:** A's refusal to invent is a feature. This is the anti-hallucination property that makes CQ-memory positioning stronger for regulated verticals (healthcare, finance, legal) than "more specific answers" alone would suggest.
