"""
Test meeting summaries with ground truth for benchmarking extraction models.

Each test case has:
  - summary: A realistic meeting summary (the kind ShoulderSurf generates)
  - expected_facts: Minimum facts the model should extract
  - expected_action_items: Minimum action items the model should extract
  - difficulty: How hard this is to extract from (easy/medium/hard)
"""

TEST_CASES = [
    {
        "id": "standup_simple",
        "name": "Simple Daily Standup",
        "difficulty": "easy",
        "summary": """Daily standup for the Platform team, March 18, 2026.

Attendees: Sarah Chen (Tech Lead), Marcus Williams (Backend), Priya Patel (Frontend).

Sarah reported that the API rate limiter is deployed to staging and passing load tests at 10,000 requests per second. She plans to roll it out to production tomorrow after the morning traffic peak.

Marcus said he finished the PostgreSQL migration script for the user preferences table. He mentioned there are about 2 million rows to migrate and estimates it will take roughly 45 minutes with the batched approach. He needs Sarah to review the rollback procedure before he runs it.

Priya shared that the new dashboard components are ready for QA. She flagged that the date picker has a timezone bug on Safari that she will fix today.""",

        "expected_facts": [
            {"fact": "Sarah Chen is the Tech Lead on the Platform team", "category": "identity"},
            {"fact": "Marcus Williams works on backend for the Platform team", "category": "identity"},
            {"fact": "Priya Patel works on frontend for the Platform team", "category": "identity"},
            {"fact": "API rate limiter is deployed to staging and passing load tests at 10K rps", "category": "experience"},
            {"fact": "PostgreSQL migration for user preferences table has 2 million rows", "category": "experience"},
            {"fact": "Dashboard components are ready for QA", "category": "experience"},
            {"fact": "Date picker has a timezone bug on Safari", "category": "experience"},
        ],
        "expected_action_items": [
            {"action": "Roll out API rate limiter to production after morning traffic peak", "owner": "Sarah"},
            {"action": "Review rollback procedure for PostgreSQL migration", "owner": "Sarah"},
            {"action": "Fix Safari timezone bug in date picker", "owner": "Priya"},
        ],
    },
    {
        "id": "stakeholder_complex",
        "name": "Complex Multi-Stakeholder Planning",
        "difficulty": "hard",
        "summary": """Quarterly planning session for Widget 2.0, March 15, 2026.

Attendees: Bob Martinez (VP Product), Lisa Park (Engineering Director), Tom Reeves (Design Lead), Angela Foster (Sales Director).

Bob opened by stating that Widget 2.0 needs to ship by end of Q2, which is non-negotiable because three enterprise customers — Acme Corp, TechFlow, and Meridian Systems — have it written into their renewal contracts. He emphasized that losing even one of these accounts would represent over $2M in ARR.

Lisa pushed back on the timeline, saying the current architecture cannot support the real-time collaboration feature that Acme specifically requested. She estimated it would take an additional 6 weeks to build the WebSocket infrastructure properly. She proposed cutting the offline mode feature to make room.

Tom disagreed with cutting offline mode, explaining that their user research shows 40% of target users work in areas with unreliable internet. He suggested shipping real-time collaboration as a beta feature in Q2 and making it GA in Q3.

Angela sided with Bob on the timeline but agreed with Tom about offline mode. She revealed that Meridian's CTO specifically mentioned offline support as a deciding factor during their last call. She offered to negotiate with Acme about accepting a beta version of real-time collaboration.

The group agreed on a compromise: ship Widget 2.0 in Q2 with offline mode and real-time collaboration in beta. Angela will call Acme's CTO this week to confirm they accept the beta approach. Bob said he needs someone to lead the Widget 2.0 project full-time and asked Lisa to identify a candidate by next Monday.""",

        "expected_facts": [
            {"fact": "Bob Martinez is VP Product", "category": "identity"},
            {"fact": "Lisa Park is Engineering Director", "category": "identity"},
            {"fact": "Tom Reeves is Design Lead", "category": "identity"},
            {"fact": "Angela Foster is Sales Director", "category": "identity"},
            {"fact": "Widget 2.0 must ship by end of Q2 2026", "category": "experience"},
            {"fact": "Three enterprise customers (Acme Corp, TechFlow, Meridian Systems) have Widget 2.0 in renewal contracts", "category": "experience"},
            {"fact": "Losing these accounts represents over $2M in ARR", "category": "experience"},
            {"fact": "Current architecture cannot support real-time collaboration", "category": "experience"},
            {"fact": "WebSocket infrastructure would take an additional 6 weeks", "category": "experience"},
            {"fact": "40% of target users work in areas with unreliable internet", "category": "experience"},
            {"fact": "Meridian's CTO mentioned offline support as a deciding factor", "category": "experience"},
            {"fact": "Team agreed to ship with offline mode and real-time collaboration in beta", "category": "experience"},
        ],
        "expected_action_items": [
            {"action": "Call Acme's CTO to confirm they accept beta real-time collaboration", "owner": "Angela"},
            {"action": "Identify a candidate to lead Widget 2.0 full-time", "owner": "Lisa", "deadline": "next Monday"},
        ],
    },
    {
        "id": "action_heavy",
        "name": "Action-Item Heavy Sprint Review",
        "difficulty": "medium",
        "summary": """Sprint 14 review and planning for the Checkout Redesign project, March 19, 2026.

Attendees: Jamie O'Brien (PM), Dev Kapoor (Senior Engineer), Rachel Kim (QA Lead).

Sprint 14 results: 18 of 22 story points completed. The payment form refactor is done and reduced checkout abandonment by 12% in A/B testing. The address auto-complete integration with Google Maps is 80% complete — Dev said the Canadian postal code edge cases are taking longer than expected.

Jamie reviewed the customer feedback from the beta. Key findings: users love the one-click reorder button but are confused by the new shipping options layout. Three users specifically complained that they couldn't find the gift wrap option.

For Sprint 15, the team committed to:
- Dev will finish the address auto-complete by Wednesday, including Canadian postal codes
- Rachel will write regression tests for the payment form by Thursday
- Jamie will redesign the shipping options UI based on the feedback and have mockups ready for review by Friday
- Dev will start the Apple Pay integration on Thursday after the address work is done
- Rachel needs access to the staging payment sandbox — Jamie will submit the access request today
- The team will do a joint demo for stakeholders next Tuesday at 2pm
- Jamie will send the updated sprint metrics to the VP of Product by end of day Friday""",

        "expected_facts": [
            {"fact": "Jamie O'Brien is the PM on the Checkout Redesign project", "category": "identity"},
            {"fact": "Dev Kapoor is a Senior Engineer", "category": "identity"},
            {"fact": "Rachel Kim is the QA Lead", "category": "identity"},
            {"fact": "Sprint 14 completed 18 of 22 story points", "category": "experience"},
            {"fact": "Payment form refactor reduced checkout abandonment by 12% in A/B testing", "category": "experience"},
            {"fact": "Address auto-complete integration is 80% complete", "category": "experience"},
            {"fact": "Canadian postal code edge cases are causing delays", "category": "experience"},
            {"fact": "Users love the one-click reorder button", "category": "experience"},
            {"fact": "Users are confused by the new shipping options layout", "category": "experience"},
            {"fact": "Three users couldn't find the gift wrap option", "category": "experience"},
        ],
        "expected_action_items": [
            {"action": "Finish address auto-complete including Canadian postal codes", "owner": "Dev", "deadline": "Wednesday"},
            {"action": "Write regression tests for payment form", "owner": "Rachel", "deadline": "Thursday"},
            {"action": "Redesign shipping options UI and have mockups ready", "owner": "Jamie", "deadline": "Friday"},
            {"action": "Start Apple Pay integration", "owner": "Dev", "deadline": "Thursday"},
            {"action": "Submit access request for staging payment sandbox for Rachel", "owner": "Jamie", "deadline": "today"},
            {"action": "Joint stakeholder demo", "owner": "team", "deadline": "next Tuesday 2pm"},
            {"action": "Send updated sprint metrics to VP of Product", "owner": "Jamie", "deadline": "end of day Friday"},
        ],
    },
    {
        "id": "subtle_implied",
        "name": "Subtle and Implied Facts",
        "difficulty": "hard",
        "summary": """Architecture review for the data pipeline migration, March 17, 2026.

Attendees: Yuki Tanaka, Chris Evans, Maria Santos.

Yuki presented the proposal to migrate from Apache Kafka to Amazon Kinesis. She acknowledged this is controversial since the team has invested heavily in Kafka expertise over the past three years. However, the AWS bill for running self-managed Kafka clusters has grown to $14,000 per month, and the team spends roughly 20% of their time on Kafka operations instead of feature work.

Chris was visibly skeptical. He pointed out that the last major migration — from MongoDB to PostgreSQL two years ago — took twice as long as estimated and caused a week-long production incident. He said he would support the migration only if they could run both systems in parallel for at least a month.

Maria, who joined the team six months ago from a company that used Kinesis extensively, offered to lead the proof of concept. She noted that Kinesis has improved significantly since 2024 and now supports exactly-once delivery, which was the team's main concern last time they evaluated it.

The team decided to proceed with a two-week proof of concept. Yuki asked Chris to document the top 10 Kafka configurations they rely on so Maria can verify Kinesis equivalents. Chris reluctantly agreed but said he wants weekly check-ins during the migration to catch issues early.""",

        "expected_facts": [
            {"fact": "Yuki Tanaka proposed migrating from Kafka to Kinesis", "category": "experience"},
            {"fact": "Team has three years of Kafka expertise", "category": "experience"},
            {"fact": "Self-managed Kafka clusters cost $14,000 per month on AWS", "category": "experience"},
            {"fact": "Team spends roughly 20% of time on Kafka operations", "category": "experience"},
            {"fact": "Previous MongoDB to PostgreSQL migration took twice as long as estimated", "category": "experience"},
            {"fact": "Previous migration caused a week-long production incident", "category": "experience"},
            {"fact": "Maria Santos joined the team six months ago", "category": "identity"},
            {"fact": "Maria has prior experience with Kinesis from her previous company", "category": "identity"},
            {"fact": "Chris Evans is skeptical about the migration", "category": "trait"},
            {"fact": "Team decided to do a two-week proof of concept", "category": "experience"},
        ],
        "expected_action_items": [
            {"action": "Lead the Kinesis proof of concept", "owner": "Maria"},
            {"action": "Document top 10 Kafka configurations the team relies on", "owner": "Chris"},
            {"action": "Weekly check-ins during the migration", "owner": "team"},
        ],
    },
    {
        "id": "handoff_context",
        "name": "Project Handoff with Cross-Meeting Context",
        "difficulty": "medium",
        "summary": """Handoff meeting for the Mobile App v3.0 project, March 20, 2026.

Attendees: Nathan Park (outgoing lead), Sofia Rodriguez (incoming lead), James Wu (CTO).

James explained that Nathan is moving to the Platform Infrastructure team next week, and Sofia will take over as Mobile App lead. He stressed that continuity is critical because the v3.0 launch is scheduled for April 15.

Nathan walked Sofia through the current state:
- The iOS build is feature-complete and in TestFlight beta with 200 external testers
- The Android build is about two weeks behind iOS due to a memory leak in the camera module that took longer to fix
- The biggest open risk is the push notification service migration from Firebase to their in-house system, which is about 60% complete
- He recommended keeping Wei Zhang on the notification work since she has the most context

Sofia asked about the vendor relationship with PixelCraft, the design agency doing the app icons and marketing materials. Nathan said the contract expires March 31 and needs to be renewed if they want PixelCraft to do the launch assets. He has been happy with their work and recommends renewing.

James mentioned that the board wants to see the mobile DAU numbers improve by 25% within 60 days of the v3.0 launch, and he has committed to that target. He asked Sofia to put together a launch metrics dashboard by end of next week.""",

        "expected_facts": [
            {"fact": "Nathan Park is moving to Platform Infrastructure team next week", "category": "experience"},
            {"fact": "Sofia Rodriguez is the incoming Mobile App lead", "category": "identity"},
            {"fact": "James Wu is the CTO", "category": "identity"},
            {"fact": "Mobile App v3.0 launch is scheduled for April 15", "category": "experience"},
            {"fact": "iOS build is feature-complete and in TestFlight beta with 200 external testers", "category": "experience"},
            {"fact": "Android build is about two weeks behind iOS", "category": "experience"},
            {"fact": "Memory leak in camera module caused the Android delay", "category": "experience"},
            {"fact": "Push notification migration from Firebase to in-house is 60% complete", "category": "experience"},
            {"fact": "Wei Zhang has the most context on the notification work", "category": "identity"},
            {"fact": "PixelCraft design agency contract expires March 31", "category": "experience"},
            {"fact": "Nathan recommends renewing the PixelCraft contract", "category": "preference"},
            {"fact": "Board wants 25% DAU improvement within 60 days of v3.0 launch", "category": "experience"},
        ],
        "expected_action_items": [
            {"action": "Renew PixelCraft contract before March 31", "owner": "Sofia"},
            {"action": "Put together launch metrics dashboard", "owner": "Sofia", "deadline": "end of next week"},
            {"action": "Keep Wei Zhang on push notification migration work", "owner": "Sofia"},
        ],
    },
]
