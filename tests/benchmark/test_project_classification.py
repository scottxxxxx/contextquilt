#!/usr/bin/env python3
"""
A/B test: Tightened project classification rules in extraction prompt.
Tests that work projects are still extracted but discussion topics are not.

Usage:
    OPENROUTER_API_KEY=sk-... python tests/benchmark/test_project_classification.py
"""

import asyncio
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "contextquilt", "services"))

from llm_client import LLMClient
from extraction_prompts import MEETING_SUMMARY_SYSTEM

# --- VARIANT PROMPT: Tighten project classification ---

# Replace the project row in the patch types table
VARIANT_PROMPT = MEETING_SUMMARY_SYSTEM.replace(
    "| project    | An active initiative, usually with a deadline                  | IS the container     |",
    "| project    | A work initiative the submitting user or their team OWNS and is actively building/delivering. Must show ownership signals: \"we're building\", \"our project\", \"I'm working on\", assigned tasks, team members, deadlines. NOT topics merely discussed or referenced. | IS the container     |",
)

# Replace the TYPE ACCURACY project rule
VARIANT_PROMPT = VARIANT_PROMPT.replace(
    "- A project is any topic or initiative the user is tracking across sessions. Do NOT editorialize what counts as a project — the user decides that by recording a session about it.",
    """- A project is a WORK INITIATIVE the submitting user or their team owns. It requires at least one ownership signal:
  - Active work language: "we're building", "our project", "I'm working on", "the team is delivering"
  - Assigned tasks/people: someone owns a commitment or blocker within it
  - Team involvement: multiple participants are contributing to it
  - A topic merely DISCUSSED or REFERENCED is NOT a project. Podcast subjects, news stories, case studies, customer anecdotes, and external events are NEVER projects — they are takeaways at most.
  - If unsure whether something is a project or a discussion topic, do NOT create a project patch.""",
)

# --- TEST TRANSCRIPTS ---

# Case 1: Real work meeting (should extract projects)
WORK_MEETING = """Florida Blue Meeting - Mar 24, 2026

Scott and the Florida Blue team discussed three main topics:

1. Sales Call Summarization: Florida Blue needs transcription and summarization of approximately 1,500 sales call audio files. Scott will use Deepgram Nova 3 for transcription. He'll provide both transcription and summary outputs in an Excel format. Scott asked for 2 days after receiving the audio files. Travis will upload the files via FTP.

2. Multi-Language Support (Language Line): Florida Blue has scenarios where a caller speaks Spanish. Nova 3 supports auto-detect. Sridev will send an email to Scott, Anand, and Amanda about the language line requirements.

3. Open Support Tickets: The team reviewed several tickets. Amanda handles escalation through customer success.

The production date is June 28, 2026. Scott will reach out to Anand regarding Nova 3."""

# Case 2: Podcast / content discussion (should NOT extract projects)
PODCAST_DISCUSSION = """True Crime Podcast Review - Mar 25, 2026

Scott and his friend Mike were catching up and discussing recent podcast episodes they'd been listening to.

Scott mentioned he's been binge-listening to a podcast about Gary's Killing Investigation, a cold case from 1987 where a small-town sheriff was found dead under mysterious circumstances. "The forensics in this case are incredible," Scott said. "They used luminol on the barn floor 30 years later and still found traces."

Mike recommended another podcast about The Vanishing at Lake Mead — a series about three hikers who disappeared in 2019. "It's really well produced, reminds me of Serial season one."

They also talked about a documentary series on the Enron scandal called The Smartest Guys in the Room. Scott said he'd already seen it but Mike was just getting into it.

Scott mentioned he's been thinking about starting his own podcast about AI and productivity tools. "I've been sketching out episode ideas but haven't committed to anything yet."

Mike suggested they do a joint episode about how they use AI tools at work. Scott said he'd think about it."""

