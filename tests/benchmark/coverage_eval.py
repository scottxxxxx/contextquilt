#!/usr/bin/env python3
"""Extraction coverage eval — recall against known-answer fixtures.

Measures what the extraction pipeline MISSES: each fixture pairs a
transcript with an expected-memories list, and the eval scores recall
per type plus deadline-resolution accuracy. Built 2026-07-30 after the
traffic-era audit showed systematic under-extraction (patch budgets +
prompt self-limiting) that model choice does not fix.

Match rule (deterministic, no LLM judge): an expected item matches an
extracted patch when the type matches (`type` or one of `alt_types`),
every keyword appears case-insensitively in value.text, and — when the
expectation pins them — owner_contains appears in value.owner and
deadline_date equals value.deadline_date exactly. Each extracted patch
satisfies at most one expectation (greedy, expectation order).

Usage (needs an Anthropic key — env CQ_ANTHROPIC_API_KEY, or run inside
the prod container where Secret Manager provides it):

    python tests/benchmark/coverage_eval.py                    # all fixtures
    python tests/benchmark/coverage_eval.py --fixture stillwater_dense
    python tests/benchmark/coverage_eval.py --model claude-sonnet-5
    python tests/benchmark/coverage_eval.py --prompt legacy    # MEETING_SUMMARY_SYSTEM
    python tests/benchmark/coverage_eval.py --cap 12           # simulate worker cap

The default prompt is generated from the repo's SS manifest fixture
(init-db/11_shouldersurf_schema.json) via schema_prompt_builder — the
same path prod uses, minus DB round-trip. Raw model output is scored
first; --cap additionally scores the post-truncation list so the two
loss lanes (model self-limiting vs worker cap) are visible separately.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from src.contextquilt.services.llm_client_anthropic import AnthropicLLMClient  # noqa: E402
from src.contextquilt.services.schema_prompt_builder import build_prompt  # noqa: E402
from src.contextquilt.services.extraction_prompts import MEETING_SUMMARY_SYSTEM  # noqa: E402
from src.contextquilt.services import deadline_resolver  # noqa: E402

COVERAGE_DIR = Path(__file__).parent / "coverage"
MANIFEST_PATH = REPO / "init-db" / "11_shouldersurf_schema.json"


def load_fixtures(only: str | None):
    fixtures = []
    for exp_path in sorted(COVERAGE_DIR.glob("*.expected.json")):
        name = exp_path.name.replace(".expected.json", "")
        if only and name != only:
            continue
        spec = json.loads(exp_path.read_text())
        t_ref = spec.get("transcript") or f"{name}.txt"
        t_path = (exp_path.parent / t_ref).resolve()
        fixtures.append({
            "name": name,
            "transcript": t_path.read_text(),
            "meeting_date": spec["meeting_date"],
            "expected": spec["expected"],
        })
    return fixtures


def parse_json(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except Exception:
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            try:
                return json.loads(text[s:e + 1])
            except Exception:
                return None
    return None


def matches(exp: dict, patch: dict) -> bool:
    ptype = patch.get("type", "")
    ok_types = [exp["type"]] + list(exp.get("alt_types") or [])
    if ptype not in ok_types:
        return False
    value = patch.get("value") or {}
    text = (value.get("text") or "").lower()
    if not all(k.lower() in text for k in exp.get("keywords", [])):
        return False
    oc = exp.get("owner_contains")
    if oc and oc.lower() not in (value.get("owner") or "").lower():
        return False
    dd = exp.get("deadline_date")
    if dd and value.get("deadline_date") != dd:
        return False
    return True


def score(expected: list, patches: list) -> dict:
    claimed = set()
    hits, misses = [], []
    for exp in expected:
        found = None
        for i, p in enumerate(patches):
            if i in claimed:
                continue
            if matches(exp, p):
                found = i
                break
        if found is not None:
            claimed.add(found)
            hits.append(exp["id"])
        else:
            # Diagnose NEAR misses: right type+keywords but a pinned
            # field failed — that's a resolution miss, not an
            # extraction miss.
            near = None
            relaxed = {k: v for k, v in exp.items()
                       if k not in ("deadline_date", "owner_contains")}
            for i, p in enumerate(patches):
                if i in claimed:
                    continue
                if matches(relaxed, p):
                    v = p.get("value") or {}
                    near = {"got_deadline_date": v.get("deadline_date"),
                            "got_owner": v.get("owner")}
                    break
            misses.append({"id": exp["id"], "near": near})
    dd_expected = [e for e in expected if e.get("deadline_date")]
    dd_hit = [e for e in dd_expected if e["id"] in hits]
    return {
        "recall": f"{len(hits)}/{len(expected)}",
        "recall_pct": round(100 * len(hits) / max(1, len(expected))),
        "deadline_resolution": f"{len(dd_hit)}/{len(dd_expected)}",
        "unmatched_extracted": len(patches) - len(claimed),
        "misses": misses,
    }


async def run_eval(args):
    if args.prompt == "legacy":
        system_prompt = MEETING_SUMMARY_SYSTEM
    else:
        system_prompt = build_prompt(json.loads(MANIFEST_PATH.read_text()))

    client = AnthropicLLMClient(model=args.model) if args.model else AnthropicLLMClient()
    results = []
    try:
        for fx in load_fixtures(args.fixture):
            from datetime import date as _date
            md = _date.fromisoformat(fx["meeting_date"])
            user = f"Meeting date: {md.isoformat()} ({md.strftime('%A')})\n\n{fx['transcript']}"
            body = {
                "model": client.model,
                "max_tokens": args.max_tokens,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user}],
            }
            # Mirror the live client's per-model request shape.
            from src.contextquilt.services.llm_client_anthropic import _model_rejects_sampling
            if _model_rejects_sampling(client.model):
                if not client.model.startswith("claude-fable"):
                    body["thinking"] = {"type": "disabled"}
            else:
                body["temperature"] = 0.1
            start = time.monotonic()
            resp = await client._client.post("/v1/messages", json=body)
            latency = time.monotonic() - start
            resp.raise_for_status()
            data = resp.json()
            text = "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
            content = parse_json(text)
            if content is None:
                results.append({"fixture": fx["name"], "error": "JSON parse failed"})
                continue
            patches = content.get("patches") or []
            if args.micropass and patches:
                items = deadline_resolver.collect_deadline_items(patches)
                if items:
                    msys, muser = deadline_resolver.build_micropass_prompt(md, items)
                    mbody = {"model": client.model, "max_tokens": 1500,
                             "system": msys,
                             "messages": [{"role": "user", "content": muser}]}
                    if _model_rejects_sampling(client.model):
                        if not client.model.startswith("claude-fable"):
                            mbody["thinking"] = {"type": "disabled"}
                    else:
                        mbody["temperature"] = 0.1
                    mresp = await client._client.post("/v1/messages", json=mbody)
                    mresp.raise_for_status()
                    mtext = "".join(b.get("text", "") for b in mresp.json().get("content", [])
                                    if b.get("type") == "text")
                    res = deadline_resolver.parse_micropass_response(mtext)
                    if res:
                        deadline_resolver.apply_resolutions(
                            patches, res, md, {i for i, _, _ in items})
            row = {"fixture": fx["name"], "model": client.model,
                   "patches_emitted": len(patches),
                   "latency_s": round(latency, 1)}
            row["raw"] = score(fx["expected"], patches)
            if args.cap:
                row[f"capped_at_{args.cap}"] = score(fx["expected"], patches[:args.cap])
            results.append(row)
    finally:
        await client._client.aclose()
    print(json.dumps(results, indent=2, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", help="run one fixture by name")
    ap.add_argument("--model", help="override model id")
    ap.add_argument("--prompt", choices=["manifest", "legacy"], default="manifest")
    ap.add_argument("--cap", type=int, help="also score after truncating to N patches")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--micropass", action="store_true", help="run the deadline micro-pass after extraction")
    args = ap.parse_args()
    if not (os.environ.get("CQ_ANTHROPIC_API_KEY") or os.environ.get("CQ_GCP_PROJECT")):
        sys.exit("Need CQ_ANTHROPIC_API_KEY (or run where Secret Manager is configured)")
    asyncio.run(run_eval(args))


if __name__ == "__main__":
    main()
