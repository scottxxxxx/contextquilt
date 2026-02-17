"""
Context Quilt - Cold Path Worker
Handles Async Memory Consolidation & "The Four Prompts"
"""

import asyncio
import json
import os
import structlog
import redis.asyncio as redis
import asyncpg
from typing import Dict, Any, List
from datetime import datetime
import uuid

# Configure Logging
logger = structlog.get_logger()

# Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")


def classify_fact(fact_text: str) -> str:
    """
    Hybrid Approach: Python-based fact classification.
    Let the LLM extract facts (fuzzy work), let Python classify (strict work).
    
    Categories (PatchCategory):
    - identity: Who the user is (name, role, team, job title, skills/expertise)
    - preference: The 'What' - Content choices (likes, dislikes, constraints, e.g. "Vegan")
    - trait: The 'How' - Style/Behavior (communication style, personality, e.g. "Concise")
    - experience: Episodic memory (projects, past events, specific interactions)
    """
    fact_lower = fact_text.lower()
    
    # Identity patterns - who they are & what they know (Skills are part of Identity)
    identity_keywords = [
        'name is', 'i am a', 'my role', 'works as', 'job title',
        'team', 'works at', 'employed', 'developer', 'engineer',
        'manager', 'designer', 'analyst', 'architect', 'lead',
        'knows', 'know ', 'experienced', 'expert', 'familiar with', 'proficient',
        'years of', 'using python', 'using rust', 'using java', 'using typescript',
        'certified', 'degree in', 'can write', 'programs in', 'codes in', 'fluent in'
    ]
    if any(kw in fact_lower for kw in identity_keywords):
        return 'identity'
    
    # Preference patterns - what they prefer
    preference_keywords = [
        'prefers', 'likes', 'loves', 'favorite', 'dislikes', 'hates',
        'hate', 'dislike', 'love', 'like to', # Added base forms
        'rather', 'instead of', 'over', 'better than', 'prefer',
        'chooses', 'enjoys', 'appreciates', 'avoids', 'doesn\'t like',
        'vegan', 'aisle seat' # Examples from user
    ]
    if any(kw in fact_lower for kw in preference_keywords):
        return 'preference'
    
    # Experience patterns - what they are doing / have done (Projects are Experiences)
    experience_keywords = [
        'working on', 'current project', 'building', 'developing',
        'implementing', 'debugging', 'refactoring', 'migrating',
        'task', 'sprint', 'roadmap', 'deadline', 'milestone',
        'remember when', 'last week', 'yesterday', 'meeting', 'discussed'
    ]
    if any(kw in fact_lower for kw in experience_keywords):
        return 'experience'
    
    # Trait patterns - how they behave
    trait_keywords = [
        'concise', 'technical', 'verbose', 'detailed', 'simple',
        'tone', 'style', 'responds', 'slow', 'fast'
    ]
    if any(kw in fact_lower for kw in trait_keywords):
        return 'trait'
    
    # Default to trait (behavioral patterns) or experience if it looks like an event
    return 'trait'


def extract_facts_from_response(raw_response: str) -> List[str]:
    """
    Deterministic Guardrail: Extract facts from any LLM output format.
    Implements the "Layer 3" code fix for unreliable LLM outputs.
    
    Handles:
    - JSON array: ["fact1", "fact2"]
    - JSON objects: {"fact": "...", "category": "..."}
    - FINAL JSON block from Detective prompt
    - Plain text sentences as fallback
    """
    import re
    
    facts = []
    
    # Strategy 1: Look for FINAL JSON block (Detective prompt output)
    final_json_match = re.search(r'FINAL JSON:\s*\[([^\]]+)\]', raw_response, re.IGNORECASE | re.DOTALL)
    if final_json_match:
        try:
            json_str = '[' + final_json_match.group(1) + ']'
            parsed = json.loads(json_str)
            for item in parsed:
                if isinstance(item, str):
                    facts.append(item)
                elif isinstance(item, dict) and 'fact' in item:
                    facts.append(item['fact'])
            if facts:
                return facts
        except json.JSONDecodeError:
            pass
    
    # Strategy 2: Look for JSON array anywhere in response
    try:
        start_idx = raw_response.find('[')
        end_idx = raw_response.rfind(']')
        if start_idx != -1 and end_idx != -1:
            json_str = raw_response[start_idx:end_idx+1]
            parsed = json.loads(json_str)
            for item in parsed:
                if isinstance(item, str):
                    facts.append(item)
                elif isinstance(item, dict) and 'fact' in item:
                    facts.append(item['fact'])
            if facts:
                return facts
    except json.JSONDecodeError:
        pass
    
    # Strategy 3: Extract individual JSON objects with regex
    obj_pattern = r'\{[^{}]*"fact"\s*:\s*"([^"]+)"[^{}]*\}'
    matches = re.findall(obj_pattern, raw_response, re.DOTALL)
    if matches:
        return matches
    
    # Strategy 4: Extract quoted strings (potential facts)
    quoted = re.findall(r'"([^"]{10,})"', raw_response)
    if quoted:
        # Filter out JSON-like noise and keep fact-like statements
        facts = [q for q in quoted if not q.startswith('{') and 'fact' not in q.lower()]
        if facts:
            return facts[:5]  # Limit to 5 facts
    
    # Strategy 5: FINAL FALLBACK - Extract sentences from plain text
    # Only use if response looks like prose (not JSON attempts)
    if '{' not in raw_response and '[' not in raw_response:
        # Split on sentence boundaries
        sentences = re.split(r'[.!?]\s+', raw_response.strip())
        for sentence in sentences:
            sentence = sentence.strip()
            # Keep sentences that look like facts about the user
            if len(sentence) > 15 and any(kw in sentence.lower() for kw in 
                ['user', 'prefers', 'likes', 'knows', 'uses', 'works', 'new to', 'i am', 'i use', 'i prefer']):
                facts.append(sentence)
        if facts:
            return facts[:5]
    
    return facts


