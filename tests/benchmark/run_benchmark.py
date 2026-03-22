#!/usr/bin/env python3
"""
Context Quilt Model Benchmark Harness

Runs test meeting summaries through candidate extraction models and scores
each on: fact extraction accuracy, action item extraction, JSON validity,
token usage, latency, and cost.

Usage:
    # Set API keys for the providers you want to test:
    export OPENAI_API_KEY=sk-...
    export GOOGLE_API_KEY=AIza...

    # Run all models:
    python tests/benchmark/run_benchmark.py

    # Run a specific model:
    python tests/benchmark/run_benchmark.py --model gpt-4.1-nano

    # Run a specific test case:
    python tests/benchmark/run_benchmark.py --case stakeholder_complex
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass

# Add project root and services directly to path to avoid pulling in full project deps
_root = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, _root)
sys.path.insert(0, os.path.join(_root, "src", "contextquilt", "services"))
sys.path.insert(0, os.path.join(_root, "tests", "benchmark"))

from llm_client import LLMClient, LLMResponse
from extraction_prompts import MEETING_SUMMARY_SYSTEM
from test_summaries import TEST_CASES

# Model configurations to benchmark
MODELS = {
    "gpt-4.1-nano": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4.1-nano",
    },
    "gpt-4o-mini": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
    "gpt-5.4-nano": {
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "model": "gpt-5.4-nano",
    },
    "gemini-2.5-flash-lite": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GOOGLE_API_KEY",
        "model": "gemini-2.5-flash-lite",
    },
    # --- OpenRouter models ---
    "or/qwen3-4b-free": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "qwen/qwen3-4b:free",
    },
    "or/mistral-small-3.1-free": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "mistralai/mistral-small-3.1-24b-instruct:free",
    },
    "or/qwen-turbo": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "qwen/qwen-turbo",
    },
    "or/qwen3.5-flash": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "qwen/qwen3.5-flash-02-23",
    },
    "or/qwen3-14b": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "qwen/qwen3-14b",
    },
    "or/mistral-small-3.1-paid": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "mistralai/mistral-small-3.1-24b-instruct",
    },
    "or/command-r7b": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model": "cohere/command-r7b-12-2024",
    },
}


@dataclass
class ScoreResult:
    """Scoring result for a single model on a single test case."""
    model: str
    case_id: str
    facts_found: int
    facts_expected: int
    facts_score: float  # 0.0 - 1.0
    actions_found: int
    actions_expected: int
    actions_score: float  # 0.0 - 1.0
    json_valid: bool
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float
    extra_facts: int  # facts extracted beyond expected (not necessarily bad)


def score_facts(extracted: list[dict], expected: list[dict]) -> tuple[int, float, int]:
    """
    Score extracted facts against expected ground truth.
    Uses keyword overlap — a match if 50%+ of expected fact words appear.
    Returns (matched_count, score, extra_count).
    """
    matched = 0
    for exp in expected:
        exp_words = set(exp["fact"].lower().split())
        # Remove common words
        exp_words -= {"the", "a", "an", "is", "are", "was", "were", "to", "for", "of", "in", "on", "and", "that"}
        if len(exp_words) < 2:
            continue

        for ext in extracted:
            fact_text = ext.get("fact", "") if isinstance(ext, dict) else str(ext)
            ext_words = set(fact_text.lower().split())
            overlap = exp_words & ext_words
            if len(overlap) >= len(exp_words) * 0.5:
                matched += 1
                break

    total_expected = len(expected)
    score = matched / total_expected if total_expected > 0 else 1.0
    extra = max(0, len(extracted) - total_expected)
    return matched, score, extra


def score_actions(extracted: list[dict], expected: list[dict]) -> tuple[int, float]:
    """
    Score extracted action items against expected ground truth.
    Returns (matched_count, score).
    """
    matched = 0
    for exp in expected:
        exp_words = set(exp["action"].lower().split())
        exp_words -= {"the", "a", "an", "is", "to", "for", "of", "in", "on", "and", "will"}
        if len(exp_words) < 2:
            continue

        for ext in extracted:
            action_text = ext.get("action", "") if isinstance(ext, dict) else str(ext)
            ext_words = set(action_text.lower().split())
            overlap = exp_words & ext_words
            if len(overlap) >= len(exp_words) * 0.4:
                matched += 1
                break

    total_expected = len(expected)
    score = matched / total_expected if total_expected > 0 else 1.0
    return matched, score


async def run_single(client: LLMClient, case: dict, model_name: str) -> ScoreResult:
    """Run a single test case through a model and score it."""
    response = await client.extract(
        system_prompt=MEETING_SUMMARY_SYSTEM,
        user_content=case["summary"],
    )

    extracted_facts = response.content.get("facts", [])
    extracted_actions = response.content.get("action_items", [])

    facts_matched, facts_score, extra_facts = score_facts(extracted_facts, case["expected_facts"])
    actions_matched, actions_score = score_actions(extracted_actions, case["expected_action_items"])

    return ScoreResult(
        model=model_name,
        case_id=case["id"],
        facts_found=len(extracted_facts),
        facts_expected=len(case["expected_facts"]),
        facts_score=facts_score,
        actions_found=len(extracted_actions),
        actions_expected=len(case["expected_action_items"]),
        actions_score=actions_score,
        json_valid=response.json_valid,
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        latency_ms=response.latency_ms,
        cost_usd=response.cost_usd,
        extra_facts=extra_facts,
    )


async def benchmark_model(model_name: str, config: dict, cases: list[dict]) -> list[ScoreResult]:
    """Run all test cases through a single model."""
    api_key = os.getenv(config["api_key_env"], "")
    if not api_key:
        print(f"  SKIPPED: {config['api_key_env']} not set")
        return []

    client = LLMClient(
        api_key=api_key,
        base_url=config["base_url"],
        model=config["model"],
    )

    results = []
    for case in cases:
        try:
            result = await run_single(client, case, model_name)
            results.append(result)
            status = "OK" if result.facts_score >= 0.7 else "LOW"
            print(f"  [{status}] {case['id']}: facts={result.facts_score:.0%} actions={result.actions_score:.0%} "
                  f"latency={result.latency_ms:.0f}ms cost=${result.cost_usd:.6f}")
        except Exception as e:
            print(f"  [ERR] {case['id']}: {e}")

    await client.close()
    return results


def print_summary(all_results: dict[str, list[ScoreResult]]):
    """Print a comparison table of all models."""
    print("\n" + "=" * 100)
    print("BENCHMARK SUMMARY")
    print("=" * 100)

    header = f"{'Model':<25} {'Fact Score':>10} {'Action Score':>12} {'JSON Valid':>10} {'Avg Latency':>12} {'Avg Cost':>10} {'Tokens':>10}"
    print(header)
    print("-" * 100)

    for model_name, results in all_results.items():
        if not results:
            print(f"{model_name:<25} {'SKIPPED':>10}")
            continue

        n = len(results)
        avg_facts = sum(r.facts_score for r in results) / n
        avg_actions = sum(r.actions_score for r in results) / n
        json_ok = sum(1 for r in results if r.json_valid)
        avg_latency = sum(r.latency_ms for r in results) / n
        avg_cost = sum(r.cost_usd for r in results) / n
        avg_tokens = sum(r.input_tokens + r.output_tokens for r in results) / n

        print(f"{model_name:<25} {avg_facts:>9.0%} {avg_actions:>11.0%} "
              f"{json_ok}/{n}:>10 {avg_latency:>10.0f}ms ${avg_cost:>9.6f} {avg_tokens:>9.0f}")

    print("=" * 100)

    # Per-case breakdown
    print("\nPER-CASE DETAIL")
    print("-" * 100)
    for model_name, results in all_results.items():
        if not results:
            continue
        print(f"\n  {model_name}:")
        for r in results:
            print(f"    {r.case_id:<25} facts={r.facts_found}/{r.facts_expected} ({r.facts_score:.0%}) "
                  f"actions={r.actions_found}/{r.actions_expected} ({r.actions_score:.0%}) "
                  f"extra_facts={r.extra_facts} "
                  f"latency={r.latency_ms:.0f}ms cost=${r.cost_usd:.6f}")


def save_results(all_results: dict[str, list[ScoreResult]], output_path: str):
    """Save raw results to JSON for later analysis."""
    data = {}
    for model_name, results in all_results.items():
        data[model_name] = [
            {
                "case_id": r.case_id,
                "facts_found": r.facts_found,
                "facts_expected": r.facts_expected,
                "facts_score": r.facts_score,
                "actions_found": r.actions_found,
                "actions_expected": r.actions_expected,
                "actions_score": r.actions_score,
                "json_valid": r.json_valid,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "latency_ms": r.latency_ms,
                "cost_usd": r.cost_usd,
                "extra_facts": r.extra_facts,
            }
            for r in results
        ]

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nRaw results saved to {output_path}")


async def main():
    parser = argparse.ArgumentParser(description="Context Quilt Model Benchmark")
    parser.add_argument("--model", help="Run only this model (e.g., gpt-4.1-nano)")
    parser.add_argument("--case", help="Run only this test case (e.g., stakeholder_complex)")
    parser.add_argument("--output", default="tests/benchmark/results.json", help="Output JSON path")
    args = parser.parse_args()

    # Filter models
    models = MODELS
    if args.model:
        if args.model not in MODELS:
            print(f"Unknown model: {args.model}. Available: {', '.join(MODELS.keys())}")
            sys.exit(1)
        models = {args.model: MODELS[args.model]}

    # Filter cases
    cases = TEST_CASES
    if args.case:
        cases = [c for c in TEST_CASES if c["id"] == args.case]
        if not cases:
            print(f"Unknown case: {args.case}. Available: {', '.join(c['id'] for c in TEST_CASES)}")
            sys.exit(1)

    print(f"Running {len(cases)} test cases across {len(models)} models\n")

    all_results: dict[str, list[ScoreResult]] = {}

    for model_name, config in models.items():
        print(f"\n--- {model_name} ---")
        results = await benchmark_model(model_name, config, cases)
        all_results[model_name] = results

    print_summary(all_results)
    save_results(all_results, args.output)


if __name__ == "__main__":
    asyncio.run(main())