# Case 3: Mixed — work meeting that references external topics
MIXED_MEETING = """Product Strategy Meeting - Mar 26, 2026

Scott led a product strategy session for the ContextQuilt team.

"I was reading about how Notion implemented their AI features," Scott started. "They use a retrieval-augmented approach similar to what we're building, but their context window management is different. There's a good breakdown in Simon Willison's blog."

The team discussed the upcoming v4.0 release. Diana is leading the multi-tenant isolation feature. "We need RBAC done by April 15," she said. Scott committed to having the API schema reviewed by Friday.

Jake brought up a competitor analysis: "Mem.ai just launched their graph memory feature. It's similar to our episodic layer but they're using Neo4j directly instead of Postgres recursive CTEs."

Scott: "That's interesting but our approach is more cost-effective at scale. Let's not chase their architecture. Focus on what makes us different — the connected quilt model."

They briefly discussed the Y Combinator Winter 2026 batch — one of the companies (MemoryOS) is doing something adjacent but focused on personal knowledge management rather than enterprise.

Diana committed to a draft RBAC spec by Monday. Jake will benchmark our graph traversal against Neo4j for the technical blog post."""

# Case 4: Meeting with no project context (user recording casually)
CASUAL_MEETING = """Coffee Chat with Sarah - Mar 27, 2026

Scott had a casual coffee chat with his colleague Sarah from the marketing team.

Sarah mentioned she's been overwhelmed with the rebrand launch. "We're juggling the website redesign, new brand guidelines, and the launch event all at once."

Scott offered to help review the technical sections of the new website copy. Sarah appreciated it and said she'd send him the draft by Wednesday.

They talked about the company offsite next month. Sarah is organizing it and needs headcount by Friday. Scott said he'd confirm his attendance today.

They also chatted about a book Sarah recommended — Thinking in Systems by Donella Meadows. Scott said he'd add it to his reading list.

Scott mentioned he's been experimenting with using Claude for meeting notes. Sarah was curious and asked for a demo sometime next week."""

TEST_CASES = {
    "work_meeting": {
        "transcript": WORK_MEETING,
        "user": "Scott",
        "expect_projects": True,
        "expected_project_names": ["Florida Blue", "Multi-Language", "Language Line"],
        "description": "Real work meeting — should extract project patches",
    },
    "podcast": {
        "transcript": PODCAST_DISCUSSION,
        "user": "Scott",
        "expect_projects": False,
        "not_projects": ["Gary's Killing", "Vanishing at Lake Mead", "Enron", "Smartest Guys"],
        "description": "Podcast discussion — should NOT extract project patches for shows/cases",
    },
    "mixed": {
        "transcript": MIXED_MEETING,
        "user": "Scott",
        "expect_projects": True,
        "expected_project_names": ["ContextQuilt", "v4.0", "multi-tenant"],
        "not_projects": ["Notion", "Mem.ai", "MemoryOS", "Y Combinator", "Simon Willison"],
        "description": "Work meeting referencing external companies — only own projects",
    },
    "casual": {
        "transcript": CASUAL_MEETING,
        "user": "Scott",
        "expect_projects": False,
        "not_projects": ["Thinking in Systems", "Donella Meadows"],
        "description": "Casual chat — might extract rebrand as project (Sarah owns it, not Scott)",
    },
}


async def run_extraction(client, prompt, transcript, user):
    user_content = f"The submitting user is: {user}\n\n{transcript}"
    result = await client.extract(system_prompt=prompt, user_content=user_content)
    return result


def analyze_projects(content, case_info):
    """Check which project patches were extracted and whether they're appropriate."""
    patches = content.get("patches", [])
    project_patches = [p for p in patches if p.get("type") == "project"]
    non_project_patches = [p for p in patches if p.get("type") != "project"]

    project_texts = []
    for p in project_patches:
        v = p.get("value", {})
        text = v.get("text", str(v)) if isinstance(v, dict) else str(v)
        project_texts.append(text)

    # Check for false positives
    false_positives = []
    for name in case_info.get("not_projects", []):
        for pt in project_texts:
            if name.lower() in pt.lower():
                false_positives.append(f"{name} → '{pt}'")

    return {
        "project_count": len(project_patches),
        "project_texts": project_texts,
        "total_patches": len(patches),
        "non_project_patches": len(non_project_patches),
        "false_positives": false_positives,
        "types": dict(Counter(p.get("type", "?") for p in patches)),
    }


