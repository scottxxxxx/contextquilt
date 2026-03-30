#!/usr/bin/env python3
"""
Edge case transcripts for testing communication profile extraction.
Tests scenarios where adding comm profile to the prompt could cause problems.
"""

# Edge Case 1: Submitting user barely speaks (mostly listening)
# Risk: LLM might hallucinate a profile from other speakers' communication styles
QUIET_USER = """Sprint Review - Mar 25, 2026

Marcus led the sprint review for the payments team. He gave a detailed 20-minute walkthrough of the new checkout flow, explaining every edge case in the retry logic. "So when Stripe returns a 402, we now distinguish between insufficient funds and expired card — the user sees a different message for each," Marcus explained.

Sarah from QA pushed back hard: "The error messages are confusing. 'Payment method declined' doesn't tell the user what to do next. We need actionable copy."

Marcus agreed: "Fair point. I'll revise the copy by Thursday."

DevOps lead Priya flagged a concern about the staging environment: "We're seeing 500ms latency on the payment webhook. That's way above our 200ms SLA. I need to investigate whether it's the new middleware or a Stripe API issue."

Scott asked one question: "Is the retry logic tested against the sandbox?"

Marcus confirmed it was.

The team agreed to ship the checkout flow to staging by Friday. Marcus owns the copy revision. Priya owns the latency investigation."""

# Edge Case 2: Extremely short meeting (barely any content)
# Risk: LLM might over-extract from thin content, or profile scores might be unreliable
SHORT_MEETING = """Quick Sync - Mar 26, 2026

Scott: Hey, just checking — did the deploy go through?
Kim: Yeah, it's live. No issues.
Scott: Cool. And the metrics dashboard?
Kim: Still waiting on the Datadog API key from infra.
Scott: OK I'll ping them. Talk later."""

# Edge Case 3: Highly emotional/heated meeting
# Risk: Emotional content might bias profile scores or cause the LLM to extract
# sentiment as traits/preferences
HEATED_MEETING = """Incident Postmortem - Mar 27, 2026

Scott opened the postmortem visibly frustrated: "This is the third outage in two weeks. We can't keep doing this. The on-call process is broken."

Engineering lead Jake pushed back: "The process isn't broken — we're understaffed. I've been saying for months we need another SRE. Nobody listened."

Product manager Lisa tried to mediate: "Let's focus on what we can control. What's the root cause?"

Jake explained: "The database connection pool maxed out at 100 connections. We hit it during the marketing email blast at 2 PM. The pool size was set in 2023 when we had 10x fewer users."

Scott: "Why wasn't this caught in load testing?"

Jake: "Because our load tests use 50 concurrent users. Real traffic was 2,000. Nobody updated the test config since last year. Honestly, this is a leadership failure — we've been cutting corners on infrastructure for months."

Lisa: "OK, action items. Jake, resize the connection pool to 500 by EOD. Scott, you own updating the load test config to match real traffic patterns. I'll schedule a capacity planning session for next week."

Scott agreed but added: "I also want a pre-deploy checklist that includes connection pool validation. We need to stop being reactive."

The team committed to a 48-hour fix window. Jake will also document the incident in Confluence."""

# Edge Case 4: Multi-language / code-heavy technical meeting
# Risk: Code snippets and technical jargon might confuse the extraction,
# or profile might max out technical_level for everyone
TECHNICAL_DEEP_DIVE = """Architecture Review - Mar 28, 2026

Scott presented the proposed migration from REST to gRPC for the internal service mesh.

"The main bottleneck is serialization overhead. Our p99 latency on the user-profile service is 45ms, and 30ms of that is JSON parsing. With protobuf, we'd cut that to under 5ms. Here's the proto definition I'm proposing:

message UserProfile {
  string user_id = 1;
  repeated Preference preferences = 2;
  CommunicationStyle style = 3;
  google.protobuf.Timestamp last_active = 4;
}

The tricky part is backward compatibility. We need to support both REST and gRPC during the migration window — probably 6 weeks. I'm thinking we use grpc-gateway to auto-generate REST proxies from the proto files."

Senior architect Diana raised concerns: "gRPC is great for internal services but terrible for browser clients. Our admin dashboard calls these endpoints directly. We'd need to either keep a REST layer permanently for the dashboard or migrate the dashboard to use grpc-web."

Scott: "Good point. Let's keep REST for external-facing endpoints and only use gRPC for service-to-service. That simplifies the migration to just 4 internal services."

Diana agreed: "That's more pragmatic. Start with user-profile since it's the highest traffic. If the latency improvement holds, we roll out to the others."

Junior dev Kai asked: "What about observability? Our current Datadog integration relies on HTTP middleware for tracing."

Scott: "We'd switch to OpenTelemetry gRPC interceptors. The instrumentation is actually cleaner — one interceptor handles both tracing and metrics. I'll set up a proof of concept this week."

Decision: Migrate internal service-to-service calls to gRPC, keep REST for external APIs. Start with user-profile service. Scott owns the PoC by Friday."""

# Edge Case 5: Raw diarized transcript with speaker labels (not narrative)
# Risk: Speaker labels like [Speaker 1] might confuse trait attribution,
# or the profile might not be extractable from diarized format
RAW_DIARIZED = """[Scott] OK so where are we with the onboarding flow redesign?

[Speaker 2] We tested three variants last week. Variant B with the progressive disclosure had the best completion rate — 78% versus 61% for the control.

[Scott] That's a big lift. What was different about B?

[Speaker 2] Instead of showing all 12 form fields at once, we split it into 3 steps. Users see their progress. The drop-off point shifted from step 1 to step 3, which means they're getting further before abandoning.

[Scott] Makes sense. Let's go with B. When can we ship it?

[Speaker 3] I need two more days for accessibility testing. The step indicators don't have ARIA labels yet.

[Scott] That's a blocker. Can you prioritize that?

[Speaker 3] Yeah, I'll have it done by Wednesday.

[Speaker 2] One more thing — the mobile conversion is still 20% lower than desktop. The step UI doesn't adapt well to small screens. I'd recommend a dedicated mobile layout.

[Scott] Agreed but let's ship desktop first, then iterate on mobile. Don't want to hold up the 78% win."""

EDGE_CASES = {
    "quiet_user": {
        "transcript": QUIET_USER,
        "user": "Scott",
        "risk": "User barely speaks — LLM might hallucinate profile from other speakers",
        "expected_profile": "null or very low confidence (Scott only asked one question)",
    },
    "short_meeting": {
        "transcript": SHORT_MEETING,
        "user": "Scott",
        "risk": "Too little content — profile scores unreliable, might over-extract patches",
        "expected_profile": "null or low scores (5 lines of dialogue)",
    },
    "heated_meeting": {
        "transcript": HEATED_MEETING,
        "user": "Scott",
        "risk": "Emotional content might leak into traits, frustration ≠ personality",
        "expected_profile": "High directness (frustrated, assertive), should NOT become a trait like 'aggressive'",
    },
    "technical_deep_dive": {
        "transcript": TECHNICAL_DEEP_DIVE,
        "user": "Scott",
        "risk": "Code in transcript might confuse extraction, everyone sounds technical",
        "expected_profile": "High technical_level, high directness, moderate verbosity",
    },
    "raw_diarized": {
        "transcript": RAW_DIARIZED,
        "user": "Scott",
        "risk": "Speaker labels, unnamed speakers, short turns — profile harder to assess",
        "expected_profile": "High directness (Scott is decisive), low verbosity (short responses)",
    },
}
