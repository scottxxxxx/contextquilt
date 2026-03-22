# Context Quilt: Architecture Overview (V3)

**Version:** 3.0
**Date:** March 22, 2026
**Status:** Active Development

## What is Context Quilt?

Context Quilt is a persistent cognitive memory layer for AI applications. It sits between applications and their LLM providers, automatically learning from conversations and injecting relevant context into future interactions.

It solves two problems:

1. **The Goldfish Memory Problem** — LLMs are stateless. Every conversation starts from zero. Context Quilt remembers what happened in previous interactions and surfaces it when relevant.

2. **The Memory Fragmentation Problem** — Users interact with AI across multiple platforms (Slack, email, meeting tools, coding assistants). Each platform forgets everything the moment you leave. Context Quilt unifies memory across all of them.

## The Quilt Metaphor

The system is called a "quilt" because it weaves together different types of context into a coherent whole:

- **Patches** are individual pieces of knowledge — a fact, a preference, an action item, a relationship between entities
- **Stitching** is the graph layer that connects patches — Bob works on Widget 2.0, Widget 2.0 has a deadline from Acme Corp, Acme Corp's CTO is David Chen
- **The Quilt** is the complete, interconnected picture of what the system knows about a user's world

A pile of disconnected facts is not a quilt. The value comes from the connections.

## Core Architecture Principles

### 1. Zero-Latency Asynchronous Architecture

The system decouples read and write paths:

- **Read Path (Hot, Synchronous):** When an app needs context, CQ serves it from a pre-computed cache (Redis). No LLM calls. Target: <10ms overhead.
- **Write Path (Cold, Asynchronous):** After the user receives their response, CQ processes the interaction in the background — extracting facts, building entity relationships, updating the graph. This can take seconds or minutes; the user never waits.

This separation is a core architectural invariant. Never block the read path with expensive operations.

### 2. Provider-Agnostic Design

Context Quilt does not prescribe:
- Which LLM provider the app uses (OpenAI, Anthropic, Google, local models)
- How the app authenticates users (Apple, Google, email, SSO)
- What the app does (meeting copilot, customer support, coding assistant)
- What metadata the app attaches to interactions (project names, ticket IDs, session tags)

CQ accepts generic metadata as key-value pairs. The app defines the schema; CQ stores and queries it.

### 3. Intelligent, Not Prescriptive

CQ figures out what's relevant from the text it receives. Apps don't need to format requests in a specific way, tag entities, or specify what context to retrieve. CQ reads the incoming text, recognizes entities it knows about, traverses its graph, and returns relevant context.

Apps that want explicit control can still use template-based enrichment (`[[placeholders]]`). But the default path is: send text, get context back.

## Product Structure

### Context Quilt Core (Open Source, Apache 2.0)
- Core API and memory layer
- Extraction pipeline with pluggable models
- Graph memory and recall
- Generic metadata system

### Context Quilt Enterprise (Commercial)
- RBAC and multi-tenant isolation
- PII redaction
- Audit trails
- Management dashboards
- Cross-platform memory sync

## Patent Protection

This project has provisional patent protection for:
- The asynchronous zero-latency architecture
- The hybrid cognitive data model (the quilt)
- Specific active enrichment methods

When modifying core architectural components, preserve the novel aspects of these methods.

## Document Index

| Document | What it covers |
|----------|---------------|
| [01-memory-model.md](01-memory-model.md) | Three memory tiers, graph layer, entities and relationships |
| [02-pipeline.md](02-pipeline.md) | Extraction pipeline, four cognitive roles, model selection |
| [03-queue-and-lifecycle.md](03-queue-and-lifecycle.md) | Meeting queue, batching, context budget, triggers |
| [04-recall.md](04-recall.md) | How recall works, entity matching, hot path context injection |
| [05-integration.md](05-integration.md) | CloudZap/ShoulderSurf flow, capture points, metadata |
| [06-configuration.md](06-configuration.md) | All settings, environment variables, admin dashboard |
| [07-api-reference.md](07-api-reference.md) | Complete API endpoint documentation |
