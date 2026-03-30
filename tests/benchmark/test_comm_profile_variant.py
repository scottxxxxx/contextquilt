#!/usr/bin/env python3
"""
A/B test: Same extraction prompt + communication_profile added to JSON output.
Compares against baseline to detect quality regression.

Usage:
    OPENROUTER_API_KEY=sk-... python tests/benchmark/test_comm_profile_variant.py [--user "Display Name"]
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "contextquilt", "services"))

from llm_client import LLMClient
from extraction_prompts import MEETING_SUMMARY_SYSTEM

# The variant: add communication_profile to the JSON schema in the prompt
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

# Inject after "exactly three keys:" → "exactly four keys:"
VARIANT_PROMPT = MEETING_SUMMARY_SYSTEM.replace(
    "return a JSON object with exactly three keys:",
    "return a JSON object with exactly four keys:"
) + COMM_PROFILE_ADDITION

FLORIDA_BLUE_TRANSCRIPT = """Florida Blue Meeting - Mar 24, 2026

Scott and the Florida Blue team discussed three main topics:

1. Sales Call Summarization: Florida Blue needs transcription and summarization of approximately 1,500 sales call audio files. The summary should be about 1,000-5,000 characters (to be confirmed). Scott will use the best available model and Deepgram Nova 3 for transcription. He'll provide both transcription and summary outputs in an Excel format. Scott asked for 2 days after receiving the audio files. Travis will upload the files via FTP.

2. Multi-Language Support (Language Line): Florida Blue has scenarios where a caller speaks Spanish, an English-speaking agent picks up, and a translator is conferenced in. Currently using Deepgram Nova 2 which requires the language to be specified upfront. Nova 3 supports both Spanish and English and can auto-detect. Scott confirmed Nova 3 supports this. The team discussed upgrading from Nova 2 to Nova 3 for all transcription. Florida Blue is currently on their own VPC, not Core's servers, which makes the upgrade harder. Sridev will send an email to Scott, Anand, and Amanda about the language line requirements.

