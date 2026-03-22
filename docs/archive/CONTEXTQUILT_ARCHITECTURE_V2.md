# ContextQuilt System Architecture v2.0

This document outlines the comprehensive architecture for the ContextQuilt Intelligent AI Gateway, incorporating all key inventions and design decisions. It serves as the foundation for the patent application and future development.

## 1. Core Concept & Mission

ContextQuilt is an Intelligent AI Gateway that provides a unified, low-latency, and persistent cognitive memory layer for all AI agents. Its mission is to solve the "goldfish memory" and "memory fragmentation" problems by acting as a "slide-in-place" layer that weaves together all siloed pieces of context into a single, coherent, and continuous "quilt."

## 2. Core Intellectual Property: The Inventions

Our defensibility comes from a series of interconnected, patentable inventions.

### Invention 1: "Zero-Latency" Asynchronous Architecture

This architecture decouples the read and write paths to provide context with no perceptible latency.

*   **The Synchronous "Fast Path":** This is the real-time path for an incoming user prompt.
    1.  The gateway receives the user's prompt.
    2.  It performs an ultra-fast lookup in **Working Memory** (Redis) to retrieve pre-computed, ready-to-use context (summaries, synthesized instructions).
    3.  It injects this context into the prompt and immediately forwards it to the downstream LLM.
    4.  The gateway's overhead on this path is limited to a single Redis lookup, ensuring near-zero added latency.

*   **The Asynchronous "Slow Path":** This path executes in the background, *after* the user has received their response.
    1.  The full conversation transcript (prompt, context, response) is sent to a job queue.
    2.  The **Asynchronous Cognitive Engine** (a background worker) picks up the job.
    3.  It performs all heavy computational tasks: communication profiling, summarization, fact extraction, etc.
    4.  It updates the persistent **Factual** and **Episodic** memory stores.
    5.  It pre-computes and caches the context for the *next* interaction in **Working Memory**.

### Invention 2: In-Prompt Markup Language

This provides a simple, declarative way for developers to interact with the gateway's features directly within the prompt string.

*   **Syntax:** The default syntax uses double square brackets, e.g., `[[cq.fact name='user_name']]`.
*   **User-Configurable Delimiters:** To avoid conflicts, developers can define their own custom delimiters (e.g., `##...##`) in their application settings. The gateway looks up the correct delimiter based on the developer's API key.
*   **Transparent Proxy Model:** Developers integrate by simply changing the API endpoint URL to point to the ContextQuilt gateway.
*   **BYOK (Bring Your Own Key):** The gateway uses the developer's own downstream LLM API keys, which are stored securely (encrypted). This keeps the developer in control of their billing relationship with LLM providers.
*   **In-Prompt Feature Control:** The markup includes "control tags" that allow developers to enable or disable gateway features on a per-request basis.
    *   `[[cq.control.style_matching.disable]]`: Turns off communication style matching.
    *   `[[cq.control.memory.disable_episodic]]`: Ignores conversational history for this request.
    *   `[[cq.control.style_matching.intensity value=4]]`: Overrides the default style matching intensity.

### Invention 3: Proactive, Application-Scoped Cache Warming

This is a mechanism to proactively "warm" the Working Memory cache to solve the "cold start" problem for new sessions.

*   **The "Hint" Trigger:** A developer's application can send a non-blocking "hint" request to a `/warm-cache` endpoint, containing the `user_id` and `application_id`.
*   **Scoped Pre-Loading:** The gateway receives this hint and kicks off a background process to load relevant context from persistent memory into the high-speed Working Memory cache. The context is filtered by `application_id` to ensure relevance.
*   **Optional Intent:** The hint can optionally include an `intent` or `topics` to further refine the context that gets loaded.

### Invention 4: Automated Communication Profiling & Style Matching

The system automatically learns a user's communication style to enable hyper-personalized AI responses.

*   **The User Communication Trait Profile:** A detailed JSON object that captures a user's style. It is generated and updated asynchronously by the Cognitive Engine. Each trait includes a `value` and a `confidence` score.
*   **Comprehensive Schema:** The profile includes:
    *   **Structural Traits:** `formality`, `verbosity`, `sentence_complexity`, `punctuation_style`.
    *   **Semantic Traits:** `directness`, `expressed_confidence`, `question_ratio`, `profanity_usage`.
    *   **Lexical Profile:** `jargon_density` and a `vocabulary` list of characteristic slang, idioms, emojis, etc.
*   **Separation of "State" vs. "Trait":** The long-term profile only stores stable "traits." Temporary emotional "states" like `current_sentiment` are stored in a separate, session-specific `Situational Context` object in Working Memory.
*   **Tunable Style Matching:** A developer can control the *intensity* of style matching (`style_matching_intensity: 0-5`) on a per-request basis.
*   **Developer-Controlled Policies:** Developers can set application-level policies, such as a `profanity_handling_policy` (`strict_censor`, `ignore`, `allow_mirroring`).

## 3. System Components & Implementation Details

### 3.1. Memory Layers (Logical)

*   **Working Memory:** A high-speed cache (Redis) for short-TTL, in-session context and pre-synthesized instructions.
*   **Episodic Memory:** A graph database for storing the history and relationships of conversations over time.
*   **Factual Memory:** A key-value or SQL database for storing explicit user facts, preferences, and the `User Communication Trait Profile`.

### 3.2. Asynchronous Cognitive Engine (Model Strategy)

The engine uses a "mixture of experts" approach with small, self-hostable models for cost-effective processing.

*   **NER (Named Entity Recognition):** `spaCy`.
*   **Fact & Preference Extraction:** A two-stage process: a small classifier (e.g., `MiniLM`) to detect significance, followed by a call to `Phi-3-mini` with a structured prompt to extract the JSON object.
*   **Communication Profiling & Summarization:** `Phi-3-mini`, a flexible and capable small language model.

### 3.3. Memory Lifecycle & Decay

*   **Decay Function:** The `User Communication Trait Profile` is kept current using a time-based decay function, implemented as a **moving average over the last 'N' interactions**. This ensures the profile adapts to changes in a user's style over time. 'N' is a configurable parameter.
*   **Event-Based Invalidation:** The system also supports event-based invalidation for Factual Memory (e.g., a "new address" event marks the old address as outdated).

## 4. Enterprise Features

### 4.1. LLM Observability & Analytics Dashboard

This feature provides enterprise customers with deep insights into their LLM usage, cost, and performance.

*   **Metrics Tracked:** The gateway logs detailed metrics for every call, including latency (inbound, LLM, total), token counts (input, output), estimated cost, cache hit rates, and error codes, all filterable by `user_id` and `application_id`.
*   **MVP Technology Stack:**
    *   **Data Collection:** `OpenTelemetry` for standardized, vendor-neutral data export.
    *   **Data Storage:** `Prometheus` for metrics and `Loki` for logs.
    *   **Dashboarding:** `Grafana` for a powerful, no-code, embeddable dashboard experience. This stack is fully open-source and can be bundled into the enterprise appliance.