def batch_messages(messages: List[Dict], batch_size: int = 10) -> List[List[Dict]]:
    """
    Batch long conversations into chunks of `batch_size` messages.
    Prevents LLM timeout on long conversations and improves focus.
    
    Args:
        messages: List of chat messages
        batch_size: Max messages per batch (default 10)
    
    Returns:
        List of message batches
    """
    if len(messages) <= batch_size:
        return [messages]
    
    batches = []
    for i in range(0, len(messages), batch_size):
        batch = messages[i:i + batch_size]
        batches.append(batch)
    
    logger.info("conversation_batched", total_messages=len(messages), batches=len(batches))
    return batches

class ColdPathWorker:
    def __init__(self):
        self.redis = None
        self.db = None
        self.running = False
        
    async def start(self):
        """Initialize connections and start processing loop"""
        logger.info("worker_starting")
        
        # Connect to Redis
        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        
        # Connect to Postgres
        self.db = await asyncpg.connect(DATABASE_URL)
        
        self.running = True
        logger.info("worker_ready")
        
        # Start Stream Consumer
        await self.consume_stream()
        
    async def stop(self):
        """Cleanup connections"""
        self.running = False
        if self.redis:
            await self.redis.close()
        if self.db:
            await self.db.close()
        logger.info("worker_stopped")

    async def consume_stream(self):
        """Main Loop: Consume from Redis Stream"""
        stream_key = "memory_updates"
        group_name = "workers"
        consumer_name = f"worker_{os.getpid()}"
        
        # Create Consumer Group (ignore if exists)
        try:
            await self.redis.xgroup_create(stream_key, group_name, mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        while self.running:
            try:
                # Read from stream
                entries = await self.redis.xreadgroup(
                    group_name, consumer_name, {stream_key: ">"}, count=1, block=5000
                )
                
                if not entries:
                    continue
                    
                for stream, messages in entries:
                    for message_id, data in messages:
                        try:
                            payload = json.loads(data["data"])
                            await self.process_task(payload)
                            # Ack message
                            await self.redis.xack(stream_key, group_name, message_id)
                        except Exception as e:
                            logger.error("processing_failed", error=str(e), message_id=message_id)
                            
            except Exception as e:
                logger.error("stream_error", error=str(e))
                await asyncio.sleep(1)

    async def process_task(self, payload: Dict[str, Any]):
        """Router for different task types"""
        task_type = payload.get("interaction_type") or payload.get("type")
        user_id = payload.get("user_id")
        
        logger.info("processing_task", type=task_type, user_id=user_id)
        
        if task_type == "hydrate":
            await self.hydrate_cache(user_id)
        elif task_type == "tool_call":
            await self.handle_active_learning(payload)
        elif task_type == "trace":
            await self.handle_passive_learning(payload)
        elif task_type == "chat_log":
            await self.handle_chat_log(payload)
        else:
            logger.warning("unknown_task_type", type=task_type)

    async def get_prompt(self, key: str) -> str:
        """Fetch system prompt from DB or fallback to default if missing (safety)"""
        try:
            row = await self.db.fetchrow("""
                SELECT prompt_text 
                FROM prompt_versions 
                WHERE prompt_key = $1 AND is_active = TRUE
                ORDER BY version_num DESC 
                LIMIT 1
            """, key)
            
            if row:
                return row["prompt_text"]
        except Exception as e:
            logger.error("prompt_fetch_failed", key=key, error=str(e))
        
        # Fallbacks to prevent crash if DB is empty/down
        if key == "archivist":
             return """<s>[INST] You are the Archivist. Analyze this agent execution trace.
Extract key facts about the user and classify each into a category.
Pay close attention to the agent's "Internal Monologue" (thoughts) and "Tool Inputs/Outputs" as they often reveal hidden user constraints (e.g., budget < $500).

Categories to use:
- identity: Who the user is (name, role, team, credentials, skills)
- preference: What the user prefers (tools, styles, methods, constraints, likes/dislikes)
- trait: Behavioral patterns (communication style, work habits, personality)
- experience: Episodic memory (current projects, past events, specific interactions)

Return ONLY a JSON object with 'facts' (list of objects with 'fact' and 'category').
Example: {{"facts": [{{"fact": "Prefers dark mode", "category": "preference"}}]}}

TRACE:
{trace_text}
[/INST]"""
        elif key == "detective":
            return """<s>[INST] You are the Data Detective for the Context Quilt system.
Your goal is to extract facts about the user from a conversation.

[INSTRUCTIONS]
1. Read the conversation transcript below.
2. First, write a "THOUGHT PROCESS" block. Go through the transcript line by line and identify facts about the user.
3. Then, output a "FINAL JSON" block with an array of fact strings.
4. Focus on: identity, preferences, traits, and experiences.
5. Output ONLY what the USER reveals about themselves, not the assistant.

[CHAT LOG]
{chat_text}

[OUTPUT FORMAT]
THOUGHT PROCESS:
(your analysis here)

FINAL JSON:
["fact 1", "fact 2", ...]
[/INST]THOUGHT PROCESS:"""
        return ""

    # ============================================
    # Handlers
    # ============================================

    async def hydrate_cache(self, user_id: str):
        """
        Hydration Workflow: Postgres -> Redis
        Fetches full profile and active context.
        """
        # 1. Fetch from Postgres (The Vault)
        try:
            row = await self.db.fetchrow("SELECT variables, last_updated FROM profiles WHERE user_id = $1", user_id)
        except Exception as e:
            logger.error("db_fetch_failed", error=str(e), user_id=user_id)
            return

        if not row:
            logger.warning("user_not_found", user_id=user_id)
            return

        # Handle JSONB serialization
        variables = row["variables"]
        if isinstance(variables, str):
            variables = json.loads(variables)
            
        profile_data = {
            "variables": variables,
            "last_updated": row["last_updated"].isoformat() if row["last_updated"] else "now"
        }
        
        # 2. Write to Redis (Hot Cache)
        cache_key = f"active_context:{user_id}"
        await self.redis.set(cache_key, json.dumps(profile_data), ex=3600) # 1 hour TTL
        
        logger.info("hydration_complete", user_id=user_id)

    async def handle_active_learning(self, payload: Dict[str, Any]):
        """
        Active Learning: Agent explicitly saved a fact.
        Direct write to Postgres + Invalidate Cache.
        """
        fact = payload.get("fact")
        category = payload.get("category")
        user_id = payload.get("user_id")
        
        if not fact or not user_id:
            logger.warning("missing_fact_data", user_id=user_id)
            return

        source = payload.get("source", "explicit")
        timestamp = payload.get("timestamp")
        persistence = payload.get("persistence", "sticky")

        try:
            # Ensure user exists
            await self.db.execute(
                "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                user_id
            )
            
            # 1. Write to Postgres (New Schema)
            # source='explicit' -> origin_mode='declared'
            # 1. Write to Postgres (New Schema)
            # source='explicit' -> origin_mode='declared'
            patch_id = str(uuid.uuid4())
            subject_key = f"user:{user_id}"
            origin_mode = "declared" if source == "explicit" else "inferred"
            patch_name = f"active_learning_{patch_id[:8]}" 

            # Value must be JSONB. Wrapping string fact in JSON string or object.
            # Using JSON object for structure
            value_json = json.dumps({"text": fact})
            
            created_at = datetime.fromisoformat(timestamp) if timestamp else datetime.utcnow()

            # Insert Patch (No subject_key)
            await self.db.execute(
                """
                INSERT INTO context_patches (
                    patch_id, patch_name, patch_type, value,
                    origin_mode, source_prompt, confidence, persistence, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                patch_id, patch_name, category, value_json,
                origin_mode, "manual", 1.0, persistence, created_at, created_at
            )
            
            # Insert Subject Association
            await self.db.execute(
                "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
                patch_id, subject_key
            )

            # Initialize Usage Metrics
            await self.db.execute(
                """
                INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
                VALUES ($1, 1, $2, 1.0)
                """,
                patch_id, created_at
            )
            
            # ACL: Grant implicit read/write to the app that created it
            app_id = payload.get("app_id")
            if app_id:
                await self.db.execute(
                    """
                    INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write, can_delete)
                    VALUES ($1, $2, TRUE, TRUE, TRUE)
                    """,
                    patch_id, app_id
                )
            logger.info("fact_stored", fact=fact, category=category)
        except Exception as e:
            logger.error("db_insert_failed", error=str(e))
            return
        
        # 2. Trigger Re-Hydration to update cache
        await self.hydrate_cache(user_id)

    async def handle_passive_learning(self, payload: Dict[str, Any]):
        """
        Passive Learning: Analyze Execution Trace.
        Uses Mistral 7B (Ollama) to extract insights.
        """
        trace = payload.get("execution_trace")
        if not trace:
            return

        logger.info("analyzing_trace", steps=len(trace))
        
        # Construct Prompt
        trace_text = json.dumps(trace, indent=2)
        base_prompt = await self.get_prompt("archivist")
        prompt = base_prompt.replace("{trace_text}", trace_text)

        # Call Ollama
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    OLLAMA_URL,
                    json={
                        "model": "qwen2.5-coder:7b-instruct",
                        "prompt": prompt,
                        "stream": False
                    },
                    timeout=240.0
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
                    
                    # Parse output (Raw text, not JSON enforced)
                    raw_response = result["response"]
                    logger.info("trace_analysis_complete", response=raw_response)
                
                # Store extracted facts
                try:
                    # Attempt to parse JSON from LLM response
                    # Find the first '{' and last '}' to handle potential preamble/postamble
                    start_idx = raw_response.find('{')
                    end_idx = raw_response.rfind('}')
                    
                    if start_idx != -1 and end_idx != -1:
                        json_str = raw_response[start_idx:end_idx+1]
                        parsed = json.loads(json_str)
                        facts = parsed.get("facts", [])
                        
                        user_id = payload.get("user_id")
                        if user_id and facts:
                            # Ensure user exists
                            await self.db.execute(
                                "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                                user_id
                            )
                            
                            for fact_item in facts:
                                # Handle both old format (string) and new format (object with fact/category)
                                if isinstance(fact_item, str):
                                    fact_text = fact_item
                                    category = "trait"
                                else:
                                    fact_text = fact_item.get("fact", str(fact_item))
                                    category = fact_item.get("category", "trait")
                                
                                timestamp = payload.get("timestamp")
                                created_at = datetime.fromisoformat(timestamp) if timestamp else datetime.utcnow()
                                
                                patch_id = str(uuid.uuid4())
                                patch_id = str(uuid.uuid4())
                                subject_key = f"user:{user_id}"
                                patch_name = f"passive_trace_{patch_id[:8]}"
                                value_json = json.dumps({"text": fact_text})
                                
                                await self.db.execute(
                                    """
                                    INSERT INTO context_patches (
                                        patch_id, patch_name, patch_type, value,
                                        origin_mode, source_prompt, confidence, persistence, created_at, updated_at
                                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                                    """,
                                    patch_id, patch_name, category, value_json,
                                    "inferred", "archivist", 0.7, "sticky", created_at, created_at
                                )

                                # Subject association
                                await self.db.execute(
                                    "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
                                    patch_id, subject_key
                                )

                                # Initialize Usage Metrics
                                await self.db.execute(
                                    """
                                    INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
                                    VALUES ($1, 1, $2, 1.0)
                                    """,
                                    patch_id, created_at
                                )

                                # ACL (Optional for passive? Grant to app if present)
                                app_id = payload.get("app_id")
                                if app_id:
                                    await self.db.execute(
                                        "INSERT INTO context_patch_acl (patch_id, app_id, can_read) VALUES ($1, $2, TRUE)",
                                        patch_id, app_id
                                    )
                            logger.info("facts_stored", count=len(facts))
                            
                            # Trigger hydration
                            await self.hydrate_cache(user_id)
                except Exception as e:
                    logger.error("fact_extraction_failed", error=str(e))
                
        except Exception as e:
            logger.error("ollama_call_failed", error=str(e))

    async def handle_chat_log(self, payload: Dict[str, Any]):
        """
        Legacy: Analyze raw chat log.
        Batches long conversations into 10-message chunks to prevent timeouts.
        """
        messages = payload.get("messages")
        if not messages:
            return
        
        # Batch long conversations to prevent LLM timeout
        # Using smaller batches (6) for slower local models
        batches = batch_messages(messages, batch_size=6)
        logger.info("analyzing_chat", messages=len(messages), batches=len(batches))
        
        all_facts = []  # Aggregate facts from all batches
        
        for batch_num, batch in enumerate(batches, 1):
            logger.info("processing_batch", batch=batch_num, messages=len(batch))
            
            # The Detective Prompt (from Context Quilt Architecture)
            chat_text = json.dumps(batch, indent=2)
            base_prompt = await self.get_prompt("detective")
            prompt = base_prompt.replace("{chat_text}", chat_text)

            # Call Ollama for this batch
            try:
                import aiohttp
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        OLLAMA_URL,
                        json={
                        "model": "qwen2.5-coder:7b-instruct",
                            "prompt": prompt,
                            "stream": False
                        },
                        timeout=aiohttp.ClientTimeout(total=300)
                    ) as response:
                        response.raise_for_status()
                        result = await response.json()
                        
                        raw_response = result["response"]
                        logger.info("batch_analysis_complete", batch=batch_num, response=raw_response[:200])

                        # Extract facts from this batch
                        batch_facts = extract_facts_from_response(raw_response)
                        if batch_facts:
                            logger.info("batch_facts_extracted", batch=batch_num, count=len(batch_facts))
                            
                            # Store facts immediately (Incremental Processing)
                            user_id = payload.get("user_id")
                            if user_id:
                                try:
                                    await self.db.execute(
                                        "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                                        user_id
                                    )
                                    
                                    for fact_item in batch_facts:
                                        # Normalize to string
                                        if isinstance(fact_item, str):
                                            fact_text = fact_item
                                        elif isinstance(fact_item, dict):
                                            fact_text = fact_item.get("fact", str(fact_item))
                                        else:
                                            fact_text = str(fact_item)
                                        
                                        # HYBRID APPROACH: Python classifies (strict work)
                                        category = classify_fact(fact_text)
                                        
                                        timestamp = payload.get("timestamp")
                                        created_at = datetime.fromisoformat(timestamp) if timestamp else datetime.utcnow()
                                        
                                        patch_id = str(uuid.uuid4())
                                        patch_id = str(uuid.uuid4())
                                        subject_key = f"user:{user_id}"
                                        patch_name = f"chat_insight_{patch_id[:8]}"
                                        value_json = json.dumps({"text": fact_text})

                                        await self.db.execute(
                                            """
                                            INSERT INTO context_patches (
                                                patch_id, patch_name, patch_type, value,
                                                origin_mode, source_prompt, confidence, persistence, created_at, updated_at
                                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                                            """,
                                            patch_id, patch_name, category, value_json,
                                            "inferred", "detective", 0.8, "sticky", created_at, created_at
                                        )

                                        await self.db.execute(
                                            "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
                                            patch_id, subject_key
                                        )

                                        # Initialize Usage Metrics
                                        await self.db.execute(
                                            """
                                            INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
                                            VALUES ($1, 1, $2, 1.0)
                                            """,
                                            patch_id, created_at
                                        )

                                        app_id = payload.get("app_id")
                                        if app_id:
                                            await self.db.execute(
                                                "INSERT INTO context_patch_acl (patch_id, app_id, can_read) VALUES ($1, $2, TRUE)",
                                                patch_id, app_id
                                            )
                                    
                                    logger.info("batch_facts_stored", batch=batch_num, count=len(batch_facts))
                                    # Hydrate cache incrementally so user sees updates faster
                                    await self.hydrate_cache(user_id)
                                    
                                except Exception as e:
                                    logger.error("batch_fact_storage_failed", batch=batch_num, error=str(e))
                        
            except Exception as e:
                logger.error("batch_ollama_failed", batch=batch_num, error=str(e))
                continue  # Continue with next batch even if one fails

if __name__ == "__main__":
    worker = ColdPathWorker()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(worker.start())
    except KeyboardInterrupt:
        loop.run_until_complete(worker.stop())
