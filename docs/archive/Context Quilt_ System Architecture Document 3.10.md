# **Context Quilt: System Architecture Document**

## **Scalable Cognitive Memory Infrastructure for AI Applications**

Version: 3.10  
Date: December 12, 2025  
Status: Implementation Ready - Comprehensive Technical Specifications

## **1. System Overview**

### **1.1 Core Value Proposition: A Paradigm Shift in AI Memory**

Context Quilt signifies a foundational advancement in the interaction paradigm between Artificial Intelligence and human users, transitioning from isolated, stateless exchanges to a continuous, cognitive engagement model. It operates as a sophisticated **Context Enrichment API**, empowering AI applications to retain a comprehensive memory of user interactions not merely within the confines of a single session, but across the entirety of the user lifecycle. This persistence spans days, weeks, and potentially years, transforming generic, one-size-fits-all AI responses into highly personalized, context-aware dialogues that foster trust, enhance efficiency, and ensure continuity. By transcending simple keyword matching to achieve a profound understanding of user preferences, history, and intent, Context Quilt enables applications to anticipate user needs proactively rather than simply reacting to explicit commands.  
The system guarantees sub-20ms latency for context retrieval, ensuring that the user experience remains instantaneous and responsive—a non-negotiable requirement for modern, high-performance applications where even minor delays can degrade user satisfaction. Concurrently, Context Quilt employs a robust, multi-stage asynchronous learning pipeline to continuously refine its understanding of the user. This dual-path architecture facilitates deep, meaningful profile construction without compromising real-time performance, effectively resolving the historical trade-off between computational intelligence and system responsiveness. The architecture is inherently designed for horizontal scalability, capable of supporting millions of concurrent users while strictly adhering to latency service level agreements (SLAs).  
Key Differentiator:  
The contemporary landscape of AI memory solutions is predominantly characterized by two distinct approaches, each possessing inherent limitations when deployed in isolation:

* **Retrieval-Augmented Generation (RAG)** systems are highly effective at retrieving static documents and corporate knowledge bases to ground AI responses in factual data. However, they fundamentally lack the capacity to track the dynamic, evolving state of an individual user. While a RAG system may understand company policy, it remains ignorant of the user's identity, role, or specific context. Consequently, RAG interactions are inherently impersonal, treating each query as a discrete event against a static corpus.  
* **Vector Databases** serve as the foundational storage mechanism for semantic search, housing raw embeddings of text segments. While indispensable for similarity matching, they function as a low-level infrastructure component requiring significant engineering overhead to manage user identity, temporal relevance, and state effectively. They provide the storage medium—the "hard drive"—but lack the organizational logic—the "operating system"—necessary for coherent memory management, leaving developers to construct complex retrieval and context management logic from scratch.

Context Quilt bridges this critical functional gap by managing **Stateful User Memory**. It organizes user understanding into four distinct, interconnected layers of "Context Patches":

* **Identity:** This layer encompasses immutable attributes such as the user's name, professional role, verified credentials, and unique identifiers. It forms the stable core of the user's digital twin.
* **Preferences:** This layer captures explicit and implicit constraints, likes, and dislikes (e.g., dietary restrictions, preferred tools). These parameters guide decision-making.
* **Traits:** This layer consists of behavioral and stylistic characteristics inferred from interactions (e.g., technical proficiency, patience levels, verbosity).
* **Experience:** This layer captures the historical context of what the user has *done* or *gone through* (e.g., "Deployed a Kubernetes cluster last week", "Traveled to Tokyo"). It provides the episodic grounding for current interactions.

This rich, structured context is seamlessly injected into Large Language Model (LLM) prompts via a transparent, developer-friendly **Template System**. This ensures that every interaction is informed by a comprehensive understanding of the user, eliminating the need for complex, ad-hoc prompt engineering by the developer. This abstraction layer simplifies the development process, allowing engineering teams to focus on creating superior application logic rather than managing intricate memory infrastructure.

### **1.2 The "Template API" Pivot: Empowering Developers**

