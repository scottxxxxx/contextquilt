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
    COMMUNICATION_PROFILE_SYSTEM,
)
from contextquilt.services.extraction_schema import (
    EXTRACTION_SCHEMA,
    enforce_owner_gate,
    enforce_connection_requirements,
    enforce_person_ownership,
    normalize_owner_in_transcript,
    sanitize_you_marker_from_patches,
    strip_ephemeral_fields,
)
from contextquilt.services.schema_prompt_builder import (
    build_prompt as build_schema_prompt,
    build_output_schema as build_schema_output_schema,
)
from contextquilt.gateway.extraction import classify_fact

# Configure Logging
logger = structlog.get_logger()

# Configuration
_redis_host = os.getenv("REDIS_HOST", "localhost")
_redis_port = os.getenv("REDIS_PORT", "6379")
_redis_password = os.getenv("REDIS_PASSWORD", "")
REDIS_URL = os.getenv("REDIS_URL", f"redis://:{_redis_password}@{_redis_host}:{_redis_port}" if _redis_password else f"redis://{_redis_host}:{_redis_port}")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")

# Queue settings
QUEUE_MAX_WAIT_MINUTES = int(os.getenv("CQ_QUEUE_MAX_WAIT_MINUTES", "60"))
QUEUE_BUDGET_THRESHOLD = float(os.getenv("CQ_QUEUE_BUDGET_THRESHOLD", "0.8"))
QUEUE_CHECK_INTERVAL_SECONDS = 30  # How often to check queues for processing

# Extraction caps — belt-and-suspenders with prompt limits
MAX_FACTS_PER_MEETING = 5
MAX_ACTION_ITEMS_PER_MEETING = 3
MAX_PATCHES_PER_MEETING = 12  # Connected quilt model (replaces facts+actions for V2)
MAX_ENTITIES_PER_MEETING = 10
MAX_RELATIONSHIPS_PER_MEETING = 10

# Default persistence by patch type (used when registry lookup unavailable).
# identity and experience have been retired per the v1 taxonomy decision —
# see docs/memos/patch-taxonomy-simplification.md.
DEFAULT_PERSISTENCE = {
    "trait": "sticky", "preference": "sticky",
    "role": "sticky", "person": "sticky", "project": "sticky",
    "decision": "sticky", "takeaway": "decaying",
    "commitment": "sticky", "blocker": "sticky",
    "goal": "sticky", "constraint": "sticky", "org": "sticky",
    "event": "decaying",
}

# Known context windows for common models (tokens)
KNOWN_CONTEXT_WINDOWS = {
    "mistralai/mistral-small-3.1-24b-instruct": 128000,
    "gpt-4.1-nano": 128000,
    "gpt-4o-mini": 128000,
    "gpt-5.4-nano": 128000,
    "qwen/qwen-turbo": 131000,
    "gemini-2.5-flash-lite": 1000000,
    "cohere/command-r7b-12-2024": 128000,
}
DEFAULT_CONTEXT_WINDOW = 128000


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
    project: str | None = None,
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
            # Facts about other participants that got classified as user-only
            # types (trait / preference) must be reclassified as takeaway
            # instead — traits/preferences describe the submitting user only.
            about_user = fact_item.get("about_user", True)
            if not about_user and category in ("trait", "preference"):
                category = "takeaway"
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
                origin_mode, source_prompt, confidence, persistence, project, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            patch_id, patch_name, category, value_json,
            "inferred", source_prompt, 0.8,
            "sticky" if category in ("trait", "preference") else "decaying",
            project,
            created_at, created_at
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
                    "INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write, can_delete) VALUES ($1, $2::uuid, TRUE, TRUE, TRUE)",
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
    project: str | None = None,
):
    """Store extracted action items as commitment-type patches (V1 fallback path)."""
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
                origin_mode, source_prompt, confidence, persistence, project, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            patch_id, patch_name, "commitment", value_json,
            "inferred", "meeting_summary", 0.8, "decaying", project, created_at, created_at
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
                    "INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write, can_delete) VALUES ($1, $2::uuid, TRUE, TRUE, TRUE)",
                    patch_id, app_id
                )
            except Exception:
                pass  # Skip ACL if app_id isn't a registered UUID

        stored += 1

    return stored


