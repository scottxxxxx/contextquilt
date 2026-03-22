#!/usr/bin/env python3
"""
Context Quilt Pipeline Benchmark

Compares three pipeline configurations:
  1. Single call — one model extracts facts, profiles, and episode summary
  2. Two calls — extraction + behavioral profiling
  3. Four calls — Picker, Stitcher, Designer, Cataloger (original architecture)

Uses the Widget 2.0 meeting summary as the test input.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "contextquilt", "services"))

from llm_client import LLMClient, LLMResponse

OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
BASE_URL = "https://openrouter.ai/api/v1"

TEST_SUMMARY = """# Widget 2.0 Kickoff - Summary

Attendees: Sarah Chen (Tech Lead), Bob Martinez (VP Product)

Sarah opened by confirming that Acme Corp signed the renewal with a June 15 deadline for real-time collaboration — it's a contractual obligation.

Bob said he's been evaluating the WebSocket infrastructure and estimates a working prototype in three weeks. He flagged that Tom's user research shows 40% of target users have unreliable internet, making offline mode critical.

Sarah proposed shipping offline mode as core and real-time collab as beta. She asked Bob to have the WebSocket prototype ready by April 5th, and said she'd schedule a demo with Acme's CTO David Chen the following week.

Bob agreed to April 5th but needs staging environment access — asked Sarah to submit the request today. He also noted the project budget is capped at $150,000 per Lisa from finance.

They agreed on weekly check-ins every Tuesday at 10am starting next week."""

# ============================================================
# PROMPTS
# ============================================================

SINGLE_CALL_SYSTEM = """You are a structured data extraction engine for Context Quilt.
Analyze this meeting summary and return a JSON object with exactly four keys:

{
  "facts": [{"fact": "string", "category": "identity|preference|trait|experience", "participants": ["names"]}],
  "action_items": [{"action": "string", "owner": "string", "deadline": "string or null"}],
  "communication_profile": {
    "verbosity": 0.0, "technical_level": 0.0, "directness": 0.0,
    "formality": 0.0, "warmth": 0.0, "detail_orientation": 0.0
  },
  "episode": {
    "summary": "1-2 sentence summary",
    "goal": "snake_case_verb_phrase",
    "outcome": "info_provided|action_completed|investigation_started|not_resolved",
    "domain": "string"
  }
}

Score communication traits 0.0-1.0 based on how participants communicate in the summary."""

PICKER_SYSTEM = """You are "The Picker" for Context Quilt. Extract concrete facts and action items from a meeting summary.

Return JSON:
{
  "facts": [{"fact": "concise statement", "category": "identity|preference|trait|experience", "participants": ["names"]}],
  "action_items": [{"action": "what", "owner": "who", "deadline": "when or null"}]
}

Rules: Every fact must be grounded in the text. Keep facts concise. Capture decisions, commitments, and context."""

STITCHER_SYSTEM = """You are "The Stitcher" for Context Quilt. Organize these extracted facts into a structured user profile.

Return JSON:
{
  "identity_facts": {"semantic_key": "value"},
  "preference_facts": {"semantic_key": "value"},
  "task_facts": {"semantic_key": "value"},
  "constraints_facts": {"semantic_key": "value"}
}

Rules: Use semantic keys (not generic). Omit empty categories. Do not invent data not in the input."""

DESIGNER_SYSTEM = """You are "The Designer" for Context Quilt. Analyze communication patterns from a meeting to build a behavioral profile.

Score each trait 0.0-1.0:
- verbosity: 0=terse, 1=verbose
- technical_level: 0=layperson, 1=expert
- directness: 0=hedging, 1=decisive
- formality: 0=casual, 1=formal
- warmth: 0=transactional, 1=warm
- detail_orientation: 0=vague, 1=specific

Return JSON:
{
  "communication_profile": {
    "verbosity": 0.0, "technical_level": 0.0, "directness": 0.0,
    "formality": 0.0, "warmth": 0.0, "detail_orientation": 0.0
  }
}"""

CATALOGER_SYSTEM = """You are "The Cataloger" for Context Quilt. Summarize this meeting at a high level.

Return JSON:
{
  "episode_summary": "1-2 sentence summary, no names",
  "goal": "snake_case_verb_phrase",
  "outcome": "info_provided|action_completed|investigation_started|not_resolved",
  "domain": "string describing the topic area"
}