We have strategically transitioned away from the traditional LLM Gateway model, which functions as a heavy proxy for all LLM traffic. This pivot to a **Template-based Context Enrichment API** offers substantial advantages in terms of flexibility, reliability, and developer control. Instead of routing mission-critical LLM requests through our infrastructure—thereby creating a potential single point of failure and adding latency—developers invoke Context Quilt to *enrich* their prompts with context, subsequently calling their LLM provider directly.

**Detailed Interaction Flow:**

1. **Application Request:** The developer's application initiates the interaction by issuing a call to the /v1/enrich endpoint. The request payload includes a prompt template containing specific placeholders for dynamic content, such as User Profile: [[food_allergies]] or Strategy: [[guidance_strategy]].
2. **Context Resolution:** Upon receipt, Context Quilt processes the template. It parses the string to identify the requested variables (e.g., food_allergies) and retrieves the corresponding values from its high-speed Redis cache or persistent PostgreSQL storage. This step includes permission verification and data freshness validation.
3. **Enrichment Response:** Context Quilt returns the fully populated text string to the application. For example: User Profile: Peanuts, Shellfish.
4. **LLM Interaction:** The application transmits this enriched text to its chosen LLM provider (e.g., OpenAI, Anthropic, or a local open-source model).
5. **Asynchronous Learning:** Following the completion of the LLM interaction, the application transmits the full chat log (comprising both user input and assistant response) to the /v1/memory/update endpoint. This action triggers the asynchronous **Learning Pipeline**, enabling Context Quilt to update its memory stores without impacting the user's latency.

## **2. Architecture: Dual-Path Processing**

To reconcile the seemingly contradictory objectives of ultra-low latency retrieval and deep, computationally intensive learning, we employ a **Hot/Cold Architecture**.

### **2.1 The Hot Path (Synchronous)**

* **Goal:** < 20ms Response Time (P99) for context retrieval.
* **Responsibility:** Read-only retrieval of pre-computed, cached context.
* **Infrastructure:** High-performance **FastAPI** application server integrated with **Redis** (Active Cache).
* **Detailed Logic:**  
  1. **Receive Template:** Accept template string.
  2. **Parse Variables:** Regex scan for placeholders `[[variable_name]]`.
  3. **Fetch values from Redis:** Batched MGET lookup.
  4. **Access Control Check:** Verify App ID authorizations.
  5. **Inject strings into Template:** Substitute values.
  6. **Return to caller:** Immediate response.

### **2.2 The Cold Path (Asynchronous)**

* **Goal:** **Deep Learning & Extraction**. Zero user-facing latency.
* **Responsibility:** The "brain" of the system: extracting facts, inferring traits, classifying information, and updating the database.
* **Infrastructure:** Orchestrated by Python Workers utilizing **Qwen 2.5 (7B)** (via Ollama on NVIDIA L4 GPU) for intelligence and **PostgreSQL** as the "Cold Storage" vault.
* **Detailed Logic (The Pipeline):**  
  1. **User Interaction:** Chat log received via API.
  2. **Trigger Pipeline:** The "Picker" picks relevant facts.
  3. **Classification:** The "Designer" assigns types (Identity, Preference, Trait, Experience).
  4. **Persistence:** The "Cataloger" saves to PostgreSQL.
  5. **Hydration:** Updates Redis for the next Hot Path request.

## **3. Data Architecture: The "Hydration" Model**

We utilize **PostgreSQL** as the central "Vault" and **Redis** as the "Active Context" layer.

### **3.1 Storage Layers**

| Tier | Technology | Role | Data Types and Usage |
| :---- | :---- | :---- | :---- |
| **Cold Storage** | **PostgreSQL** | **The Vault** | **Identity, Preference, Trait, Experience**. Stores the definitive record of all "Context Patches" (Facts) and their metadata (Origin, Confidence, Source). |
| **Hot Storage** | **Redis** | **Active Context** | **Hydrated Profile**. A flattened JSON object containing all active variables for text injection. |

### **3.2 The Pre-fetch ("Hydration") Workflow**

1. **Trigger:** Session start or first cached miss triggers hydration.
2. **Action:** "Heavy" query against PostgreSQL fetches complete profile.
3. **Cache:** Serialized to Redis with TTL (e.g., 30 mins).
4. **Usage:** Subsequent `/enrich` calls read exclusively from Redis.

