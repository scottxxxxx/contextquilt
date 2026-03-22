# Context Quilt: Technical Architecture Document
## Automatic Context Memory Infrastructure for LLM Applications

**Version:** 1.0  
**Date:** November 18, 2025  
**Status:** Architecture Proposal - Peer Review

---

## Executive Summary

Context Quilt is a drop-in context-as-a-service layer that sits between applications and LLM providers, automatically managing conversation history, user preferences, and long-term memory. Developers simply change their API endpoint and provide a `user_id` - Context Quilt handles the rest.

**Core Value Proposition:**
- Zero state management required in application code
- Automatic memory persistence across sessions
- Intelligent context injection with sub-100ms latency
- Cross-session continuity (recall from weeks prior)

**Key Differentiator:** Unlike RAG systems that retrieve external documents, Context Quilt creates and manages *internal experiential memory* - making every interaction smarter by understanding the complete user journey.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Solution Architecture](#2-solution-architecture)
3. [Three-Tier Memory System](#3-three-tier-memory-system)
4. [Automatic Detection & Classification](#4-automatic-detection--classification)
5. [Communication Profile System](#5-communication-profile-system)
6. [Latency Optimization Strategy](#6-latency-optimization-strategy)
7. [API Design](#7-api-design)
8. [Implementation Recommendations](#8-implementation-recommendations)
9. [Data Flow Architecture](#9-data-flow-architecture)
10. [Security & Privacy Considerations](#10-security--privacy-considerations)
11. [Competitive Positioning](#11-competitive-positioning)
12. [Success Metrics](#12-success-metrics)

---

## 1. Problem Statement

### Current Developer Pain Points

**Without Context Quilt:**
```javascript
// Developer must manually manage context
const response = await openai.chat.completions.create({
  model: "gpt-4",
  messages: [
    // Must manually include all previous messages
    { role: "user", content: "My order hasn't arrived" },
    { role: "assistant", content: "What's your order number?" },
    { role: "user", content: "ORDER-12345" },
    { role: "assistant", content: "Let me check..." },
    { role: "user", content: "Any update?" }  // 5th message
  ]
});
// Problems:
// ❌ Context grows linearly - token costs explode
// ❌ No persistence across sessions
// ❌ No way to recall information from days/weeks ago
// ❌ Complex state management in application
// ❌ Lost context when user switches devices
```

**With Context Quilt:**
```javascript
// Developer sends only new message
const response = await fetch('https://api.contextquilt.com/v1/chat', {
  method: 'POST',
  headers: { 'Authorization': 'Bearer API_KEY' },
  body: JSON.stringify({
    user_id: "customer_789",  // ← Only additional field needed
    application: "support_chatbot",
    messages: [
      { role: "user", content: "Any update?" }  // Just the new message
    ]
  })
});
// Benefits:
// ✅ Full context automatically maintained
// ✅ Persists across sessions
// ✅ Recalls context from weeks ago
// ✅ Zero state management needed
// ✅ Optimized token usage
```

---

## 2. Solution Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client Application                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             │ POST /v1/chat
                             │ {user_id, messages}
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Context Quilt Gateway                         │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              HOT PATH (<50ms total)                       │  │
│  │                                                            │  │
│  │  1. Receive Request                                       │  │
│  │  2. Lookup Cache (Redis) ────────────> [Redis Cache]     │  │
│  │  3. Assemble Context                   - Working Memory   │  │
│  │  4. Inject into Prompt                 - Profile Cache    │  │
│  │  5. Forward to LLM ─────────────────> [OpenAI/Anthropic] │  │
│  │  6. Stream Response                                        │  │
│  │  7. Cache Interaction (async trigger)                     │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             │ Async Event Bus (Kafka/Redis Streams)
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                COLD PATH (Background Workers)                    │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Asynchronous Memory Consolidation Pipeline              │  │
│  │                                                            │  │
│  │  1. Extract Entities & Intent                             │  │
│  │  2. Classify Memory Type                                  │  │
│  │  3. Detect Preferences & Profile Updates                  │  │
│  │  4. Store in Appropriate Database                         │  │
│  │  5. Update Caches                                          │  │
│  │  6. Summarize & Compress Old Memories                     │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────┬────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Hybrid Memory Storage                          │
│                                                                   │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐   │
│  │   Redis      │  │ PostgreSQL   │  │ Neo4j + Qdrant     │   │
│  │  (Working    │  │  (Factual    │  │ (Episodic Memory)  │   │
│  │   Memory)    │  │   Memory)    │  │                     │   │
│  │              │  │              │  │ - Graph Relations   │   │
│  │ - Raw Turns  │  │ - Prefs      │  │ - Vector Search    │   │
│  │ - Session    │  │ - Facts      │  │ - Time-bound       │   │
│  │   Cache      │  │ - Profile    │  │   Episodes         │   │
│  │ - TTL: 1hr   │  │ - Permanent  │  │ - TTL: 30 days     │   │
│  └──────────────┘  └──────────────┘  └────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### Key Architectural Principles

1. **Asynchronous-First Design**: User-facing requests never wait for memory consolidation
2. **Hierarchical Caching**: Multi-layer cache (L1: Redis, L2: PostgreSQL, L3: Graph/Vector)
3. **Hybrid Storage**: Different memory types use optimal databases
4. **Streaming Responses**: Return LLM responses immediately while processing in background
5. **Graceful Degradation**: System works even if memory services are temporarily unavailable

---

## 3. Three-Tier Memory System

### Memory Type Classification

Based on cognitive science research, Context Quilt implements three distinct memory types, each optimized for different recall patterns and retention periods.

| Memory Type | Lifespan | Storage | Query Pattern | Auto-Expire | Purpose |
|-------------|----------|---------|---------------|-------------|---------|
| **Working Memory** | Current session (15-60 min) | Redis (in-memory) | Key-value lookup by session_id | Yes (TTL) | Immediate context for ongoing conversation |
| **Episodic Memory** | Days to weeks | Neo4j (graph) + Qdrant (vector) | Graph traversal + semantic search | Yes (decay) | "What happened?" - specific events and experiences |
| **Factual Memory** | Permanent | PostgreSQL (relational) | Structured queries by user_id | No | "What do I know?" - hard facts and preferences |

### 3.1 Working Memory (Short-Term)

**Purpose:** Holds raw conversation turns for the active session.

**Data Structure:**
```json
{
  "session_id": "sess_abc123",
  "user_id": "user_789",
  "application": "support_chatbot",
  "turns": [
    {
      "turn_id": 1,
      "timestamp": "2024-11-18T10:00:00Z",
      "user_message": "My order hasn't arrived",
      "assistant_response": "I'd be happy to help. What's your order number?",
      "metadata": {
        "model": "gpt-4",
        "tokens": 45
      }
    },
    {
      "turn_id": 2,
      "timestamp": "2024-11-18T10:00:15Z",
      "user_message": "ORDER-12345",
      "assistant_response": "Let me check on ORDER-12345...",
      "metadata": {
        "model": "gpt-4",
        "tokens": 38
      }
    }
  ],
  "ttl": 3600,  // 1 hour
  "last_activity": "2024-11-18T10:00:15Z"
}
```

**Storage Technology:** Redis Hash with automatic TTL
- **Read Latency:** <1ms
- **Write Latency:** <1ms
- **Why Redis:** In-memory speed, native TTL support, pub/sub for async triggers

**Retrieval Strategy:**
```python
def get_working_memory(session_id):
    key = f"working_memory:{session_id}"
    turns = redis.hgetall(key)
    return turns[-5:]  # Last 5 turns for immediate context
```

### 3.2 Episodic Memory (Medium-Term)

**Purpose:** Stores specific events, experiences, and their relationships over time.

**Data Structure (Graph):**
```
// Neo4j Graph Representation
(User:user_789)-[:HAD_CONVERSATION {
  date: "2024-11-18",
  session_id: "sess_abc123",
  summary: "Troubleshot delivery delay for ORDER-12345"
}]->(Episode:delivery_issue)

(Episode)-[:INVOLVED]->(Entity:ORDER-12345)
(Episode)-[:RESULTED_IN]->(Outcome:resolved)
(Episode)-[:EMOTION]->(Sentiment:frustrated_then_satisfied)

(User)-[:OWNS]->(Entity:ORDER-12345)
(Entity)-[:HAS_STATUS]->(Status:delivered)
```

**Data Structure (Vector):**
```json
{
  "episode_id": "ep_xyz789",
  "user_id": "user_789",
  "timestamp": "2024-11-18T10:00:00Z",
  "summary": "User reported ORDER-12345 delivery delay. Issue resolved: package rerouted, delivered 2 days later.",
  "embedding": [0.123, -0.456, 0.789, ...],  // 1536-dim vector
  "entities": ["ORDER-12345", "delivery", "delay", "resolution"],
  "sentiment": "negative_to_positive",
  "outcome": "resolved",
  "ttl_days": 30
}
```

**Storage Technology:** 
- **Neo4j/FalkorDB** for graph relationships
- **Qdrant/Pinecone** for semantic search
- Combined queries for complex retrieval

**Why Hybrid?**
- Graph: Answers "How does X relate to Y?" (e.g., "What projects is Alice involved in?")
- Vector: Answers "What similar situations occurred?" (semantic similarity)

**Retrieval Strategy:**
```python
def get_episodic_memory(user_id, current_context):
    # 1. Vector search for semantic similarity
    similar_episodes = qdrant.search(
        collection="episodes",
        query_vector=embed(current_context),
        filter={"user_id": user_id},
        limit=3
    )
    
    # 2. Graph traversal for related entities
    related_episodes = neo4j.query("""
        MATCH (u:User {id: $user_id})-[:HAD_CONVERSATION]->(ep:Episode)
        WHERE ep.timestamp > datetime() - duration({days: 30})
        RETURN ep
        ORDER BY ep.timestamp DESC
        LIMIT 5
    """, user_id=user_id)
    
    # 3. Merge and rank
    return merge_and_rank(similar_episodes, related_episodes)
```

**Decay Strategy:**
```python
decay_schedule = {
    "0-7_days": {
        "retention": "full",
        "storage": "hot_storage",
        "detail_level": "complete"
    },
    "7-30_days": {
        "retention": "summarized",
        "storage": "warm_storage",
        "detail_level": "session_summaries"
    },
    "30-90_days": {
        "retention": "compressed",
        "storage": "cold_storage", 
        "detail_level": "weekly_summaries_only"
    },
    "90+_days": {
        "retention": "archived",
        "storage": "object_storage",
        "detail_level": "extracted_facts_only"
    }
}
```

### 3.3 Factual Memory (Long-Term)

**Purpose:** Stores permanent facts, preferences, and user profile data.

**Data Structure:**
```json
{
  "user_id": "user_789",
  "profile": {
    "name": "Alice Chen",
    "role": "Senior Engineer",
    "company": "TechCorp",
    "tier": "premium"
  },
  "preferences": {
    "communication": {
      "technical_level": "expert",
      "verbosity": "concise",
      "tone": "direct",
      "no_hand_holding": true,
      "confidence": 0.92,
      "last_updated": "2024-11-18T10:00:00Z"
    },
    "content": {
      "prefers": ["code_examples", "bullet_points", "technical_docs"],
      "dislikes": ["analogies", "long_explanations"],
      "confidence": 0.87
    }
  },
  "hard_facts": [
    {
      "fact_id": "fact_123",
      "category": "dietary",
      "type": "allergy",
      "value": "peanuts",
      "severity": "critical",
      "source": "explicit_statement",
      "confidence": 1.0,
      "created_at": "2024-10-01T14:30:00Z"
    },
    {
      "fact_id": "fact_124",
      "category": "preference",
      "type": "beverage",
      "item": "coffee",
      "sentiment": "dislike",
      "confidence": 0.95,
      "created_at": "2024-11-15T09:20:00Z"
    }
  ],
  "domains": [
    {"domain": "machine_learning", "expertise": "expert", "confidence": 0.94},
    {"domain": "distributed_systems", "expertise": "advanced", "confidence": 0.89},
    {"domain": "frontend_dev", "expertise": "intermediate", "confidence": 0.71}
  ],
  "metadata": {
    "first_seen": "2024-09-01T08:00:00Z",
    "last_active": "2024-11-18T10:00:00Z",
    "total_interactions": 347,
    "total_sessions": 89
  }
}
```

**Storage Technology:** PostgreSQL with JSONB columns
- **Why PostgreSQL:** ACID compliance, structured queries, JSONB flexibility, proven scalability

**Schema Design:**
```sql
CREATE TABLE users (
    user_id VARCHAR(255) PRIMARY KEY,
    profile JSONB NOT NULL,
    preferences JSONB NOT NULL DEFAULT '{}',
    domains JSONB[] DEFAULT ARRAY[]::JSONB[],
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE user_facts (
    fact_id VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(255) REFERENCES users(user_id),
    category VARCHAR(100) NOT NULL,
    type VARCHAR(100) NOT NULL,
    value JSONB NOT NULL,
    confidence FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    source VARCHAR(100) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NULL
);

CREATE INDEX idx_user_facts_user_id ON user_facts(user_id);
CREATE INDEX idx_user_facts_category ON user_facts(category);
CREATE INDEX idx_user_facts_confidence ON user_facts(confidence DESC);
```

**Retrieval Strategy:**
```python
def get_factual_memory(user_id):
    # Fetch from PostgreSQL (or cache if available)
    cache_key = f"factual_memory:{user_id}"
    
    # Try Redis cache first (L1)
    cached = redis.get(cache_key)
    if cached:
        return json.loads(cached)
    
    # Fetch from PostgreSQL (L2)
    user = db.query("""
        SELECT profile, preferences, domains, metadata
        FROM users
        WHERE user_id = %s
    """, user_id)
    
    facts = db.query("""
        SELECT * FROM user_facts
        WHERE user_id = %s
        AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY confidence DESC, created_at DESC
    """, user_id)
    
    result = {**user, "facts": facts}
    
    # Cache for future requests
    redis.setex(cache_key, 3600, json.dumps(result))
    
    return result
```

---

## 4. Automatic Detection & Classification

### 4.1 Detection Pipeline Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                   Async Detection Pipeline                       │
│                                                                   │
│  [New Conversation] → [Event Bus] → [Worker Pool]               │
│                                           │                       │
│                                           ▼                       │
│                              ┌──────────────────────┐           │
│                              │  Intent Classifier    │           │
│                              │  (Fast Model: 20ms)   │           │
│                              └──────────┬───────────┘           │
│                                         │                         │
│                    ┌────────────────────┼────────────────┐      │
│                    ▼                    ▼                ▼       │
│          ┌─────────────────┐  ┌─────────────┐  ┌──────────┐   │
│          │ Entity Extractor│  │  Sentiment  │  │  Style   │   │
│          │  (NER + LLM)    │  │  Analysis   │  │ Detector │   │
│          └────────┬────────┘  └──────┬──────┘  └────┬─────┘   │
│                   │                   │              │          │
│                   └───────────────────┴──────────────┘          │
│                                       │                          │
│                                       ▼                          │
│                          ┌─────────────────────┐               │
│                          │ Memory Classifier   │               │
│                          │ & Router            │               │
│                          └──────────┬──────────┘               │
│                                     │                           │
│                    ┌────────────────┼─────────────┐            │
│                    ▼                ▼              ▼            │
│              [Working Memory]  [Episodic]   [Factual]          │
│              (Redis)           (Neo4j)      (PostgreSQL)        │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 Intent Classification

**Model:** Fine-tuned DistilBERT or similar small, fast classifier

**Classes:**
```python
INTENT_CLASSES = [
    "PREFERENCE_STATED",      # "I prefer X", "I like Y"
    "FACT_DISCLOSED",         # "My name is", "I work at"
    "EVENT_OCCURRED",         # "I just bought", "I completed"
    "QUESTION_ANSWERED",      # Factual Q&A worth remembering
    "STYLE_INDICATED",        # Communication style signals
    "RELATIONSHIP_MENTIONED", # "Alice works on Project X"
    "OPINION_EXPRESSED",      # "I think", "In my opinion"
    "CHITCHAT",              # No memorable content
    "COMMAND_ISSUED",        # "Remember this", "Forget that"
]
```

**Implementation:**
```python
async def classify_intent(conversation_turn):
    """
    Fast intent classification using local model
    Target: <20ms latency
    """
    # Prepare input
    text = f"{conversation_turn['user_message']} {conversation_turn['assistant_response']}"
    
    # Run classification
    result = intent_classifier.predict(text)
    # Returns: {
    #   "primary_intent": "PREFERENCE_STATED",
    #   "confidence": 0.92,
    #   "secondary_intents": ["STYLE_INDICATED"]
    # }
    
    return result
```

### 4.3 Entity & Relationship Extraction

**Approach:** Hybrid NER + LLM-based extraction

**Pipeline:**
```python
async def extract_entities(conversation_turn):
    """
    Extract structured entities from conversation
    Target: <100ms latency
    """
    
    # Step 1: Fast NER for common entities (10ms)
    ner_entities = spacy_model(text).ents
    # Extracts: PERSON, ORG, DATE, PRODUCT, etc.
    
    # Step 2: LLM-based extraction for complex entities (80ms)
    if intent in ["PREFERENCE_STATED", "FACT_DISCLOSED"]:
        llm_prompt = f"""
        Extract structured information from this conversation:
        User: {user_message}
        Assistant: {assistant_response}
        
        Output JSON:
        {{
          "preferences": [{{"category": "...", "item": "...", "sentiment": "like/dislike"}}],
          "facts": [{{"type": "...", "value": "..."}}],
          "relationships": [{{"subject": "...", "relation": "...", "object": "..."}}]
        }}
        """
        
        llm_result = await llm_extract(llm_prompt, model="gpt-4o-mini")
    
    # Step 3: Merge results
    return {
        "ner_entities": ner_entities,
        "structured_data": llm_result,
        "confidence": calculate_confidence(ner_entities, llm_result)
    }
```

**Example Extraction:**

Input:
```
User: "I don't like coffee, but I love tea. Also, I work at TechCorp as a senior engineer."
Assistant: "Got it! I'll remember your beverage preferences and your role at TechCorp."
```

Output:
```json
{
  "preferences": [
    {
      "category": "beverage",
      "item": "coffee",
      "sentiment": "dislike",
      "confidence": 0.95
    },
    {
      "category": "beverage",
      "item": "tea",
      "sentiment": "like",
      "confidence": 0.97
    }
  ],
  "facts": [
    {
      "type": "employer",
      "value": "TechCorp",
      "confidence": 0.98
    },
    {
      "type": "job_title",
      "value": "senior engineer",
      "confidence": 0.96
    }
  ],
  "entities": [
    {"text": "TechCorp", "type": "ORG"},
    {"text": "senior engineer", "type": "ROLE"}
  ]
}
```

### 4.4 Memory Classification & Routing

**Decision Tree:**
```python
async def classify_and_route_memory(intent, entities, conversation):
    """
    Determine which memory system(s) should store this information
    """
    
    # ALWAYS store in Working Memory (immediate context)
    await store_working_memory(conversation)
    
    # Route to long-term storage based on intent
    if intent == "PREFERENCE_STATED":
        for pref in entities["preferences"]:
            await store_factual_memory(
                user_id=conversation["user_id"],
                category="preference",
                data=pref,
                permanent=True
            )
    
    elif intent == "FACT_DISCLOSED":
        for fact in entities["facts"]:
            await store_factual_memory(
                user_id=conversation["user_id"],
                category="fact",
                data=fact,
                permanent=True
            )
    
    elif intent in ["EVENT_OCCURRED", "RELATIONSHIP_MENTIONED"]:
        # Store as episodic memory with relationships
        await store_episodic_memory(
            user_id=conversation["user_id"],
            episode={
                "summary": generate_summary(conversation),
                "entities": entities,
                "relationships": entities["relationships"],
                "timestamp": conversation["timestamp"],
                "ttl_days": 30
            }
        )
        
        # Also create graph relationships
        await create_graph_relationships(
            user_id=conversation["user_id"],
            relationships=entities["relationships"]
        )
    
    elif intent == "STYLE_INDICATED":
        # Update communication profile
        await update_communication_profile(
            user_id=conversation["user_id"],
            signals=extract_style_signals(conversation)
        )
```

---

## 5. Communication Profile System

### 5.1 Profile Structure

The Communication Profile is a meta-layer that influences how all prompts are constructed.

**Complete Profile Schema:**
```json
{
  "user_id": "user_789",
  "communication_profile": {
    "verbosity": {
      "level": "concise",           // concise | balanced | detailed
      "confidence": 0.87,
      "signals": [
        "avg_message_length: 9 words",
        "user_feedback: 'too long' (1x)",
        "prefers_bullet_points: true"
      ],
      "updated_at": "2024-11-18T10:00:00Z"
    },
    
    "technical_level": {
      "level": "expert",             // novice | intermediate | expert
      "confidence": 0.93,
      "signals": [
        "uses_technical_jargon: high",
        "asks_for_implementation_details: frequent",
        "corrects_technical_errors: 3x"
      ],
      "domains": {
        "software_engineering": 0.96,
        "machine_learning": 0.91,
        "devops": 0.78
      },
      "updated_at": "2024-11-18T09:45:00Z"
    },
    
    "interaction_style": {
      "style": "direct",             // friendly | professional | direct | casual
      "formality": "informal",       // formal | informal | mixed
      "confidence": 0.81,
      "signals": [
        "uses_greetings: rarely",
        "politeness_markers: low",
        "gets_to_point: always",
        "uses_contractions: frequently"
      ],
      "updated_at": "2024-11-17T14:20:00Z"
    },
    
    "format_preference": {
      "preferred_formats": [
        {"format": "code_examples", "weight": 0.95},
        {"format": "bullet_points", "weight": 0.88},
        {"format": "tables", "weight": 0.82},
        {"format": "diagrams", "weight": 0.76}
      ],
      "disliked_formats": [
        {"format": "long_paragraphs", "weight": 0.89},
        {"format": "analogies", "weight": 0.73}
      ],
      "confidence": 0.84,
      "updated_at": "2024-11-16T11:30:00Z"
    },
    
    "tone_preference": {
      "preferred_tone": "pragmatic",  // empathetic | pragmatic | enthusiastic | neutral
      "confidence": 0.76,
      "signals": [
        "responds_well_to: direct_solutions",
        "dismisses: small_talk"
      ],
      "updated_at": "2024-11-15T16:45:00Z"
    },
    
    "response_time_preference": {
      "preferred_speed": "fast",      // fast | balanced | thorough
      "max_acceptable_delay": "5s",
      "confidence": 0.69,
      "updated_at": "2024-11-14T10:00:00Z"
    }
  },
  
  "profile_metadata": {
    "total_interactions": 347,
    "profile_completeness": 0.87,    // 0-1 scale
    "last_profile_update": "2024-11-18T10:00:00Z",
    "created_at": "2024-09-01T08:00:00Z"
  }
}
```

### 5.2 Automatic Profile Detection

#### Verbosity Detection

```python
def detect_verbosity(user_history, feedback_signals):
    """
    Analyze user message patterns to infer verbosity preference
    """
    
    # Signals
    avg_user_message_length = np.mean([
        len(msg.split()) for msg in user_history[-20:]
    ])
    
    explicit_feedback = check_feedback_keywords(user_history, [
        "too long", "tldr", "brief", "concise", "short",
        "more detail", "elaborate", "explain more"
    ])
    
    response_patterns = analyze_user_responses(user_history)
    # Does user often say "got it" after short answers?
    # Does user ask follow-ups after concise responses?
    
    # Decision logic
    if explicit_feedback.get("concise") or avg_user_message_length < 12:
        return {
            "level": "concise",
            "confidence": 0.9 if explicit_feedback else 0.75,
            "signals": [
                f"avg_message_length: {avg_user_message_length} words",
                f"explicit_feedback: {explicit_feedback.get('concise', 'none')}"
            ]
        }
    elif avg_user_message_length > 40:
        return {"level": "detailed", "confidence": 0.8, "signals": [...]}
    else:
        return {"level": "balanced", "confidence": 0.7, "signals": [...]}
```

#### Technical Level Detection

```python
def detect_technical_level(conversation_history):
    """
    Determine user's technical sophistication
    """
    
    # Analyze technical vocabulary
    technical_terms = extract_technical_terms(conversation_history)
    technical_density = len(technical_terms) / total_words(conversation_history)
    
    # Check for technical behaviors
    asks_for_implementation = count_phrases(conversation_history, [
        "how does it work",
        "implementation details",
        "under the hood",
        "algorithm",
        "architecture"
    ])
    
    corrects_technical_errors = detect_corrections(conversation_history)
    
    uses_jargon = check_domain_jargon(conversation_history, domains=[
        "software_engineering",
        "machine_learning", 
        "distributed_systems"
    ])
    
    # LLM-based classification for complex cases
    if technical_density > 0.15 or uses_jargon["count"] > 5:
        llm_classification = await classify_with_llm(
            conversation_history[-10:],
            prompt="Classify technical level: novice | intermediate | expert"
        )
        return {
            "level": llm_classification["level"],
            "confidence": llm_classification["confidence"],
            "domains": uses_jargon["domains"],
            "signals": [
                f"technical_density: {technical_density:.2f}",
                f"jargon_usage: {uses_jargon['count']}",
                f"asks_implementation_details: {asks_for_implementation}"
            ]
        }
    
    # Rule-based fallback
    if technical_density < 0.05:
        return {"level": "novice", "confidence": 0.80}
    elif technical_density < 0.12:
        return {"level": "intermediate", "confidence": 0.75}
    else:
        return {"level": "expert", "confidence": 0.85}
```

#### Interaction Style Detection

```python
def detect_interaction_style(conversation_history):
    """
    Infer preferred interaction style from conversation patterns
    """
    
    # Extract features
    features = {
        "uses_greetings": count_greetings(conversation_history),
        "politeness_markers": count_words(conversation_history, [
            "please", "thank you", "thanks", "appreciate"
        ]),
        "emoji_usage": count_emojis(conversation_history),
        "exclamation_marks": count_char(conversation_history, "!"),
        "question_marks": count_char(conversation_history, "?"),
        "formality_score": calculate_formality(conversation_history),
        "avg_response_time": calculate_avg_response_time(conversation_history),
        "message_directness": analyze_directness(conversation_history)
    }
    
    # Classification
    if features["message_directness"] > 0.8 and features["politeness_markers"] < 2:
        style = "direct"
        formality = "informal"
        confidence = 0.85
    elif features["politeness_markers"] > 5 and features["formality_score"] > 0.7:
        style = "professional"
        formality = "formal"
        confidence = 0.88
    elif features["emoji_usage"] > 3 or features["exclamation_marks"] > 5:
        style = "friendly"
        formality = "casual"
        confidence = 0.82
    else:
        style = "balanced"
        formality = "mixed"
        confidence = 0.70
    
    return {
        "style": style,
        "formality": formality,
        "confidence": confidence,
        "signals": [
            f"uses_greetings: {features['uses_greetings']}",
            f"politeness_markers: {features['politeness_markers']}",
            f"directness: {features['message_directness']:.2f}"
        ]
    }
```

#### Format Preference Detection

```python
def detect_format_preference(conversation_history, user_feedback):
    """
    Learn which response formats user prefers
    """
    
    # Analyze assistant response formats and user reactions
    response_analysis = []
    
    for i, turn in enumerate(conversation_history):
        if turn["role"] == "assistant":
            # Detect format of assistant response
            response_format = classify_response_format(turn["content"])
            # Examples: bullet_points, paragraphs, code, tables, etc.
            
            # Check user's next message for satisfaction signals
            if i + 1 < len(conversation_history):
                next_user_msg = conversation_history[i + 1]["content"]
                satisfaction = analyze_satisfaction(next_user_msg)
                # Positive signals: "perfect", "exactly", "got it", quick acceptance
                # Negative signals: "too much", "simpler", "TLDR", confusion
                
                response_analysis.append({
                    "format": response_format,
                    "satisfaction": satisfaction,
                    "engagement": len(next_user_msg)
                })
    
    # Aggregate preferences
    format_scores = {}
    for analysis in response_analysis:
        fmt = analysis["format"]
        if fmt not in format_scores:
            format_scores[fmt] = {"positive": 0, "negative": 0, "total": 0}
        
        format_scores[fmt]["total"] += 1
        if analysis["satisfaction"] > 0.7:
            format_scores[fmt]["positive"] += 1
        elif analysis["satisfaction"] < 0.3:
            format_scores[fmt]["negative"] += 1
    
    # Calculate preferences
    preferred = []
    disliked = []
    
    for fmt, scores in format_scores.items():
        if scores["total"] >= 3:  # Minimum sample size
            preference_score = (scores["positive"] - scores["negative"]) / scores["total"]
            
            if preference_score > 0.3:
                preferred.append({"format": fmt, "weight": preference_score})
            elif preference_score < -0.3:
                disliked.append({"format": fmt, "weight": abs(preference_score)})
    
    return {
        "preferred_formats": sorted(preferred, key=lambda x: x["weight"], reverse=True),
        "disliked_formats": sorted(disliked, key=lambda x: x["weight"], reverse=True),
        "confidence": calculate_confidence(format_scores),
        "sample_size": sum(s["total"] for s in format_scores.values())
    }
```

### 5.3 Profile-Based Prompt Enrichment

**How profiles influence prompts:**

```python
def enrich_prompt_with_profile(user_message, user_profile):
    """
    Inject communication profile into system prompt
    """
    
    profile_instructions = []
    
    # Verbosity
    if user_profile["verbosity"]["level"] == "concise":
        profile_instructions.append(
            "User prefers concise responses. Be brief and direct. Use bullet points."
        )
    elif user_profile["verbosity"]["level"] == "detailed":
        profile_instructions.append(
            "User prefers detailed explanations. Provide thorough responses with examples."
        )
    
    # Technical level
    tech_level = user_profile["technical_level"]["level"]
    if tech_level == "expert":
        profile_instructions.append(
            "User is technically expert. Skip basic explanations. Use technical terminology. "
            "Provide implementation details and edge cases."
        )
    elif tech_level == "novice":
        profile_instructions.append(
            "User is new to technical topics. Use simple language. Provide analogies and examples."
        )
    
    # Format preference
    preferred_formats = user_profile["format_preference"]["preferred_formats"]
    if preferred_formats:
        top_formats = [f["format"] for f in preferred_formats[:3]]
        profile_instructions.append(
            f"User prefers these formats: {', '.join(top_formats)}."
        )
    
    disliked_formats = user_profile["format_preference"]["disliked_formats"]
    if disliked_formats:
        avoid_formats = [f["format"] for f in disliked_formats]
        profile_instructions.append(
            f"Avoid these formats: {', '.join(avoid_formats)}."
        )
    
    # Interaction style
    if user_profile["interaction_style"]["style"] == "direct":
        profile_instructions.append(
            "User prefers direct communication. Skip pleasantries. Get to the point."
        )
    elif user_profile["interaction_style"]["style"] == "friendly":
        profile_instructions.append(
            "User appreciates friendly, warm communication. Use a conversational tone."
        )
    
    # Construct enriched system prompt
    system_prompt = f"""
[COMMUNICATION PROFILE]
{chr(10).join(profile_instructions)}

[CONFIDENCE NOTE]
Profile confidence: {user_profile['profile_metadata']['profile_completeness']:.0%}
This profile is based on {user_profile['profile_metadata']['total_interactions']} interactions.

[CURRENT MESSAGE]
{user_message}
"""
    
    return system_prompt
```

**Example Enriched Prompt:**

User asks: "How does OAuth work?"

**Without profile:**
```
User: How does OAuth work?
```

**With expert profile:**
```
[COMMUNICATION PROFILE]
User prefers concise responses. Be brief and direct. Use bullet points.
User is technically expert. Skip basic explanations. Use technical terminology.
User prefers: code_examples, bullet_points, technical_docs.
Avoid: analogies, long_explanations.
User prefers direct communication. Skip pleasantries. Get to the point.

Profile confidence: 87%
Based on 347 interactions.

[CURRENT MESSAGE]
User: How does OAuth work?
```

**LLM Response (with profile):**
```
OAuth 2.0 authorization framework (RFC 6749):

Grant Types:
• Authorization Code - server-side apps, most secure
• Implicit - deprecated, client-side (security concerns)
• Client Credentials - machine-to-machine
• Resource Owner Password - legacy, avoid if possible

Flow:
1. Client requests authorization endpoint
2. User authenticates, grants scope
3. Auth server returns code
4. Client exchanges code for access_token
5. Client uses token in Authorization header

Security:
• Use PKCE (RFC 7636) for public clients
• Short-lived access tokens (15min)
• Refresh tokens for long sessions
• Validate redirect_uri strictly

Need implementation code?
```

**LLM Response (without profile - beginner assumption):**
```
Great question! OAuth is like a valet key for your car. 🚗

Instead of giving someone your house key (your password), you give them a special valet key that only works for specific things. This way, apps can access your information without knowing your actual password!

Here's how it works step by step:

1. First, imagine you want to use an app that needs to access your Google Photos...

[much longer explanation with analogies]
```

---

## 6. Latency Optimization Strategy

### 6.1 Latency Budget

**Target Performance:**
- **Hot Path (Context Retrieval):** <50ms
- **LLM Response:** Variable (model-dependent, typically 1-3s for streaming start)
- **Cold Path (Memory Consolidation):** Asynchronous, no user-facing impact

**Breakdown:**
```
Total User-Perceived Latency Target: < 200ms overhead

┌─────────────────────────────────────────────────────────┐
│ Component                      │ Target    │ Strategy   │
├────────────────────────────────┼───────────┼────────────┤
│ API Gateway (auth, routing)    │ <5ms      │ Nginx/Envoy│
│ Redis Cache Lookup (L1)        │ <2ms      │ In-memory  │
│ Context Assembly               │ <10ms     │ Optimized  │
│ Profile Injection              │ <3ms      │ Templates  │
│ PostgreSQL Lookup (cache miss) │ <20ms     │ Indexed    │
│ Vector Search (if needed)      │ <30ms     │ HNSW index │
│ Graph Query (if needed)        │ <40ms     │ Optimized  │
│ Prompt Construction            │ <5ms      │ String ops │
│ Network to LLM Provider        │ <50ms     │ Streaming  │
├────────────────────────────────┼───────────┼────────────┤
│ TOTAL (Cache Hit)              │ ~25ms     │ ✅         │
│ TOTAL (Cache Miss + DB)        │ ~75ms     │ ✅         │
│ TOTAL (Full Context Search)    │ ~120ms    │ ✅         │
└─────────────────────────────────────────────────────────┘
```

### 6.2 Hierarchical Caching Strategy

```
┌─────────────────────────────────────────────────────────────┐
│                   L1 Cache: Redis (In-Memory)                │
│  • Working Memory (raw turns)           → <1ms              │
│  • User Profile (factual memory)        → <2ms              │
│  • Session State                        → <1ms              │
│  • TTL: 1 hour (working), 1 day (profile)                   │
└─────────────────────────────────────────────────────────────┘
                             │
                             │ Cache Miss
                             ▼
┌─────────────────────────────────────────────────────────────┐
│               L2 Cache: PostgreSQL (Indexed)                 │
│  • User Facts & Preferences             → <20ms             │
│  • Communication Profiles               → <15ms             │
│  • JSONB indices on frequent queries                         │
└─────────────────────────────────────────────────────────────┘
                             │
                             │ Complex Query Needed
                             ▼
┌─────────────────────────────────────────────────────────────┐
│          L3: Hybrid Search (Vector + Graph)                  │
│  • Semantic Search (Qdrant)             → <30ms             │
│  • Graph Traversal (Neo4j)              → <40ms             │
│  • Combined queries for episodic recall                      │
└─────────────────────────────────────────────────────────────┘
```

**Cache Invalidation Strategy:**
```python
# Write-through caching
async def update_user_profile(user_id, updates):
    # 1. Update source of truth (PostgreSQL)
    await db.execute(
        "UPDATE users SET profile = profile || %s WHERE user_id = %s",
        (json.dumps(updates), user_id)
    )
    
    # 2. Invalidate cache
    await redis.delete(f"profile:{user_id}")
    
    # 3. Optionally warm cache immediately
    new_profile = await db.fetch_one(
        "SELECT * FROM users WHERE user_id = %s", user_id
    )
    await redis.setex(
        f"profile:{user_id}", 
        3600,  # 1 hour
        json.dumps(new_profile)
    )
```

### 6.3 Request Handling: Hot Path vs Cold Path

**Hot Path (Synchronous, User-Facing):**
```python
@app.post("/v1/chat")
async def chat_endpoint(request: ChatRequest):
    start_time = time.time()
    
    # STEP 1: Fast lookups only (target: <25ms)
    working_memory = await redis.hgetall(f"session:{request.session_id}")
    user_profile = await redis.get(f"profile:{request.user_id}")
    
    if not user_profile:
        # Cache miss - fetch from PostgreSQL
        user_profile = await db.fetch_one(
            "SELECT * FROM users WHERE user_id = %s", 
            request.user_id
        )
        await redis.setex(f"profile:{request.user_id}", 3600, json.dumps(user_profile))
    
    # STEP 2: Assemble context (target: <10ms)
    context = assemble_context(
        working_memory=working_memory,
        user_profile=user_profile,
        current_message=request.messages[-1]
    )
    
    # STEP 3: Inject profile-based instructions (target: <5ms)
    enriched_prompt = enrich_with_profile(context, user_profile)
    
    # STEP 4: Forward to LLM (streaming)
    llm_response = await openai_client.chat.completions.create(
        model=request.model,
        messages=enriched_prompt,
        stream=True
    )
    
    # STEP 5: Stream response to client
    async def generate():
        collected_response = []
        async for chunk in llm_response:
            collected_response.append(chunk.choices[0].delta.content)
            yield chunk
        
        # STEP 6: Trigger async consolidation (non-blocking)
        full_response = "".join(collected_response)
        await publish_to_event_bus({
            "user_id": request.user_id,
            "session_id": request.session_id,
            "user_message": request.messages[-1],
            "assistant_response": full_response,
            "timestamp": datetime.now().isoformat()
        })
    
    latency_ms = (time.time() - start_time) * 1000
    return StreamingResponse(
        generate(),
        headers={"X-Context-Quilt-Latency": f"{latency_ms}ms"}
    )
```

**Cold Path (Asynchronous, Background):**
```python
# Worker consuming from Kafka/Redis Streams
async def memory_consolidation_worker():
    async for event in event_bus.consume("chat.completed"):
        try:
            # This runs AFTER user has received response
            # No latency impact on user experience
            
            # Extract & classify (100-200ms, acceptable)
            intent = await classify_intent(event)
            entities = await extract_entities(event)
            
            # Update memories (50-100ms)
            await update_memories(event["user_id"], intent, entities)
            
            # Update profile (30-50ms)
            await update_communication_profile(event["user_id"], event)
            
            # Summarize if needed (200-500ms for old conversations)
            await consolidate_old_memories(event["user_id"])
            
            # Update caches
            await warm_caches(event["user_id"])
            
        except Exception as e:
            logger.error(f"Memory consolidation failed: {e}")
            # Don't crash - memory updates are best-effort
```

### 6.4 API Design: REST vs GraphQL Recommendation

**Recommendation: REST API with Batch Endpoints**

**Why NOT GraphQL:**
- ❌ Adds 20-50ms overhead for query parsing and resolution
- ❌ Overfetching/underfetching not a concern (we control both sides)
- ❌ Caching is simpler with REST (standard HTTP cache headers)
- ❌ Adds client complexity (need GraphQL client library)

**Why REST:**
- ✅ Simpler, faster, lower latency
- ✅ Better HTTP caching support
- ✅ Easier for developers to integrate
- ✅ Standard authentication patterns (Bearer tokens)
- ✅ Can add GraphQL later if needed

**REST API Design:**

```yaml
# Primary endpoint - optimized for minimal latency
POST /v1/chat
Content-Type: application/json
Authorization: Bearer {api_key}

{
  "user_id": "user_123",
  "session_id": "sess_abc",  # optional
  "application": "support_chatbot",
  "messages": [
    {"role": "user", "content": "..."}
  ],
  "model": "gpt-4",  # optional
  "context_config": {  # optional
    "include_history": true,
    "max_history_turns": 5,
    "include_profile": true
  }
}

Response (streaming):
Server-Sent Events (SSE) or JSON stream

# Batch endpoint for efficiency
POST /v1/chat/batch
{
  "requests": [
    {"user_id": "user_1", "messages": [...]},
    {"user_id": "user_2", "messages": [...]}
  ]
}

# Memory management endpoints
GET /v1/memory/{user_id}
PUT /v1/memory/{user_id}
DELETE /v1/memory/{user_id}

# Profile endpoints
GET /v1/profile/{user_id}
PATCH /v1/profile/{user_id}
```

**Alternative: gRPC for High-Throughput**

For enterprise customers with very high volume:

```protobuf
service ContextQuilt {
  rpc Chat(ChatRequest) returns (stream ChatResponse);
  rpc BatchChat(BatchChatRequest) returns (BatchChatResponse);
  rpc GetMemory(UserRequest) returns (MemoryResponse);
}
```

**Performance:**
- REST: ~25ms overhead (sufficient for most use cases)
- gRPC: ~10ms overhead (worth it at >1000 req/s scale)

**Recommendation:** Start with REST, offer gRPC as premium tier.

---

## 7. API Design

### 7.1 Core Chat Endpoint

```yaml
POST /v1/chat
```

**Request:**
```json
{
  "user_id": "user_123",
  "session_id": "sess_abc",
  "application": "support_chatbot",
  "messages": [
    {
      "role": "user",
      "content": "My order hasn't arrived yet"
    }
  ],
  "model": "gpt-4",
  "temperature": 0.7,
  "max_tokens": 1000,
  "stream": true,
  "context_config": {
    "include_history": true,
    "max_history_turns": 5,
    "history_timeframe": "24h",
    "include_long_term_memory": true,
    "include_profile": true,
    "context_tags": ["preferences", "past_orders"],
    "semantic_search": false
  },
  "metadata": {
    "channel": "web_chat",
    "user_tier": "premium"
  }
}
```

**Response (Streaming):**
```json
// Server-Sent Events (SSE) stream
data: {"type": "context", "data": {"tokens_added": 127, "sources": ["working_memory", "profile"]}}

data: {"type": "chunk", "content": "I'd be happy"}

data: {"type": "chunk", "content": " to help you"}

data: {"type": "chunk", "content": " with that."}

data: {"type": "metadata", "data": {"model": "gpt-4", "latency_ms": 45, "cached": true}}

data: {"type": "done"}
```

**Response (Non-Streaming):**
```json
{
  "id": "req_xyz789",
  "user_id": "user_123",
  "session_id": "sess_abc",
  "model_used": "gpt-4",
  "response": {
    "role": "assistant",
    "content": "I'd be happy to help you with that. Could you provide your order number?"
  },
  "context_quilt": {
    "enriched": true,
    "patches_used": ["working_memory", "user_profile", "episodic_memory"],
    "context_tokens": 127,
    "cache_hit": true,
    "retrieval_latency_ms": 18
  },
  "usage": {
    "prompt_tokens": 234,
    "completion_tokens": 45,
    "total_tokens": 279
  },
  "metadata": {
    "latency_ms": 1250,
    "cost_usd": 0.0234
  }
}
```

### 7.2 Memory Management Endpoints

```yaml
# Get user memory
GET /v1/memory/{user_id}

Response:
{
  "user_id": "user_123",
  "working_memory": {
    "current_session": {...},
    "recent_turns": [...]
  },
  "factual_memory": {
    "profile": {...},
    "preferences": [...],
    "facts": [...]
  },
  "episodic_memory": {
    "recent_episodes": [...],
    "summary": "..."
  },
  "communication_profile": {...}
}

# Update user memory (explicit)
PUT /v1/memory/{user_id}
{
  "preferences": {
    "dietary": {
      "restrictions": ["peanut allergy"],
      "priority": "critical"
    }
  },
  "facts": [
    {"type": "name", "value": "Alice Chen"}
  ]
}

# Delete user memory
DELETE /v1/memory/{user_id}?type=episodic&before=2024-01-01

# Memory search
POST /v1/memory/{user_id}/search
{
  "query": "What did we discuss about Project X?",
  "type": "episodic",
  "limit": 5
}
```

### 7.3 Profile Management

```yaml
# Get communication profile
GET /v1/profile/{user_id}

# Update profile (override auto-detection)
PATCH /v1/profile/{user_id}
{
  "verbosity": {
    "level": "concise",
    "override": true
  },
  "technical_level": {
    "level": "expert",
    "domains": ["machine_learning"]
  }
}

# Reset profile (allow re-learning)
DELETE /v1/profile/{user_id}
```

### 7.4 Webhook Support

```yaml
# Register webhook for memory events
POST /v1/webhooks
{
  "url": "https://your-app.com/webhooks/memory",
  "events": ["memory.created", "memory.updated", "profile.updated"],
  "user_id": "user_123"  # optional, for specific user
}

# Webhook payload example
POST https://your-app.com/webhooks/memory
{
  "event": "profile.updated",
  "user_id": "user_123",
  "timestamp": "2024-11-18T10:00:00Z",
  "data": {
    "field": "technical_level",
    "old_value": "intermediate",
    "new_value": "expert",
    "confidence": 0.93
  }
}
```

---

## 8. Implementation Recommendations

### 8.1 Technology Stack

```yaml
API Gateway:
  Primary: FastAPI (Python) - rapid development, async support
  Alternative: Go (Gin/Fiber) - lower latency, harder to iterate
  Load Balancer: Nginx or Envoy

Hot Path Cache (L1):
  Primary: Redis 7.x
  Config:
    - Redis Cluster for horizontal scaling
    - Redis Streams for event bus
    - RedisJSON for structured data
    - RedisSearch for vector cache (if needed)
  Deployment: Managed (AWS ElastiCache, Redis Cloud)

Persistent Storage (L2):
  Factual Memory: PostgreSQL 15+
    - JSONB columns for flexibility
    - Partial indexes on user_id
    - Connection pooling (PgBouncer)
  
  Episodic Memory:
    Graph: Neo4j or FalkorDB
    Vector: Qdrant (self-hosted) or Pinecone (managed)
  
  Deployment: Managed where possible

Message Queue:
  Primary: Redis Streams (if already using Redis)
  Alternative: Apache Kafka (for >10k events/sec)
  Fallback: AWS SQS (simplest, managed)

Background Workers:
  Framework: Celery (Python) or Temporal (Go)
  Scaling: Kubernetes HPA based on queue depth

Classification Models:
  Intent Classification: Fine-tuned DistilBERT
  Entity Extraction: spaCy + GPT-4o-mini for complex cases
  Hosting: Modal, BentoML, or AWS SageMaker

Monitoring:
  Metrics: Prometheus + Grafana
  Tracing: OpenTelemetry → Jaeger
  Logs: Loki or CloudWatch
  Errors: Sentry

Infrastructure:
  Primary: Kubernetes on AWS/GCP
  Alternative: Railway, Render (for MVP)
```

### 8.2 Deployment Architecture

```
                    ┌─────────────────┐
                    │   CDN/Cloudflare│
                    │   (DDoS protect)│
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Load Balancer  │
                    │  (Nginx/Envoy)  │
                    └────────┬────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼────────┐  ┌────────▼────────┐  ┌───────▼────────┐
│  API Gateway   │  │  API Gateway    │  │  API Gateway   │
│  (FastAPI)     │  │  (FastAPI)      │  │  (FastAPI)     │
│  Pod 1         │  │  Pod 2          │  │  Pod N         │
└───────┬────────┘  └────────┬────────┘  └───────┬────────┘
        │                    │                    │
        └────────────────────┼────────────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼────────┐  ┌────────▼────────┐  ┌───────▼────────┐
│ Redis Cluster  │  │  PostgreSQL     │  │  Neo4j/Qdrant  │
│ (Primary)      │  │  (Primary)      │  │                 │
│                │  │                 │  │                 │
│ - Replica 1    │  │ - Replica 1     │  │                 │
│ - Replica 2    │  │ - Replica 2     │  │                 │
└───────┬────────┘  └────────┬────────┘  └───────┬────────┘
        │                    │                    │
        └────────────────────┼────────────────────┘
                             │
                    ┌────────▼────────┐
                    │  Redis Streams  │
                    │  (Event Bus)    │
                    └────────┬────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────▼────────┐  ┌────────▼────────┐  ┌───────▼────────┐
│  Worker Pool   │  │  Worker Pool    │  │  Worker Pool   │
│  (Memory Proc) │  │  (Classification│  │  (Summarization│
│                │  │   & Extraction) │  │   & Decay)     │
└────────────────┘  └─────────────────┘  └────────────────┘
```

### 8.3 Scaling Strategy

**Phase 1: MVP (0-1,000 users)**
- Single region deployment
- Redis (single instance, 8GB)
- PostgreSQL (1 primary, 1 replica)
- 3-5 API Gateway pods
- 2-3 Worker pods
- Cost: ~$500-800/month

**Phase 2: Growth (1,000-50,000 users)**
- Multi-AZ deployment
- Redis Cluster (3 primaries, 3 replicas)
- PostgreSQL (1 primary, 2 replicas, read scaling)
- 10-20 API Gateway pods (autoscaling)
- 5-10 Worker pods (queue-based scaling)
- Add Neo4j/Qdrant clusters
- Cost: ~$3,000-5,000/month

**Phase 3: Scale (50,000-500,000 users)**
- Multi-region deployment
- Redis Cluster (12+ nodes)
- PostgreSQL sharding by user_id
- 50+ API Gateway pods
- 20+ Worker pods
- Dedicated vector search cluster
- Add read replicas in multiple regions
- Cost: ~$15,000-30,000/month

**Phase 4: Enterprise (500,000+ users)**
- Global deployment
- Redis Enterprise (active-active)
- Distributed PostgreSQL (Citus or CockroachDB)
- Kubernetes across 3+ regions
- Dedicated tenants for large customers
- Cost: ~$50,000+/month

### 8.4 Data Retention & Compliance

```yaml
Data Retention Policy:
  Working Memory: 1 hour (auto-expire)
  Episodic Memory:
    - Recent (0-30 days): Full detail
    - Medium (30-90 days): Summarized
    - Old (90+ days): Archived to S3/GCS
  Factual Memory: Permanent (user-controlled deletion)

GDPR Compliance:
  Right to Access: GET /v1/memory/{user_id}
  Right to Deletion: DELETE /v1/memory/{user_id}
  Right to Portability: Export endpoint
  Data Processing Agreement: Required for EU customers

SOC 2 / ISO 27001:
  Encryption at rest: All databases
  Encryption in transit: TLS 1.3
  Audit logging: All API calls
  Access controls: RBAC on all systems
  Data isolation: Separate schemas per customer (enterprise)
```

---

## 9. Data Flow Architecture

### 9.1 Complete Request Flow

```
┌──────────────────────────────────────────────────────────────┐
│ 1. CLIENT REQUEST                                             │
│    POST /v1/chat                                              │
│    {user_id: "user_123", messages: [...]}                     │
└────────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ 2. API GATEWAY                                                │
│    • Authenticate API key                                     │
│    • Rate limiting check                                      │
│    • Route to handler                                         │
│    Latency: ~5ms                                              │
└────────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ 3. CONTEXT ASSEMBLY (Hot Path)                                │
│                                                                │
│    A. Check Redis (L1 Cache)                                  │
│       • Working memory: session:{session_id}                  │
│       • User profile: profile:{user_id}                       │
│       Latency: ~2ms                                           │
│                                                                │
│    B. On Cache Miss → Query PostgreSQL (L2)                   │
│       • SELECT * FROM users WHERE user_id = ?                 │
│       • Cache result in Redis (TTL: 1hr)                      │
│       Latency: ~20ms                                          │
│                                                                │
│    C. If Semantic Search Needed → Query Qdrant (L3)          │
│       • vector_search(query_embedding, user_id)               │
│       Latency: ~30ms                                          │
│                                                                │
│    D. Assemble Enriched Context                               │
│       • Combine: profile + working memory + episodes          │
│       • Apply profile-based prompt instructions               │
│       Latency: ~10ms                                          │
│                                                                │
│    TOTAL HOT PATH: 25-75ms (cached to full search)           │
└────────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ 4. LLM FORWARDING                                             │
│    • Construct final prompt with enriched context             │
│    • Forward to OpenAI/Anthropic API                          │
│    • Stream response back to client                           │
│    Latency: Model-dependent (~1-3s for first token)          │
└────────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ 5. ASYNC TRIGGER (Non-Blocking)                              │
│    • Publish event to Redis Streams / Kafka                   │
│    Event: {user_id, messages, response, timestamp}           │
│    Latency: ~2ms (async, no user impact)                     │
└────────────────────────────┬─────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────┐
│ 6. COLD PATH (Background Workers)                             │
│                                                                │
│    A. Intent Classification                                   │
│       • classify_intent(conversation)                         │
│       Latency: ~50ms                                          │
│                                                                │
│    B. Entity Extraction                                       │
│       • NER + LLM-based extraction                            │
│       Latency: ~100ms                                         │
│                                                                │
│    C. Memory Classification & Storage                         │
│       • Route to: Working / Episodic / Factual                │
│       • Update PostgreSQL, Neo4j, Qdrant                      │
│       Latency: ~100ms                                         │
│                                                                │
│    D. Profile Updates                                         │
│       • Update communication profile                          │
│       • Detect preferences                                    │
│       Latency: ~50ms                                          │
│                                                                │
│    E. Cache Warming                                           │
│       • Update Redis with new profile                         │
│       Latency: ~10ms                                          │
│                                                                │
│    F. Summarization (Periodic)                                │
│       • Consolidate old episodic memories                     │
│       • Archive to cold storage                               │
│       Latency: ~500ms (happens infrequently)                  │
│                                                                │
│    TOTAL COLD PATH: 300-800ms (no user impact)               │
└──────────────────────────────────────────────────────────────┘
```

### 9.2 Memory Write Flow

```
User Interaction
       │
       ▼
┌─────────────────┐
│ Working Memory  │ ← Always stored immediately
│ (Redis)         │   (synchronous, <1ms)
└─────────────────┘
       │
       │ (After response sent)
       │
       ▼
┌─────────────────┐
│  Event Bus      │ ← Async trigger
│ (Redis Streams) │
└─────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────┐
│           Background Worker                      │
│                                                   │
│  1. Classify → Intent Detector                   │
│      ↓                                            │
│  2. Extract → Entity Extractor                   │
│      ↓                                            │
│  3. Route:                                        │
│      • PREFERENCE → Factual Memory (PostgreSQL)  │
│      • FACT → Factual Memory                     │
│      • EVENT → Episodic Memory (Neo4j + Qdrant)  │
│      • STYLE → Communication Profile             │
│      ↓                                            │
│  4. Update Caches → Redis                        │
└─────────────────────────────────────────────────┘
```

### 9.3 Memory Read Flow

```
User Query
    │
    ▼
┌─────────────────────────────────────────────┐
│ Check Redis (L1) - <2ms                     │
│  Key: profile:{user_id}                     │
└─────────────────┬───────────────────────────┘
                  │
        ┌─────────┴─────────┐
        │                   │
    Cache Hit           Cache Miss
        │                   │
        │                   ▼
        │         ┌──────────────────────────┐
        │         │ Query PostgreSQL (L2)    │
        │         │  ~20ms                   │
        │         └──────────┬───────────────┘
        │                    │
        │                    │ Cache in Redis
        │                    │
        └────────┬───────────┘
                 │
                 ▼
      ┌──────────────────────┐
      │ If Semantic Search   │
      │ Needed:              │
      │  Query Qdrant (L3)   │
      │   ~30ms              │
      └──────────┬───────────┘
                 │
                 ▼
      ┌──────────────────────┐
      │ Assemble Context     │
      │  ~10ms               │
      └──────────┬───────────┘
                 │
                 ▼
           [Use in Prompt]
```

---

## 10. Security & Privacy Considerations

### 10.1 Data Security

```yaml
Encryption:
  At Rest:
    - PostgreSQL: AES-256
    - Redis: Redis encryption (if using Redis Enterprise)
    - Neo4j: Native encryption
    - Backups: Encrypted in S3/GCS
  
  In Transit:
    - TLS 1.3 for all API calls
    - mTLS between services (optional for enterprise)
  
  Application Level:
    - API keys hashed (bcrypt)
    - User IDs never exposed in logs
    - PII detection and masking in logs

Authentication & Authorization:
  API Keys:
    - Format: cq_live_xxxxxxxxxxxxx (production)
    - Format: cq_test_xxxxxxxxxxxxx (testing)
    - Scoped permissions (read, write, delete)
    - Rate limiting per key
  
  User Isolation:
    - All queries filtered by user_id
    - Row-level security in PostgreSQL
    - Separate Redis keyspaces per tenant (enterprise)

Rate Limiting:
  Tier-based:
    - Free: 100 requests/hour
    - Pro: 10,000 requests/hour
    - Enterprise: Custom limits
  
  Implementation:
    - Redis-based rate limiter
    - Per API key
    - Graceful degradation (return cached results)
```

### 10.2 Privacy & Compliance

```yaml
Data Minimization:
  - Only store what's necessary for context
  - Auto-expire ephemeral data (working memory)
  - Configurable retention periods

User Rights (GDPR):
  Right to Access:
    - GET /v1/memory/{user_id} → Full export
  
  Right to Deletion:
    - DELETE /v1/memory/{user_id} → Hard delete
    - Propagate to all stores (PostgreSQL, Neo4j, Qdrant, Redis)
  
  Right to Rectification:
    - PUT /v1/memory/{user_id} → Update incorrect data
  
  Right to Portability:
    - GET /v1/memory/{user_id}/export → JSON export

Audit Logging:
  What to Log:
    - All API requests (without PII in logs)
    - Memory access (read/write/delete)
    - Profile updates
    - Admin actions
  
  Retention:
    - 1 year for audit logs
    - Immutable, append-only storage
    - Ship to SIEM (Splunk, Datadog)

Anonymization:
  - Option to use anonymous user_id (e.g., hashed email)
  - No PII required for service to work
  - Customer controls what data is sent
```

### 10.3 Content Safety

```yaml
PII Detection:
  - Scan messages for: SSN, credit cards, passwords
  - Use regex + NER models
  - Option to mask or block detected PII
  - Alert customer if PII detected

Toxic Content:
  - Run toxicity classifier on messages
  - Flag (not block) toxic content
  - Provide content moderation API

Data Leakage Prevention:
  - Never include other users' data in context
  - Strict user_id filtering on all queries
  - Test isolation in CI/CD
```

---

## 11. Competitive Positioning

### 11.1 Competitive Landscape

| Competitor | Focus | Strengths | Weaknesses vs Context Quilt |
|------------|-------|-----------|----------------------------|
| **OpenAI Memory** | Native LLM memory | Free, integrated | Limited control, vendor lock-in, no cross-model support |
| **AWS Bedrock Memory** | Cloud-integrated | AWS ecosystem | AWS-only, not model-agnostic |
| **LangChain Memory** | Framework-level | Open source, flexible | Requires implementation, no managed service |
| **Mem0** | Episodic memory | Proven architecture | Less focus on profiles, newer |
| **Pinecone/Weaviate** | Vector databases | Semantic search | Not true memory, just RAG |

### 11.2 Context Quilt's Unique Value

**1. Drop-in Simplicity**
- Change URL + add `user_id` = done
- No complex integration, no state management

**2. Communication Profiles**
- Unique! Automatically learns user preferences
- Influences every interaction
- Not just "what happened" but "how to communicate"

**3. True Cognitive Memory**
- Working + Episodic + Factual memory system
- Based on cognitive science research
- Not just vector search disguised as memory

**4. Model-Agnostic**
- Works with any LLM provider
- Switch models without losing memory
- No vendor lock-in

**5. Production-Ready**
- Sub-100ms latency
- Handles 10k+ req/s
- Built for scale from day 1

### 11.3 Positioning Statement

> "Context Quilt is the cognitive memory layer for LLM applications. We automatically manage conversation history, user preferences, and long-term memory so developers can focus on building great AI experiences. Unlike RAG systems that retrieve documents, we create experiential memory that makes every interaction smarter."

---

## 12. Success Metrics

### 12.1 Technical Metrics

```yaml
Performance:
  P50 Latency: <30ms (context retrieval)
  P95 Latency: <75ms
  P99 Latency: <150ms
  Availability: 99.9% (3 nines)
  Error Rate: <0.1%

Memory Quality:
  Preference Detection Accuracy: >90%
  Intent Classification Accuracy: >85%
  Entity Extraction F1 Score: >0.88
  Profile Completeness: >80% after 10 interactions

Efficiency:
  Context Token Reduction: 30-50% vs full history
  Cache Hit Rate: >85%
  Memory Consolidation Time: <500ms (p95)
```

### 12.2 Business Metrics

```yaml
Adoption:
  Time to First API Call: <5 minutes
  API Integration Time: <30 minutes
  Week 1 Retention: >70%
  Month 1 Retention: >50%

Value Delivered:
  Developer Time Saved: 10+ hours/week (no state management)
  Token Cost Reduction: 30-50% (smart context)
  User Satisfaction Increase: 20%+ (personalization)
  First Contact Resolution: 15%+ improvement (for support bots)

Growth:
  API Calls/Month: Track growth
  Active Users: Track growth
  Revenue: Usage-based pricing
```

### 12.3 Customer Success Metrics

```yaml
For Support Chatbots:
  Average Handle Time (AHT): 16-50% reduction
  Customer Satisfaction (CSAT): 17-44% increase
  First Contact Resolution (FCR): Key driver improvement

For Sales Assistants:
  Lead Qualification Efficiency: $10k-30k saved/rep/year
  Conversion Rate: Track improvement

For Internal Tools:
  Developer Productivity: Up to 40% increase
  Time to Answer: 60%+ reduction
```

---

## 13. Recommendations & Next Steps

### 13.1 MVP Scope

**Core Features (Month 1-2):**
1. ✅ Basic chat endpoint with context injection
2. ✅ Three-tier memory system (Working, Episodic, Factual)
3. ✅ Automatic intent classification
4. ✅ Basic preference detection
5. ✅ Redis caching layer
6. ✅ PostgreSQL for factual memory
7. ✅ Async memory consolidation
8. ✅ API key authentication
9. ✅ Basic rate limiting

**Defer to Post-MVP:**
- ❌ Communication profiles (complex, add later)
- ❌ Graph database (start with PostgreSQL only)
- ❌ Vector search (add when semantic search needed)
- ❌ Advanced summarization (basic version first)
- ❌ Multi-region deployment
- ❌ gRPC support

### 13.2 Implementation Phases

**Phase 1: Proof of Concept (Weeks 1-2)**
- FastAPI service with basic endpoints
- Redis for caching only
- PostgreSQL for all storage
- Single classification model (intent only)
- Manual testing with 5-10 test users

**Phase 2: MVP (Weeks 3-6)**
- Add async worker pipeline
- Implement three-tier memory
- Add entity extraction
- Basic preference detection
- Deploy to staging environment
- Beta with 50-100 users

**Phase 3: Production Launch (Weeks 7-10)**
- Add monitoring (Prometheus, Grafana)
- Implement proper error handling
- Add webhook support
- Security hardening
- Launch with 500-1,000 users
- Gather feedback

**Phase 4: Scale (Months 3-6)**
- Add communication profiles
- Implement graph database for episodic memory
- Add vector search for semantic recall
- Multi-region deployment
- Enterprise features (SSO, custom retention)

### 13.3 Open Questions for Peer Review

1. **Latency Budget**: Is 50-75ms acceptable for context retrieval? Should we target lower?

2. **Memory Decay**: Is 30-day TTL for episodic memory reasonable? Should it be configurable?

3. **Classification Accuracy**: What's the minimum acceptable accuracy for preference detection? (Suggested: 90%)

4. **Privacy**: Should we provide option to disable automatic memory collection? (Suggested: Yes, opt-in)

5. **Pricing Model**: Usage-based (per API call) or subscription (per user)? (Suggested: Hybrid)

6. **GraphQL**: Any strong opinions on adding GraphQL support? (Current recommendation: No, adds latency)

7. **Self-Hosted**: Should we offer self-hosted option for enterprises? (Suggested: Yes, for large customers)

8. **Multi-Tenancy**: Separate databases per customer or shared with row-level security? (Current: Shared for standard, separate for enterprise)

---

## Appendix A: Example Code Snippets

### A.1 Context Assembly

```python
async def assemble_context(
    user_id: str,
    session_id: str,
    current_message: str,
    config: ContextConfig
) -> EnrichedContext:
    """
    Assemble enriched context from multiple sources
    Target: <25ms total
    """
    
    # Parallel fetch (run concurrently)
    working_memory_task = fetch_working_memory(session_id)
    profile_task = fetch_user_profile(user_id)
    
    working_memory, profile = await asyncio.gather(
        working_memory_task,
        profile_task
    )
    
    # Optional: Fetch episodic memory if semantic search enabled
    episodic_memory = None
    if config.semantic_search:
        episodic_memory = await fetch_episodic_memory(user_id, current_message)
    
    # Assemble
    context_parts = []
    
    # Add profile-based instructions
    if profile and config.include_profile:
        context_parts.append(format_profile_instructions(profile))
    
    # Add recent conversation history
    if working_memory and config.include_history:
        recent_turns = working_memory[-config.max_history_turns:]
        context_parts.append(format_conversation_history(recent_turns))
    
    # Add episodic memories
    if episodic_memory:
        context_parts.append(format_episodic_context(episodic_memory))
    
    # Combine
    enriched_context = "\n\n".join(context_parts)
    
    return EnrichedContext(
        system_prompt=enriched_context,
        sources=["working_memory", "profile", "episodic_memory"],
        tokens=estimate_tokens(enriched_context)
    )
```

### A.2 Intent Classification

```python
class IntentClassifier:
    def __init__(self):
        self.model = load_model("distilbert-intent-classifier")
    
    async def classify(self, user_message: str, assistant_response: str) -> Intent:
        """
        Classify conversation intent
        Target: <20ms
        """
        
        # Prepare input
        text = f"User: {user_message}\nAssistant: {assistant_response}"
        
        # Run inference
        logits = self.model(text)
        probabilities = softmax(logits)
        
        # Get top prediction
        primary_idx = np.argmax(probabilities)
        primary_intent = INTENT_CLASSES[primary_idx]
        confidence = probabilities[primary_idx]
        
        # Get secondary intents (if confidence >0.3)
        secondary_intents = [
            INTENT_CLASSES[i] 
            for i, prob in enumerate(probabilities) 
            if prob > 0.3 and i != primary_idx
        ]
        
        return Intent(
            primary=primary_intent,
            confidence=float(confidence),
            secondary=secondary_intents
        )
```

---

## Appendix B: Database Schemas

### B.1 PostgreSQL Schema

```sql
-- Users table (factual memory)
CREATE TABLE users (
    user_id VARCHAR(255) PRIMARY KEY,
    profile JSONB NOT NULL DEFAULT '{}',
    preferences JSONB NOT NULL DEFAULT '{}',
    communication_profile JSONB NOT NULL DEFAULT '{}',
    domains JSONB[] DEFAULT ARRAY[]::JSONB[],
    metadata JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Facts table
CREATE TABLE user_facts (
    fact_id VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(255) REFERENCES users(user_id) ON DELETE CASCADE,
    category VARCHAR(100) NOT NULL,
    type VARCHAR(100) NOT NULL,
    value JSONB NOT NULL,
    confidence FLOAT NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    source VARCHAR(100) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP NULL
);

-- Sessions table (for analytics)
CREATE TABLE sessions (
    session_id VARCHAR(255) PRIMARY KEY,
    user_id VARCHAR(255) REFERENCES users(user_id) ON DELETE CASCADE,
    application VARCHAR(100) NOT NULL,
    started_at TIMESTAMP DEFAULT NOW(),
    ended_at TIMESTAMP NULL,
    turn_count INTEGER DEFAULT 0,
    metadata JSONB DEFAULT '{}'
);

-- Conversation turns (for compliance/audit)
CREATE TABLE conversation_turns (
    turn_id VARCHAR(255) PRIMARY KEY,
    session_id VARCHAR(255) REFERENCES sessions(session_id) ON DELETE CASCADE,
    user_id VARCHAR(255) NOT NULL,
    user_message TEXT NOT NULL,
    assistant_response TEXT NOT NULL,
    model_used VARCHAR(100),
    tokens_used INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_user_facts_user_id ON user_facts(user_id);
CREATE INDEX idx_user_facts_category ON user_facts(category);
CREATE INDEX idx_user_facts_confidence ON user_facts(confidence DESC);
CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_conversation_turns_session_id ON conversation_turns(session_id);
CREATE INDEX idx_conversation_turns_created_at ON conversation_turns(created_at DESC);

-- GIN index for JSONB queries
CREATE INDEX idx_users_preferences ON users USING GIN (preferences);
CREATE INDEX idx_users_profile ON users USING GIN (profile);
```

### B.2 Neo4j Schema (Graph)

```cypher
// Constraints
CREATE CONSTRAINT user_id IF NOT EXISTS FOR (u:User) REQUIRE u.id IS UNIQUE;
CREATE CONSTRAINT episode_id IF NOT EXISTS FOR (e:Episode) REQUIRE e.id IS UNIQUE;
CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (ent:Entity) REQUIRE ent.id IS UNIQUE;

// Indexes
CREATE INDEX episode_timestamp IF NOT EXISTS FOR (e:Episode) ON (e.timestamp);
CREATE INDEX episode_user IF NOT EXISTS FOR (e:Episode) ON (e.user_id);

// Example relationships
(:User {id: 'user_123'})-[:HAD_CONVERSATION {
  date: datetime('2024-11-18T10:00:00Z'),
  session_id: 'sess_abc',
  summary: 'Troubleshot delivery issue'
}]->(:Episode {
  id: 'ep_xyz',
  type: 'support_interaction',
  outcome: 'resolved'
})

(:Episode)-[:INVOLVED]->(:Entity {
  id: 'ORDER-12345',
  type: 'order'
})

(:Episode)-[:RESULTED_IN]->(:Outcome {
  type: 'resolved',
  sentiment: 'positive'
})
```

---

## Appendix C: Infrastructure Platform Comparison & Cost Analysis

### C.1 Platform Recommendation: AWS (Best Overall)

**Recommendation: AWS for production, with multi-cloud strategy for enterprise tier**

**Why AWS:**
1. **Best-in-class managed services** - ElastiCache (Redis), RDS (PostgreSQL), EKS
2. **Lowest latency** - Most LLM providers (OpenAI, Anthropic) are on AWS, reducing network hops
3. **Global infrastructure** - 33 regions for multi-region deployment
4. **Mature ecosystem** - Best tooling, documentation, community support
5. **Cost optimization** - Savings plans, spot instances, reserved capacity

**When to use alternatives:**
- **GCP**: If you need tight integration with Google Workspace APIs, or prefer BigQuery for analytics
- **Azure**: If you're an enterprise customer with existing Azure commitment or need Azure AD integration
- **Multi-cloud**: For enterprise tier to avoid vendor lock-in

### C.2 Detailed Cost Comparison (Phase 1 - MVP)

**Target: 1,000 users, 100,000 API calls/day (~3 million/month)**

#### AWS (Recommended for MVP)

```yaml
Compute:
  - EKS Control Plane: $73/month
  - API Gateway Pods (2x t3.small = 2 vCPU, 2GB each): $30/month
  - Worker Pods (2x t3.small): $30/month
  - Total Compute: $133/month

Database:
  - RDS PostgreSQL (db.t3.medium: 2 vCPU, 4GB): 
      On-Demand: $118/month
      1-year Reserved: $71/month (40% savings)
  - Backup storage (50GB): $5/month
  - Total Database: $76/month (with reserved)

Cache:
  - ElastiCache Redis (cache.t3.medium: 2 vCPU, 3.09GB):
      On-Demand: $84/month
      1-year Reserved: $50/month (40% savings)
  - Total Cache: $50/month (with reserved)

Networking:
  - Application Load Balancer: $23/month (base) + $0.008/LCU-hour
  - LCU hours (low traffic): ~$8/month
  - Data transfer out (50GB): $4.50/month
  - Total Networking: $35.50/month

Storage:
  - S3 for backups/archives (100GB): $2.30/month
  - EBS volumes (100GB GP3): $8/month
  - Total Storage: $10.30/month

Monitoring & Logs:
  - CloudWatch logs (10GB): $5/month
  - CloudWatch metrics: $10/month
  - Total Monitoring: $15/month

TOTAL AWS (MVP): $319.80/month
With on-demand (no reserved): $435/month
```

#### GCP (Alternative)

```yaml
Compute:
  - GKE Cluster (zonal): $74/month
  - API Gateway (2x e2-small): $27/month
  - Workers (2x e2-small): $27/month
  - Total Compute: $128/month

Database:
  - Cloud SQL PostgreSQL (db-custom-2-8192):
      On-Demand: $150/month
      1-year Committed: $106/month (30% savings)
  - Total Database: $106/month

Cache:
  - Memorystore Redis (Basic, 4GB): $120/month
    Note: GCP Redis is more expensive than AWS
  - Total Cache: $120/month

Networking:
  - Load Balancer: $18/month + $0.008/GB
  - Egress (50GB): $6/month
  - Total Networking: $24/month

Storage:
  - Cloud Storage (100GB): $2/month
  - Persistent Disks (100GB): $10/month
  - Total Storage: $12/month

Monitoring:
  - Cloud Logging (10GB): $5/month
  - Cloud Monitoring: $8/month
  - Total Monitoring: $13/month

TOTAL GCP (MVP): $403/month
With on-demand: $493/month
```

#### Azure (Alternative)

```yaml
Compute:
  - AKS (free control plane)
  - API Gateway (2x B2s VMs): $62/month
  - Workers (2x B2s VMs): $62/month
  - Total Compute: $124/month

Database:
  - Azure Database for PostgreSQL (Gen5, 2 vCore):
      Pay-as-you-go: $146/month
      1-year Reserved: $97/month (34% savings)
  - Total Database: $97/month

Cache:
  - Azure Cache for Redis (C1 Standard: 1GB): $76/month
    Note: Need C2 (2.5GB) for better performance: $152/month
  - Total Cache: $152/month (C2)

Networking:
  - Application Gateway (Basic): $18/month
  - Data transfer (50GB): $4/month
  - Total Networking: $22/month

Storage:
  - Blob Storage (100GB): $2/month
  - Managed Disks (100GB): $10/month
  - Total Storage: $12/month

Monitoring:
  - Log Analytics (10GB): $12/month
  - Application Insights: $8/month
  - Total Monitoring: $20/month

TOTAL Azure (MVP): $427/month
With on-demand: $542/month
```

#### Budget Hosting Options (NOT RECOMMENDED for Production)

```yaml
Hostinger / DigitalOcean / Hetzner - NOT suitable for Context Quilt

Why NOT recommended:
  ❌ No managed Redis cluster (critical for performance)
  ❌ No managed PostgreSQL with read replicas
  ❌ Limited global infrastructure (higher latency)
  ❌ Less reliable networking
  ❌ No enterprise SLA guarantees
  ❌ Harder to scale horizontally

If you MUST use budget hosting (testing only):

Hetzner (Cheapest, EU-based):
  - CPX31 (4 vCPU, 8GB) for API: €13/month ($14)
  - CPX31 for Workers: €13/month ($14)
  - CPX21 (3 vCPU, 4GB) for Redis: €9/month ($10)
  - CPX21 for PostgreSQL: €9/month ($10)
  - Load Balancer: €5/month ($5)
  - Snapshots/Backups: €5/month ($5)
  TOTAL: ~$58/month
  
  ⚠️ Critical Issues:
  - Single region (Germany), 80-120ms latency to US
  - Manual Redis setup (no managed service)
  - Manual PostgreSQL replication
  - No auto-scaling
  - You manage everything (high ops burden)
  - Fails <50ms hot path requirement

DigitalOcean (Slightly better):
  - App Platform (2x containers): $24/month
  - Managed PostgreSQL (2 vCPU, 4GB): $60/month
  - Managed Redis (1GB): $15/month
  - Load Balancer: $12/month
  - Spaces (storage): $5/month
  TOTAL: ~$116/month
  
  ⚠️ Issues:
  - Limited to 1GB Redis (too small)
  - No Kubernetes (harder to scale)
  - Fewer regions than AWS/GCP
  - Less mature managed services
```

### C.3 Cost Comparison Summary (MVP Phase)

| Platform | Monthly Cost | Best For | Latency to LLMs | Scalability | Ops Burden |
|----------|--------------|----------|-----------------|-------------|------------|
| **AWS** | **$320-435** | **Production** | **5-10ms** | **Excellent** | **Low** |
| GCP | $403-493 | Google integration | 10-15ms | Excellent | Low |
| Azure | $427-542 | Enterprise/MS shops | 15-20ms | Good | Medium |
| Hetzner | $58 | Testing only | 80-120ms | Manual | Very High |
| DigitalOcean | $116 | Small projects | 40-60ms | Limited | Medium |

### C.4 Phase 2 - Growth (50,000 users, 5M calls/day)

#### AWS (Recommended)

```yaml
Compute (EKS):
  - Control Plane: $73/month
  - API Gateway: 10x t3.medium (2 vCPU, 4GB): $380/month
  - Workers: 5x t3.medium: $190/month
  - Total Compute: $643/month

Database (RDS PostgreSQL):
  - Primary: db.r5.large (2 vCPU, 16GB): $313/month (reserved)
  - Read Replica 1: db.r5.large: $313/month
  - Read Replica 2: db.r5.large: $313/month
  - Backup storage (500GB): $48/month
  - Total Database: $987/month

Cache (ElastiCache Redis):
  - Redis Cluster: 3x cache.r5.large (2 vCPU, 13.07GB): $900/month (reserved)
  - Total Cache: $900/month

Graph Database (Self-hosted Neo4j on EC2):
  - Instance: r5.xlarge (4 vCPU, 32GB): $330/month (reserved)
  - EBS storage (500GB GP3): $40/month
  - Total Neo4j: $370/month

Vector DB (Self-hosted Qdrant on EC2):
  - Instance: r5.2xlarge (8 vCPU, 64GB): $660/month (reserved)
  - EBS storage (1TB GP3): $80/month
  - Total Qdrant: $740/month

Networking:
  - Application Load Balancer: $23/month
  - LCU hours (medium traffic): $80/month
  - Data transfer out (1TB): $90/month
  - Total Networking: $193/month

Storage & Monitoring: $263/month

Message Queue (Redis Streams - included in Redis)

TOTAL AWS (Growth): $4,096/month
  - Without Neo4j/Qdrant (defer): $2,986/month
```

### C.5 Phase 3 - Scale (500,000 users, 50M calls/day)

#### AWS Enterprise Architecture

```yaml
Compute (Multi-Region):
  - EKS Clusters (3 regions): $219/month
  - API Gateway: 50x t3.large: $3,500/month
  - Workers: 20x t3.large: $1,400/month
  - Total Compute: $5,119/month

Database (Aurora Global):
  - Primary + 2 secondaries + 6 replicas: $7,500/month

Cache (Redis Global Datastore):
  - 3 regions, r5.2xlarge clusters: $4,800/month

Graph/Vector:
  - Neo4j AuraDB Enterprise: $3,000/month
  - Qdrant Cloud: $2,500/month
  - Total: $5,500/month

Networking, Storage, Monitoring: $2,830/month

TOTAL AWS (Scale): $25,749/month

With optimizations (Savings Plans, Reserved Instances):
  - Optimized: $17,000-19,000/month
```

### C.6 Why AWS for Best Performance

**Latency to LLM Providers:**
```yaml
OpenAI API Location: AWS us-east-1
Anthropic API Location: AWS us-east-1

AWS us-east-1 → OpenAI: 5-10ms ✅
GCP us-central1 → OpenAI: 10-15ms ⚠️
Azure eastus → OpenAI: 15-20ms ⚠️
Hetzner EU → OpenAI: 80-120ms ❌

Recommendation: Deploy in AWS us-east-1 for lowest latency
```

**Managed Services Quality:**
```yaml
Redis Performance:
  AWS ElastiCache: 1-2ms read latency ✅
  GCP Memorystore: 2-3ms read latency ⚠️
  Azure Cache: 3-5ms read latency ⚠️
  Self-hosted: 2-10ms (depends on setup) ⚠️

PostgreSQL Performance:
  AWS Aurora: 5-10ms read latency ✅
  GCP Cloud SQL: 8-12ms read latency ⚠️
  Azure PostgreSQL: 10-15ms read latency ⚠️
```

### C.7 Final Recommendation

**For Context Quilt:**

1. **MVP (0-10k users):** AWS us-east-1
   - Cost: $320-435/month
   - Services: EKS, ElastiCache, RDS
   - No Neo4j/Qdrant initially

2. **Growth (10k-100k users):** AWS Multi-AZ
   - Cost: $3,000-4,000/month
   - Add: Neo4j, Qdrant, read replicas
   - Regions: us-east-1 + us-west-2

3. **Scale (100k-1M users):** AWS Multi-Region
   - Cost: $17,000-20,000/month (optimized)
   - Services: Aurora Global, Redis Global
   - Regions: us-east-1, us-west-2, eu-west-1

**DO NOT use Hostinger/budget hosting** - the 70-110ms latency penalty makes it impossible to meet the <50ms hot path requirement, which is critical for Context Quilt's value proposition.
```

### C.2 LLM Costs (Pass-Through)

```yaml
Classification Models (Self-Hosted):
  - Intent Classifier: $200/month (Modal/BentoML)
  - Entity Extraction (spaCy): Free (self-hosted)
  - LLM for complex extraction (GPT-4o-mini): ~$50/month

Customer's LLM Costs:
  - Pass-through (customer pays OpenAI/Anthropic directly)
  - We add ~100-200 tokens per request for context
  - Additional cost: ~$0.0002-0.0004 per request
```

---

## Document Change Log

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 1.0 | 2024-11-18 | Initial draft | Context Quilt Team |

---

**End of Document**

For questions or feedback on this architecture proposal, please contact: [architecture@contextquilt.com]