async def main():
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("CQ_LLM_API_KEY")
    base_url = os.getenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("CQ_LLM_MODEL", "mistralai/mistral-small-3.1-24b-instruct")

    if not api_key:
        print("Set OPENROUTER_API_KEY or CQ_LLM_API_KEY")
        sys.exit(1)

    target = sys.argv[1] if len(sys.argv) > 1 else None
    cases = {target: TEST_CASES[target]} if target else TEST_CASES

    print(f"=== PROJECT CLASSIFICATION A/B TEST ===")
    print(f"Model: {model}")
    print()

    client = LLMClient(api_key=api_key, base_url=base_url, model=model)
    all_results = []

    try:
        for case_name, case_info in cases.items():
            print(f"{'='*70}")
            print(f"CASE: {case_name}")
            print(f"DESC: {case_info['description']}")
            print()

            # Run baseline
            print(f"  Running BASELINE...")
            b_result = await run_extraction(client, MEETING_SUMMARY_SYSTEM, case_info["transcript"], case_info["user"])
            b = analyze_projects(b_result.content, case_info)

            # Run variant
            print(f"  Running VARIANT (tightened project rules)...")
            v_result = await run_extraction(client, VARIANT_PROMPT, case_info["transcript"], case_info["user"])
            v = analyze_projects(v_result.content, case_info)

            print(f"\n  {'BASELINE':>30s}  {'VARIANT':>30s}")
            print(f"  {'─'*30}  {'─'*30}")
            print(f"  {'Total patches:':>30s}  {b['total_patches']:>5d}  {v['total_patches']:>24d}")
            print(f"  {'Project patches:':>30s}  {b['project_count']:>5d}  {v['project_count']:>24d}")
            print(f"  {'Non-project patches:':>30s}  {b['non_project_patches']:>5d}  {v['non_project_patches']:>24d}")

            print(f"\n  BASELINE projects: {b['project_texts'] or '(none)'}")
            print(f"  VARIANT projects:  {v['project_texts'] or '(none)'}")

            if b['false_positives']:
                print(f"\n  ⚠ BASELINE false positives: {b['false_positives']}")
            if v['false_positives']:
                print(f"\n  ⚠ VARIANT false positives: {v['false_positives']}")
            elif case_info.get("not_projects"):
                print(f"\n  ✓ VARIANT: no false positive projects")

            print(f"\n  BASELINE types: {b['types']}")
            print(f"  VARIANT types:  {v['types']}")

            # Score
            if not case_info["expect_projects"]:
                b_score = "✓ PASS" if b["project_count"] == 0 else f"✗ FAIL ({b['project_count']} projects)"
                v_score = "✓ PASS" if v["project_count"] == 0 else f"✗ FAIL ({v['project_count']} projects)"
                print(f"\n  Expected: NO project patches")
                print(f"  BASELINE: {b_score}")
                print(f"  VARIANT:  {v_score}")
            else:
                b_fp = len(b["false_positives"])
                v_fp = len(v["false_positives"])
                b_score = f"✓ {b['project_count']} projects, {b_fp} false positives"
                v_score = f"✓ {v['project_count']} projects, {v_fp} false positives"
                print(f"\n  Expected: project patches for owned work")
                print(f"  BASELINE: {b_score}")
                print(f"  VARIANT:  {v_score}")

            all_results.append({
                "case": case_name,
                "baseline": b,
                "variant": v,
                "baseline_cost": b_result.cost_usd,
                "variant_cost": v_result.cost_usd,
            })
            print()

    finally:
        await client.close()

    # Summary
    print(f"\n{'='*70}")
    print(f"SUMMARY")
    print(f"{'='*70}")
    print(f"{'Case':<20} {'B-Projects':>12} {'V-Projects':>12} {'B-FalsePos':>12} {'V-FalsePos':>12}")
    print(f"{'─'*20} {'─'*12} {'─'*12} {'─'*12} {'─'*12}")
    for r in all_results:
        print(f"{r['case']:<20} {r['baseline']['project_count']:>12} {r['variant']['project_count']:>12} "
              f"{len(r['baseline']['false_positives']):>12} {len(r['variant']['false_positives']):>12}")

    out_path = os.path.join(os.path.dirname(__file__), "project_classification_results.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