3. Open Support Tickets: The team reviewed several tickets including 70293 (call transfer visibility - agent 2 seeing agent 1's transcription), 70311 (audio hooks stopping randomly), 70413 (model deployment - has a Jira created), 70412 (engineering investigating), and 69227 (duplicate messages). Scott looked up ticket statuses and added internal comments. Amanda handles escalation through customer success.

The production date for these capabilities is June 28, 2026. Implementation needs to happen within 4 weeks. Scott will reach out to Anand regarding Nova 3 and the language line capabilities."""


async def main():
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("CQ_LLM_API_KEY")
    base_url = os.getenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("CQ_LLM_MODEL", "mistralai/mistral-small-3.1-24b-instruct")

    if not api_key:
        print("Set OPENROUTER_API_KEY or CQ_LLM_API_KEY")
        sys.exit(1)

    display_name = "Scott"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--user" and i < len(sys.argv) - 1:
            display_name = sys.argv[i + 1]

    user_content = f"The submitting user is: {display_name}\n\n" + FLORIDA_BLUE_TRANSCRIPT

    print(f"=== COMMUNICATION PROFILE VARIANT TEST ===")
    print(f"Model: {model}")
    print(f"Submitting user: {display_name}")
    print()

    # Load baseline for comparison
    baseline_path = os.path.join(os.path.dirname(__file__), "extraction_baseline_result.json")
    baseline = None
    if os.path.exists(baseline_path):
        with open(baseline_path) as f:
            baseline = json.load(f)
        print(f"Baseline loaded: {len(baseline.get('patches', []))} patches, "
              f"{len(baseline.get('entities', []))} entities, "
              f"{len(baseline.get('relationships', []))} relationships")
    else:
        print("No baseline found — run test_extraction_dryrun.py first")

    print(f"\nRunning variant extraction...")
    client = LLMClient(api_key=api_key, base_url=base_url, model=model)

    try:
        result = await client.extract(
            system_prompt=VARIANT_PROMPT,
            user_content=user_content,
        )
    finally:
        await client.close()

    content = result.content
    patches = content.get("patches", [])
    entities = content.get("entities", [])
    rels = content.get("relationships", [])
    comm = content.get("communication_profile")

    # Display patches
    print(f"\n{'='*60}")
    print(f"PATCHES ({len(patches)}):")
    icons = {
        "trait": "🧠", "preference": "💡", "role": "👤", "person": "🤝",
        "project": "📋", "decision": "✅", "commitment": "🤝",
        "blocker": "🚫", "takeaway": "💭", "experience": "📝",
    }
    for p in patches:
        ptype = p.get("type", "?")
        value = p.get("value", {})
        text = value.get("text", str(value)) if isinstance(value, dict) else str(value)
        icon = icons.get(ptype, "•")
        extras = []
        if isinstance(value, dict):
            if value.get("owner"): extras.append(f"owner: {value['owner']}")
        extra_str = f" ({', '.join(extras)})" if extras else ""
        print(f"  {icon} [{ptype}] {text}{extra_str}")
        for conn in p.get("connects_to", []):
            print(f"       └── {conn.get('role','?')}/{conn.get('label','')} → [{conn.get('target_type','')}] \"{conn.get('target_text','?')}\"")

    # Display communication profile
    print(f"\n{'='*60}")
    print(f"COMMUNICATION PROFILE:")
    if comm:
        for dim, score in comm.items():
            bar = "█" * int((score or 0) * 20) + "░" * (20 - int((score or 0) * 20))
            print(f"  {dim:20s} {bar} {score}")
    else:
        print("  (null — submitting user barely spoke)")

    # Display entities & relationships counts
    print(f"\n{'='*60}")
    print(f"ENTITIES ({len(entities)}):")
    for e in entities:
        print(f"  [{e.get('type', '?')}] {e.get('name', '?')}")

    print(f"\nRELATIONSHIPS ({len(rels)}):")
    for r in rels:
        print(f"  {r.get('from', '?')} --{r.get('type', '?')}--> {r.get('to', '?')}")

    # Compare with baseline
    if baseline:
        print(f"\n{'='*60}")
        print(f"COMPARISON vs BASELINE:")
        b_patches = baseline.get("patches", [])
        b_entities = baseline.get("entities", [])
        b_rels = baseline.get("relationships", [])

        print(f"  Patches:       {len(b_patches)} → {len(patches)} ({'⚠ LOST' if len(patches) < len(b_patches) else '✓ OK' if len(patches) >= len(b_patches) else ''})")
        print(f"  Entities:      {len(b_entities)} → {len(entities)} ({'⚠ LOST' if len(entities) < len(b_entities) else '✓ OK'})")
        print(f"  Relationships: {len(b_rels)} → {len(rels)} ({'⚠ LOST' if len(rels) < len(b_rels) else '✓ OK'})")

        # Check patch type distribution
        def type_counts(plist):
            counts = {}
            for p in plist:
                t = p.get("type", "?")
                counts[t] = counts.get(t, 0) + 1
            return counts

        b_types = type_counts(b_patches)
        v_types = type_counts(patches)
        all_types = sorted(set(list(b_types.keys()) + list(v_types.keys())))
        print(f"\n  Patch type distribution:")
        for t in all_types:
            b = b_types.get(t, 0)
            v = v_types.get(t, 0)
            delta = v - b
            marker = f" ({'+' if delta > 0 else ''}{delta})" if delta != 0 else ""
            print(f"    {t:15s}  {b} → {v}{marker}")

        # Check text overlap (how many baseline patches have a match in variant)
        b_texts = {p.get("value", {}).get("text", "").lower().strip() for p in b_patches if isinstance(p.get("value"), dict)}
        v_texts = {p.get("value", {}).get("text", "").lower().strip() for p in patches if isinstance(p.get("value"), dict)}

        # Fuzzy match: check if any variant text contains 60%+ of baseline words
        def word_overlap(a, b):
            a_words = set(a.split())
            b_words = set(b.split())
            if not a_words:
                return 0.0
            return len(a_words & b_words) / len(a_words)

        matched = 0
        for bt in b_texts:
            best = max((word_overlap(bt, vt) for vt in v_texts), default=0)
            if best >= 0.5:
                matched += 1
        coverage = matched / len(b_texts) * 100 if b_texts else 100

        print(f"\n  Baseline coverage: {matched}/{len(b_texts)} patches matched ({coverage:.0f}%)")
        if coverage < 80:
            print(f"  ⚠ REGRESSION: <80% of baseline patches preserved")
        else:
            print(f"  ✓ Good: ≥80% of baseline patches preserved")

    # Summary
    print(f"\n{'='*60}")
    print(f"EXTRACTION SUMMARY")
    print(f"  Patches:       {len(patches)}")
    total_connections = sum(len(p.get("connects_to", [])) for p in patches)
    print(f"  Connections:   {total_connections}")
    print(f"  Entities:      {len(entities)}")
    print(f"  Relationships: {len(rels)}")
    print(f"  Comm Profile:  {'Yes' if comm else 'No'}")
    print(f"  Model:         {result.model}")
    print(f"  Latency:       {result.latency_ms:.0f}ms")
    print(f"  Cost:          ${result.cost_usd:.6f}")
    print(f"  Tokens:        {result.input_tokens} in / {result.output_tokens} out")
    print(f"  JSON valid:    {result.json_valid}")

    # Save
    out_path = os.path.join(os.path.dirname(__file__), "extraction_comm_profile_result.json")
    with open(out_path, "w") as f:
        json.dump(content, f, indent=2)
    print(f"\nRaw output saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