async def store_connected_patches(
    db,
    user_id: str,
    patches: List[Dict[str, Any]],
    source_prompt: str,
    app_id: str | None = None,
    timestamp: str | None = None,
    project: str | None = None,
    project_id: str | None = None,
    origin_id: str | None = None,
    origin_type: str | None = None,
):
    """
    Store typed, connected patches (Connected Quilt V2 model).
    Two-pass: create all patches first, then create connections between them.
    """
    if not patches:
        return 0

    await db.execute(
        "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
        user_id
    )

    created_at = datetime.fromisoformat(timestamp) if timestamp else datetime.utcnow()
    subject_key = f"user:{user_id}"

    # Pass 1: Create all patches, build lookup map for connection resolution
    patch_lookup = {}  # (text_lower, type) → patch_id
    stored = 0

    for patch in patches:
        if not isinstance(patch, dict):
            continue

        patch_type = patch.get("type", "takeaway")
        value = patch.get("value", {})
        if isinstance(value, str):
            value = {"text": value}
        text = value.get("text", "")
        if not text:
            continue

        # Deduplication: check if an active patch with same type and similar text already exists
        # Uses trigram similarity (pg_trgm) to catch near-duplicates like
        # "Deliver samples in 2 days" vs "Deliver transcription samples within 2 days"
        existing = await db.fetchrow(
            """
            SELECT cp.patch_id FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1 AND cp.patch_type = $2
              AND SIMILARITY(LOWER(cp.value->>'text'), LOWER($3)) > 0.6
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY SIMILARITY(LOWER(cp.value->>'text'), LOWER($3)) DESC
            LIMIT 1
            """,
            subject_key, patch_type, text
        )
        if existing:
            # Reuse existing patch — update last_seen timestamp
            patch_id = str(existing["patch_id"])
            await db.execute(
                "UPDATE context_patches SET updated_at = $1 WHERE patch_id = $2::uuid",
                created_at, patch_id
            )
            await db.execute(
                "UPDATE patch_usage_metrics SET access_count = access_count + 1, last_accessed_at = $1 WHERE patch_id = $2::uuid",
                created_at, patch_id
            )
            patch_lookup[(text.lower().strip(), patch_type)] = patch_id
            logger.debug("patch_deduplicated", type=patch_type, text=text[:50], patch_id=patch_id)
            continue

        patch_id = str(uuid.uuid4())
        patch_name = f"{source_prompt}_{patch_id[:8]}"
        value_json = json.dumps(value)
        persistence = DEFAULT_PERSISTENCE.get(patch_type, "decaying")

        # Project-scoped types get the project tag (per the v1.1 SS schema).
        # `deliverable` is a Connection-facet type but project-scoped: every
        # deliverable lives inside a project and should carry the parent's
        # project/origin metadata alongside the episode types.
        project_scoped_types = (
            "decision", "commitment", "blocker", "takeaway",
            "goal", "constraint", "event", "deliverable",
        )
        patch_project = project if patch_type in project_scoped_types else None
        # Role patches can also be project-scoped if they have a belongs_to connection
        if patch_type == "role" and project:
            connects_to = patch.get("connects_to", [])
            if any(c.get("role") == "parent" for c in connects_to):
                patch_project = project

        # Project-scoped patches get both the text project name and the stable project_id
        patch_project_id = project_id if patch_type in project_scoped_types or (patch_type == "role" and patch_project) else None
        patch_origin_id = origin_id if patch_type in project_scoped_types else None
        patch_origin_type = origin_type if patch_origin_id else None

        await db.execute(
            """
            INSERT INTO context_patches (
                patch_id, patch_name, patch_type, value,
                origin_mode, source_prompt, confidence, persistence,
                project, project_id, origin_id, origin_type,
                status, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            """,
            patch_id, patch_name, patch_type, value_json,
            "inferred", source_prompt, 0.8, persistence,
            patch_project, patch_project_id, patch_origin_id, patch_origin_type,
            "active", created_at, created_at
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
                    "INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write, can_delete) VALUES ($1, $2::uuid, TRUE, TRUE, TRUE)",
                    patch_id, app_id
                )
            except Exception:
                pass

        patch_lookup[(text.lower().strip(), patch_type)] = patch_id
        stored += 1

    # Pass 2: Create connections between patches
    connections_created = 0
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        connects_to = patch.get("connects_to", [])
        if not connects_to:
            continue

        value = patch.get("value", {})
        if isinstance(value, str):
            value = {"text": value}
        from_text = value.get("text", "").lower().strip()
        from_type = patch.get("type", "takeaway")
        from_id = patch_lookup.get((from_text, from_type))
        if not from_id:
            continue

        for conn in connects_to:
            target_text = conn.get("target_text", "").lower().strip()
            target_type = conn.get("target_type", "")
            role = conn.get("role", "informs")
            label = conn.get("label", "")

            if not target_text or not role:
                continue

            # Resolve target: check current batch first
            to_id = patch_lookup.get((target_text, target_type))

            # If not in batch, check existing patches for this user
            if not to_id:
                row = await db.fetchrow(
                    """
                    SELECT cp.patch_id FROM context_patches cp
                    JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
                    WHERE ps.subject_key = $1 AND cp.patch_type = $2
                      AND LOWER(cp.value->>'text') = $3 AND cp.status = 'active'
                    LIMIT 1
                    """,
                    subject_key, target_type, target_text
                )
                if row:
                    to_id = str(row["patch_id"])

            # If still unresolved, create a stub patch for the target
            if not to_id:
                to_id = str(uuid.uuid4())
                stub_name = f"{source_prompt}_{to_id[:8]}"
                stub_value = json.dumps({"text": conn.get("target_text", "")})
                stub_persistence = DEFAULT_PERSISTENCE.get(target_type, "sticky")
                stub_project = project if target_type in project_scoped_types else None
                stub_project_id = project_id if target_type in project_scoped_types else None
                stub_origin_id = origin_id if target_type in project_scoped_types else None
                stub_origin_type = origin_type if stub_origin_id else None

                await db.execute(
                    """
                    INSERT INTO context_patches (
                        patch_id, patch_name, patch_type, value,
                        origin_mode, source_prompt, confidence, persistence,
                        project, project_id, origin_id, origin_type,
                        status, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    """,
                    to_id, stub_name, target_type, stub_value,
                    "inferred", source_prompt, 0.6, stub_persistence,
                    stub_project, stub_project_id, stub_origin_id, stub_origin_type,
                    "active", created_at, created_at
                )
                await db.execute(
                    "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
                    to_id, subject_key
                )
                await db.execute(
                    """
                    INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
                    VALUES ($1, 1, $2, 1.0)
                    """,
                    to_id, created_at
                )
                patch_lookup[(target_text, target_type)] = to_id
                stored += 1

            # Normalize direction: "owns" should always go FROM person TO the thing owned
            # The LLM often puts "owns" on a commitment pointing to a person (reversed)
            actual_from = from_id
            actual_to = to_id
            if label == "owns" and from_type != "person" and target_type == "person":
                actual_from, actual_to = to_id, from_id

            # Create the connection
            try:
                await db.execute(
                    """
                    INSERT INTO patch_connections (from_patch_id, to_patch_id, connection_role, connection_label, context)
                    VALUES ($1::uuid, $2::uuid, $3, $4, $5)
                    ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO NOTHING
                    """,
                    actual_from, actual_to, role, label, conn.get("context")
                )
                connections_created += 1

                # Lifecycle trigger: REPLACES → archive the target
                if role == "replaces":
                    await db.execute(
                        "UPDATE context_patches SET status = 'archived', completed_at = NOW() WHERE patch_id = $1::uuid",
                        to_id
                    )
            except Exception as e:
                logger.warning("connection_failed", error=str(e), from_id=from_id, to_id=to_id)

    logger.info("connected_patches_stored", patches=stored, connections=connections_created, user_id=user_id)
    return stored


