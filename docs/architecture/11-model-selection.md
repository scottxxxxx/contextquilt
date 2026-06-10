# 11: Model Selection

## Current Default: Claude Haiku 4.5 (direct via Anthropic, OpenRouter as fallback)

`claude-haiku-4-5-20251001` direct against `api.anthropic.com`, with OpenRouter (`anthropic/claude-haiku-4.5`) as a narrow-allowlist fallback for transient auth, rate-limit, 5xx, or network failures.

Selected after two rounds of benchmarking (March-April 2026). The key differentiator was quality on real-world messy transcripts, especially correct handling of the `(you)` speaker marker for trait attribution. The Anthropic-direct wire path landed June 2026 (PRs #120 / #121 / #124 / #126) to avoid the OpenRouter markup and gain operator-controlled key rotation via the dashboard Providers tab. See [06-configuration.md](06-configuration.md) for the env vars and Secret Manager bootstrap.

**Cost:** $1.00 per 1M input tokens, $5.00 per 1M output tokens (Anthropic list pricing). ~$0.029 per meeting extraction at typical sizes, same as before but without the ~5% OpenRouter markup.

**Failure routing:** Anthropic primary path; falls back to OpenRouter on `httpx.TimeoutException`, `httpx.NetworkError`, or HTTP `401`/`403`/`429`/`5xx` from the Anthropic API. `400`/`422` and JSON parse errors pass through (they indicate request-shape bugs that the fallback would also hit). Every fallback fires an `anthropic_fallback_to_or` operator alert with 30-min dedup.

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

### Switching the Anthropic-native model

The Anthropic-direct primary path is controlled by:

```
CQ_ANTHROPIC_MODEL=claude-sonnet-4-6        # or claude-opus-4-7, etc.
```

Note that this is the bare native Anthropic model ID, not the OpenRouter slash form.

### Switching the OpenRouter fallback model

The OpenRouter fallback wrapper translates the native ID into the slash form automatically for the models in `_OR_MODEL_TRANSLATION` (`llm_client_fallback.py`). To override:

```
CQ_LLM_MODEL=your-preferred/model-id
```

### Switching providers entirely

To flip back to OpenRouter as primary (no Anthropic-direct path), set:

```
CQ_LLM_PRIMARY_PROVIDER=openrouter
```

This skips building the AnthropicLLMClient and runs OpenRouter-only with no fallback wrapper. Useful for tests, regression rollback, or any env without an Anthropic key.

See `env.example` for full provider configuration examples (Anthropic direct, OpenRouter, OpenAI direct, Gemini direct, Ollama local).
