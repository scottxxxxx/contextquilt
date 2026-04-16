#!/usr/bin/env python3
"""
Test: does the extraction prompt correctly gate trait/preference/identity
patches on the presence of the `(you)` speaker marker?

Runs the same transcript through extraction TWICE:
  1. WITHOUT `(you)` marker  — Scott labeled as [Scott]
  2. WITH    `(you)` marker  — Scott labeled as [Scott (you)]

Then prints both patch sets side-by-side and flags the gating behavior.

Expected behavior:
  - No marker  → zero trait / preference / identity patches for Scott
  - With marker → trait / preference / identity patches allowed, attributed to Scott

Usage:
    CQ_LLM_API_KEY=sk-... python tests/benchmark/test_you_marker_gating.py [transcript_file]

If no transcript file is passed, uses a small embedded sample with clear
self-facts ("I'm the backend lead, I prefer Go over Rust") so the gating
behavior is easy to eyeball.
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
from extraction_schema import EXTRACTION_SCHEMA, enforce_you_marker_gate

# Types that should be suppressed when `(you)` marker is absent
SELF_TYPED = {"trait", "preference", "identity"}

OWNER_NAME = "Scott"

# Embedded transcript designed to exercise many patch/relationship types.
#
# Expected signals (with `(you)` marker present, for Scott):
#   SELF-TYPED (suppressed without marker):
#     - trait:       Scott is the backend lead
#     - identity:    Scott based in Boston
#     - preference:  Scott prefers async over meetings; Scott prefers Go over Rust
#     - role:        Scott as backend lead on payments
#   OTHERS (should appear regardless of marker):
#     - project:     Payments / checkout redesign
#     - person:      Alan (mobile lead, Dallas), Priya (PM)
#     - decision:    Use Stripe Connect over building in-house
#     - commitment:  Scott → write RFC by Friday;  Alan → ship iOS checkout v2 next sprint
#     - blocker:     Compliance review on Stripe Connect not scheduled
#     - takeaway:    Sandbox-only for first milestone (no prod traffic)
#   RELATIONSHIPS:
#     - Scott's RFC commitment  depends_on  compliance-review blocker
#     - Priya's scheduling action  resolves  compliance-review blocker
#     - Stripe Connect decision  informs  Alan's iOS commitment
#     - Alan's iOS commitment   parent of / belongs to  checkout redesign project
SAMPLE_TRANSCRIPT = """[Scott]  Morning everyone. Quick intros since Priya just joined.
[Scott]  I'm the backend lead on the payments project, based in Boston.
[Scott]  Fair warning, I prefer async over meetings — happy to move this to a doc if folks want.
[Scott]  For the record I'd rather debug in Go than Rust any day.
[Alan]  Hey, I'm Alan, mobile lead on the same project, working out of Dallas.
[Priya]  And I'm Priya, the PM — I'll be coordinating the checkout redesign.
[Priya]  Okay, the big question on the table: do we build the new payment flow in-house or use Stripe Connect?
[Scott]  I've looked at both. Stripe Connect saves us six months of compliance work. Let's go with Stripe Connect.
[Alan]  Agreed, that unblocks the iOS side too. I can ship iOS checkout v2 next sprint if the backend is ready.
[Priya]  One issue — we haven't scheduled the compliance review for Stripe Connect yet. That's a blocker.
[Priya]  I'll get compliance on the calendar this week.
[Scott]  Good. I'll write up the technical RFC by Friday, but I can't finalize it until compliance signs off.
[Alan]  My iOS work depends on Scott's RFC — I'll start scaffolding the client once it lands.
[Priya]  One more thing, for the first milestone we're staying in sandbox only — no production traffic yet.
[Scott]  Got it. Sandbox-only for M1, understood.
[Priya]  Thanks everyone — I'll send notes.
"""


def inject_you_marker(transcript: str, owner: str) -> str:
    """Replace every [OwnerName] with [OwnerName (you)] — same logic SS uses."""
    return transcript.replace(f"[{owner}]", f"[{owner} (you)]")


def summarize_patches(patches: list, label: str, you_flag=None) -> dict:
    """Return type counts and a flat list for printing."""
    types = Counter(p.get("type", "?") for p in patches)
    self_typed_count = sum(types[t] for t in SELF_TYPED)
    return {
        "label": label,
        "total": len(patches),
        "types": dict(types),
        "self_typed_count": self_typed_count,
        "patches": patches,
        "you_flag": you_flag,
    }


def print_patch_set(summary: dict):
    print(f"\n{'=' * 70}")
    print(f"  {summary['label']}")
    print(f"{'=' * 70}")
    print(f"  you_speaker_present: {summary.get('you_flag')}")
    print(f"  Total patches: {summary['total']}")
    print(f"  Type breakdown: {summary['types']}")
    print(f"  Self-typed (trait/preference/identity): {summary['self_typed_count']}")
    print()
    for p in summary["patches"]:
        ptype = p.get("type", "?")
        value = p.get("value", {})
        text = value.get("text", str(value)) if isinstance(value, dict) else str(value)
        owner = value.get("owner") if isinstance(value, dict) else None
        owner_str = f" (owner: {owner})" if owner else ""
        flag = " ⚠ SELF-TYPED" if ptype in SELF_TYPED else ""
        print(f"    [{ptype}]{flag} {text}{owner_str}")


async def extract(client: LLMClient, transcript: str) -> tuple[list, object]:
    """Return (patches, you_speaker_present_flag). Applies the same post-
    processing gate the worker uses in production."""
    result = await client.extract(
        system_prompt=MEETING_SUMMARY_SYSTEM,
        user_content=transcript,
        json_schema=EXTRACTION_SCHEMA,
    )
    enforce_you_marker_gate(result.content, transcript)
    return (
        result.content.get("patches", []),
        result.content.get("you_speaker_present", "<missing>"),
    )


MODELS = [
    "mistralai/mistral-small-3.1-24b-instruct",
    "openai/gpt-4o-mini",
    "anthropic/claude-haiku-4.5",
]


async def run_model(model: str, base_url: str, api_key: str, without_marker: str, with_marker: str):
    client = LLMClient(api_key=api_key, base_url=base_url, model=model)
    try:
        patches_off, flag_off = await extract(client, without_marker)
        patches_on, flag_on = await extract(client, with_marker)
    finally:
        await client.close()
    return (
        summarize_patches(patches_off, f"{model} — WITHOUT (you)", flag_off),
        summarize_patches(patches_on, f"{model} — WITH (you)", flag_on),
    )


async def main():
    api_key = os.getenv("CQ_LLM_API_KEY") or os.getenv("OPENROUTER_API_KEY")
    base_url = os.getenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")

    if not api_key:
        print("ERROR: Set CQ_LLM_API_KEY (or OPENROUTER_API_KEY)")
        sys.exit(1)

    # Load transcript
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            base_transcript = f.read()
        print(f"Loaded transcript: {sys.argv[1]} ({len(base_transcript)} chars)")
    else:
        base_transcript = SAMPLE_TRANSCRIPT
        print(f"Using embedded sample transcript ({len(base_transcript)} chars)")

    if f"[{OWNER_NAME}]" not in base_transcript:
        print(f"WARNING: transcript does not contain [{OWNER_NAME}] — injection will be a no-op.")

    without_marker = base_transcript
    with_marker = inject_you_marker(base_transcript, OWNER_NAME)
    inject_count = with_marker.count(f"[{OWNER_NAME} (you)]")
    print(f"Injected (you) marker in {inject_count} location(s).")
    print(f"Testing {len(MODELS)} model(s): {', '.join(MODELS)}\n")

    all_results = {}
    for model in MODELS:
        print(f"Running {model}...")
        try:
            off, on = await run_model(model, base_url, api_key, without_marker, with_marker)
            print_patch_set(off)
            print_patch_set(on)
            all_results[model] = {"off": off, "on": on}
        except Exception as e:
            print(f"  ERROR on {model}: {e}")
            all_results[model] = {"error": str(e)}

    # Comparison table
    print(f"\n{'=' * 78}")
    print("  GATING COMPARISON — self-typed (trait/preference/identity) patch counts")
    print(f"{'=' * 78}")
    header = f"  {'model':<42} {'off':>5} {'on':>5} {'delta':>6}  verdict"
    print(header)
    print("  " + "-" * 76)
    for model in MODELS:
        r = all_results.get(model, {})
        if "error" in r:
            print(f"  {model:<42}  ERROR: {r['error'][:40]}")
            continue
        off = r["off"]["self_typed_count"]
        on = r["on"]["self_typed_count"]
        delta = on - off
        if off == 0 and on > 0:
            verdict = "PASS"
        elif off > 0:
            verdict = "LEAK"
        elif off == 0 and on == 0:
            verdict = "OVER-SUPPRESS"
        else:
            verdict = "?"
        print(f"  {model:<42} {off:>5} {on:>5} {delta:>+6}  {verdict}")

    # Save all outputs
    out_path = os.path.join(os.path.dirname(__file__), "you_marker_gating_result.json")
    serializable = {}
    for model, r in all_results.items():
        if "error" in r:
            serializable[model] = r
        else:
            serializable[model] = {
                "without_marker": r["off"]["patches"],
                "with_marker": r["on"]["patches"],
                "off_self_typed": r["off"]["self_typed_count"],
                "on_self_typed": r["on"]["self_typed_count"],
            }
    with open(out_path, "w") as f:
        json.dump(serializable, f, indent=2)
    print(f"\nRaw patches saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
