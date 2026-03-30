#!/usr/bin/env python3
"""
Run both baseline and comm-profile-variant extraction on edge case transcripts.
Compares patch quality and checks for profile extraction problems.

Usage:
    OPENROUTER_API_KEY=sk-... python tests/benchmark/test_edge_cases.py [case_name]
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "contextquilt", "services"))

from llm_client import LLMClient
from extraction_prompts import MEETING_SUMMARY_SYSTEM
from edge_case_transcripts import EDGE_CASES

# Same variant prompt as test_comm_profile_variant.py
COMM_PROFILE_ADDITION = """

COMMUNICATION PROFILE — also return a fourth key, "communication_profile", scoring the submitting user's style from this conversation:

{
  "communication_profile": {
    "verbosity": 0.0-1.0,
    "directness": 0.0-1.0,
    "formality": 0.0-1.0,
    "technical_level": 0.0-1.0
  }
}

- Score each dimension from 0.0 (low) to 1.0 (high) based ONLY on how the submitting user communicates
- verbosity: 0.0 = terse one-liners, 1.0 = detailed storytelling
- directness: 0.0 = hedging/uncertain, 1.0 = decisive/imperative
- formality: 0.0 = casual/slang, 1.0 = formal/professional
- technical_level: 0.0 = layperson, 1.0 = deep technical expertise
- If the submitting user barely speaks, return null for communication_profile
- This does NOT count toward the patch/entity/relationship limits"""

VARIANT_PROMPT = MEETING_SUMMARY_SYSTEM.replace(
    "return a JSON object with exactly three keys:",
    "return a JSON object with exactly four keys:"
) + COMM_PROFILE_ADDITION


def compare_extractions(baseline, variant, case_name, case_info):
    """Compare baseline and variant extraction results."""
    b_patches = baseline.get("patches", [])
    v_patches = variant.get("patches", [])
    b_entities = baseline.get("entities", [])
    v_entities = variant.get("entities", [])
    b_rels = baseline.get("relationships", [])
    v_rels = variant.get("relationships", [])
    comm = variant.get("communication_profile")

    def type_counts(plist):
        counts = {}
        for p in plist:
            t = p.get("type", "?")
            counts[t] = counts.get(t, 0) + 1
        return counts

    def word_overlap(a, b):
        a_words = set(a.lower().split())
        b_words = set(b.lower().split())
        if not a_words:
            return 0.0
        return len(a_words & b_words) / len(a_words)

    # Coverage check
    b_texts = [p.get("value", {}).get("text", "").strip() for p in b_patches if isinstance(p.get("value"), dict)]
    v_texts = [p.get("value", {}).get("text", "").strip() for p in v_patches if isinstance(p.get("value"), dict)]

    matched = 0
    for bt in b_texts:
        if not bt:
            continue
        best = max((word_overlap(bt, vt) for vt in v_texts if vt), default=0)
        if best >= 0.5:
            matched += 1
    coverage = matched / len(b_texts) * 100 if b_texts else 100

    b_types = type_counts(b_patches)
    v_types = type_counts(v_patches)

    result = {
        "case": case_name,
        "risk": case_info["risk"],
        "baseline_patches": len(b_patches),
        "variant_patches": len(v_patches),
        "baseline_entities": len(b_entities),
        "variant_entities": len(v_entities),
        "baseline_rels": len(b_rels),
        "variant_rels": len(v_rels),
        "baseline_types": b_types,
        "variant_types": v_types,
        "coverage_pct": coverage,
        "comm_profile": comm,
        "expected_profile": case_info["expected_profile"],
    }

    return result


async def run_case(client, case_name, case_info, prompt, label):
    """Run extraction on a single case."""
    user_content = f"The submitting user is: {case_info['user']}\n\n{case_info['transcript']}"
    try:
        result = await client.extract(system_prompt=prompt, user_content=user_content)
        return {
            "content": result.content,
            "cost": result.cost_usd,
            "tokens_in": result.input_tokens,
            "tokens_out": result.output_tokens,
            "latency_ms": result.latency_ms,
            "json_valid": result.json_valid,
        }
    except Exception as e:
        print(f"  ⚠ {label} failed for {case_name}: {e}")
        return None


async def main():
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("CQ_LLM_API_KEY")
    base_url = os.getenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("CQ_LLM_MODEL", "mistralai/mistral-small-3.1-24b-instruct")

    if not api_key:
        print("Set OPENROUTER_API_KEY or CQ_LLM_API_KEY")
        sys.exit(1)

    # Optional: run single case
    target_case = sys.argv[1] if len(sys.argv) > 1 else None
    cases = {target_case: EDGE_CASES[target_case]} if target_case else EDGE_CASES

    print(f"=== EDGE CASE A/B TEST ===")
    print(f"Model: {model}")
    print(f"Cases: {', '.join(cases.keys())}")
    print()

    client = LLMClient(api_key=api_key, base_url=base_url, model=model)
    all_results = []

    try:
        for case_name, case_info in cases.items():
            print(f"{'='*70}")
            print(f"CASE: {case_name}")
            print(f"RISK: {case_info['risk']}")
            print(f"EXPECTED PROFILE: {case_info['expected_profile']}")
            print()

            # Run baseline
            print(f"  Running baseline...")
            b = await run_case(client, case_name, case_info, MEETING_SUMMARY_SYSTEM, "baseline")

            # Run variant
            print(f"  Running variant (with comm profile)...")
            v = await run_case(client, case_name, case_info, VARIANT_PROMPT, "variant")

            if not b or not v:
                print(f"  ⚠ Skipping comparison — extraction failed")
                continue

            comp = compare_extractions(b["content"], v["content"], case_name, case_info)
            comp["baseline_cost"] = b["cost"]
            comp["variant_cost"] = v["cost"]
            comp["baseline_tokens_out"] = b["tokens_out"]
            comp["variant_tokens_out"] = v["tokens_out"]
            all_results.append(comp)

            # Print comparison
            ok = "✓" if comp["coverage_pct"] >= 80 else "⚠"
            print(f"\n  RESULTS:")
            print(f"    Patches:    {comp['baseline_patches']} → {comp['variant_patches']}")
            print(f"    Entities:   {comp['baseline_entities']} → {comp['variant_entities']}")
            print(f"    Rels:       {comp['baseline_rels']} → {comp['variant_rels']}")
            print(f"    Coverage:   {comp['coverage_pct']:.0f}% {ok}")
            print(f"    Cost:       ${comp['baseline_cost']:.6f} → ${comp['variant_cost']:.6f}")
            print(f"    Tokens out: {comp['baseline_tokens_out']} → {comp['variant_tokens_out']}")

            # Type distribution changes
            all_types = sorted(set(list(comp['baseline_types'].keys()) + list(comp['variant_types'].keys())))
            changes = []
            for t in all_types:
                bv = comp['baseline_types'].get(t, 0)
                vv = comp['variant_types'].get(t, 0)
                if bv != vv:
                    changes.append(f"{t}: {bv}→{vv}")
            if changes:
                print(f"    Type changes: {', '.join(changes)}")

            # Communication profile
            print(f"\n    COMMUNICATION PROFILE:")
            comm = comp["comm_profile"]
            if comm is None:
                print(f"      null (submitting user barely spoke)")
            elif isinstance(comm, dict):
                for dim, score in comm.items():
                    if score is not None:
                        bar = "█" * int(score * 10) + "░" * (10 - int(score * 10))
                        print(f"      {dim:20s} {bar} {score}")
                    else:
                        print(f"      {dim:20s} null")
            else:
                print(f"      Unexpected format: {comm}")

            # Profile sanity check
            print(f"\n    EXPECTED: {case_info['expected_profile']}")
            print()

    finally:
        await client.close()

    # Summary table
    print(f"\n{'='*70}")
    print(f"SUMMARY")
    print(f"{'='*70}")
    print(f"{'Case':<20} {'Patches':>10} {'Coverage':>10} {'Profile':>10} {'Cost Δ':>10}")
    print(f"{'-'*20} {'-'*10} {'-'*10} {'-'*10} {'-'*10}")
    for r in all_results:
        patch_delta = f"{r['baseline_patches']}→{r['variant_patches']}"
        coverage = f"{r['coverage_pct']:.0f}%"
        has_profile = "Yes" if r["comm_profile"] else "null"
        cost_delta = f"+{(r['variant_cost'] - r['baseline_cost'])*1000000:.0f}µ$"
        print(f"{r['case']:<20} {patch_delta:>10} {coverage:>10} {has_profile:>10} {cost_delta:>10}")

    # Save results
    out_path = os.path.join(os.path.dirname(__file__), "edge_case_results.json")
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