## **4. Variable Discovery & Definition**

We support a **Declarative Memory Model**. Developers define *what* information they want to track (the schema) in `memory_schema.yaml`, and the system handles the extraction.

### **4.1 The Variable Lifecycle**

| Stage | Status | Behavior |
| :---- | :---- | :---- |
| **1. Candidate** | **Discovered** | Detected by the Learning Pipeline but not yet in Schema. "Suggested" to developer. |
| **2. Registered** | **Dormant** | In Schema, but not used in active Templates. Stored in DB, not Cached. |
| **3. Active** | **Live** | Used in a live Template. Actively pre-fetched and Cached in Redis. |

## **5. The Four-Stage Learning Pipeline**

Our Async Worker utilizes **Qwen 2.5 (7B-Instruct)** to execute a sophisticated memory consolidation pipeline known as **"Picker-Stitcher-Designer-Cataloger"**. This modular approach turns raw text into structured "Context Patches".

### **5.1 Stage 1: The Picker (Discovery)**

*   **Role:** The "Detective".
*   **Task:** Reads the raw conversation transcript. It "picks" out salient information about the user, separating signal from noise. It uses Chain-of-Thought prompting to identify facts.
*   **Prompt Strategy:** "You are the Data Detective... Identify facts about the user... Output ONLY what the USER reveals."
*   **Output:** A raw list of extracted fact candidates (e.g., "User likes dark mode", "User is a python dev").

### **5.2 Stage 2: The Stitcher (Synthesis)**

*   **Role:** The "Weaver".
*   **Task:** Takes the raw text output from the Picker and structures it. It standardizes the format, deduplicates information, and prepares it for classification. It "stitches" separate observations into coherent potential patches.
*   **Output:** Structured objects ready for typing.

### **5.3 Stage 3: The Designer (Classification)**

*   **Role:** The "Architect".
*   **Task:** Analyzes each specific fact and assigns it a **Context Type**:
    *   **Identity:** Who they are.
    *   **Preference:** What they want.
    *   **Trait:** How they behave.
    *   **Experience:** What they have done.
*   **Refinement:** It also assigns metadata like **Confidence Score** and determines if the fact updates an existing patch or creates a new one.

### **5.4 Stage 4: The Cataloger (Persistence)**

*   **Role:** The "Librarian".
*   **Task:** The final commit phase. It writes the structured "Context Patches" to the **PostgreSQL** database.
*   **Indexing:** It manages vector embedding (via pgvector) for semantic search and ensures the **Redis** cache is invalidated/updated so the user's next interaction immediately reflects the new memory.

## **6. Implementation & Cost Model**

### **6.1 Infrastructure**

*   **Compute:** We utilize Google Cloud **g2-standard-4** instances (or equivalent), equipped with the **NVIDIA L4 GPU**.
*   **Model:** **Qwen 2.5 (7B Coder/Instruct)**. We have benchmarked Qwen 2.5 as superior to Mistral for structured JSON extraction and code understanding tasks.
*   **Orchestration:** Docker Compose (Development) / Kubernetes (Production).

### **6.2 Cost Estimate**

*   **Workload:** Processing 1,000 conversations.
*   **Hardware:** NVIDIA L4 GPU instance (~$0.75/hr on-demand, ~$0.23/hr spot).
*   **Throughput:** Qwen 2.5 on L4 achieves high throughput, allowing for < $1.00 cost per 1,000 deep-learning cycles.

## **7. Headless Agent Integration**

Context Quilt supports autonomous agents via **Execution Traces**.

*   **Endpoint:** `/v1/memory`
*   **Interaction Type:** `trace`
*   **Payload:** Captures the "Thought", "Tool Call", and "Tool Output" of an agent loop.
*   **Benefit:** The **Picker** is tuned to analyze `tool_call` arguments (e.g., `search_flights(max_price=500)`) to extract implicit constraints (e.g., "Budget < $500") even if the user never stated them explicitly. This effectively gives stateless agents a persistent memory across varied tasks.
