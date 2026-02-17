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


# Context Quilt Project

See comprehensive documentation in:
- `llm-gateway-project.md` - Full technical specification
- `GETTING_STARTED.md` - Setup instructions
- `README.md` - Project overview

## Quick Context
This is Context Quilt - an LLM gateway with unified memory management.
Currently in MVP phase with FastAPI + OpenAI integration.

Next priorities:
1. Replace in-memory storage with PostgreSQL + Redis
2. Add proper authentication
3. Implement A/B testing framework
Recommended Next Commands