Rules: Focus high level. Do not extract specific facts."""


async def run_single_call(client: LLMClient, model: str):
    """Config 1: One model does everything."""
    start = time.monotonic()
    result = await client.extract(SINGLE_CALL_SYSTEM, TEST_SUMMARY, model=model)
    total_ms = (time.monotonic() - start) * 1000

    return {
        "config": "single_call",
        "model": model,
        "calls": 1,
        "total_ms": total_ms,
        "total_cost": result.cost_usd,
        "total_input_tokens": result.input_tokens,
        "total_output_tokens": result.output_tokens,
        "json_valid": result.json_valid,
        "content": result.content,
    }


async def run_two_calls(client: LLMClient, picker_model: str, designer_model: str):
    """Config 2: Picker+Stitcher combined, then Designer."""
    start = time.monotonic()
    total_cost = 0
    total_in = 0
    total_out = 0
    all_valid = True

    # Call 1: Pick + Stitch
    r1 = await client.extract(PICKER_SYSTEM, TEST_SUMMARY, model=picker_model)
    total_cost += r1.cost_usd
    total_in += r1.input_tokens
    total_out += r1.output_tokens
    all_valid = all_valid and r1.json_valid

    # Call 2: Designer
    r2 = await client.extract(DESIGNER_SYSTEM, TEST_SUMMARY, model=designer_model)
    total_cost += r2.cost_usd
    total_in += r2.input_tokens
    total_out += r2.output_tokens
    all_valid = all_valid and r2.json_valid

    total_ms = (time.monotonic() - start) * 1000

    combined = {**r1.content, **r2.content}
    return {
        "config": "two_calls",
        "models": {"picker": picker_model, "designer": designer_model},
        "calls": 2,
        "total_ms": total_ms,
        "total_cost": total_cost,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "json_valid": all_valid,
        "content": combined,
    }


async def run_four_calls(client: LLMClient, picker_model: str, stitcher_model: str,
                          designer_model: str, cataloger_model: str):
    """Config 3: Full four-role pipeline."""
    start = time.monotonic()
    total_cost = 0
    total_in = 0
    total_out = 0
    all_valid = True

    # 1. Picker
    r1 = await client.extract(PICKER_SYSTEM, TEST_SUMMARY, model=picker_model)
    total_cost += r1.cost_usd
    total_in += r1.input_tokens
    total_out += r1.output_tokens
    all_valid = all_valid and r1.json_valid

    # 2. Stitcher (takes Picker output as input)
    picker_output = json.dumps(r1.content.get("facts", []), indent=2)
    r2 = await client.extract(STITCHER_SYSTEM, picker_output, model=stitcher_model)
    total_cost += r2.cost_usd
    total_in += r2.input_tokens
    total_out += r2.output_tokens
    all_valid = all_valid and r2.json_valid

    # 3. Designer (analyzes original summary)
    r3 = await client.extract(DESIGNER_SYSTEM, TEST_SUMMARY, model=designer_model)
    total_cost += r3.cost_usd
    total_in += r3.input_tokens
    total_out += r3.output_tokens
    all_valid = all_valid and r3.json_valid

    # 4. Cataloger (summarizes original summary)
    r4 = await client.extract(CATALOGER_SYSTEM, TEST_SUMMARY, model=cataloger_model)
    total_cost += r4.cost_usd
    total_in += r4.input_tokens
    total_out += r4.output_tokens
    all_valid = all_valid and r4.json_valid

    total_ms = (time.monotonic() - start) * 1000

    combined = {
        "facts": r1.content.get("facts", []),
        "action_items": r1.content.get("action_items", []),
        "profile": r2.content,
        **r3.content,
        "episode": r4.content,
    }

    return {
        "config": "four_calls",
        "models": {
            "picker": picker_model,
            "stitcher": stitcher_model,
            "designer": designer_model,
            "cataloger": cataloger_model,
        },
        "calls": 4,
        "total_ms": total_ms,
        "total_cost": total_cost,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "json_valid": all_valid,
        "content": combined,
    }


def print_result(r):
    """Print a benchmark result."""
    config = r["config"]
    models = r.get("model") or r.get("models")
    print(f"\n{'='*70}")
    print(f"CONFIG: {config}")
    print(f"Models: {models}")
    print(f"Calls: {r['calls']}  |  Time: {r['total_ms']:.0f}ms  |  Cost: ${r['total_cost']:.6f}")
    print(f"Tokens: {r['total_input_tokens']} in / {r['total_output_tokens']} out  |  JSON valid: {r['json_valid']}")
    print(f"-"*70)

    content = r["content"]

    # Facts
    facts = content.get("facts", [])
    print(f"\nFacts ({len(facts)}):")
    for f in facts:
        if isinstance(f, dict):
            print(f"  [{f.get('category','?')}] {f.get('fact','?')}")
        else:
            print(f"  {f}")

    # Action items
    actions = content.get("action_items", [])
    print(f"\nAction Items ({len(actions)}):")
    for a in actions:
        if isinstance(a, dict):
            owner = a.get("owner", "?")
            deadline = a.get("deadline", "")
            dl = f" (by {deadline})" if deadline else ""
            print(f"  [{owner}] {a.get('action','?')}{dl}")

    # Communication profile
    cp = content.get("communication_profile", {})
    if cp:
        print(f"\nCommunication Profile:")
        for k, v in cp.items():
            if isinstance(v, (int, float)):
                bar = "#" * int(v * 10)
                print(f"  {k:<20} {v:.1f} {bar}")

    # Episode
    ep = content.get("episode", {})
    if ep:
        print(f"\nEpisode:")
        for k, v in ep.items():
            print(f"  {k}: {v}")

    # Stitched profile (four-call only)
    profile = content.get("profile", {})
    if profile:
        print(f"\nStitched Profile:")
        for category, vals in profile.items():
            if isinstance(vals, dict) and vals:
                print(f"  {category}:")
                for k, v in vals.items():
                    print(f"    {k}: {v}")


async def main():
    if not OPENROUTER_KEY:
        print("Set OPENROUTER_API_KEY")
        sys.exit(1)

    client = LLMClient(api_key=OPENROUTER_KEY, base_url=BASE_URL, model="placeholder")

    results = []

    # Config 1: Single call — best general-purpose model
    print("\n>>> Running: Single call (mistral-small-3.1)...")
    r1 = await run_single_call(client, "mistralai/mistral-small-3.1-24b-instruct")
    print_result(r1)
    results.append(r1)

    # Config 2: Two calls — picker + designer
    print("\n>>> Running: Two calls (mistral-small + qwen-turbo)...")
    r2 = await run_two_calls(
        client,
        picker_model="mistralai/mistral-small-3.1-24b-instruct",  # Best fact extractor from benchmarks
        designer_model="qwen/qwen-turbo",  # Good at nuanced analysis, very cheap
    )
    print_result(r2)
    results.append(r2)

    # Config 3: Four calls — best model per role
    print("\n>>> Running: Four calls (specialized per role)...")
    r3 = await run_four_calls(
        client,
        picker_model="mistralai/mistral-small-3.1-24b-instruct",  # Best fact extraction (90% in benchmarks)
        stitcher_model="qwen/qwen-turbo",  # Schema organization — cheap, structured output
        designer_model="qwen/qwen3-14b",  # Behavioral analysis — mid-size model for nuance
        cataloger_model="cohere/command-r7b-12-2024",  # Summarization — Cohere is strong here, cheapest
    )
    print_result(r3)
    results.append(r3)

    # Summary comparison
    print(f"\n{'='*70}")
    print("COMPARISON SUMMARY")
    print(f"{'='*70}")
    print(f"{'Config':<15} {'Calls':<6} {'Time':>8} {'Cost':>12} {'Facts':>6} {'Actions':>8} {'Profile':>8} {'Episode':>8}")
    print("-" * 80)
    for r in results:
        c = r["content"]
        has_profile = "Yes" if c.get("communication_profile") else "No"
        has_episode = "Yes" if c.get("episode") else "No"
        facts = len(c.get("facts", []))
        actions = len(c.get("action_items", []))
        print(f"{r['config']:<15} {r['calls']:<6} {r['total_ms']:>7.0f}ms ${r['total_cost']:>10.6f} {facts:>6} {actions:>8} {has_profile:>8} {has_episode:>8}")

    # Save results
    output_path = "tests/benchmark/pipeline_results.json"
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
