#!/usr/bin/env python3
"""
Test: Can an LLM extract entities and relationships from a meeting summary
in a single call alongside facts and action items?

This validates whether the graph layer can be built from the same extraction
call rather than requiring a separate pipeline step.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "contextquilt", "services"))

from llm_client import LLMClient

GRAPH_EXTRACTION_PROMPT = """You are a structured data extraction engine for Context Quilt, a persistent memory system.

Analyze this meeting summary and return a JSON object with exactly four keys:

{
  "facts": [
    {"fact": "concise statement", "category": "identity|preference|trait|experience", "participants": ["names"]}
  ],
  "action_items": [
    {"action": "what needs to be done", "owner": "who", "deadline": "when or null"}
  ],
  "entities": [
    {"name": "exact name", "type": "person|project|company|feature|artifact|deadline|metric", "description": "brief context"}
  ],
  "relationships": [
    {"from": "entity name", "to": "entity name", "type": "relationship type", "context": "brief explanation"}
  ]
}

ENTITY TYPES:
- person: Named individuals
- project: Named projects or initiatives
- company: Organizations or clients
- feature: Product features or capabilities
- artifact: Deliverables, prototypes, documents
- deadline: Specific dates or timeframes
- metric: Numbers, budgets, percentages

RELATIONSHIP TYPES (use descriptive verbs):
- works_on, leads, owns, committed_to
- requires, depends_on, blocks
- includes, part_of
- has_deadline, due_by
- contacted_by, reports_to, cto_of
- budgeted_at, capped_at
- decided, proposed, agreed_to

Rules:
1. Entity names must be exact as mentioned (e.g., "Bob Martinez" not "Bob")
2. Every relationship must reference entities from the entities list
3. Include temporal relationships (deadlines, schedules)
4. Capture decisions as relationships (e.g., team --decided--> "ship offline mode as core")
5. Keep descriptions brief"""


TEST_SUMMARY = """# Widget 2.0 Kickoff - Summary

Attendees: Sarah Chen (Tech Lead), Bob Martinez (VP Product)

Sarah opened by confirming that Acme Corp signed the renewal with a June 15 deadline for real-time collaboration — it's a contractual obligation.

Bob said he's been evaluating the WebSocket infrastructure and estimates a working prototype in three weeks. He flagged that Tom's user research shows 40% of target users have unreliable internet, making offline mode critical.

Sarah proposed shipping offline mode as core and real-time collab as beta. She asked Bob to have the WebSocket prototype ready by April 5th, and said she'd schedule a demo with Acme's CTO David Chen the following week.

Bob agreed to April 5th but needs staging environment access — asked Sarah to submit the request today. He also noted the project budget is capped at $150,000 per Lisa from finance.

They agreed on weekly check-ins every Tuesday at 10am starting next week."""


async def main():
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        print("Set OPENROUTER_API_KEY")
        sys.exit(1)

    client = LLMClient(api_key=key, base_url="https://openrouter.ai/api/v1", model="mistralai/mistral-small-3.1-24b-instruct")

    print("Extracting entities and relationships...\n")
    result = await client.extract(GRAPH_EXTRACTION_PROMPT, TEST_SUMMARY)

    content = result.content

    # Facts
    facts = content.get("facts", [])
    print(f"FACTS ({len(facts)}):")
    for f in facts:
        cat = f.get("category", "?") if isinstance(f, dict) else "?"
        text = f.get("fact", f) if isinstance(f, dict) else f
        print(f"  [{cat}] {text}")

    # Action items
    actions = content.get("action_items", [])
    print(f"\nACTION ITEMS ({len(actions)}):")
    for a in actions:
        owner = a.get("owner", "?")
        dl = f" (by {a.get('deadline')})" if a.get("deadline") else ""
        print(f"  [{owner}] {a.get('action', '?')}{dl}")

    # Entities
    entities = content.get("entities", [])
    print(f"\nENTITIES ({len(entities)}):")
    for e in entities:
        print(f"  [{e.get('type', '?')}] {e.get('name', '?')} — {e.get('description', '')}")

    # Relationships
    rels = content.get("relationships", [])
    print(f"\nRELATIONSHIPS ({len(rels)}):")
    for r in rels:
        print(f"  {r.get('from', '?')} --{r.get('type', '?')}--> {r.get('to', '?')}")
        if r.get("context"):
            print(f"    ({r['context']})")

    # Stats
    print(f"\n--- Stats ---")
    print(f"Model: {result.model}")
    print(f"Latency: {result.latency_ms:.0f}ms")
    print(f"Cost: ${result.cost_usd:.6f}")
    print(f"Tokens: {result.input_tokens} in / {result.output_tokens} out")
    print(f"JSON valid: {result.json_valid}")

    # Save full output
    with open("tests/benchmark/graph_extraction_result.json", "w") as f:
        json.dump(content, f, indent=2)
    print(f"\nFull output saved to tests/benchmark/graph_extraction_result.json")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
