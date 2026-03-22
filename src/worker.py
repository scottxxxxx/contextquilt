"""
Context Quilt - Cold Path Worker
Handles Async Memory Consolidation via hosted LLM extraction.

Uses the LLMClient for structured extraction via any OpenAI-compatible API.
Default: Mistral Small 3.1 via OpenRouter ($0.03/$0.11 per M tokens).
"""

import asyncio
import json
import os
import sys
import structlog
import redis.asyncio as redis
import asyncpg
from typing import Dict, Any, List
from datetime import datetime
import uuid

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contextquilt.services.llm_client import LLMClient
from contextquilt.services.extraction_prompts import (
    MEETING_SUMMARY_SYSTEM,
    CONVERSATION_SYSTEM,
    TRACE_SYSTEM,
)
from contextquilt.gateway.extraction import classify_fact

# Configure Logging
logger = structlog.get_logger()

# Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")


def batch_messages(messages: List[Dict], batch_size: int = 10) -> List[List[Dict]]:
    """Batch long conversations into chunks to prevent LLM timeout."""
    if len(messages) <= batch_size:
        return [messages]
    batches = []
    for i in range(0, len(messages), batch_size):
        batches.append(messages[i:i + batch_size])
    logger.info("conversation_batched", total_messages=len(messages), batches=len(batches))
    return batches