async def store_entities(
    db,
    redis_client,
    user_id: str,
    entities: list[dict],
    metadata: dict | None = None,
):
    """
    Store extracted entities to Postgres. Upserts by (user_id, name, entity_type).
    Updates the Redis entity name index for hot path matching.
    """
    if not entities:
        return 0

    stored = 0
    for ent in entities:
        name = ent.get("name", "").strip()
        entity_type = ent.get("type", "").strip()
        description = ent.get("description", "")
        if not name or not entity_type:
            continue

        # Upsert: insert or update mention count + last_seen
        await db.execute(
            """
            INSERT INTO entities (user_id, name, entity_type, description, metadata)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id, name, entity_type) DO UPDATE SET
                description = COALESCE(NULLIF(EXCLUDED.description, ''), entities.description),
                last_seen_at = NOW(),
                mention_count = entities.mention_count + 1,
                metadata = entities.metadata || EXCLUDED.metadata
            """,
            user_id, name, entity_type, description,
            json.dumps(metadata or {}),
        )
        stored += 1

    # Update Redis entity name index for this user
    await _rebuild_entity_index(db, redis_client, user_id)

    return stored


async def store_relationships(
    db,
    user_id: str,
    relationships: list[dict],
    metadata: dict | None = None,
):
    """
    Store extracted relationships between entities.
    Resolves entity names to entity_ids. Upserts by (user_id, from, to, type).
    """
    if not relationships:
        return 0

    stored = 0
    for rel in relationships:
        from_name = rel.get("from", "").strip()
        to_name = rel.get("to", "").strip()
        rel_type = rel.get("type", "").strip()
        context = rel.get("context", "")
        if not from_name or not to_name or not rel_type:
            continue

        # Resolve entity IDs by name (match any type for this user)
        from_row = await db.fetchrow(
            "SELECT entity_id FROM entities WHERE user_id = $1 AND name = $2 LIMIT 1",
            user_id, from_name
        )
        to_row = await db.fetchrow(
            "SELECT entity_id FROM entities WHERE user_id = $1 AND name = $2 LIMIT 1",
            user_id, to_name
        )

        if not from_row or not to_row:
            logger.debug("relationship_skipped", reason="entity_not_found",
                         from_name=from_name, to_name=to_name)
            continue

        from_id = from_row["entity_id"]
        to_id = to_row["entity_id"]

        await db.execute(
            """
            INSERT INTO relationships (user_id, from_entity_id, to_entity_id, relationship_type, context, metadata)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (user_id, from_entity_id, to_entity_id, relationship_type) DO UPDATE SET
                context = COALESCE(NULLIF(EXCLUDED.context, ''), relationships.context),
                last_seen_at = NOW(),
                mention_count = relationships.mention_count + 1
            """,
            user_id, from_id, to_id, rel_type, context,
            json.dumps(metadata or {}),
        )
        stored += 1

    return stored


