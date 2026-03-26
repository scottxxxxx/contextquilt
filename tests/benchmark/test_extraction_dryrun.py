#!/usr/bin/env python3
"""
Dry-run extraction test: feeds a transcript through the MEETING_SUMMARY_SYSTEM
prompt and prints what patches would be saved. No database writes.

Supports both V1 (flat facts) and V2 (connected patches) output formats.

Usage:
    OPENROUTER_API_KEY=sk-... python tests/benchmark/test_extraction_dryrun.py [transcript_file] [--user "Display Name"]
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "contextquilt", "services"))

from llm_client import LLMClient
from extraction_prompts import MEETING_SUMMARY_SYSTEM

MAX_PATCHES = 12
MAX_ENTITIES = 10
MAX_RELATIONSHIPS = 10

FLORIDA_BLUE_TRANSCRIPT = """Florida Blue Meeting - Mar 24, 2026

Scott and the Florida Blue team discussed three main topics:

1. Sales Call Summarization: Florida Blue needs transcription and summarization of approximately 1,500 sales call audio files. The summary should be about 1,000-5,000 characters (to be confirmed). Scott will use the best available model and Deepgram Nova 3 for transcription. He'll provide both transcription and summary outputs in an Excel format. Scott asked for 2 days after receiving the audio files. Travis will upload the files via FTP.

2. Multi-Language Support (Language Line): Florida Blue has scenarios where a caller speaks Spanish, an English-speaking agent picks up, and a translator is conferenced in. Currently using Deepgram Nova 2 which requires the language to be specified upfront. Nova 3 supports both Spanish and English and can auto-detect. Scott confirmed Nova 3 supports this. The team discussed upgrading from Nova 2 to Nova 3 for all transcription. Florida Blue is currently on their own VPC, not Core's servers, which makes the upgrade harder. Sridev will send an email to Scott, Anand, and Amanda about the language line requirements.

