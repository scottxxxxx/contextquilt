# 02: The Extraction Pipeline

## Overview

The extraction pipeline is the cold path process that transforms raw content (meeting summaries, queries, responses) into structured knowledge: facts, entities, relationships, action items, and communication profiles.

## Pipeline Modes

CQ supports two pipeline modes, selectable via configuration:

### Single-Call Mode (Default)

One LLM call extracts everything — facts, action items, entities, relationships, communication profile, and episode summary. This is simpler, cheaper, and in benchmarks produced the best results.

**When to use:** Most deployments. The default model (Mistral Small 3.1) handles all four cognitive functions well in a single prompt.

**Cost:** ~$0.00008-0.00014 per extraction.

### Multi-Role Mode (Enterprise)

Four separate LLM calls, each with a specialized prompt and optionally a different model:

| Role | What it does | Best model type |
|------|-------------|-----------------|
| **Picker** | Extracts raw facts and action items from text | Fast, good at structured extraction |
| **Stitcher** | Organizes facts into a user profile schema | Good at classification and schema adherence |
| **Designer** | Analyzes communication patterns and builds a behavioral profile | Needs nuance for behavioral analysis |
| **Cataloger** | Creates a high-level episode summary | Simple summarization, cheapest model |

**When to use:** When an enterprise customer wants to:
- Use a fine-tuned model for one role (e.g., a custom Designer trained on their industry's communication patterns)
- Run one role on-premise (e.g., Picker on a local model because data can't leave the network)
- Optimize cost by using smaller models for simpler roles

**Context window consideration:** In multi-role mode, CQ uses the smallest context window across all configured models as the queue budget. This ensures the queued content fits every model in the pipeline.

## The Four Cognitive Roles

### The Picker

**Purpose:** Extract concrete facts, action items, entities, and relationships from raw text.

**Input:** Meeting summary, transcript chunk, or query+response pair.

**Output:**
```json
{
  "facts": [{"fact": "...", "category": "identity|preference|trait|experience", "participants": ["..."]}],
  "action_items": [{"action": "...", "owner": "...", "deadline": "..."}],
  "entities": [{"name": "...", "type": "person|project|company|...", "description": "..."}],
  "relationships": [{"from": "...", "to": "...", "type": "...", "context": "..."}]
}
```

**Rules:**
- Every fact must be grounded in the source text — no inference
- Entity names must be exact as mentioned
- Relationships must reference entities from the entities list
- Capture decisions, commitments, deadlines, and constraints

### The Stitcher

**Purpose:** Organize extracted facts into a structured user profile.

**Input:** The Picker's output (list of facts).

**Output:**
```json
{
  "identity_facts": {"job_title": "Tech Lead", ...},
  "preference_facts": {"offline_mode": "critical", ...},
  "task_facts": {"current_project": "Widget 2.0", ...},
  "constraints_facts": {"budget": "$150,000", ...}
}
```

**Rules:**
- Use semantic keys (not generic like "fact_1")
- Omit empty categories
- Do not invent data not in the input

### The Designer

**Purpose:** Analyze communication patterns to build a behavioral profile.

**Input:** The original text (not the Picker's output — needs raw language to analyze style).

**Output:**
```json
{
  "communication_profile": {
    "verbosity": 0.6,
    "technical_level": 0.7,
    "directness": 0.8,
    "formality": 0.6,
    "warmth": 0.4,
    "detail_orientation": 0.8
  }
}
```

**Scoring:** Each trait is 0.0-1.0. The profile is updated as a moving average over the last N interactions, not replaced each time.

### The Cataloger

**Purpose:** Summarize the episode at a high level for long-term memory.

**Input:** The original text.

**Output:**
```json
{
  "episode_summary": "Team aligned on Widget 2.0 scope with offline as core and real-time collab as beta.",
  "goal": "project_kickoff",
  "outcome": "action_completed",
  "domain": "software development"
}
```

**Rules:**
- Focus high level — do not extract specific facts (the Picker handles that)
- No PII in the summary

## Model Selection

### Default Configuration

CQ ships with a default model configured via environment variables:

```
CQ_LLM_API_KEY=...
CQ_LLM_BASE_URL=https://openrouter.ai/api/v1
CQ_LLM_MODEL=mistralai/mistral-small-3.1-24b-instruct
```

This model is used for all pipeline roles in single-call mode.

### Multi-Role Configuration

In multi-role mode, each role can have its own model:

```
CQ_PIPELINE_MODE=multi_role
CQ_PICKER_MODEL=mistralai/mistral-small-3.1-24b-instruct
CQ_PICKER_BASE_URL=https://openrouter.ai/api/v1
CQ_STITCHER_MODEL=qwen/qwen-turbo
CQ_DESIGNER_MODEL=qwen/qwen3-14b
CQ_CATALOGER_MODEL=cohere/command-r7b-12-2024
```

If a role's model is not specified, it falls back to the default `CQ_LLM_MODEL`.

### Supported Providers

Any OpenAI-compatible API works:
- **OpenRouter** — access to 200+ models via one API key
- **OpenAI** — GPT-4.1-nano, GPT-4o-mini, GPT-5.4-nano
- **Google Gemini** — via OpenAI-compatible endpoint
- **Ollama** — local models, no API key needed
- **vLLM / LiteLLM / LM Studio** — self-hosted endpoints

### Benchmark Results

Benchmarked across 8 models on 5 meeting summary test cases (March 2026):

| Model | Fact Accuracy | Action Accuracy | Cost/extraction |
|-------|-------------|----------------|-----------------|
| Mistral Small 3.1 | 90% | 80% | $0.00009 |
| gpt-5.4-nano | 87% | 70% | $0.00095 |
| Qwen Turbo | 84% | 73% | $0.00008 |
| Gemini 2.5 Flash-Lite | 75% | 93% | $0.00031 |
| gpt-4.1-nano | 73% | 80% | $0.00027 |

Mistral Small 3.1 selected as default for best accuracy-to-cost ratio.