async def store_facts(
    db,
    user_id: str,
    facts: List[Dict[str, Any]],
    source_prompt: str,
    app_id: str | None = None,
    timestamp: str | None = None,
):
    """
    Store extracted facts and action items to Postgres.
    Shared by all handlers to eliminate code duplication.
    """
    if not facts:
        return 0

    await db.execute(
        "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
        user_id
    )

    created_at = datetime.fromisoformat(timestamp) if timestamp else datetime.utcnow()
    stored = 0

    for fact_item in facts:
        # Handle both structured (dict with fact/category) and plain string facts
        if isinstance(fact_item, dict):
            fact_text = fact_item.get("fact", str(fact_item))
            # Use LLM-provided category if available, fall back to Python classifier
            category = fact_item.get("category", classify_fact(fact_text))
        elif isinstance(fact_item, str):
            fact_text = fact_item
            category = classify_fact(fact_text)
        else:
            continue

        patch_id = str(uuid.uuid4())
        subject_key = f"user:{user_id}"
        patch_name = f"{source_prompt}_{patch_id[:8]}"
        value_json = json.dumps({"text": fact_text})

        # Store participants if present (for cross-meeting context)
        participants = fact_item.get("participants", []) if isinstance(fact_item, dict) else []
        if participants:
            value_json = json.dumps({"text": fact_text, "participants": participants})

        await db.execute(
            """
            INSERT INTO context_patches (
                patch_id, patch_name, patch_type, value,
                origin_mode, source_prompt, confidence, persistence, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            patch_id, patch_name, category, value_json,
            "inferred", source_prompt, 0.8, "sticky", created_at, created_at
        )

        await db.execute(
            "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
            patch_id, subject_key
        )

        await db.execute(
            """
            INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
            VALUES ($1, 1, $2, 1.0)
            """,
            patch_id, created_at
        )

        if app_id:
            try:
                await db.execute(
                    "INSERT INTO context_patch_acl (patch_id, app_id, can_read) VALUES ($1, $2::uuid, TRUE)",
                    patch_id, app_id
                )
            except Exception:
                pass  # Skip ACL if app_id isn't a registered UUID

        stored += 1

    return stored


async def store_action_items(
    db,
    user_id: str,
    action_items: List[Dict[str, Any]],
    app_id: str | None = None,
    timestamp: str | None = None,
):
    """Store extracted action items as experience-type patches."""
    if not action_items:
        return 0

    created_at = datetime.fromisoformat(timestamp) if timestamp else datetime.utcnow()
    stored = 0

    for item in action_items:
        action = item.get("action", "")
        owner = item.get("owner", "")
        deadline = item.get("deadline")
        if not action:
            continue

        patch_id = str(uuid.uuid4())
        subject_key = f"user:{user_id}"
        patch_name = f"action_item_{patch_id[:8]}"
        value_json = json.dumps({
            "text": action,
            "owner": owner,
            "deadline": deadline,
            "type": "action_item",
        })

        await db.execute(
            """
            INSERT INTO context_patches (
                patch_id, patch_name, patch_type, value,
                origin_mode, source_prompt, confidence, persistence, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            """,
            patch_id, patch_name, "experience", value_json,
            "inferred", "meeting_summary", 0.8, "sticky", created_at, created_at
        )

        await db.execute(
            "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
            patch_id, subject_key
        )

        await db.execute(
            """
            INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
            VALUES ($1, 1, $2, 1.0)
            """,
            patch_id, created_at
        )

        if app_id:
            try:
                await db.execute(
                    "INSERT INTO context_patch_acl (patch_id, app_id, can_read) VALUES ($1, $2::uuid, TRUE)",
                    patch_id, app_id
                )
            except Exception:
                pass  # Skip ACL if app_id isn't a registered UUID

        stored += 1

    return stored


class ColdPathWorker:
    def __init__(self):
        self.redis = None
        self.db = None
        self.llm = None
        self.running = False

    async def start(self):
        """Initialize connections and start processing loop"""
        logger.info("worker_starting")

        # Validate LLM config before starting
        llm_key = os.getenv("CQ_LLM_API_KEY", "")
        llm_url = os.getenv("CQ_LLM_BASE_URL", "")
        llm_model = os.getenv("CQ_LLM_MODEL", "")
        if not llm_key or not llm_url:
            logger.error(
                "llm_not_configured",
                hint="Set CQ_LLM_API_KEY and CQ_LLM_BASE_URL in your .env file. "
                     "See env.example for options (OpenRouter, OpenAI, Gemini, Ollama, etc.)"
            )
            raise SystemExit("CQ_LLM_API_KEY and CQ_LLM_BASE_URL are required. See env.example.")

        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        self.db = await asyncpg.connect(DATABASE_URL)
        self.llm = LLMClient()  # Configured via CQ_LLM_* env vars

        self.running = True
        logger.info("worker_ready",
                     model=self.llm.model,
                     base_url=self.llm.base_url)

        await self.consume_stream()

    async def stop(self):
        """Cleanup connections"""
        self.running = False
        if self.llm:
            await self.llm.close()
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

        try:
            await self.redis.xgroup_create(stream_key, group_name, mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

        while self.running:
            try:
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
        elif task_type == "meeting_summary":
            await self.handle_meeting_summary(payload)
        elif task_type == "trace":
            await self.handle_passive_learning(payload)
        elif task_type == "chat_log":
            await self.handle_chat_log(payload)
        else:
            logger.warning("unknown_task_type", type=task_type)

    # ============================================
    # Handlers
    # ============================================

    async def hydrate_cache(self, user_id: str):
        """Hydration Workflow: Postgres -> Redis"""
        try:
            row = await self.db.fetchrow(
                "SELECT variables, last_updated FROM profiles WHERE user_id = $1", user_id
            )
        except Exception as e:
            logger.error("db_fetch_failed", error=str(e), user_id=user_id)
            return

        if not row:
            logger.warning("user_not_found", user_id=user_id)
            return

        variables = row["variables"]
        if isinstance(variables, str):
            variables = json.loads(variables)

        profile_data = {
            "variables": variables,
            "last_updated": row["last_updated"].isoformat() if row["last_updated"] else "now"
        }

        cache_key = f"active_context:{user_id}"
        await self.redis.set(cache_key, json.dumps(profile_data), ex=3600)

        logger.info("hydration_complete", user_id=user_id)

    async def handle_active_learning(self, payload: Dict[str, Any]):
        """Active Learning: Agent explicitly saved a fact. Direct write, no LLM needed."""
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
            await self.db.execute(
                "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                user_id
            )

            patch_id = str(uuid.uuid4())
            subject_key = f"user:{user_id}"
            origin_mode = "declared" if source == "explicit" else "inferred"
            patch_name = f"active_learning_{patch_id[:8]}"
            value_json = json.dumps({"text": fact})
            created_at = datetime.fromisoformat(timestamp) if timestamp else datetime.utcnow()

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

            await self.db.execute(
                "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
                patch_id, subject_key
            )

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

        await self.hydrate_cache(user_id)

    async def handle_meeting_summary(self, payload: Dict[str, Any]):
        """
        Meeting Summary: Extract facts and action items from a meeting summary.
        Primary use case for ShoulderSurf via CloudZap.
        """
        summary = payload.get("summary") or payload.get("content")
        user_id = payload.get("user_id")
        if not summary or not user_id:
            logger.warning("missing_summary_data", user_id=user_id)
            return

        logger.info("analyzing_meeting_summary", user_id=user_id, length=len(summary))

        try:
            response = await self.llm.extract(
                system_prompt=MEETING_SUMMARY_SYSTEM,
                user_content=summary,
            )

            facts = response.content.get("facts", [])
            action_items = response.content.get("action_items", [])

            app_id = payload.get("app_id")
            timestamp = payload.get("timestamp")

            facts_stored = await store_facts(
                self.db, user_id, facts, "meeting_summary", app_id, timestamp
            )
            actions_stored = await store_action_items(
                self.db, user_id, action_items, app_id, timestamp
            )

            logger.info(
                "meeting_summary_complete",
                user_id=user_id,
                facts_stored=facts_stored,
                actions_stored=actions_stored,
                cost_usd=response.cost_usd,
                model=response.model,
            )

            await self.hydrate_cache(user_id)

        except Exception as e:
            logger.error("meeting_summary_failed", error=str(e), user_id=user_id)

    async def handle_passive_learning(self, payload: Dict[str, Any]):
        """Passive Learning: Analyze agent execution trace."""
        trace = payload.get("execution_trace")
        if not trace:
            return

        user_id = payload.get("user_id")
        logger.info("analyzing_trace", steps=len(trace))

        try:
            trace_text = json.dumps(trace, indent=2)
            response = await self.llm.extract(
                system_prompt=TRACE_SYSTEM,
                user_content=trace_text,
            )

            facts = response.content.get("facts", [])
            app_id = payload.get("app_id")
            timestamp = payload.get("timestamp")

            stored = await store_facts(
                self.db, user_id, facts, "archivist", app_id, timestamp
            )

            logger.info("trace_complete", facts_stored=stored, cost_usd=response.cost_usd)
            await self.hydrate_cache(user_id)

        except Exception as e:
            logger.error("trace_analysis_failed", error=str(e))

    async def handle_chat_log(self, payload: Dict[str, Any]):
        """Analyze conversation log. Batches long conversations."""
        messages = payload.get("messages")
        if not messages:
            return

        user_id = payload.get("user_id")
        batches = batch_messages(messages, batch_size=10)
        logger.info("analyzing_chat", messages=len(messages), batches=len(batches))

        total_stored = 0
        for batch_num, batch in enumerate(batches, 1):
            logger.info("processing_batch", batch=batch_num, messages=len(batch))

            try:
                chat_text = json.dumps(batch, indent=2)
                response = await self.llm.extract(
                    system_prompt=CONVERSATION_SYSTEM,
                    user_content=chat_text,
                )

                facts = response.content.get("facts", [])
                app_id = payload.get("app_id")
                timestamp = payload.get("timestamp")

                stored = await store_facts(
                    self.db, user_id, facts, "detective", app_id, timestamp
                )
                total_stored += stored

                logger.info("batch_complete", batch=batch_num, facts_stored=stored)
                await self.hydrate_cache(user_id)

            except Exception as e:
                logger.error("batch_failed", batch=batch_num, error=str(e))
                continue

        logger.info("chat_analysis_complete", total_facts_stored=total_stored)


if __name__ == "__main__":
    worker = ColdPathWorker()
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(worker.start())
    except KeyboardInterrupt:
        loop.run_until_complete(worker.stop())