3. Open Support Tickets: The team reviewed several tickets including 70293 (call transfer visibility - agent 2 seeing agent 1's transcription), 70311 (audio hooks stopping randomly), 70413 (model deployment - has a Jira created), 70412 (engineering investigating), and 69227 (duplicate messages). Scott looked up ticket statuses and added internal comments. Amanda handles escalation through customer success.

The production date for these capabilities is June 28, 2026. Implementation needs to happen within 4 weeks. Scott will reach out to Anand regarding Nova 3 and the language line capabilities."""


def display_v2_patches(content):
    """Display Connected Quilt V2 format (typed patches with connections)."""
    patches = content.get("patches", [])
    capped = len(patches) > MAX_PATCHES

    if capped:
        print(f"\n{'='*60}")
        print(f"PATCHES ({len(patches)} extracted, would be CAPPED to {MAX_PATCHES}):")
    else:
        print(f"\n{'='*60}")
        print(f"PATCHES ({len(patches)}):")

    for i, p in enumerate(patches):
        marker = " ✂" if i >= MAX_PATCHES else ""
        ptype = p.get("type", "?")
        value = p.get("value", {})
        text = value.get("text", str(value)) if isinstance(value, dict) else str(value)

        # Type icon
        icons = {
            "trait": "🧠", "preference": "💡", "role": "👤", "person": "🤝",
            "project": "📋", "decision": "✅", "commitment": "🤝",
            "blocker": "🚫", "takeaway": "💭", "experience": "📝",
        }
        icon = icons.get(ptype, "•")

        # Extra fields
        extras = []
        if isinstance(value, dict):
            if value.get("owner"):
                extras.append(f"owner: {value['owner']}")
            if value.get("deadline"):
                extras.append(f"deadline: {value['deadline']}")
        extra_str = f" ({', '.join(extras)})" if extras else ""

        print(f"\n  {icon} [{ptype}] {text}{extra_str}{marker}")

        # Connections
        connects_to = p.get("connects_to", [])
        for conn in connects_to:
            role = conn.get("role", "?")
            label = conn.get("label", "")
            target = conn.get("target_text", "?")
            target_type = conn.get("target_type", "")
            label_str = f"/{label}" if label else ""
            print(f"       └── {role}{label_str} → [{target_type}] \"{target}\"")

    return patches


def display_v1_facts(content):
    """Display V1 format (flat facts + action_items) for backward compat."""
    facts = content.get("facts", [])
    print(f"\nFACTS ({len(facts)}):")
    for f in facts:
        cat = f.get("category", "?") if isinstance(f, dict) else "?"
        text = f.get("fact", f) if isinstance(f, dict) else f
        print(f"  [{cat}] {text}")

    actions = content.get("action_items", [])
    print(f"\nACTION ITEMS ({len(actions)}):")
    for a in actions:
        owner = a.get("owner", "?")
        dl = f" (by {a.get('deadline')})" if a.get("deadline") else ""
        print(f"  [{owner}] {a.get('action', '?')}{dl}")


async def main():
    api_key = os.getenv("OPENROUTER_API_KEY") or os.getenv("CQ_LLM_API_KEY")
    base_url = os.getenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    model = os.getenv("CQ_LLM_MODEL", "mistralai/mistral-small-3.1-24b-instruct")

    if not api_key:
        print("Set OPENROUTER_API_KEY or CQ_LLM_API_KEY")
        sys.exit(1)

    # Parse args
    transcript_file = None
    display_name = None
    i = 1
    while i < len(sys.argv):
        if sys.argv[i] == "--user" and i + 1 < len(sys.argv):
            display_name = sys.argv[i + 1]
            i += 2
        else:
            transcript_file = sys.argv[i]
            i += 1

    if transcript_file:
        with open(transcript_file) as f:
            transcript = f.read()
        print(f"Loaded transcript from {transcript_file} ({len(transcript)} chars)")
    else:
        transcript = FLORIDA_BLUE_TRANSCRIPT
        print(f"Using embedded Florida Blue transcript ({len(transcript)} chars)")

    user_content = transcript
    if display_name:
        user_content = f"The submitting user is: {display_name}\n\n" + transcript
        print(f"Submitting user: {display_name}")

    print(f"Model: {model}")
    print(f"Endpoint: {base_url}")

    client = LLMClient(api_key=api_key, base_url=base_url, model=model)

    try:
        result = await client.extract(
            system_prompt=MEETING_SUMMARY_SYSTEM,
            user_content=user_content,
        )
    finally:
        await client.close()

    content = result.content

    # Detect format: V2 (patches) or V1 (facts)
    is_v2 = "patches" in content
    if is_v2:
        patches = display_v2_patches(content)
    else:
        display_v1_facts(content)
        patches = content.get("facts", [])

    # Entities (same in both formats)
    entities = content.get("entities", [])
    capped_ents = len(entities) > MAX_ENTITIES
    print(f"\n{'='*60}")
    if capped_ents:
        print(f"ENTITIES ({len(entities)} extracted, would be CAPPED to {MAX_ENTITIES}):")
    else:
        print(f"ENTITIES ({len(entities)}):")
    for i, e in enumerate(entities):
        marker = " ✂" if i >= MAX_ENTITIES else ""
        print(f"  [{e.get('type', '?')}] {e.get('name', '?')} — {e.get('description', '')}{marker}")

    # Relationships
    rels = content.get("relationships", [])
    capped_rels = len(rels) > MAX_RELATIONSHIPS
    if capped_rels:
        print(f"\nRELATIONSHIPS ({len(rels)} extracted, would be CAPPED to {MAX_RELATIONSHIPS}):")
    else:
        print(f"\nRELATIONSHIPS ({len(rels)}):")
    for i, r in enumerate(rels):
        marker = " ✂" if i >= MAX_RELATIONSHIPS else ""
        print(f"  {r.get('from', '?')} --{r.get('type', '?')}--> {r.get('to', '?')}{marker}")
        if r.get("context"):
            print(f"    ({r['context']})")

    # Summary
    print(f"\n{'='*60}")
    print(f"EXTRACTION SUMMARY")
    print(f"  Format:        {'Connected Quilt V2' if is_v2 else 'Flat V1'}")
    print(f"  Patches:       {len(patches)}{' (OVER CAP)' if len(patches) > MAX_PATCHES else ''}")
    if is_v2:
        total_connections = sum(len(p.get("connects_to", [])) for p in patches)
        print(f"  Connections:   {total_connections}")
    print(f"  Entities:      {len(entities)}{' (OVER CAP)' if capped_ents else ''}")
    print(f"  Relationships: {len(rels)}{' (OVER CAP)' if capped_rels else ''}")
    print(f"  Model:         {result.model}")
    print(f"  Latency:       {result.latency_ms:.0f}ms")
    print(f"  Cost:          ${result.cost_usd:.6f}")
    print(f"  Tokens:        {result.input_tokens} in / {result.output_tokens} out")
    print(f"  JSON valid:    {result.json_valid}")

    # Save raw output
    out_path = os.path.join(os.path.dirname(__file__), "extraction_dryrun_result.json")
    with open(out_path, "w") as f:
        json.dump(content, f, indent=2)
    print(f"\nRaw output saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