async def _rebuild_entity_index(db, redis_client, user_id: str):
    """
    Rebuild the Redis entity name index for a user.
    Stores all entity names as a set for fast text matching on the hot path.
    """
    try:
        rows = await db.fetch(
            "SELECT name FROM entities WHERE user_id = $1",
            user_id
        )
        key = f"entity_index:{user_id}"
        if rows:
            names = [row["name"] for row in rows]
            await redis_client.delete(key)
            await redis_client.sadd(key, *names)
            await redis_client.expire(key, 7200)  # 2 hour TTL
            logger.info("entity_index_rebuilt", user_id=user_id, count=len(names))
    except Exception as e:
        logger.error("entity_index_rebuild_failed", user_id=user_id, error=str(e))


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
        self.llm = LLMClient()  # Default LLM client (server's own key)
        self._app_llm_cache: Dict[str, LLMClient] = {}  # Per-app BYOK clients

        # Get context window for budget calculation
        model_name = self.llm.model
        self.context_window = int(os.getenv(
            "CQ_LLM_CONTEXT_WINDOW",
            str(KNOWN_CONTEXT_WINDOWS.get(model_name, DEFAULT_CONTEXT_WINDOW))
        ))
        # Available budget = window - prompt overhead - output reserve
        self.context_budget = int((self.context_window - 2800) * QUEUE_BUDGET_THRESHOLD)

        self.running = True
        logger.info("worker_ready",
                     model=self.llm.model,
                     base_url=self.llm.base_url,
                     context_budget=self.context_budget)

        # Run stream consumer, queue checker, and decay worker concurrently
        await asyncio.gather(
            self.consume_stream(),
            self.check_queues_loop(),
            self.decay_loop(),
        )

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

    async def check_queues_loop(self):
        """Periodically check meeting queues and process any that are ready."""
        while self.running:
            try:
                await asyncio.sleep(QUEUE_CHECK_INTERVAL_SECONDS)
                await self._process_ready_queues()
            except Exception as e:
                logger.error("queue_check_error", error=str(e))

    async def decay_loop(self):
        """Periodically archive stale patches based on effective TTL.

        Effective TTL per patch:
          COALESCE(
              permanence_override → PERMANENCE_CLASS_DAYS[class],
              patch_type_registry.default_ttl_days for the type
          )

        Access-based refresh (patch_usage_metrics.last_accessed_at recent)
        exempts a patch regardless of override or type default.
        """
        # Fallback TTLs by patch_type when registry has no entry.
        DEFAULT_TTLS = {
            "takeaway": 14,
            "blocker": 30,
            "commitment": 30,
            "event": 90,
        }
        # Maps the 7 permanence classes → days. None = never expires.
        PERMANENCE_CLASS_DAYS = {
            "permanent": None,
            "decade": None,
            "year": 365,
            "quarter": 90,
            "month": 30,
            "week": 14,
            "day": 1,
        }
        # Run every 6 hours
        DECAY_INTERVAL_SECONDS = 6 * 60 * 60
        # Wait 60 seconds after startup before first run
        await asyncio.sleep(60)

        while self.running:
            try:
                total_archived = 0

                # Step 1: Archive patches with explicit permanence_override.
                # These run per-class and cross-cut patch_type.
                for perm_class, ttl_days in PERMANENCE_CLASS_DAYS.items():
                    if ttl_days is None:
                        continue  # permanent/decade never expire
                    result = await self.db.execute(
                        """
                        UPDATE context_patches SET status = 'archived', updated_at = NOW()
                        WHERE permanence_override = $1
                          AND COALESCE(status, 'active') = 'active'
                          AND updated_at < NOW() - INTERVAL '1 day' * $2
                          AND patch_id NOT IN (
                              SELECT patch_id FROM patch_usage_metrics
                              WHERE last_accessed_at > NOW() - INTERVAL '1 day' * $2
                          )
                        """,
                        perm_class, ttl_days
                    )
                    count = int(result.split()[-1]) if result else 0
                    if count > 0:
                        total_archived += count
                        logger.info(
                            "decay_archived_override",
                            permanence_override=perm_class,
                            count=count,
                            ttl_days=ttl_days,
                        )

                # Step 2: Archive patches using their type's default TTL.
                # Exclude patches with an override (handled in Step 1).
                for patch_type, ttl_days in DEFAULT_TTLS.items():
                    # Look up TTL from registry if available, fall back to default
                    try:
                        row = await self.db.fetchrow(
                            "SELECT default_ttl_days FROM patch_type_registry WHERE type_key = $1",
                            patch_type
                        )
                        if row and row["default_ttl_days"] is not None:
                            ttl_days = row["default_ttl_days"]
                    except Exception:
                        pass  # Registry table may not exist yet

                    # Archive patches older than TTL that haven't been accessed recently
                    # and have no explicit override.
                    result = await self.db.execute(
                        """
                        UPDATE context_patches SET status = 'archived', updated_at = NOW()
                        WHERE patch_type = $1
                          AND permanence_override IS NULL
                          AND COALESCE(status, 'active') = 'active'
                          AND updated_at < NOW() - INTERVAL '1 day' * $2
                          AND patch_id NOT IN (
                              SELECT patch_id FROM patch_usage_metrics
                              WHERE last_accessed_at > NOW() - INTERVAL '1 day' * $2
                          )
                        """,
                        patch_type, ttl_days
                    )
                    # Parse "UPDATE N" to get count
                    count = int(result.split()[-1]) if result else 0
                    if count > 0:
                        total_archived += count
                        logger.info("decay_archived", patch_type=patch_type, count=count, ttl_days=ttl_days)

                if total_archived > 0:
                    logger.info("decay_cycle_complete", total_archived=total_archived)
                else:
                    logger.debug("decay_cycle_complete", total_archived=0)

            except Exception as e:
                logger.error("decay_error", error=str(e))

            await asyncio.sleep(DECAY_INTERVAL_SECONDS)

    async def _process_ready_queues(self):
        """Find queues that have exceeded the time window and process them."""
        # Scan for meeting queue keys
        cursor = b"0"
        while True:
            cursor, keys = await self.redis.scan(cursor=cursor, match="origin_queue:*", count=100)
            for key in keys:
                # Check last event timestamp
                last_ts_str = await self.redis.get(f"{key}:last_event")
                if not last_ts_str:
                    continue

                last_event = datetime.fromisoformat(last_ts_str)
                elapsed_minutes = (datetime.utcnow() - last_event).total_seconds() / 60

                if elapsed_minutes >= QUEUE_MAX_WAIT_MINUTES:
                    # Time trigger: queue is quiet for long enough
                    origin_key = key if isinstance(key, str) else key.decode()
                    # Key format: origin_queue:{user_id}:{origin_type}:{origin_id}
                    queue_suffix = origin_key.replace("origin_queue:", "")
                    logger.info("queue_time_trigger", queue=queue_suffix, elapsed_minutes=round(elapsed_minutes))
                    await self._process_queue_by_key(origin_key)

            if cursor == b"0" or cursor == 0:
                break

    async def _buffer_event(self, payload: Dict[str, Any], origin_id: str, origin_type: str):
        """Add an event to an origin's queue. Check budget trigger."""
        queue_key = f"origin_queue:{payload.get('user_id', 'unknown')}:{origin_type}:{origin_id}"
        event_json = json.dumps(payload)

        await self.redis.rpush(queue_key, event_json)
        await self.redis.set(f"{queue_key}:last_event", datetime.utcnow().isoformat())
        # Keep queues alive for 24 hours max
        await self.redis.expire(queue_key, 86400)
        await self.redis.expire(f"{queue_key}:last_event", 86400)

        # Check budget trigger — estimate tokens from content length
        queue_size = await self.redis.llen(queue_key)
        total_chars = 0
        events = await self.redis.lrange(queue_key, 0, -1)
        for evt in events:
            evt_data = json.loads(evt)
            total_chars += len(evt_data.get("summary", ""))
            total_chars += len(evt_data.get("content", ""))
            total_chars += len(evt_data.get("response", ""))

        # Rough estimate: 4 chars ≈ 1 token
        estimated_tokens = total_chars // 4

        if estimated_tokens >= self.context_budget:
            logger.info("queue_budget_trigger", origin_id=origin_id, origin_type=origin_type,
                        estimated_tokens=estimated_tokens, budget=self.context_budget)
            await self._process_queue_by_key(queue_key)
        else:
            logger.info("event_buffered", origin_id=origin_id, origin_type=origin_type,
                        queue_size=queue_size, estimated_tokens=estimated_tokens)

    async def _process_queue_by_key(self, queue_key: str):
        """Consolidate all events in an origin queue and run one extraction."""

        # Pop all events from the queue
        events = await self.redis.lrange(queue_key, 0, -1)
        if not events:
            return

        await self.redis.delete(queue_key)
        await self.redis.delete(f"{queue_key}:last_event")

        # Parse events and consolidate
        user_id = None
        metadata = {}
        sections = []

        for evt_json in events:
            evt = json.loads(evt_json)
            if not user_id:
                user_id = evt.get("user_id")
            if evt.get("metadata"):
                metadata.update(evt["metadata"])

            evt_type = evt.get("interaction_type", "unknown")
            if evt.get("summary"):
                sections.append(f"[SUMMARY] {evt['summary']}")
            if evt.get("content"):
                sections.append(f"[QUERY] {evt['content']}")
            if evt.get("response"):
                sections.append(f"[RESPONSE] {evt['response']}")
            if evt_type == "sentiment" and evt.get("content"):
                sections.append(f"[SENTIMENT] {evt['content']}")

        origin_id = metadata.get("origin_id")
        origin_type = metadata.get("origin_type")

        if not user_id or not sections:
            logger.warning("queue_empty_after_consolidation",
                           origin_id=origin_id, origin_type=origin_type)
            return

        consolidated_text = "\n\n".join(sections)
        logger.info("queue_processing", origin_id=origin_id, origin_type=origin_type,
                     events=len(events), consolidated_length=len(consolidated_text))

        # Run as a single meeting_summary extraction
        consolidated_payload = {
            "user_id": user_id,
            "interaction_type": "meeting_summary",
            "summary": consolidated_text,
            "metadata": metadata,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self.handle_meeting_summary(consolidated_payload)

    async def process_task(self, payload: Dict[str, Any]):
        """Router for different task types. Buffers events with origin_id."""
        task_type = payload.get("interaction_type") or payload.get("type")
        user_id = payload.get("user_id")
        metadata = payload.get("metadata", {})
        origin_id = metadata.get("origin_id") if metadata else None
        origin_type = metadata.get("origin_type") if metadata else None

        logger.info("processing_task", type=task_type, user_id=user_id,
                    origin_id=origin_id, origin_type=origin_type)

        # Update profile identity fields if provided in metadata
        if user_id and metadata:
            display_name = metadata.get("display_name")
            email = metadata.get("email")
            if display_name or email:
                await self._update_profile_identity(user_id, display_name, email)

        # System tasks — always process immediately
        if task_type == "hydrate":
            await self.hydrate_cache(user_id)
            return
        if task_type == "tool_call":
            await self.handle_active_learning(payload)
            return

        # End-of-meeting full transcript — process immediately, never buffer.
        # (this IS the complete meeting, sent by ShoulderSurf at session end)
        # Flush any orphaned buffered events for this origin — the transcript supersedes them.
        if task_type == "meeting_transcript":
            if origin_id and origin_type and user_id:
                queue_key = f"origin_queue:{user_id}:{origin_type}:{origin_id}"
                flushed = await self.redis.llen(queue_key)
                if flushed:
                    await self.redis.delete(queue_key)
                    await self.redis.delete(f"{queue_key}:last_event")
                    logger.info("queue_flushed_by_transcript",
                                origin_id=origin_id, origin_type=origin_type,
                                flushed_events=flushed)
            payload["summary"] = payload.get("content", "")
            await self.handle_meeting_summary(payload)
            return

        # If event has an origin_id, buffer it for consolidated processing
        if origin_id and origin_type and task_type in ("meeting_summary", "query", "summary", "sentiment"):
            await self._buffer_event(payload, origin_id, origin_type)
            return

        # No origin_id — process immediately
        if task_type in ("meeting_summary", "summary"):
            await self.handle_meeting_summary(payload)
        elif task_type in ("query", "analysis"):
            # Treat standalone queries as meeting summaries (extract facts from content+response)
            content = payload.get("content", "")
            response = payload.get("response", "")
            if content or response:
                combined = ""
                if content:
                    combined += f"[QUERY] {content}\n"
                if response:
                    combined += f"[RESPONSE] {response}\n"
                payload["summary"] = combined
                await self.handle_meeting_summary(payload)
            else:
                logger.info("query_no_content", type=task_type, user_id=user_id)
        elif task_type == "trace":
            await self.handle_passive_learning(payload)
        elif task_type == "chat_log":
            await self.handle_chat_log(payload)
        else:
            logger.warning("unknown_task_type", type=task_type)

    # ============================================
    # Handlers
    # ============================================

    async def _resolve_extraction_prompt(
        self, app_id: str | None
    ) -> tuple[str, Dict[str, Any]]:
        """Pick the system prompt + structured-output schema for this app.

        If the app has a registered manifest in `app_schemas`, generate
        the prompt from it (or use `extraction_prompt_override` verbatim
        if present). Otherwise fall back to the universal hardcoded
        MEETING_SUMMARY_SYSTEM + EXTRACTION_SCHEMA.
        """
        if not app_id:
            return MEETING_SUMMARY_SYSTEM, EXTRACTION_SCHEMA

        try:
            row = await self.db.fetchrow(
                """
                SELECT manifest FROM app_schemas
                WHERE app_id = $1::uuid
                ORDER BY version DESC
                LIMIT 1
                """,
                app_id,
            )
            if not row:
                return MEETING_SUMMARY_SYSTEM, EXTRACTION_SCHEMA
            manifest = row["manifest"]
            if isinstance(manifest, str):
                manifest = json.loads(manifest)
            prompt = build_schema_prompt(manifest)
            schema = build_schema_output_schema(manifest)
            return prompt, schema
        except Exception as e:
            logger.warning(
                "schema_prompt_resolution_failed",
                app_id=app_id,
                error=str(e),
            )
            return MEETING_SUMMARY_SYSTEM, EXTRACTION_SCHEMA

    async def _get_llm_for_app(self, app_id: str | None) -> LLMClient:
        """Get the LLM client for an app. Uses app's BYOK key if available, else server default."""
        if not app_id:
            return self.llm

        # Check cache first
        if app_id in self._app_llm_cache:
            return self._app_llm_cache[app_id]

        # Look up app's LLM config from DB
        try:
            row = await self.db.fetchrow(
                "SELECT llm_api_key_encrypted, llm_base_url, llm_model FROM applications WHERE app_id = $1",
                app_id
            )
            if row and row["llm_api_key_encrypted"]:
                from contextquilt.services.key_encryption import decrypt_key
                api_key = decrypt_key(row["llm_api_key_encrypted"])
                if api_key:
                    client = LLMClient(
                        api_key=api_key,
                        base_url=row["llm_base_url"] or self.llm.base_url,
                        model=row["llm_model"] or self.llm.model,
                    )
                    self._app_llm_cache[app_id] = client
                    logger.info("byok_client_created", app_id=app_id[:8])
                    return client
        except Exception as e:
            logger.warning("byok_lookup_failed", app_id=app_id[:8], error=str(e))

        # Fall back to server default
        return self.llm

    async def hydrate_cache(self, user_id: str):
        """Hydration Workflow: Postgres -> Redis. Warms profile + entity index."""
        try:
            row = await self.db.fetchrow(
                "SELECT variables, last_updated, display_name, email FROM profiles WHERE user_id = $1", user_id
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
            "last_updated": row["last_updated"].isoformat() if row["last_updated"] else "now",
            "display_name": row["display_name"],
            "email": row["email"],
        }

        cache_key = f"active_context:{user_id}"
        await self.redis.set(cache_key, json.dumps(profile_data), ex=3600)

        # Also warm the entity name index — recall's first step uses this for fast matching
        await _rebuild_entity_index(self.db, self.redis, user_id)

        logger.info("hydration_complete", user_id=user_id)

    async def _update_profile_identity(self, user_id: str, display_name: str | None, email: str | None):
        """Update display_name and/or email on the user profile if provided."""
        try:
            # Ensure profile exists
            await self.db.execute(
                "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
                user_id
            )
            if display_name and email:
                await self.db.execute(
                    "UPDATE profiles SET display_name = $1, email = $2 WHERE user_id = $3",
                    display_name, email, user_id
                )
            elif display_name:
                await self.db.execute(
                    "UPDATE profiles SET display_name = $1 WHERE user_id = $2",
                    display_name, user_id
                )
            elif email:
                await self.db.execute(
                    "UPDATE profiles SET email = $1 WHERE user_id = $2",
                    email, user_id
                )
            logger.info("profile_identity_updated", user_id=user_id,
                        display_name=display_name, email=email)
        except Exception as e:
            logger.error("profile_identity_update_failed", error=str(e), user_id=user_id)

    async def _ensure_user_project_connections(self, user_id: str, display_name: str | None):
        """
        Ensure the submitting user's person patch has works_on connections
        to all their project patches. The user works on every project in
        their quilt — the LLM doesn't always create these edges.
        """
        if not display_name:
            return

        subject_key = f"user:{user_id}"
        try:
            # Find the user's person patch (short name or "(you)" marker)
            name_lower = display_name.strip().lower()
            person_row = await self.db.fetchrow(
                """
                SELECT cp.patch_id FROM context_patches cp
                JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
                WHERE ps.subject_key = $1 AND cp.patch_type = 'person'
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND (LOWER(TRIM(cp.value->>'text')) = $2
                       OR LOWER(TRIM(cp.value->>'text')) = $3)
                LIMIT 1
                """,
                subject_key, name_lower, name_lower + " (you)"
            )
            if not person_row:
                return

            person_patch_id = str(person_row["patch_id"])

            # Find all project patches for this user
            project_rows = await self.db.fetch(
                """
                SELECT cp.patch_id FROM context_patches cp
                JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
                WHERE ps.subject_key = $1 AND cp.patch_type = 'project'
                  AND COALESCE(cp.status, 'active') = 'active'
                """,
                subject_key
            )

            connected = 0
            for proj in project_rows:
                proj_id = str(proj["patch_id"])
                await self.db.execute(
                    """
                    INSERT INTO patch_connections (from_patch_id, to_patch_id, connection_role, connection_label)
                    VALUES ($1::uuid, $2::uuid, 'informs', 'works_on')
                    ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO NOTHING
                    """,
                    person_patch_id, proj_id
                )
                connected += 1

            if connected:
                logger.info("user_project_connections_ensured",
                            user_id=user_id, person_patch=person_patch_id, projects=connected)
        except Exception as e:
            logger.warning("user_project_connections_failed", error=str(e), user_id=user_id)

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

        # Identify the submitting user so the LLM can attribute self-typed
        # patches correctly. Three signals the platform accepts:
        #   1. Inline "(you)" marker in transcript — the LLM reads this directly
        #      (e.g. SS injects "[Scott (you)]" client-side after voice match).
        #   2. Legacy structured metadata.owner_speaker_label — CQ injects the
        #      marker server-side for apps that pre-date the user_label name.
        #   3. New structured metadata.user_label + user_identified +
        #      identification_source — GhostPour-forwarded fields from the
        #      caller's identity layer. Only honored when user_identified is
        #      true and identification_source is not "none", so a passthrough
        #      label without confidence does NOT trigger marker injection.
        # All three paths land at the same place: transcript with inline marker
        # by the time the LLM sees it. display_name is a legacy fallback.
        metadata = payload.get("metadata", {}) or {}
        owner_speaker_label = metadata.get("owner_speaker_label")
        display_name = metadata.get("display_name")

        user_label = metadata.get("user_label")
        user_identified = metadata.get("user_identified")
        identification_source = metadata.get("identification_source")
        if (
            not owner_speaker_label
            and user_label
            and user_identified is True
            and identification_source
            and identification_source != "none"
        ):
            owner_speaker_label = user_label

        effective_summary = normalize_owner_in_transcript(summary, owner_speaker_label)
        injected_marker = (
            owner_speaker_label is not None
            and "(you)" not in summary
            and "(you)" in effective_summary
        )
        if injected_marker:
            logger.info(
                "owner_marker_injected_server_side",
                user_id=user_id,
                owner_speaker_label=owner_speaker_label,
                identification_source=identification_source,
            )

        has_you_marker = "(you)" in effective_summary.lower()
        if has_you_marker:
            user_context = ""  # inline marker is sufficient
        elif display_name:
            user_context = f"The submitting user is: {display_name}\n\n"
        else:
            user_context = ""

        try:
            app_id = payload.get("app_id")
            llm = await self._get_llm_for_app(app_id)

            # Prefer the app's registered schema-driven prompt. Falls back
            # to the universal hardcoded MEETING_SUMMARY_SYSTEM only when
            # the app has no registered manifest.
            resolved_prompt, resolved_schema = await self._resolve_extraction_prompt(app_id)

            response = await llm.extract(
                system_prompt=resolved_prompt,
                user_content=user_context + effective_summary,
                json_schema=resolved_schema,
            )

            # --- Audit: capture pre-filter state ---
            patches_before_filters = len(response.content.get("patches") or [])
            reasoning_chars = len(response.content.get("_reasoning") or "")

            enforce_owner_gate(response.content, effective_summary)
            owner_gate_filtered = 0
            if (g := response.content.get("_owner_gate_enforced")):
                owner_gate_filtered = g.get("filtered", 0)
                if owner_gate_filtered:
                    logger.warning(
                        "owner_gate_filtered_patches",
                        user_id=user_id,
                        filtered=owner_gate_filtered,
                        model=response.model,
                    )
            # Meeting-level project context (from payload metadata) — when
            # present, the enforcer injects synthetic parent connections for
            # patches missing them instead of dropping them. The Pass-2
            # resolver later matches the injected target against existing
            # DB rows, so child patches survive when the LLM correctly omits
            # a project that already exists.
            meeting_project_for_enforcement = (
                metadata.get("project") if metadata else None
            )
            enforce_connection_requirements(
                response.content, meeting_project=meeting_project_for_enforcement
            )
            connection_dropped = 0
            if (c := response.content.get("_connection_enforced")):
                connection_dropped = c.get("count", 0)
                auto_parented = c.get("auto_parented", [])
                if connection_dropped:
                    logger.warning(
                        "connection_enforced_dropped_patches",
                        user_id=user_id,
                        count=connection_dropped,
                        dropped=c["dropped"],
                        model=response.model,
                    )
                if auto_parented:
                    logger.info(
                        "connection_enforced_auto_parented",
                        user_id=user_id,
                        count=len(auto_parented),
                        project=meeting_project_for_enforcement,
                        patches=auto_parented,
                        model=response.model,
                    )

            # Person-ownership safety net. The prompt requires a person
            # patch + owns connection for every named action-item owner,
            # but Haiku 4.5 compliance is unreliable. enforce_person_ownership
            # walks commitment/blocker/decision/goal patches with a real
            # human owner_text, ensures a matching person patch exists, and
            # appends a person→action `owns` connection. Pass user_label so
            # the (you) speaker doesn't get a synthetic person patch about
            # themselves.
            enforce_person_ownership(response.content, user_label=user_label)
            if (po := response.content.get("_person_ownership_enforced")):
                injected_persons = po.get("persons_injected", [])
                injected_edges = po.get("connections_injected", [])
                if injected_persons or injected_edges:
                    logger.info(
                        "person_ownership_enforced",
                        user_id=user_id,
                        persons_injected=injected_persons,
                        connections_injected=len(injected_edges),
                        model=response.model,
                    )

            patches_after_filters = len(response.content.get("patches") or [])

            if reasoning_chars:
                logger.debug(
                    "extraction_reasoning",
                    user_id=user_id,
                    reasoning_chars=reasoning_chars,
                    model=response.model,
                )
            sanitize_you_marker_from_patches(response.content)
            strip_ephemeral_fields(response.content)

            timestamp = payload.get("timestamp")
            project = metadata.get("project") if metadata else None
            project_id = metadata.get("project_id") if metadata else None
            origin_id = metadata.get("origin_id") if metadata else None
            origin_type = metadata.get("origin_type") if metadata else None

            # Auto-register project if project_id provided
            if project_id and project:
                try:
                    await self.db.execute(
                        """INSERT INTO projects (project_id, user_id, name)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (project_id) DO UPDATE SET updated_at = NOW()""",
                        project_id, user_id, project
                    )
                except Exception:
                    pass  # Table may not exist yet

            # Connected Quilt V2: patches with connections
            patches = response.content.get("patches", [])
            entities = response.content.get("entities", [])
            relationships = response.content.get("relationships", [])

            if patches:
                # V2 model — typed, connected patches
                if len(patches) > MAX_PATCHES_PER_MEETING:
                    logger.warning("extraction_capped", type="patches", original=len(patches), capped=MAX_PATCHES_PER_MEETING)
                    patches = patches[:MAX_PATCHES_PER_MEETING]

                patches_stored = await store_connected_patches(
                    self.db, user_id, patches, "meeting_summary", app_id, timestamp,
                    project, project_id, origin_id, origin_type
                )
                facts_stored = patches_stored
                actions_stored = 0

                # Auto-connect the (you) person patch to all project patches.
                # The submitting user works on every project in their quilt.
                await self._ensure_user_project_connections(user_id, display_name)
            else:
                # V1 fallback — flat facts + action_items
                facts = response.content.get("facts", [])
                action_items = response.content.get("action_items", [])

                if len(facts) > MAX_FACTS_PER_MEETING:
                    logger.warning("extraction_capped", type="facts", original=len(facts), capped=MAX_FACTS_PER_MEETING)
                    facts = facts[:MAX_FACTS_PER_MEETING]
                if len(action_items) > MAX_ACTION_ITEMS_PER_MEETING:
                    logger.warning("extraction_capped", type="action_items", original=len(action_items), capped=MAX_ACTION_ITEMS_PER_MEETING)
                    action_items = action_items[:MAX_ACTION_ITEMS_PER_MEETING]

                facts_stored = await store_facts(
                    self.db, user_id, facts, "meeting_summary", app_id, timestamp, project
                )
                actions_stored = await store_action_items(
                    self.db, user_id, action_items, app_id, timestamp, project
                )

            # Entities and relationships always stored (feeds entity name index)
            if len(entities) > MAX_ENTITIES_PER_MEETING:
                logger.warning("extraction_capped", type="entities", original=len(entities), capped=MAX_ENTITIES_PER_MEETING)
                entities = entities[:MAX_ENTITIES_PER_MEETING]
            if len(relationships) > MAX_RELATIONSHIPS_PER_MEETING:
                logger.warning("extraction_capped", type="relationships", original=len(relationships), capped=MAX_RELATIONSHIPS_PER_MEETING)
                relationships = relationships[:MAX_RELATIONSHIPS_PER_MEETING]

            entities_stored = await store_entities(
                self.db, self.redis, user_id, entities, metadata
            )
            relationships_stored = await store_relationships(
                self.db, user_id, relationships, metadata
            )

            logger.info(
                "meeting_summary_complete",
                user_id=user_id,
                facts_stored=facts_stored,
                actions_stored=actions_stored,
                entities_stored=entities_stored,
                relationships_stored=relationships_stored,
                cost_usd=response.cost_usd,
                model=response.model,
            )

            # Persist ingestion audit log for dashboard observability
            try:
                await self.db.execute(
                    """
                    INSERT INTO extraction_metrics (
                        user_id, model, input_tokens, output_tokens,
                        cost_usd, latency_ms, patches_extracted, entities_extracted,
                        source_prompt, app_id, origin_id, origin_type, interaction_type,
                        owner_speaker_label, owner_marker_present,
                        owner_gate_filtered, connection_dropped,
                        patches_before_filters, patches_after_filters,
                        reasoning_chars, transcript_chars,
                        user_identified, identification_source
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23)
                    """,
                    user_id, response.model, response.input_tokens, response.output_tokens,
                    response.cost_usd, response.latency_ms, facts_stored, entities_stored,
                    "meeting_summary", app_id, origin_id, origin_type, "meeting_summary",
                    owner_speaker_label, has_you_marker,
                    owner_gate_filtered, connection_dropped,
                    patches_before_filters, patches_after_filters,
                    reasoning_chars, len(summary),
                    user_identified, identification_source,
                )
            except Exception as e:
                logger.warning("metrics_insert_failed", error=str(e))

            # Communication profile: separate lightweight call, only when (you) marker present
            if has_you_marker:
                await self._extract_communication_profile(user_id, summary, app_id)

            await self.hydrate_cache(user_id)

        except Exception as e:
            logger.error("meeting_summary_failed", error=str(e), user_id=user_id)

    async def _extract_communication_profile(self, user_id: str, transcript: str, app_id: str = None):
        """Extract communication style scores from the (you) speaker's dialogue.
        Runs as a separate lightweight LLM call. Blends with existing profile via rolling average."""
        try:
            llm = await self._get_llm_for_app(app_id)
            response = await llm.extract(
                system_prompt=COMMUNICATION_PROFILE_SYSTEM,
                user_content=transcript,
            )

            scores = response.content
            if not scores or not isinstance(scores, dict):
                logger.info("comm_profile_null", user_id=user_id, reason="null or invalid response")
                return

            # Validate: all expected dimensions present with numeric values
            dimensions = ("verbosity", "directness", "formality", "technical_level", "warmth", "detail_orientation")
            valid_scores = {}
            for dim in dimensions:
                val = scores.get(dim)
                if isinstance(val, (int, float)) and 0.0 <= val <= 1.0:
                    valid_scores[dim] = round(float(val), 2)

            if not valid_scores:
                logger.info("comm_profile_null", user_id=user_id, reason="no valid scores")
                return

            # Load existing profile for rolling average
            existing = await self.db.fetchval(
                "SELECT variables->'communication_profile' FROM profiles WHERE user_id = $1",
                user_id
            )

            if existing:
                existing = json.loads(existing) if isinstance(existing, str) else existing
                sample_count = existing.get("_sample_count", 1)
                new_count = sample_count + 1

                # Weighted rolling average: blend new scores with existing
                blended = {}
                for dim in dimensions:
                    old_val = existing.get(dim)
                    new_val = valid_scores.get(dim)
                    if old_val is not None and new_val is not None:
                        blended[dim] = round((old_val * sample_count + new_val) / new_count, 2)
                    elif new_val is not None:
                        blended[dim] = new_val
                    elif old_val is not None:
                        blended[dim] = old_val

                blended["_sample_count"] = new_count
                profile_data = blended
            else:
                valid_scores["_sample_count"] = 1
                profile_data = valid_scores

            # Store in profiles.variables
            await self.db.execute(
                """UPDATE profiles
                SET variables = jsonb_set(COALESCE(variables, '{}'::jsonb), '{communication_profile}', $1::jsonb),
                    last_updated = NOW()
                WHERE user_id = $2""",
                json.dumps(profile_data), user_id
            )

            logger.info(
                "comm_profile_updated",
                user_id=user_id,
                scores={k: v for k, v in profile_data.items() if k != "_sample_count"},
                sample_count=profile_data.get("_sample_count", 1),
                cost_usd=response.cost_usd,
            )

        except Exception as e:
            logger.warning("comm_profile_failed", user_id=user_id, error=str(e))

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
            entities = response.content.get("entities", [])
            relationships = response.content.get("relationships", [])
            app_id = payload.get("app_id")
            timestamp = payload.get("timestamp")

            stored = await store_facts(
                self.db, user_id, facts, "archivist", app_id, timestamp
            )
            await store_entities(self.db, self.redis, user_id, entities)
            await store_relationships(self.db, user_id, relationships)

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
                entities = response.content.get("entities", [])
                relationships = response.content.get("relationships", [])
                app_id = payload.get("app_id")
                timestamp = payload.get("timestamp")

                stored = await store_facts(
                    self.db, user_id, facts, "detective", app_id, timestamp
                )
                await store_entities(self.db, self.redis, user_id, entities)
                await store_relationships(self.db, user_id, relationships)
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
