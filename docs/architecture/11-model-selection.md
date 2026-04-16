# 11: Model Selection

## Current Default: Claude Haiku 4.5

`anthropic/claude-haiku-4.5` via OpenRouter.

Selected after two rounds of benchmarking (March-April 2026). The key differentiator was quality on real-world messy transcripts, especially correct handling of the `(you)` speaker marker for trait attribution.

**Cost:** ~$0.029 per meeting extraction.

## Why Haiku Over Cheaper Models

The initial benchmark (March 2026, `tests/benchmark/results.json`) tested 8 models on 5 clean meeting summary test cases. Mistral Small 3.1 scored highest on fact accuracy (90%) at the lowest cost ($0.00009/extraction) and was briefly the default.

However, a second round of testing (`tests/benchmark/test_you_marker_gating.py`) evaluated models on the `(you)` speaker marker — the convention CQ uses to identify the app user in diarized transcripts. This is critical for correct trait attribution (attributing facts to the right person). On real messy transcripts, Haiku outperformed:

- **Correct `(you)` gating** — reliably distinguishes between "user said X" vs "someone else said X"
- **Robust on messy input** — handles diarization artifacts, partial sentences, overlapping speakers
- **Consistent structured output** — fewer JSON schema violations than cheaper models

## Alternatives Tested

All models below pass the `(you)` marker gating test but have trade-offs:

| Model | Notes | Approximate Cost |
|-------|-------|-----------------|
| **anthropic/claude-haiku-4.5** | **Default.** Best quality on real transcripts | ~$0.029/meeting |
| mistralai/mistral-small-3.1-24b-instruct | Cheapest, but slower and quality quirks on messy data | ~$0.0006/meeting |
| google/gemini-2.5-flash | Good quality, ~2x cost of Haiku | ~$0.06/meeting |
| google/gemini-2.5-flash-lite | Fastest (~6s), but over-extracts self-typed facts on real data | ~$0.015/meeting |
| deepseek/deepseek-chat-v3-0324 | Decent quality, very slow (~60s per extraction) | ~$0.01/meeting |
| openai/gpt-4o-mini | Adequate, higher cost for comparable quality | ~$0.03/meeting |

## Benchmark Artifacts

- `tests/benchmark/results.json` — Round 1: 8 models x 5 clean test cases (March 2026)
- `tests/benchmark/test_you_marker_gating.py` — Round 2: `(you)` marker attribution test across 6 models
- `tests/benchmark/edge_case_results.json` — Edge case extraction tests
- `tests/benchmark/project_classification_results.json` — Patch type classification tests

## Changing the Default

Set the environment variable:

```
CQ_LLM_MODEL=your-preferred/model-id
```

See `env.example` for provider-specific configuration examples (OpenRouter, OpenAI direct, Gemini direct, Ollama local).
