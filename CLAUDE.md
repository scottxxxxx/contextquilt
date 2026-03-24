# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ContextQuilt** is an Intelligent AI Gateway that provides a unified, low-latency, persistent cognitive memory layer for AI agents. It solves the "goldfish memory" problem (stateless LLMs) and "memory fragmentation" problem (siloed AI platforms) by acting as a slide-in-place layer between applications and LLM providers.

## Core Architecture Principles

### The "Zero-Latency" Asynchronous Architecture

The system uses a decoupled read/write path design:

- **Read Path (Synchronous)**: Live LLM calls query an ultra-fast in-memory "Working Memory" cache (Redis) for instant context injection with zero added latency
- **Write Path (Asynchronous)**: Background workers perform expensive cognitive consolidation (summarizing, extracting facts) after the user receives their response

**Critical**: Any implementation must maintain this separation. Never block the synchronous read path with expensive operations.

### The "Hybrid Cognitive" Data Model ("The Quilt")

The system mimics human cognition through three memory types:

1. **Factual Memory** (K/V or SQL): Explicit user preferences and facts (e.g., "I don't like coffee")
2. **Episodic Memory** (Graph Database): Relationships between entities, events, and users using GraphRAG. This is the key differentiator - stores "threads" that connect context across conversations
3. **Working Memory** (Redis Cache): Short-TTL, in-session scratchpad context

**Implementation Note**: The episodic memory graph is what makes this more than a RAG system - it preserves relationship context across platform boundaries.

### Hub-and-Spoke RAG Integration

ContextQuilt acts as a central orchestrator (hub) with pluggable vector databases (spokes):

- Core system provides cognitive/episodic memory
- Plugin API allows any vector DB (Pinecone, Weaviate, Qdrant, etc.) to integrate as semantic memory
- The system doesn't compete with RAG; it orchestrates and enhances it

## Product Structure

### ContextQuilt Core (Open Source)
- Apache 2.0 licensed
- Core gateway features and RAG plugin framework
- Intended as a Python library (`contextquilt-py`)

### ContextQuilt Enterprise (Commercial)
- Self-hosted Docker appliance
- Adds RBAC, PII redaction, audit trails, management dashboards
- Cross-platform sync for enterprise memory fragmentation solution

## Key Technical Concepts

### Active Enrichment Methods
- Context compression (ACON, LLMLingua-2) to reduce token costs
- A/B testing capabilities at the gateway level
- Cost-based throttling and routing

### Cross-Platform Memory Sync
The "neutrality moat" - enables memory continuity across siloed AI platforms (AWS, OpenAI, ServiceNow, etc.)

## Development Considerations

### Patent Protection
This project has provisional patent protection for:
- The asynchronous zero-latency architecture
- The hybrid cognitive data model
- Specific active enrichment methods

When implementing or modifying core architectural components, preserve the novel aspects of these methods.

### Performance Requirements
- **Latency**: Synchronous read path must add minimal latency (target: <10ms overhead)
- **Scalability**: Design for high-throughput enterprise workloads
- **Memory Efficiency**: Working memory should use aggressive TTL and eviction policies

### Integration Patterns
When building integrations:
- Gateway sits between application and LLM provider
- Must be "slide-in-place" - minimal application changes required
- Support multiple LLM providers (OpenAI, Anthropic, AWS Bedrock, etc.)
- Maintain provider neutrality


# Documentation

## V3 Architecture (Current)
See `docs/architecture/` for the complete V3 specification:
- `00-overview.md` — What CQ is, core concepts
- `01-memory-model.md` — Three tiers, graph layer, entities/relationships
- `02-pipeline.md` — Extraction pipeline, four roles, model selection
- `03-queue-and-lifecycle.md` — Meeting queue, batching, context budgeting
- `04-recall.md` — Intelligent recall, entity matching, hot path
- `05-integration.md` — CloudZap/ShoulderSurf flow, capture points, metadata
- `06-configuration.md` — All settings, env vars, admin dashboard
- `07-api-reference.md` — Complete API endpoint documentation

## Other Resources
- `GETTING_STARTED.md` — Setup instructions
- `README.md` — Project overview
- `docs/sales/` — Sales and marketing materials
- `docs/archive/` — Previous architecture versions (V1, V2, 3.7-3.10)

## Current Status (March 24, 2026)

**Live deployment:** `https://cq.shouldersurf.com`
**Admin dashboard:** `https://cq.shouldersurf.com/dashboard/` (protected by CQ_ADMIN_KEY)
**GCP VM:** `35.239.227.192` (shared with CloudZap via Project Bifrost)
**Version:** 3.10.0

### What's built and deployed:
- PostgreSQL + Redis on GCP VM
- Cold path worker using Mistral Small 3.1 via OpenRouter ($0.00009/extraction)
- Graph memory layer — entities and relationships extracted and stored
- `POST /v1/recall` — entity matching + graph traversal, returns context block
- `GET/PATCH/DELETE /v1/quilt/{user_id}` — user quilt CRUD with ACL
- Meeting queue with 60-min batching and context budget triggers
- Generic metadata system (meeting_id, project, any key-value pairs)
- Admin dashboard with login gate (CQ_ADMIN_KEY)
- Provider-agnostic LLM client (OpenRouter, OpenAI, Gemini, Ollama, etc.)
- User profile identity (display_name, email) via metadata passthrough
- Smart persistence: identity/preference/trait facts are `sticky`, experience/action items are `decaying`
- Relevance-filtered extraction prompts to reduce noise

### CloudZap integration:
- CloudZap calls `/v1/recall` before LLM queries when `context_quilt: true`
- CloudZap POSTs query+response to `/v1/memory` after LLM responds (async)
- Response headers `X-CQ-Matched` and `X-CQ-Entities` for iOS UI indicator
- Can pass `display_name` and `email` in metadata to populate user profiles

### Next priorities:
1. Update CloudZap to pass display_name/email in metadata from Apple Sign-In
2. End-to-end test: ShoulderSurf → CloudZap → CQ → graph → recall
3. Build CQ indicator UI in ShoulderSurf response bubbles
4. Settings page in admin dashboard for runtime config changes
5. Implement active decay scoring in recall path (weight by recency + access count)

## Related Projects

- **CloudZap** (`/Users/scottguida/cloudzap/`) — LLM gateway, CQ integration point
- **ShoulderSurf** (`/Users/scottguida/ShoulderSurf/`) — iOS meeting copilot, first CQ consumer
- **Project Bifrost** (`/Users/scottguida/bifrost/`) — Nginx Proxy Manager on shared GCP VM