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
import time
import uuid
from datetime import datetime
from typing import Any

import asyncpg
import httpx
import redis.asyncio as redis
import structlog

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contextquilt.gateway.extraction import classify_fact
from contextquilt.services.alerting import report_incident, sweep_stale_incidents
from contextquilt.services.attribution import validate_user_attribution_hint
from contextquilt.services.extraction_prompts import (
    COMMUNICATION_PROFILE_SYSTEM,
    CONVERSATION_SYSTEM,
    MEETING_SUMMARY_SYSTEM,
    TRACE_SYSTEM,
    format_open_commitments_block,
)
from contextquilt.services.extraction_schema import (
    EXTRACTION_SCHEMA,
    drop_placeholder_and_self_person_patches,
    drop_placeholder_entities,
    enforce_connection_requirements,
    enforce_connection_vocabulary,
    enforce_owner_gate,
    enforce_person_ownership,
    is_placeholder_or_self_person,
    normalize_cue_list,
    normalize_owner_in_transcript,
    sanitize_cues,
    sanitize_deadline_dates,
    sanitize_salience,
    sanitize_you_marker_from_patches,
    strip_ephemeral_fields,
    strip_owner_on_self_typed_patches,
    strip_prose_from_person_names,
)
from contextquilt.services.consolidation import (
    CONSOLIDATION_SYSTEM,
    CLUSTER_WINDOW_DAYS,
    MAX_CLUSTERS_PER_USER_PER_CYCLE,
    MAX_SOURCE_TEXTS,
    MAX_USERS_PER_APP_PER_CYCLE,
    build_synthesis_content,
    parse_consolidation_rules,
    parse_synthesis_response,
)
from contextquilt.services.corrections import (
    CORRECTION_SYSTEM,
    FALLBACK_PATCH_TYPE,
    MAX_CANDIDATES,
    MAX_CORRECTION_CHARS,
    build_correction_content,
    parse_correction_response,
)
from contextquilt.services.entity_aliasing import find_alias_candidate
from contextquilt.services.ingest_modes import is_interaction_allowed
from contextquilt.services.llm_client import LLMClient
from contextquilt.services.semantic_dedup import (
    DEDUP_JUDGE_SCHEMA,
    DEDUP_JUDGE_SYSTEM,
    MAX_JUDGE_PAIRS,
    SEMANTIC_DEDUP_FLOOR,
    TRIGRAM_DEDUP_THRESHOLD,
    build_dedup_judge_content,
    parse_dedup_verdicts,
)
from contextquilt.services.llm_client_anthropic import AnthropicLLMClient
from contextquilt.services.llm_client_fallback import (
    LLMClientWithFallback,
    primary_provider_from_env,
)
from contextquilt.services.schema_prompt_builder import (
    build_output_schema as build_schema_output_schema,
)
from contextquilt.services.schema_prompt_builder import (
    build_prompt as build_schema_prompt,
)

# Populate os.environ from Secret Manager before Settings builds.
# No-op when CQ_GCP_PROJECT is unset (local dev).
from contextquilt.secrets import ensure_secrets_in_env
ensure_secrets_in_env()
from contextquilt.config import get_settings

# Configure Logging
logger = structlog.get_logger()

# Configuration
_settings = get_settings()
REDIS_URL = _settings.redis_url
DATABASE_URL = _settings.database_url

# Queue settings
QUEUE_MAX_WAIT_MINUTES = _settings.cq_queue_max_wait_minutes
QUEUE_BUDGET_THRESHOLD = _settings.cq_queue_budget_threshold
QUEUE_CHECK_INTERVAL_SECONDS = 30  # How often to check queues for processing

# Extraction caps — belt-and-suspenders with prompt limits
MAX_FACTS_PER_MEETING = 5
MAX_ACTION_ITEMS_PER_MEETING = 3
MAX_PATCHES_PER_MEETING = 12  # Connected quilt model (replaces facts+actions for V2)
MAX_ENTITIES_PER_MEETING = 10
MAX_RELATIONSHIPS_PER_MEETING = 10

# Longitudinal (time-series) patches: an incoming observation joins an
# existing series when its descriptor field trigram-matches an active
# same-type series for this subject (and rehearsal) above this bar — the
# CQ-derived identity model (doc §12 Decision 1). Set at the "same fact"
# trigram bar and biased high on purpose: on a near-miss we open a NEW
# series rather than risk a wrong merge that would corrupt a trend line.
# Embedding match supersedes trigram when pgvector lands.
LONGITUDINAL_SERIES_MATCH_THRESHOLD = 0.6

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


def batch_messages(messages: list[dict], batch_size: int = 10) -> list[list[dict]]:
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
    facts: list[dict[str, Any]],
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
                origin_mode, source_prompt, confidence, persistence, project,
                created_at, updated_at, last_observed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            patch_id, patch_name, category, value_json,
            "inferred", source_prompt, 0.8,
            "sticky" if category in ("trait", "preference") else "decaying",
            project,
            created_at, created_at, created_at
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
    action_items: list[dict[str, Any]],
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
                origin_mode, source_prompt, confidence, persistence, project,
                created_at, updated_at, last_observed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """,
            patch_id, patch_name, "commitment", value_json,
            "inferred", "meeting_summary", 0.8, "decaying", project,
            created_at, created_at, created_at
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


def normalize_series_descriptor(text: str) -> str:
    """Canonicalize a longitudinal series descriptor (the skill/metric name)
    for identity matching: lowercase and collapse internal whitespace."""
    return " ".join((text or "").lower().split())


async def store_connected_patches(
    db,
    user_id: str,
    patches: list[dict[str, Any]],
    source_prompt: str,
    app_id: str | None = None,
    timestamp: str | None = None,
    project: str | None = None,
    project_id: str | None = None,
    origin_id: str | None = None,
    origin_type: str | None = None,
    user_label: str | None = None,
    llm=None,
    longitudinal_types: dict[str, str] | None = None,
):
    """
    Store typed, connected patches (Connected Quilt V2 model).
    Two-pass: create all patches first, then create connections between them.

    `user_label` is the (you) speaker's diarization label — Pass-2 stub
    synthesis uses it to refuse person stubs for the user themselves.

    `llm` (optional, duck-typed .extract) enables semantic dedup: trigram
    gray-zone pairs are judged by one batched LLM call. Omitted/None →
    trigram-only dedup, exactly the pre-existing behavior.

    `longitudinal_types` maps a patch_type → the value field naming its
    series descriptor (e.g. {"skill_rating": "skill"}). For those types an
    incoming patch is APPENDED as an observation to its matching series
    instead of being dedup-collapsed, so a trajectory (Weak→Meets→Strong) is
    preserved rather than overwritten. Empty/None (the SS path) → no type is
    longitudinal and behavior is exactly as before.
    """
    if not patches:
        return 0
    longitudinal_types = longitudinal_types or {}

    await db.execute(
        "INSERT INTO profiles (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
        user_id
    )

    created_at = datetime.fromisoformat(timestamp) if timestamp else datetime.utcnow()
    subject_key = f"user:{user_id}"

    # Pass 1: Create all patches, build lookup map for connection resolution
    patch_lookup = {}  # (text_lower, type) → patch_id
    stored = 0

    # Project-scoped types get the project tag (per the v1.1 SS schema).
    # `deliverable` is a Connection-facet type but project-scoped: every
    # deliverable lives inside a project and should carry the parent's
    # project/origin metadata alongside the episode types. Function scope —
    # used by inserts, Pass-2 stub synthesis, AND the gray-zone project
    # guard below.
    project_scoped_types = (
        "decision", "commitment", "blocker", "takeaway",
        "goal", "constraint", "event", "deliverable",
    )

    async def _store_cues(patch_id: str, cues: list) -> None:
        """Attach associative-retrieval cues to a patch. Idempotent (PK on
        (patch_id, cue)); the dedup/re-observe paths UNION new cues into
        the surviving patch the same way deadline detail merges forward."""
        for cue in cues or []:
            await db.execute(
                """
                INSERT INTO patch_cues (patch_id, cue) VALUES ($1::uuid, $2)
                ON CONFLICT (patch_id, cue) DO NOTHING
                """,
                patch_id, cue,
            )

    async def _apply_patch_dedup(existing_id: str, value: dict, text: str,
                                 patch_type: str, tier: str,
                                 cues: list | None = None) -> None:
        """Re-observe an existing patch instead of inserting a duplicate.

        `last_observed_at` is the freshness anchor consumed by the decay
        worker and recall scorer for self-typed patches — bumping it here
        is the only path that should ever move it; admin edits move only
        `updated_at`.

        Also merges deadline detail forward: "I'll ship it" followed a
        week later by "ship it by Friday" is one fact gaining a date, so
        when the re-observation carries a deadline_date the existing
        patch lacks, copy it over.
        """
        await db.execute(
            "UPDATE context_patches SET updated_at = $1, last_observed_at = $1 WHERE patch_id = $2::uuid",
            created_at, existing_id
        )
        await db.execute(
            "UPDATE patch_usage_metrics SET access_count = access_count + 1, last_accessed_at = $1 WHERE patch_id = $2::uuid",
            created_at, existing_id
        )
        new_dd = value.get("deadline_date")
        if new_dd:
            await db.execute(
                """
                UPDATE context_patches
                   SET value = jsonb_set(
                           jsonb_set(value, '{deadline_date}', to_jsonb($1::text)),
                           '{deadline}', to_jsonb($2::text)
                       )
                 WHERE patch_id = $3::uuid
                   AND value->>'deadline_date' IS NULL
                """,
                new_dd, value.get("deadline") or new_dd, existing_id,
            )
        # Salience merges forward like deadline detail: a re-observation
        # flagged high UPGRADES the surviving patch; nothing ever
        # auto-downgrades (a fact stated urgently once stays weighted).
        if value.get("salience") == "high":
            await db.execute(
                """
                UPDATE context_patches
                   SET value = jsonb_set(value, '{salience}', '"high"')
                 WHERE patch_id = $1::uuid
                   AND COALESCE(value->>'salience', '') <> 'high'
                """,
                existing_id,
            )
        await _store_cues(existing_id, cues or [])
        patch_lookup[(text.lower().strip(), patch_type)] = existing_id
        logger.debug("patch_deduplicated", type=patch_type, text=text[:50],
                     patch_id=existing_id, tier=tier)

    async def _insert_new_patch(patch: dict, patch_type: str, value: dict, text: str) -> None:
        """Insert a genuinely new patch (no dedup match)."""
        nonlocal stored
        patch_id = str(uuid.uuid4())
        patch_name = f"{source_prompt}_{patch_id[:8]}"
        value_json = json.dumps(value)
        persistence = DEFAULT_PERSISTENCE.get(patch_type, "decaying")

        patch_project = project if patch_type in project_scoped_types else None
        # Role patches can also be project-scoped if they have a belongs_to connection
        if patch_type == "role" and project:
            connects_to = patch.get("connects_to", [])
            if any(c.get("role") == "parent" for c in connects_to):
                patch_project = project

        # Project-scoped patches get both the text project name and the stable project_id.
        # Role-with-parent inherits the same project metadata; origin_id must follow
        # the same gate so the (project_id, origin_id) pair is internally consistent.
        # Without this, role patches landed with project_id set but origin_id NULL,
        # which SS's diagnostic surfaced as a NULL-origin "regression" 2026-05-04.
        patch_project_id = project_id if patch_type in project_scoped_types or (patch_type == "role" and patch_project) else None
        patch_origin_id = origin_id if patch_type in project_scoped_types or (patch_type == "role" and patch_project) else None
        patch_origin_type = origin_type if patch_origin_id else None

        await db.execute(
            """
            INSERT INTO context_patches (
                patch_id, patch_name, patch_type, value,
                origin_mode, source_prompt, confidence, persistence,
                project, project_id, origin_id, origin_type,
                status, created_at, updated_at, last_observed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            """,
            patch_id, patch_name, patch_type, value_json,
            "inferred", source_prompt, 0.8, persistence,
            patch_project, patch_project_id, patch_origin_id, patch_origin_type,
            "active", created_at, created_at, created_at
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

        await _store_cues(patch_id, patch.get("_cues") or [])
        patch_lookup[(text.lower().strip(), patch_type)] = patch_id
        stored += 1

    async def _append_observation(patch_id: str, value: dict) -> None:
        """Append one point to a longitudinal series' history table.

        The context_patches row keeps only the latest snapshot (for the
        byte-stable recall hot path); patch_observations holds the full
        trajectory that Review / trend queries read.
        """
        try:
            src_app = str(uuid.UUID(app_id)) if app_id else None
        except (ValueError, TypeError):
            src_app = None  # legacy non-UUID app_id → no source_app FK
        await db.execute(
            """
            INSERT INTO patch_observations
                (patch_id, observed_at, value, origin_id, origin_type, source_app)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::uuid)
            """,
            patch_id, created_at, json.dumps(value), origin_id, origin_type, src_app,
        )

    async def _insert_series_identity(patch_type: str, value: dict, text: str,
                                      descriptor: str) -> str:
        """Create the identity row for a NEW longitudinal series; return its
        patch_id. Unlike _insert_new_patch, longitudinal rows always carry
        their rehearsal (project/origin) context so a series is scoped to one
        rehearsal. The worker's hardcoded `project_scoped_types` set is
        SS-shaped and excludes app-specific longitudinal types; attaching the
        context explicitly here is a contained workaround until project
        scoping becomes manifest-driven (tracked as a follow-up).
        """
        nonlocal stored
        patch_id = str(uuid.uuid4())
        patch_name = f"series:{patch_type}:{descriptor}"[:255]
        await db.execute(
            """
            INSERT INTO context_patches (
                patch_id, patch_name, patch_type, value,
                origin_mode, source_prompt, confidence, persistence,
                project, project_id, origin_id, origin_type,
                status, created_at, updated_at, last_observed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
            """,
            patch_id, patch_name, patch_type, json.dumps(value),
            "inferred", source_prompt, 0.8, DEFAULT_PERSISTENCE.get(patch_type, "sticky"),
            project, project_id, origin_id, origin_type,
            "active", created_at, created_at, created_at,
        )
        await db.execute(
            "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
            patch_id, subject_key,
        )
        await db.execute(
            """
            INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
            VALUES ($1, 1, $2, 1.0)
            """,
            patch_id, created_at,
        )
        if app_id:
            try:
                await db.execute(
                    "INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write, can_delete) VALUES ($1, $2::uuid, TRUE, TRUE, TRUE)",
                    patch_id, app_id,
                )
            except Exception:
                pass
        patch_lookup[(text.lower().strip(), patch_type)] = patch_id
        stored += 1
        return patch_id

    async def _store_longitudinal(patch: dict, patch_type: str, value: dict,
                                  text: str, descriptor_field: str) -> None:
        """Route one longitudinal patch: match it to an existing series and
        APPEND the observation (never collapse), else open a new series.
        Series identity is CQ-derived (doc §12 Decision 1): trigram match on
        the descriptor field, scoped to this (subject, type, rehearsal).
        """
        descriptor = normalize_series_descriptor(str(value.get(descriptor_field, "")))
        if not descriptor:
            # No descriptor to key the series on — never drop the signal;
            # store it as a plain patch so it is at least retained.
            await _insert_new_patch(patch, patch_type, value, text)
            return

        match = await db.fetchrow(
            """
            SELECT cp.patch_id,
                   SIMILARITY(LOWER(cp.value->>$2), LOWER($4)) AS sim
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1 AND cp.patch_type = $3
              AND COALESCE(cp.status, 'active') = 'active'
              AND cp.project_id IS NOT DISTINCT FROM $6
              AND SIMILARITY(LOWER(cp.value->>$2), LOWER($4)) > $5
            ORDER BY sim DESC
            LIMIT 1
            """,
            subject_key, descriptor_field, patch_type, descriptor,
            LONGITUDINAL_SERIES_MATCH_THRESHOLD, project_id,
        )

        if match:
            identity_id = str(match["patch_id"])
            await _append_observation(identity_id, value)
            # Refresh the hot-path snapshot to the latest point; bump the
            # freshness + usage anchors exactly as the dedup re-observe path.
            await db.execute(
                "UPDATE context_patches SET value = $1, updated_at = $2, last_observed_at = $2 WHERE patch_id = $3::uuid",
                json.dumps(value), created_at, identity_id,
            )
            await db.execute(
                "UPDATE patch_usage_metrics SET access_count = access_count + 1, last_accessed_at = $1 WHERE patch_id = $2::uuid",
                created_at, identity_id,
            )
            patch_lookup[(text.lower().strip(), patch_type)] = identity_id
            logger.debug("longitudinal_observation_appended", type=patch_type,
                         descriptor=descriptor[:50], patch_id=identity_id,
                         sim=float(match["sim"]))
        else:
            identity_id = await _insert_series_identity(patch_type, value, text, descriptor)
            await _append_observation(identity_id, value)
            logger.debug("longitudinal_series_created", type=patch_type,
                         descriptor=descriptor[:50], patch_id=identity_id)
        await _store_cues(identity_id, patch.get("_cues") or [])

    # Gray-zone pairs deferred to one batched LLM judge call after the
    # loop: (patch, patch_type, value, text, existing_id, existing_text)
    gray_pending: list[tuple] = []
    semantic_enabled = (
        llm is not None and get_settings().cq_semantic_dedup_enabled
    )

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

        # Cues live in patch_cues, not in the value JSONB — pop them off
        # before any path serializes the value. normalize_cue_list is a
        # defensive re-check: the sanitizer chain covers LLM extraction,
        # but structured-ingest payloads reach here without it.
        patch["_cues"] = normalize_cue_list(value.pop("cues", None))

        # Longitudinal (time-series) types append an observation instead of
        # dedup-collapsing — a rating's history IS the value (doc §12 §3).
        if patch_type in longitudinal_types:
            await _store_longitudinal(
                patch, patch_type, value, text, longitudinal_types[patch_type]
            )
            continue

        # Deduplication, two tiers against active same-type patches:
        #   similarity > TRIGRAM_DEDUP_THRESHOLD          → same fact, fast path
        #   SEMANTIC_DEDUP_FLOOR < similarity <= threshold → gray zone; deferred
        #     to one batched LLM judge call ("Deploy API by EOW" vs "Ship API
        #     before end of week" lands here)
        #   below the floor                                → new patch
        existing = await db.fetchrow(
            """
            SELECT cp.patch_id, cp.value->>'text' AS existing_text, cp.project_id,
                   SIMILARITY(LOWER(cp.value->>'text'), LOWER($3)) AS sim
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1 AND cp.patch_type = $2
              AND SIMILARITY(LOWER(cp.value->>'text'), LOWER($3)) > $4
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY sim DESC
            LIMIT 1
            """,
            subject_key, patch_type, text, SEMANTIC_DEDUP_FLOOR
        )
        if existing and existing["sim"] > TRIGRAM_DEDUP_THRESHOLD:
            await _apply_patch_dedup(
                str(existing["patch_id"]), value, text, patch_type, "trigram",
                cues=patch.get("_cues"),
            )
            continue
        # Project guard for the judge: "Send the invoice" in project A and
        # project B are different facts even with identical wording. The
        # judge sees text only, so cross-project pairs never reach it.
        same_project_scope = (
            patch_type not in project_scoped_types
            or existing is None
            or existing["project_id"] == project_id
        )
        if (
            existing and semantic_enabled and same_project_scope
            and len(gray_pending) < MAX_JUDGE_PAIRS
        ):
            gray_pending.append((
                patch, patch_type, value, text,
                str(existing["patch_id"]), existing["existing_text"],
            ))
            continue

        await _insert_new_patch(patch, patch_type, value, text)

    # Phase B: judge the gray-zone pairs in one batched LLM call.
    # Verdict TRUE → re-observe the existing patch (semantic dedup);
    # FALSE or any judge failure → insert as new (today's behavior — a
    # broken judge must never lose a memory).
    if gray_pending:
        verdicts = [False] * len(gray_pending)
        try:
            response = await llm.extract(
                system_prompt=DEDUP_JUDGE_SYSTEM,
                user_content=build_dedup_judge_content(
                    [(g[3], g[5]) for g in gray_pending]
                ),
                json_schema=DEDUP_JUDGE_SCHEMA,
            )
            verdicts = parse_dedup_verdicts(response.content, len(gray_pending))
        except Exception as exc:
            logger.warning(
                "semantic_dedup_judge_failed",
                user_id=user_id, pairs=len(gray_pending), reason=str(exc)[:200],
            )
        merged = 0
        for (patch, patch_type, value, text, existing_id, existing_text), same in zip(
            gray_pending, verdicts
        ):
            if same:
                await _apply_patch_dedup(existing_id, value, text, patch_type, "semantic",
                                         cues=patch.get("_cues"))
                logger.info(
                    "semantic_dedup_merged",
                    user_id=user_id, type=patch_type,
                    new_text=text[:80], existing_text=(existing_text or "")[:80],
                    patch_id=existing_id,
                )
                merged += 1
            else:
                await _insert_new_patch(patch, patch_type, value, text)
        if merged or verdicts:
            logger.info(
                "semantic_dedup_judged",
                user_id=user_id, pairs=len(gray_pending), merged=merged,
            )

    # Pass 2: Create connections between patches.
    #
    # connections_created counts rows ACTUALLY inserted; ON-CONFLICT skips
    # land in connections_skipped_dup. Pre-fix, this counter incremented
    # unconditionally after every db.execute() — including the ON CONFLICT
    # DO NOTHING no-ops — which produced misleading log lines like
    # "connections=6" on a meeting where only 3 fresh edges hit the DB.
    # That gap was the unsolved part of the 2026-04-30 audit (separate
    # from PR #87's cap-vs-enforcer ordering bug).
    connections_created = 0
    connections_skipped_dup = 0
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
                # Same gate drop_placeholder_and_self_person_patches applies
                # to LLM-emitted person patches: never synthesize a person
                # stub for a diarization placeholder or the (you) speaker.
                # Without this, a connects_to target like the user's own
                # label re-creates the self-person patch the sanitizer just
                # dropped (seen in a prod quilt 2026-06-10).
                if target_type == "person" and is_placeholder_or_self_person(
                    conn.get("target_text", ""), user_label
                ):
                    continue
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
                        status, created_at, updated_at, last_observed_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                    """,
                    to_id, stub_name, target_type, stub_value,
                    "inferred", source_prompt, 0.6, stub_persistence,
                    stub_project, stub_project_id, stub_origin_id, stub_origin_type,
                    "active", created_at, created_at, created_at
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

            # Create the connection. asyncpg's db.execute returns the
            # command tag (e.g. "INSERT 0 1" on insert, "INSERT 0 0" on
            # ON CONFLICT skip). We split the two so the log line
            # accurately reflects what actually hit the DB.
            try:
                cmd_tag = await db.execute(
                    """
                    INSERT INTO patch_connections (from_patch_id, to_patch_id, connection_role, connection_label, context)
                    VALUES ($1::uuid, $2::uuid, $3, $4, $5)
                    ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO NOTHING
                    """,
                    actual_from, actual_to, role, label, conn.get("context")
                )
                if isinstance(cmd_tag, str) and cmd_tag.endswith(" 1"):
                    connections_created += 1
                    # Lifecycle trigger: REPLACES → archive the target.
                    # Only fire when we actually wrote the edge — re-running
                    # an already-applied replaces shouldn't re-archive.
                    if role == "replaces":
                        await db.execute(
                            "UPDATE context_patches SET status = 'archived', completed_at = NOW() WHERE patch_id = $1::uuid",
                            to_id
                        )
                else:
                    connections_skipped_dup += 1
            except Exception as e:
                logger.warning("connection_failed", error=str(e), from_id=from_id, to_id=to_id)

    logger.info(
        "connected_patches_stored",
        patches=stored,
        connections=connections_created,
        connections_skipped_dup=connections_skipped_dup,
        user_id=user_id,
    )
    return stored


async def store_entities(
    db,
    redis_client,
    user_id: str,
    entities: list[dict],
    metadata: dict | None = None,
):
    """
    Store extracted entities to Postgres, resolving alternate surface
    forms to canonical entities instead of fragmenting the graph.

    Resolution order per extracted name (within the same entity_type):
      1. case-insensitive exact name match            → re-observe
      2. recorded alias match (entity_aliases)        → re-observe canonical
      3. conservative alias heuristic vs existing
         same-type entities (token subset / initial
         expansion, UNIQUE candidate only):
           - new name is the short form  → record alias, re-observe
           - new name is the fuller form → rename canonical to it,
                                           keep the old name as alias
      4. genuinely new                                → insert

    Steps 2-3 degrade gracefully (plain insert) if entity_aliases is
    missing — the MCP deployment's separate Postgres can lag on
    migrations.
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
        # Defensive sink guard (same dual-layer pattern as cues): the
        # sanitizer chain covers LLM extraction, but chat/structured
        # lanes reach this sink without it — and this is exactly the
        # lane the 2026-06-30 Speaker-N leak came through.
        if is_placeholder_or_self_person(name):
            continue

        metadata_json = json.dumps(metadata or {})

        async def _reobserve(entity_id) -> None:
            await db.execute(
                """
                UPDATE entities SET
                    description = COALESCE(NULLIF($1, ''), description),
                    last_seen_at = NOW(),
                    mention_count = mention_count + 1,
                    metadata = metadata || $2::jsonb
                WHERE entity_id = $3
                """,
                description, metadata_json, entity_id,
            )

        # 1. Exact match, case-insensitive (the old ON CONFLICT only
        #    caught exact case, so "sarah abrams" vs "Sarah Abrams"
        #    used to create two rows).
        row = await db.fetchrow(
            """
            SELECT entity_id FROM entities
            WHERE user_id = $1 AND entity_type = $2 AND LOWER(name) = LOWER($3)
            LIMIT 1
            """,
            user_id, entity_type, name,
        )
        if row:
            await _reobserve(row["entity_id"])
            stored += 1
            continue

        try:
            # 2. Recorded alias
            row = await db.fetchrow(
                """
                SELECT e.entity_id
                FROM entity_aliases a
                JOIN entities e ON e.entity_id = a.entity_id
                WHERE a.user_id = $1 AND LOWER(a.alias) = LOWER($2)
                  AND e.entity_type = $3
                LIMIT 1
                """,
                user_id, name, entity_type,
            )
            if row:
                await _reobserve(row["entity_id"])
                stored += 1
                continue

            # 3. Alias heuristic against existing same-type entities.
            #    Only acts on a UNIQUE candidate — ambiguity ("Sarah"
            #    with both "Sarah Abrams" and "Sarah Chen" present)
            #    falls through to a separate entity, as before.
            candidate_rows = await db.fetch(
                "SELECT entity_id, name FROM entities WHERE user_id = $1 AND entity_type = $2",
                user_id, entity_type,
            )
            match = find_alias_candidate(
                name, [(r["entity_id"], r["name"]) for r in candidate_rows]
            )
            if match:
                entity_id, existing_name, direction = match
                if direction == "name_is_canonical":
                    # The fuller form arrived later — promote it to the
                    # canonical name, keep the old short form as an alias.
                    await db.execute(
                        "UPDATE entities SET name = $1 WHERE entity_id = $2",
                        name, entity_id,
                    )
                    alias_to_record = existing_name
                else:
                    alias_to_record = name
                await db.execute(
                    """
                    INSERT INTO entity_aliases (user_id, entity_id, alias, source)
                    VALUES ($1, $2, $3, 'heuristic')
                    ON CONFLICT (user_id, LOWER(alias)) DO NOTHING
                    """,
                    user_id, entity_id, alias_to_record,
                )
                await _reobserve(entity_id)
                logger.info(
                    "entity_alias_resolved",
                    user_id=user_id,
                    entity_type=entity_type,
                    alias=alias_to_record,
                    canonical=name if direction == "name_is_canonical" else existing_name,
                    direction=direction,
                )
                stored += 1
                continue
        except Exception as e:
            # entity_aliases missing (MCP DB pre-migration) or any alias
            # machinery failure — fall through to the plain insert path.
            logger.debug("entity_alias_resolution_skipped", error=str(e)[:120])

        # 4. New entity. ON CONFLICT retained as a race-safety net.
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
            metadata_json,
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

        # Resolve entity IDs by name (match any type for this user).
        # Alias-aware: a relationship referencing "S. Abrams" must land
        # on the canonical "Sarah Abrams" entity that store_entities
        # resolved the surface form to.
        from_row = await _resolve_entity_id_by_name(db, user_id, from_name)
        to_row = await _resolve_entity_id_by_name(db, user_id, to_name)

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


async def _resolve_entity_id_by_name(db, user_id: str, name: str):
    """Resolve a surface-form name to an entity row, checking canonical
    names first, then recorded aliases. Case-insensitive. Returns a row
    with entity_id or None. Falls back to name-only resolution when the
    entity_aliases table is unavailable (MCP DB pre-migration)."""
    row = await db.fetchrow(
        "SELECT entity_id FROM entities WHERE user_id = $1 AND LOWER(name) = LOWER($2) LIMIT 1",
        user_id, name,
    )
    if row:
        return row
    try:
        return await db.fetchrow(
            "SELECT entity_id FROM entity_aliases WHERE user_id = $1 AND LOWER(alias) = LOWER($2) LIMIT 1",
            user_id, name,
        )
    except Exception:
        return None


async def _rebuild_entity_index(db, redis_client, user_id: str):
    """
    Rebuild the Redis entity name index for a user.
    Stores all entity names — canonical AND aliases — as a set for fast
    text matching on the hot path. A query mentioning "S. Abrams" must
    match even though the canonical entity is "Sarah Abrams".
    """
    try:
        rows = await db.fetch(
            "SELECT name FROM entities WHERE user_id = $1",
            user_id
        )
        names = {row["name"] for row in rows}
        try:
            alias_rows = await db.fetch(
                "SELECT alias FROM entity_aliases WHERE user_id = $1",
                user_id
            )
            names |= {row["alias"] for row in alias_rows}
        except Exception:
            pass  # entity_aliases not available (MCP DB pre-migration)
        key = f"entity_index:{user_id}"
        if names:
            await redis_client.delete(key)
            await redis_client.sadd(key, *names)
            await redis_client.expire(key, 7200)  # 2 hour TTL
            logger.info("entity_index_rebuilt", user_id=user_id, count=len(names))
    except Exception as e:
        logger.error("entity_index_rebuild_failed", user_id=user_id, error=str(e))

    # Sibling rebuild for the cue index — fresh cues from this extraction
    # become matchable immediately instead of waiting out the 2h TTL.
    try:
        cue_rows = await db.fetch(
            """
            SELECT DISTINCT pc.cue
            FROM patch_cues pc
            JOIN patch_subjects ps ON ps.patch_id = pc.patch_id
            JOIN context_patches cp ON cp.patch_id = pc.patch_id
            WHERE ps.subject_key = 'user:' || $1
              AND COALESCE(cp.status, 'active') = 'active'
            """,
            user_id
        )
        cues = {row["cue"] for row in cue_rows}
        cue_key = f"cue_index:{user_id}"
        if cues:
            await redis_client.delete(cue_key)
            await redis_client.sadd(cue_key, *cues)
            await redis_client.expire(cue_key, 7200)
            logger.info("cue_index_rebuilt", user_id=user_id, count=len(cues))
    except Exception as e:
        # patch_cues may not exist yet on a lagging DB (MCP) — degrade quietly.
        logger.warning("cue_index_rebuild_failed", user_id=user_id, error=str(e))


def _build_default_llm_client(*, alert_db=None):
    """Construct the default LLM client per CQ_LLM_PRIMARY_PROVIDER.

    - "anthropic" (default): AnthropicLLMClient as primary with the
      existing OpenAI-compat LLMClient (OpenRouter by default) as the
      fallback. Saves the OR markup on every successful primary call
      and emails the operator whenever Anthropic fails.
    - "openrouter" / anything else: existing LLMClient only, no
      fallback wrapper. Useful for tests, regression rollback, or any
      env without an Anthropic key configured.

    The selection runs once at worker startup; callers above this can
    treat the returned object as opaque since both branches expose the
    same `extract()` interface.
    """
    provider = primary_provider_from_env()
    # Resolve the Anthropic key through the secrets helper so a Secret
    # Manager value (CQ_GCP_PROJECT set, env empty) gates the path
    # correctly. Env still wins over SM for local dev / operator override.
    from contextquilt.secrets import get_secret
    anthropic_key_present = bool(
        get_secret("anthropic-api-key", env_var="CQ_ANTHROPIC_API_KEY")
    )
    if provider == "anthropic" and anthropic_key_present:
        primary = AnthropicLLMClient()
        fallback = LLMClient()
        logger.info(
            "llm_client_default_built",
            primary_provider="anthropic",
            primary_model=primary.model,
            fallback_model=fallback.model,
            alert_wired=alert_db is not None,
        )
        return LLMClientWithFallback(primary, fallback, alert_db=alert_db)
    # Fall through: no Anthropic key configured, or operator explicitly
    # opted out. Preserve the previous behavior exactly.
    client = LLMClient()
    logger.info(
        "llm_client_default_built",
        primary_provider="openrouter",
        primary_model=client.model,
        fallback="none",
    )
    return client


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
        llm_key = _settings.cq_llm_api_key
        llm_url = _settings.cq_llm_base_url
        llm_model = _settings.cq_llm_model
        if not llm_key or not llm_url:
            logger.error(
                "llm_not_configured",
                hint="Set CQ_LLM_API_KEY and CQ_LLM_BASE_URL in your .env file. "
                     "See env.example for options (OpenRouter, OpenAI, Gemini, Ollama, etc.)"
            )
            raise SystemExit("CQ_LLM_API_KEY and CQ_LLM_BASE_URL are required. See env.example.")

        self.redis = redis.from_url(REDIS_URL, decode_responses=True)
        # Connection POOL, not a single connection. The cold-path loops
        # (deadline sweep, decay, backup watch, provider health) plus the
        # main extraction path all run concurrently off self.db. A single
        # asyncpg connection can't service concurrent operations — when two
        # coroutines issue a query at the same moment (most visibly when all
        # loops fire together at startup) asyncpg raises "another operation
        # is in progress". A pool hands each concurrent caller its own
        # connection. The worker uses no explicit transactions on self.db
        # (every call is a standalone .execute/.fetch/.fetchrow/.fetchval),
        # so per-statement autocommit semantics are identical to before.
        self.db = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        # Default LLM client (server's own keys). Built from env vars
        # via `_build_default_llm_client` so the worker can flip
        # between Anthropic-primary + OR-fallback (production default)
        # and OR-only (test / fallback off) without code changes.
        self.llm = _build_default_llm_client(alert_db=self.db)
        self._app_llm_cache: dict[str, LLMClient] = {}  # Per-app BYOK clients
        self._ingest_mode_cache: dict[str, tuple] = {}  # app_id -> (mode|None, monotonic_ts)

        # Get context window for budget calculation. Explicit override
        # via CQ_LLM_CONTEXT_WINDOW wins; otherwise fall back to the
        # model-name registry, then to DEFAULT_CONTEXT_WINDOW.
        model_name = self.llm.model
        override = _settings.cq_llm_context_window
        self.context_window = (
            override
            if override is not None
            else KNOWN_CONTEXT_WINDOWS.get(model_name, DEFAULT_CONTEXT_WINDOW)
        )
        # Available budget = window - prompt overhead - output reserve
        self.context_budget = int((self.context_window - 2800) * QUEUE_BUDGET_THRESHOLD)

        self.running = True
        logger.info("worker_ready",
                     model=self.llm.model,
                     base_url=self.llm.base_url,
                     context_budget=self.context_budget)

        # Run stream consumer, queue checker, decay worker, backup
        # failure watcher, and provider health daemon concurrently.
        await asyncio.gather(
            self.deadline_sweep_loop(),
            self.consume_stream(),
            self.check_queues_loop(),
            self.decay_loop(),
            self.consolidation_loop(),
            self.backup_failure_watch_loop(),
            self.provider_health_loop(),
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

            except (redis.TimeoutError, TimeoutError) as e:
                # Idle tick — the blocking XREADGROUP raced the client's
                # socket timeout with no messages pending. Expected several
                # times a minute on a quiet stream; logging it at error
                # level buried real failures (prod was emitting
                # "stream_error: Timeout reading from cq-redis" every ~6s).
                logger.debug("stream_idle_timeout", error=str(e))
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

    async def deadline_sweep_loop(self):
        """Scheduled pass over deadline-bearing completables whose due
        date has passed (the final piece of the deadline-action gap).

        First detection stamps `value.overdue_since` (UTC date) and bumps
        updated_at, which (a) gives apps a durable overdue flag in quilt
        responses without doing date math, (b) flows the patch into the
        SS delta sync, and (c) restarts the decay grace window from the
        moment we noticed — combined with the deadline-aware staleness
        anchor, an overdue item lives TTL days past its deadline instead
        of silently dying mid-flight.

        Also logs per-cycle overdue totals as the observability seed
        ("how much overdue work is the system carrying").
        """
        # The stamp is idempotent (only-where-absent), so decay cadence
        # is fine; a deadline can only flip at UTC midnight anyway.
        # (Literal, not shared with decay_loop — its interval constant is
        # local to that coroutine, which crash-looped the worker when
        # this loop referenced it. Hotfix 2026-06-12.)
        SWEEP_INTERVAL_SECONDS = 6 * 60 * 60
        iso_date_re = r"'^\d{4}-\d{2}-\d{2}$'"
        overdue_sql = (
            f"value->>'deadline_date' ~ {iso_date_re} "
            "AND (value->>'deadline_date')::date < (NOW() AT TIME ZONE 'utc')::date"
        )
        while self.running:
            try:
                result = await self.db.execute(
                    f"""
                    UPDATE context_patches
                       SET value = jsonb_set(
                               value, '{{overdue_since}}',
                               to_jsonb(to_char((NOW() AT TIME ZONE 'utc')::date, 'YYYY-MM-DD'))
                           ),
                           updated_at = NOW()
                     WHERE patch_type IN ('commitment', 'blocker')
                       AND COALESCE(status, 'active') = 'active'
                       AND completed_at IS NULL
                       AND value->>'overdue_since' IS NULL
                       AND {overdue_sql}
                    """
                )
                stamped = int(result.split()[-1]) if result else 0
                total = await self.db.fetchval(
                    f"""
                    SELECT count(*) FROM context_patches
                     WHERE patch_type IN ('commitment', 'blocker')
                       AND COALESCE(status, 'active') = 'active'
                       AND completed_at IS NULL
                       AND {overdue_sql}
                    """
                )
                if stamped:
                    logger.info("deadline_sweep_stamped", newly_overdue=stamped, total_overdue=total)
                else:
                    logger.debug("deadline_sweep_complete", total_overdue=total)
            except Exception as e:
                logger.error("deadline_sweep_error", error=str(e))
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)

    async def decay_loop(self):
        """Periodically archive stale patches based on effective TTL.

        Effective TTL per patch:
          COALESCE(
              permanence_override → PERMANENCE_CLASS_DAYS[class],
              patch_type_registry.default_ttl_days for the type
          )

        Access-based refresh (patch_usage_metrics.last_accessed_at recent)
        exempts a patch regardless of override or type default.

        Staleness anchor:
          - Self-typed patches (trait/preference/goal/constraint) use
            `last_observed_at` — bumped only when the worker dedup path
            re-observes the patch in a new transcript. Admin edits do
            not refresh these.
          - All other types use `updated_at`.
        """
        # Fallback TTLs by patch_type when registry has no entry.
        # The four FRESHNESS_TRACKED_TYPES are sticky-self-disclosure
        # patches; their long horizon reflects that people change but
        # not weekly. After 540d without re-observation we assume the
        # preference is stale and let the decay archive collect it.
        DEFAULT_TTLS = {
            "takeaway": 14,
            "blocker": 30,
            "commitment": 30,
            "event": 90,
            "trait": 540,
            "preference": 540,
            "goal": 540,
            "constraint": 540,
        }
        # Types whose staleness is measured from `last_observed_at`
        # rather than `updated_at`. Matches the partial index in
        # init-db/20_preference_freshness.sql.
        FRESHNESS_TRACKED_TYPES = {"trait", "preference", "goal", "constraint"}
        # Deadline-bearing completables anchor on GREATEST(updated_at,
        # deadline_date) so they never archive before their due date.
        DEADLINE_ANCHORED_TYPES = {"commitment", "blocker"}
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
                    # Registry is keyed (type_key, app_id) — a global row
                    # (app_id NULL) and zero+ per-app rows can coexist.
                    # Without an ordering, fetchrow's pick was arbitrary and
                    # the effective TTL flipped between the global and app
                    # values. Prefer a non-NULL app-scoped row (smallest TTL
                    # wins on multi-app tie, conservative), else the non-NULL
                    # global row, else fall through to the DEFAULT_TTLS
                    # hardcode. When a second app exists, the right shape is
                    # per-patch resolution via context_patch_acl, not a single
                    # global winner.
                    try:
                        row = await self.db.fetchrow(
                            """
                            SELECT default_ttl_days
                            FROM patch_type_registry
                            WHERE type_key = $1 AND default_ttl_days IS NOT NULL
                            ORDER BY (app_id IS NULL) ASC, default_ttl_days ASC
                            LIMIT 1
                            """,
                            patch_type
                        )
                        if row and row["default_ttl_days"] is not None:
                            ttl_days = row["default_ttl_days"]
                    except Exception:
                        pass  # Registry table may not exist yet

                    # Pick the staleness anchor. Self-typed patches use
                    # `last_observed_at` (bumped only on re-observation in
                    # an extraction); everything else uses `updated_at`.
                    # COALESCE protects against pre-PR rows that may have
                    # NULL last_observed_at before the backfill runs.
                    if patch_type in FRESHNESS_TRACKED_TYPES:
                        staleness_anchor_sql = "COALESCE(last_observed_at, created_at)"
                    elif patch_type in DEADLINE_ANCHORED_TYPES:
                        # Deadline-bearing completables must not archive
                        # before their own due date: a commitment created
                        # June 1 due Aug 15 used to decay July 1 (30d from
                        # updated_at). Anchor on whichever is later —
                        # last touch or the deadline itself — so TTL counts
                        # as grace AFTER the due date for future-dated
                        # items. Regex-guarded cast: only sanitizer-valid
                        # ISO dates participate.
                        staleness_anchor_sql = (
                            "GREATEST(updated_at, "
                            "CASE WHEN value->>'deadline_date' ~ '^\\d{4}-\\d{2}-\\d{2}$' "
                            "THEN (value->>'deadline_date')::date::timestamptz "
                            "ELSE updated_at END)"
                        )
                    else:
                        staleness_anchor_sql = "updated_at"

                    # Archive patches older than TTL that haven't been accessed recently
                    # and have no explicit override. Salience stretches or
                    # shrinks the effective TTL per patch (high ×1.5, low
                    # ×0.5, absent = ×1.0) — judgment-weighted encoding's
                    # lifecycle half; the recall scorer holds the other.
                    # The access-exemption window stays at the unmodified
                    # TTL: usage refresh is orthogonal to salience.
                    salience_ttl_sql = (
                        "(CASE value->>'salience' "
                        "WHEN 'high' THEN 1.5 WHEN 'low' THEN 0.5 ELSE 1.0 END)"
                    )
                    result = await self.db.execute(
                        f"""
                        UPDATE context_patches SET status = 'archived', updated_at = NOW()
                        WHERE patch_type = $1
                          AND permanence_override IS NULL
                          AND COALESCE(status, 'active') = 'active'
                          AND {staleness_anchor_sql} < NOW() - INTERVAL '1 day' * $2 * {salience_ttl_sql}
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
                        logger.info(
                            "decay_archived",
                            patch_type=patch_type,
                            count=count,
                            ttl_days=ttl_days,
                            anchor=(
                                "last_observed_at" if patch_type in FRESHNESS_TRACKED_TYPES
                                else "max(updated_at, deadline)" if patch_type in DEADLINE_ANCHORED_TYPES
                                else "updated_at"
                            ),
                        )

                if total_archived > 0:
                    logger.info("decay_cycle_complete", total_archived=total_archived)
                else:
                    logger.debug("decay_cycle_complete", total_archived=0)

            except Exception as e:
                logger.error("decay_error", error=str(e))

            await asyncio.sleep(DECAY_INTERVAL_SECONDS)

    async def consolidation_loop(self):
        """The "sleep" pass (doc 14): synthesize higher-order patches from
        cue-clustered sources, per manifest-declared consolidation_rules.

        Inert unless (a) CQ_CONSOLIDATION_ENABLED and (b) at least one
        registered manifest declares consolidation_rules. Derived patches
        carry origin_mode='derived', source_patch_ids, value.source_cue
        (the idempotency stamp — one consolidation per user/app/type/cue)
        and `informs` connections from every source.
        """
        # Coroutine-local on purpose — see the worker constants gotcha.
        CONSOLIDATION_INTERVAL_SECONDS = 24 * 60 * 60
        await asyncio.sleep(120)

        while self.running:
            try:
                if not get_settings().cq_consolidation_enabled:
                    await asyncio.sleep(CONSOLIDATION_INTERVAL_SECONDS)
                    continue

                app_rows = await self.db.fetch(
                    """
                    SELECT DISTINCT ON (app_id) app_id, manifest
                    FROM app_schemas
                    WHERE manifest ? 'consolidation_rules'
                    ORDER BY app_id, version DESC
                    """
                )
                total_created = 0
                for app_row in app_rows:
                    manifest = app_row["manifest"]
                    if isinstance(manifest, str):
                        manifest = json.loads(manifest)
                    rules = parse_consolidation_rules(manifest)
                    if not rules:
                        continue
                    app_id = str(app_row["app_id"])

                    user_rows = await self.db.fetch(
                        """
                        SELECT DISTINCT ps.subject_key
                        FROM context_patch_acl acl
                        JOIN patch_subjects ps ON ps.patch_id = acl.patch_id
                        WHERE acl.app_id = $1::uuid
                        ORDER BY ps.subject_key
                        LIMIT $2
                        """,
                        app_id, MAX_USERS_PER_APP_PER_CYCLE,
                    )
                    for user_row in user_rows:
                        subject_key = user_row["subject_key"]
                        created = await self._consolidate_user(
                            subject_key, app_id, rules
                        )
                        total_created += created
                if total_created:
                    logger.info("consolidation_cycle_complete", created=total_created)
                else:
                    logger.debug("consolidation_cycle_complete", created=0)
            except Exception as e:
                logger.error("consolidation_error", error=str(e))

            await asyncio.sleep(CONSOLIDATION_INTERVAL_SECONDS)

    async def _consolidate_user(
        self, subject_key: str, app_id: str, rules: list
    ) -> int:
        """Run every rule's cluster detection for one user; synthesize and
        store up to MAX_CLUSTERS_PER_USER_PER_CYCLE new derived patches."""
        created = 0
        for rule in rules:
            if created >= MAX_CLUSTERS_PER_USER_PER_CYCLE:
                break
            clusters = await self.db.fetch(
                f"""
                SELECT pc.cue,
                       array_agg(DISTINCT cp.patch_id) AS patch_ids
                FROM patch_cues pc
                JOIN context_patches cp ON cp.patch_id = pc.patch_id
                JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                JOIN context_patch_acl acl ON acl.patch_id = cp.patch_id
                WHERE ps.subject_key = $1
                  AND acl.app_id = $2::uuid
                  AND cp.patch_type = ANY($3::text[])
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND cp.created_at > NOW() - INTERVAL '{CLUSTER_WINDOW_DAYS} days'
                  -- idempotency: skip cues already consolidated for this user+type
                  AND NOT EXISTS (
                      SELECT 1 FROM context_patches d
                      JOIN patch_subjects dps ON dps.patch_id = d.patch_id
                      WHERE dps.subject_key = $1
                        AND d.patch_type = $4
                        AND d.origin_mode = 'derived'
                        AND d.value->>'source_cue' = pc.cue
                        AND COALESCE(d.status, 'active') = 'active'
                  )
                GROUP BY pc.cue
                HAVING count(DISTINCT cp.patch_id) >= $5
                ORDER BY count(DISTINCT cp.patch_id) DESC, pc.cue ASC
                LIMIT $6
                """,
                subject_key, app_id, rule["from_types"], rule["produce_type"],
                rule["min_patches"], MAX_CLUSTERS_PER_USER_PER_CYCLE,
            )
            for cluster in clusters:
                if created >= MAX_CLUSTERS_PER_USER_PER_CYCLE:
                    break
                made = await self._synthesize_cluster(
                    subject_key, app_id, rule, cluster["cue"],
                    [str(p) for p in cluster["patch_ids"]],
                )
                if made:
                    created += 1
        return created

    async def _synthesize_cluster(
        self, subject_key: str, app_id: str, rule: dict,
        cue: str, source_patch_ids: list,
    ) -> bool:
        """One synthesis call + provenance-carrying write for one cluster.
        Returns True when a derived patch was created. Any failure skips
        the cluster — consolidation must never lose or corrupt sources."""
        rows = await self.db.fetch(
            """
            SELECT value->>'text' AS text FROM context_patches
            WHERE patch_id = ANY($1::uuid[])
            ORDER BY created_at ASC
            """,
            source_patch_ids,
        )
        texts = [r["text"] for r in rows if r["text"]][:MAX_SOURCE_TEXTS]
        if len(texts) < rule["min_patches"]:
            return False
        try:
            response = await self.llm.extract(
                system_prompt=CONSOLIDATION_SYSTEM,
                user_content=build_synthesis_content(
                    cue, rule["produce_type"], texts, rule.get("guidance")
                ),
            )
            statement = parse_synthesis_response(response.content)
        except Exception as exc:
            logger.warning("consolidation_synthesis_failed",
                           subject=subject_key, cue=cue, reason=str(exc)[:200])
            return False
        if not statement:
            logger.debug("consolidation_declined", subject=subject_key, cue=cue)
            return False

        patch_id = str(uuid.uuid4())
        now = datetime.utcnow()
        value_json = json.dumps({"text": statement, "source_cue": cue})
        async with self.db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO context_patches (
                        patch_id, patch_name, patch_type, value,
                        origin_mode, source_prompt, confidence, persistence,
                        status, created_at, updated_at, last_observed_at,
                        source_patch_ids
                    ) VALUES ($1, $2, $3, $4, 'derived', 'consolidation', 0.7,
                              $5, 'active', $6, $6, $6, $7)
                    """,
                    patch_id, f"consolidation_{patch_id[:8]}",
                    rule["produce_type"], value_json,
                    DEFAULT_PERSISTENCE.get(rule["produce_type"], "sticky"),
                    now, source_patch_ids,
                )
                await conn.execute(
                    "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
                    patch_id, subject_key,
                )
                await conn.execute(
                    """
                    INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
                    VALUES ($1, 1, $2, 1.0)
                    """,
                    patch_id, now,
                )
                await conn.execute(
                    "INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write, can_delete) VALUES ($1, $2::uuid, TRUE, TRUE, TRUE)",
                    patch_id, app_id,
                )
                for src in source_patch_ids:
                    await conn.execute(
                        """
                        INSERT INTO patch_connections
                            (from_patch_id, to_patch_id, connection_role, connection_label, context)
                        VALUES ($1::uuid, $2::uuid, 'informs', 'consolidated_into', 'consolidation source')
                        ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO NOTHING
                        """,
                        src, patch_id,
                    )
        logger.info("consolidation_created", subject=subject_key, app_id=app_id,
                    cue=cue, produce_type=rule["produce_type"],
                    sources=len(source_patch_ids), patch_id=patch_id)
        return True

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

    async def _buffer_event(self, payload: dict[str, Any], origin_id: str, origin_type: str):
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

    async def process_task(self, payload: dict[str, Any]):
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
        # Ingest-mode gate (transformer contract): when the app's manifest
        # explicitly declares ingest_mode, its payloads may only reach the
        # matching adapter. Without this, a structured-mode app sending a
        # transcript-shaped payload silently flows through LLM extraction
        # with a generic prompt — plausible garbage in the quilt, no error
        # anywhere. Undeclared mode (SS) → legacy routing, unchanged.
        declared_mode = await self._resolve_ingest_mode(payload.get("app_id"))
        if not is_interaction_allowed(declared_mode, task_type):
            logger.warning(
                "ingest_mode_rejected",
                app_id=payload.get("app_id"), user_id=user_id,
                declared_mode=declared_mode, interaction_type=task_type,
                origin_id=origin_id,
            )
            return

        # Structured ingest (doc §12): pre-typed patches from apps that emit
        # their own signals (e.g. Tech Rehearsal). Already final — never
        # buffered for LLM consolidation like meeting_summary events are.
        if task_type == "structured_patches":
            await self.handle_structured_ingest(payload)
            return

        # User correction from chat (contract item 9): supersede the
        # contradicted patch. Immediate, never buffered — the user is
        # watching for the record to change.
        if task_type == "correction":
            await self.handle_correction(payload)
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

    async def _resolve_ingest_mode(self, app_id: str | None) -> str | None:
        """The app's manifest-declared ingest_mode, or None when the app has
        no manifest / the manifest predates the key (legacy routing).

        Cached ~5 min per app — this runs on every queued event, and a
        manifest re-registration taking a few minutes to affect routing is
        acceptable (same order as the entity-index TTL). Any lookup failure
        degrades to None: the gate must never take down ingestion.
        """
        if not app_id:
            return None
        cached = self._ingest_mode_cache.get(app_id)
        now = time.monotonic()
        if cached and now - cached[1] < 300:
            return cached[0]
        mode: str | None = None
        try:
            raw = await self.db.fetchval(
                """
                SELECT manifest->>'ingest_mode' FROM app_schemas
                WHERE app_id = $1::uuid
                ORDER BY version DESC
                LIMIT 1
                """,
                app_id,
            )
            mode = raw or None
        except Exception:
            mode = None
        self._ingest_mode_cache[app_id] = (mode, now)
        return mode

    async def _resolve_extraction_prompt(
        self, app_id: str | None
    ) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        """Pick the system prompt + structured-output schema for this app.

        If the app has a registered manifest in `app_schemas`, generate
        the prompt from it (or use `extraction_prompt_override` verbatim
        if present). Otherwise fall back to the universal hardcoded
        MEETING_SUMMARY_SYSTEM + EXTRACTION_SCHEMA.

        Also returns the resolved manifest (None on the universal
        fallback) so downstream sanitizers can enforce manifest-declared
        invariants — currently the connection vocabulary.
        """
        if not app_id:
            return MEETING_SUMMARY_SYSTEM, EXTRACTION_SCHEMA, None

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
                return MEETING_SUMMARY_SYSTEM, EXTRACTION_SCHEMA, None
            manifest = row["manifest"]
            if isinstance(manifest, str):
                manifest = json.loads(manifest)
            prompt = build_schema_prompt(manifest)
            schema = build_schema_output_schema(manifest)
            return prompt, schema, manifest
        except Exception as e:
            logger.warning(
                "schema_prompt_resolution_failed",
                app_id=app_id,
                error=str(e),
            )
            return MEETING_SUMMARY_SYSTEM, EXTRACTION_SCHEMA, None

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

    async def handle_active_learning(self, payload: dict[str, Any]):
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
                    origin_mode, source_prompt, confidence, persistence,
                    created_at, updated_at, last_observed_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                patch_id, patch_name, category, value_json,
                origin_mode, "manual", 1.0, persistence,
                created_at, created_at, created_at
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

    async def handle_correction(self, payload: dict[str, Any]):
        """User correction from chat (contract item 9).

        Candidate set = patches whose text appears in the passed
        context_block first (what the user was looking at), then scoped
        recent patches. One LLM call picks the contradicted patch by id
        (resolved_commitments pattern) and writes the corrected fact.
        Supersede uses only existing vocabulary: new patch is
        origin_mode='declared', stale patch is archived (delta sync
        converges devices), connected with role 'replaces'. Unmatched
        corrections still land — never lose a user-stated fact.
        """
        user_id = payload.get("user_id")
        correction_text = (payload.get("content") or "").strip()[:MAX_CORRECTION_CHARS]
        if not user_id or not correction_text:
            logger.warning("correction_missing_fields", user_id=user_id)
            return
        metadata = payload.get("metadata") or {}
        app_id = payload.get("app_id")
        context_block = payload.get("context_block") or ""
        project_id = metadata.get("project_id")
        project = metadata.get("project")
        subject_key = f"user:{user_id}"

        # Candidate set: scoped active patches, newest first. In-block
        # candidates rank first — those lines were on the user's screen.
        scope_sql = ""
        params: list = [subject_key]
        if project_id:
            scope_sql = "AND (cp.project_id = $2 OR cp.project_id IS NULL)"
            params.append(project_id)
        elif project:
            scope_sql = "AND (cp.project = $2 OR cp.project IS NULL)"
            params.append(project)
        rows = await self.db.fetch(
            f"""
            SELECT cp.patch_id, cp.patch_type, cp.value->>'text' AS text,
                   cp.project, cp.project_id, cp.origin_id, cp.origin_type
            FROM context_patches cp
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            WHERE ps.subject_key = $1
              AND COALESCE(cp.status, 'active') = 'active'
              {scope_sql}
            ORDER BY cp.created_at DESC, cp.patch_id ASC
            LIMIT 60
            """,
            *params,
        )
        in_block = [r for r in rows if r["text"] and r["text"] in context_block]
        others = [r for r in rows if r not in in_block]
        candidates = (in_block + others)[:MAX_CANDIDATES]
        by_id = {str(r["patch_id"]): r for r in candidates}

        today = datetime.utcnow().date()
        try:
            response = await self.llm.extract(
                system_prompt=CORRECTION_SYSTEM,
                user_content=build_correction_content(
                    correction_text,
                    [{"patch_id": str(r["patch_id"]), "patch_type": r["patch_type"], "text": r["text"]}
                     for r in candidates],
                    today.isoformat(),
                    scope_label=project or project_id,
                ),
            )
            parsed = parse_correction_response(
                response.content, set(by_id.keys()), meeting_date=today
            )
        except Exception as exc:
            logger.error("correction_failed", user_id=user_id, reason=str(exc)[:200])
            return
        if not parsed:
            logger.warning("correction_unparseable", user_id=user_id,
                           correction=correction_text[:100])
            return
        matched_id, value = parsed
        new_type = value.pop("_new_type", FALLBACK_PATCH_TYPE)

        old = by_id.get(matched_id) if matched_id else None
        if old is not None:
            new_type = old["patch_type"]

        now = datetime.utcnow()
        new_patch_id = str(uuid.uuid4())
        # Scope: inherit from the corrected patch; unmatched falls back to
        # the request scope for project-shaped types. Origin stays NULL —
        # the correction came from chat, not a meeting.
        new_project = old["project"] if old else (project if new_type != "trait" else None)
        new_project_id = old["project_id"] if old else (project_id if new_type != "trait" else None)
        async with self.db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO context_patches (
                        patch_id, patch_name, patch_type, value,
                        origin_mode, source_prompt, confidence, persistence,
                        project, project_id,
                        status, created_at, updated_at, last_observed_at
                    ) VALUES ($1, $2, $3, $4, 'declared', 'correction', 0.95,
                              $5, $6, $7, 'active', $8, $8, $8)
                    """,
                    new_patch_id, f"correction_{new_patch_id[:8]}", new_type,
                    json.dumps(value),
                    DEFAULT_PERSISTENCE.get(new_type, "sticky"),
                    new_project, new_project_id, now,
                )
                await conn.execute(
                    "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
                    new_patch_id, subject_key,
                )
                await conn.execute(
                    """
                    INSERT INTO patch_usage_metrics (patch_id, access_count, last_accessed_at, current_decay_score)
                    VALUES ($1, 1, $2, 1.0)
                    """,
                    new_patch_id, now,
                )
                if app_id:
                    try:
                        await conn.execute(
                            "INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write, can_delete) VALUES ($1, $2::uuid, TRUE, TRUE, TRUE)",
                            new_patch_id, app_id,
                        )
                    except Exception:
                        pass
                if old is not None:
                    await conn.execute(
                        """
                        UPDATE context_patches SET
                            status = 'archived',
                            updated_at = $1,
                            value = jsonb_set(
                                jsonb_set(value, '{corrected_by}', to_jsonb($2::text)),
                                '{correction_source}', '"user_chat"'
                            )
                        WHERE patch_id = $3::uuid
                        """,
                        now, new_patch_id, old["patch_id"],
                    )
                    await conn.execute(
                        """
                        INSERT INTO patch_connections
                            (from_patch_id, to_patch_id, connection_role, connection_label, context)
                        VALUES ($1::uuid, $2::uuid, 'replaces', 'corrects', 'user correction from chat')
                        ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO NOTHING
                        """,
                        new_patch_id, old["patch_id"],
                    )
                    # The corrected fact inherits the superseded patch's
                    # cues — the topics a fact is ABOUT survive its
                    # correction. Without this, the archived patch drops
                    # out of the cue-matched fetch leg and the corrected
                    # version never enters it: the correction would make
                    # the fact invisible to the very topic query that
                    # surfaced it (caught live by the item-9 prod smoke).
                    await conn.execute(
                        """
                        INSERT INTO patch_cues (patch_id, cue)
                        SELECT $1::uuid, cue FROM patch_cues WHERE patch_id = $2::uuid
                        ON CONFLICT (patch_id, cue) DO NOTHING
                        """,
                        new_patch_id, old["patch_id"],
                    )
        if old is not None:
            logger.info("correction_applied", user_id=user_id,
                        superseded=str(old["patch_id"]), new_patch=new_patch_id,
                        patch_type=new_type, in_block=old in in_block)
        else:
            logger.info("correction_unmatched_stored", user_id=user_id,
                        new_patch=new_patch_id, patch_type=new_type,
                        correction=correction_text[:100])

    async def handle_structured_ingest(self, payload: dict[str, Any]):
        """Structured-ingest adapter (doc §12): store pre-typed patches from
        apps that already emit typed signals (e.g. Tech Rehearsal), skipping
        the LLM extraction path entirely. This is the sibling of the
        extraction adapter (handle_meeting_summary) over the same sink.

        Locked contract (doc §6):
          - Privacy (D4): reject any transcript-shaped field; only
            patches / entities / relationships are accepted.
          - Validation (D3): validate the WHOLE batch against the app's
            registered manifest pre-write; any violation rejects the batch
            and writes nothing (atomic transaction).
          - Longitudinal (D1): patch types flagged `longitudinal` in the
            manifest append observations to a CQ-derived series instead of
            dedup-collapsing.
        """
        user_id = payload.get("user_id")
        app_id = payload.get("app_id")
        if not user_id:
            logger.warning("structured_ingest_no_user", app_id=app_id)
            return

        # D4 — privacy gate: structured mode never carries free-form capture.
        forbidden = [k for k in ("summary", "content", "messages", "transcript")
                     if payload.get(k)]
        if forbidden:
            logger.warning("structured_ingest_rejected_transcript_field",
                           user_id=user_id, app_id=app_id, fields=forbidden)
            return

        patches = payload.get("patches") or []
        if not patches:
            logger.info("structured_ingest_empty", user_id=user_id, app_id=app_id)
            return

        # Manifest is mandatory — validation against it is the whole point of
        # this path, so no manifest means we cannot accept the batch.
        manifest = None
        if app_id:
            try:
                row = await self.db.fetchrow(
                    "SELECT manifest FROM app_schemas WHERE app_id = $1::uuid ORDER BY version DESC LIMIT 1",
                    app_id,
                )
                if row:
                    manifest = row["manifest"]
                    if isinstance(manifest, str):
                        manifest = json.loads(manifest)
            except Exception as e:
                logger.warning("structured_ingest_manifest_failed",
                               user_id=user_id, app_id=app_id, error=str(e))
        if not manifest:
            logger.warning("structured_ingest_no_manifest", user_id=user_id, app_id=app_id)
            return

        patch_type_specs = {
            pt["domain_type"]: pt
            for pt in (manifest.get("patch_types") or [])
            if isinstance(pt, dict) and pt.get("domain_type")
        }
        longitudinal_types = {
            pt["domain_type"]: (pt.get("series_descriptor_field") or "text")
            for pt in patch_type_specs.values()
            if pt.get("longitudinal")
        }
        connection_labels = manifest.get("connection_labels")

        # D3 — validate the whole batch pre-write; collect every problem so
        # the client sees all of them at once, then reject if any exist.
        errors: list[str] = []
        for i, patch in enumerate(patches):
            if not isinstance(patch, dict):
                errors.append(f"patch[{i}]: not an object")
                continue
            ptype = patch.get("type")
            spec = patch_type_specs.get(ptype)
            if spec is None:
                errors.append(f"patch[{i}]: unknown type {ptype!r}")
                continue
            value = patch.get("value")
            if isinstance(value, str):
                value = {"text": value}
            if not isinstance(value, dict):
                errors.append(f"patch[{i}] ({ptype}): value must be an object or string")
                continue
            required = (
                spec.get("required_fields")
                or (spec.get("extraction_rules") or {}).get("required_fields")
                or []
            )
            for field in required:
                if not value.get(field):
                    errors.append(f"patch[{i}] ({ptype}): missing required field {field!r}")
            # A longitudinal patch must carry its series descriptor — without
            # it there is no trajectory to join.
            if ptype in longitudinal_types and not value.get(longitudinal_types[ptype]):
                errors.append(
                    f"patch[{i}] ({ptype}): missing series descriptor field "
                    f"{longitudinal_types[ptype]!r}"
                )

        # Connection vocabulary: reuse the extraction enforcer. It flips
        # reversed edges in place (a convenience we keep), but any DROP is an
        # invalid edge — and for an authoritative client that is a batch
        # error, not a silent loss.
        content = {"patches": patches}
        enforce_connection_vocabulary(content, connection_labels)
        cv = content.get("_connection_vocabulary_enforced") or {}
        if cv.get("dropped"):
            errors.append(
                f"connections: {cv['dropped']} invalid edge(s) {cv.get('dropped_detail', [])}"
            )

        if errors:
            logger.warning("structured_ingest_rejected",
                           user_id=user_id, app_id=app_id,
                           patch_count=len(patches), errors=errors[:20])
            return

        # Passed validation — assemble write context and commit atomically.
        metadata = payload.get("metadata") or {}
        timestamp = payload.get("timestamp")
        project = metadata.get("project")
        project_id = metadata.get("project_id")
        origin_id = metadata.get("origin_id")
        origin_type = metadata.get("origin_type")
        entities = payload.get("entities") or []
        relationships = payload.get("relationships") or []

        # D3 — atomic: one connection + transaction so a failure mid-write
        # leaves no partial graph or half-written trajectory. self.db is a
        # pool, so acquire a single connection and pass IT to every store
        # call (passing the pool would spread writes across connections,
        # defeating the transaction).
        async with self.db.acquire() as conn:
            async with conn.transaction():
                if project_id and project:
                    try:
                        await conn.execute(
                            """INSERT INTO projects (project_id, user_id, name)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (project_id) DO UPDATE SET updated_at = NOW()""",
                            project_id, user_id, project,
                        )
                    except Exception:
                        pass  # projects table may not exist yet
                patches_stored = await store_connected_patches(
                    conn, user_id, patches, "structured_ingest", app_id, timestamp,
                    project, project_id, origin_id, origin_type,
                    longitudinal_types=longitudinal_types,
                )
                entities_stored = await store_entities(
                    conn, self.redis, user_id, entities, metadata
                )
                relationships_stored = await store_relationships(
                    conn, user_id, relationships, metadata
                )

        logger.info("structured_ingest_complete",
                    user_id=user_id, app_id=app_id,
                    patches_stored=patches_stored,
                    entities_stored=entities_stored,
                    relationships_stored=relationships_stored,
                    longitudinal_types=list(longitudinal_types.keys()))

    async def handle_meeting_summary(self, payload: dict[str, Any]):
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

        # Subscription tier — forwarded by GhostPour on every /v1/memory
        # write. previous_tier is only set when the user crossed a tier
        # boundary inside the last 24h, letting us detect transitions
        # from the audit stream alone (without a separate webhook).
        # Both fields stay None when the upstream app doesn't populate
        # them — non-breaking.
        subscription_tier = metadata.get("subscription_tier")
        previous_tier = metadata.get("previous_tier")

        # Soft-signal user attribution hint — forwarded by the calling
        # app's identity layer when no hard user_identified=True claim
        # is available (in-chat-nudge path, replacing the post-meeting
        # attribution sheet). v1 is log-only — captured to
        # extraction_metrics for calibration; gating remains on the
        # hard user_identified path. Malformed hints are logged and
        # dropped without breaking the ingestion.
        attribution_hint = validate_user_attribution_hint(
            metadata.get("user_attribution_hint")
        )
        attribution_hint_label = (
            attribution_hint.get("speaker_label") if attribution_hint else None
        )
        attribution_hint_conf = (
            attribution_hint.get("confidence") if attribution_hint else None
        )
        attribution_hint_basis = (
            attribution_hint.get("confidence_basis") if attribution_hint else None
        )
        attribution_hint_secondary = (
            attribution_hint.get("secondary_candidate") if attribution_hint else None
        )
        attribution_hint_secondary_label = (
            attribution_hint_secondary.get("speaker_label")
            if attribution_hint_secondary
            else None
        )
        attribution_hint_secondary_conf = (
            attribution_hint_secondary.get("confidence")
            if attribution_hint_secondary
            else None
        )
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

        # Inject the user's open commitments so the LLM can detect
        # completion mentions in this transcript and report them back
        # in the resolved_commitments output. See _fetch_open_commitments
        # for the lookback window + cap.
        open_commits_block = await self._build_open_commitments_block(user_id)

        # Meeting date anchor for deadline resolution. The extraction
        # prompt asks the model to resolve relative deadlines ("tomorrow",
        # "end of week") into value.deadline_date, which is only possible
        # if the model knows when the meeting happened. payload.timestamp
        # is set at enqueue time, or by the app for backdated imports.
        meeting_date = None
        raw_ts = payload.get("timestamp")
        if raw_ts:
            try:
                meeting_date = datetime.fromisoformat(
                    str(raw_ts).replace("Z", "+00:00")
                ).date()
            except ValueError:
                meeting_date = None
        meeting_date_line = (
            f"Meeting date: {meeting_date.isoformat()}\n\n" if meeting_date else ""
        )

        # Memory language. Apps pass metadata.language (BCP-47, e.g. "es")
        # so extraction writes patch text in the user's language. Without
        # it, the prompt's LANGUAGE section falls back to the dominant
        # language of the (you) speaker — see extraction_prompts.py.
        memory_language = ""
        if metadata:
            memory_language = str(metadata.get("language") or "").strip()
        language_line = (
            f"User language: {memory_language}\n\n" if memory_language else ""
        )

        try:
            app_id = payload.get("app_id")
            llm = await self._get_llm_for_app(app_id)

            # Prefer the app's registered schema-driven prompt. Falls back
            # to the universal hardcoded MEETING_SUMMARY_SYSTEM only when
            # the app has no registered manifest.
            resolved_prompt, resolved_schema, resolved_manifest = await self._resolve_extraction_prompt(app_id)

            response = await llm.extract(
                system_prompt=resolved_prompt,
                user_content=meeting_date_line + language_line + user_context + open_commits_block + effective_summary,
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

            # Apply MAX_PATCHES_PER_MEETING to LLM output BEFORE the
            # enforcer runs. The cap exists to bound LLM-output noise; the
            # enforcer's job is structural completeness (every named owner
            # must have a person patch + owns edge). Capping the
            # post-enforcer list silently drops the synthetic person
            # patches it appends, which silently breaks PR #84 for any
            # meeting where the LLM emitted ≥ MAX_PATCHES_PER_MEETING
            # patches on its own. Cap first; then enforce.
            raw_patches = response.content.get("patches") or []
            if len(raw_patches) > MAX_PATCHES_PER_MEETING:
                logger.warning(
                    "extraction_capped",
                    type="patches",
                    original=len(raw_patches),
                    capped=MAX_PATCHES_PER_MEETING,
                )
                response.content["patches"] = raw_patches[:MAX_PATCHES_PER_MEETING]

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

            # Connection vocabulary enforcement — the LLM regularly emits
            # reversed edges (blocker blocked_by commitment) and off-spec
            # combos (works_with) that client-side validators then drop
            # silently. Flip reversed edges, drop invalid ones, against
            # the app's registered manifest. No-op for manifest-less apps.
            enforce_connection_vocabulary(
                response.content,
                (resolved_manifest or {}).get("connection_labels"),
            )
            if (cv := response.content.get("_connection_vocabulary_enforced")):
                logger.info(
                    "connection_vocabulary_enforced",
                    user_id=user_id,
                    kept=cv.get("kept", 0),
                    flipped=cv.get("flipped", 0),
                    dropped=cv.get("dropped", 0),
                    dropped_detail=cv.get("dropped_detail", []),
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
            strip_owner_on_self_typed_patches(response.content)
            strip_prose_from_person_names(response.content)
            drop_placeholder_and_self_person_patches(
                response.content, user_label=user_label
            )
            drop_placeholder_entities(response.content)
            if (pe := response.content.get("_placeholder_entities_enforced")):
                logger.info(
                    "placeholder_entities_dropped",
                    user_id=user_id,
                    entities=pe["entities_dropped"],
                    relationships_dropped=pe["relationships_dropped"],
                    model=response.model,
                )
            sanitize_cues(response.content)
            sanitize_salience(response.content)
            sanitize_deadline_dates(response.content, meeting_date=meeting_date)
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
                # V2 model — typed, connected patches.
                # Note: the cap was already applied above, BEFORE the
                # enforcer ran, so the enforcer's synthetic person patches
                # are exempt from the count.
                patches_stored = await store_connected_patches(
                    self.db, user_id, patches, "meeting_summary", app_id, timestamp,
                    project, project_id, origin_id, origin_type,
                    user_label=user_label,
                    llm=llm,
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

            # Apply commitment resolutions reported by the LLM. Validates
            # patch ownership and that the patch is actually an open
            # commitment before marking completed. Unknown or cross-user
            # patch_ids are dropped with a warning.
            commitments_resolved = await self._apply_resolved_commitments(
                user_id, response.content.get("resolved_commitments") or [],
            )

            logger.info(
                "meeting_summary_complete",
                user_id=user_id,
                facts_stored=facts_stored,
                actions_stored=actions_stored,
                entities_stored=entities_stored,
                relationships_stored=relationships_stored,
                commitments_resolved=commitments_resolved,
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
                        user_identified, identification_source,
                        subscription_tier, previous_tier,
                        attribution_hint_speaker_label, attribution_hint_confidence,
                        attribution_hint_basis,
                        attribution_hint_secondary_label, attribution_hint_secondary_confidence
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,$26,$27,$28,$29,$30)
                    """,
                    user_id, response.model, response.input_tokens, response.output_tokens,
                    response.cost_usd, response.latency_ms, facts_stored, entities_stored,
                    "meeting_summary", app_id, origin_id, origin_type, "meeting_summary",
                    owner_speaker_label, has_you_marker,
                    owner_gate_filtered, connection_dropped,
                    patches_before_filters, patches_after_filters,
                    reasoning_chars, len(summary),
                    user_identified, identification_source,
                    subscription_tier, previous_tier,
                    attribution_hint_label, attribution_hint_conf,
                    attribution_hint_basis,
                    attribution_hint_secondary_label, attribution_hint_secondary_conf,
                )
            except Exception as e:
                logger.warning("metrics_insert_failed", error=str(e))

            # Communication profile: separate lightweight call, only when (you) marker present
            if has_you_marker:
                await self._extract_communication_profile(user_id, summary, app_id)

            await self.hydrate_cache(user_id)

        except Exception as e:
            logger.error("meeting_summary_failed", error=str(e), user_id=user_id)
            await self._maybe_alert_llm_failure(e)

    async def backup_failure_watch_loop(self):
        """Poll backup_runs for status='failed' rows and report each
        as a critical-failure incident. Fingerprint includes the
        backup_run id so a single failure produces a single incident
        even though this loop fires every 5 minutes. After 30 min of
        no re-fire (per alerting auto-resolve), the row resolves; if
        the same backup_run is somehow re-attempted and fails again
        with the same id, a fresh incident opens.

        Backups happen daily, so this loop is quiet ~99% of the time.
        Reads from backup_runs are bounded by the 1h lookback."""
        check_interval_s = 300  # 5 minutes
        while self.running:
            # Resolve incidents that have gone quiet past the auto-resolve
            # window. report_incident only sweeps when a NEW failure fires,
            # so a healthy system would never close its last open incident
            # without this periodic call (it would sit open indefinitely and
            # misrepresent system health). Co-located here because this loop
            # already runs unconditionally on a tight cadence. Wrapped so a
            # sweep error can never crash-loop the worker.
            try:
                resolved = await sweep_stale_incidents(self.db)
                if resolved:
                    logger.info("incident_auto_resolved", count=resolved)
            except Exception as e:
                logger.warning("incident_sweep_error", error=str(e))
            try:
                rows = await self.db.fetch(
                    """
                    SELECT id::text AS id, started_at, error_message
                      FROM backup_runs
                     WHERE status = 'failed'
                       AND started_at >= NOW() - INTERVAL '1 hour'
                     ORDER BY started_at DESC
                     LIMIT 50
                    """
                )
                for row in rows:
                    try:
                        await report_incident(
                            self.db,
                            category="backup_failed",
                            subject=row["id"],
                            details={
                                "backup_run_id": row["id"],
                                "started_at": row["started_at"].isoformat() if row["started_at"] else None,
                                "error_message": (row["error_message"] or "")[:300],
                            },
                        )
                    except Exception as exc:
                        logger.warning("backup_alert_failed", reason=str(exc)[:200])
            except Exception as e:
                logger.error("backup_failure_watch_error", error=str(e))
            await asyncio.sleep(check_interval_s)

    async def provider_health_loop(self):
        """Probe Anthropic + OpenRouter every 15 minutes, persist each
        outcome to provider_health_probes, and alert once a provider
        racks up CONSECUTIVE_FAILURES_TO_ALERT consecutive failures.

        Why 15 minutes:
            Probes are cheap (count_tokens is $0; OR's /auth/key is
            metadata only). 15 minutes is fast enough that a dead key
            gets flagged before the next extraction is likely to hit
            it on most workloads, and slow enough that we don't grow
            the probe table at any meaningful rate.

        Why consecutive failures, not "any failure":
            Either provider can blip on a single probe (network blip,
            429 rate limit during a usage spike, brief 5xx on the
            provider side). One failed probe is noise. Three in a row
            on the 15-minute cadence (45 minutes of continuous failure)
            is signal.

        Why dedup at the alerting layer, not here:
            report_incident already dedupes by (category, subject)
            within a 30 min window. A sustained outage emails once,
            not once per probe cycle. We pass the provider name as
            subject so anthropic + openrouter failures alert
            independently.
        """
        check_interval_s = 15 * 60  # 15 minutes
        consecutive_failures_to_alert = 3
        # Wait a bit at startup so the worker is fully up before the
        # first probe — avoids a misleading first-probe failure during
        # warm-up if Anthropic auth lags behind the LLM client init.
        await asyncio.sleep(30)
        while self.running:
            try:
                from contextquilt.services.provider_health import (
                    probe_anthropic,
                    probe_openrouter,
                )
                for probe_fn in (probe_anthropic, probe_openrouter):
                    try:
                        result = await probe_fn()
                    except Exception as exc:
                        logger.error(
                            "provider_health_probe_crashed",
                            probe=probe_fn.__name__,
                            error_type=type(exc).__name__,
                            error_message=str(exc)[:200],
                        )
                        continue
                    try:
                        await self.db.execute(
                            """
                            INSERT INTO provider_health_probes (
                                provider, status, latency_ms,
                                balance_usd, limit_usd, is_free_tier,
                                error_message
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                            """,
                            result.provider,
                            result.status,
                            int(result.latency_ms) if result.latency_ms is not None else None,
                            result.balance_usd,
                            result.limit_usd,
                            result.is_free_tier,
                            result.error,
                        )
                    except Exception as exc:
                        logger.error(
                            "provider_health_probe_persist_failed",
                            provider=result.provider,
                            error_type=type(exc).__name__,
                            error_message=str(exc)[:200],
                        )
                        # Persisting failed; skip the alert pass for this
                        # provider since we can't trust the row count.
                        continue
                    logger.info(
                        "provider_health_probe",
                        provider=result.provider,
                        status=result.status,
                        latency_ms=result.latency_ms,
                        balance_usd=result.balance_usd,
                        error=result.error,
                    )

                    # Consecutive-failure alert pass. Read the last N
                    # rows for this provider; if all are 'failed', fire.
                    if result.status == "failed":
                        try:
                            recent = await self.db.fetch(
                                """
                                SELECT status FROM provider_health_probes
                                 WHERE provider = $1
                                 ORDER BY probed_at DESC
                                 LIMIT $2
                                """,
                                result.provider,
                                consecutive_failures_to_alert,
                            )
                            if (
                                len(recent) >= consecutive_failures_to_alert
                                and all(r["status"] == "failed" for r in recent)
                            ):
                                await report_incident(
                                    self.db,
                                    category="provider_health_failed",
                                    subject=result.provider,
                                    details={
                                        "provider": result.provider,
                                        "consecutive_failed_probes": consecutive_failures_to_alert,
                                        "latest_error": result.error,
                                    },
                                )
                        except Exception as exc:
                            logger.warning(
                                "provider_health_alert_failed",
                                provider=result.provider,
                                error_type=type(exc).__name__,
                                error_message=str(exc)[:200],
                            )
            except Exception as e:
                logger.error("provider_health_loop_error", error=str(e))
            await asyncio.sleep(check_interval_s)

    # ============================================================
    # Commitment resolution (pipeline closes commitments by detecting
    # completion mentions in later transcripts).
    # ============================================================

    # Lookback window for fetching user's open commitments to inject
    # into the extraction prompt. Past the 30-day decay TTL anyway,
    # so older opens have already been archived by the decay worker.
    OPEN_COMMITS_LOOKBACK_DAYS = 30
    # Cap on how many opens we shove into the prompt. The model can
    # only resolve what it sees, but unbounded injection bloats every
    # extraction call. Heaviest-user prior commits over the 30-day
    # window will stay well under this on real traffic.
    OPEN_COMMITS_MAX_INJECTED = 20

    async def _fetch_open_commitments(self, user_id: str) -> list[dict[str, Any]]:
        """Return list of {patch_id, text, created_at, deadline_date} for
        this user's open commitment patches. Open = status='active' AND
        completed_at IS NULL. Window: within the lookback OR overdue —
        an overdue commitment outlives the lookback (the deadline-aware
        decay anchor keeps it alive past 30 days), and it's exactly the
        item most worth asking the model about. Overdue items sort first
        so the injection cap never crowds them out."""
        if not user_id:
            return []
        try:
            subject_key = f"user:{user_id}"
            iso_date_re = r"'^\d{4}-\d{2}-\d{2}$'"
            overdue_sql = (
                f"(cp.value->>'deadline_date' ~ {iso_date_re} "
                "AND (cp.value->>'deadline_date')::date < (NOW() AT TIME ZONE 'utc')::date)"
            )
            rows = await self.db.fetch(
                f"""
                SELECT cp.patch_id::text AS patch_id,
                       cp.value->>'text' AS text,
                       cp.value->>'deadline_date' AS deadline_date,
                       cp.created_at
                  FROM context_patches cp
                  JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                 WHERE ps.subject_key = $1
                   AND cp.patch_type = 'commitment'
                   AND COALESCE(cp.status, 'active') = 'active'
                   AND cp.completed_at IS NULL
                   AND (cp.created_at >= NOW() - INTERVAL '{int(self.OPEN_COMMITS_LOOKBACK_DAYS)} days'
                        OR {overdue_sql})
                 ORDER BY CASE WHEN {overdue_sql} THEN 0 ELSE 1 END,
                          cp.created_at DESC
                 LIMIT $2
                """,
                subject_key, self.OPEN_COMMITS_MAX_INJECTED,
            )
            return [
                {
                    "patch_id": r["patch_id"], "text": r["text"],
                    "created_at": r["created_at"], "deadline_date": r["deadline_date"],
                }
                for r in rows
            ]
        except Exception as exc:
            logger.warning("open_commitments_fetch_failed", reason=str(exc)[:200], user_id=user_id)
            return []

    async def _build_open_commitments_block(self, user_id: str) -> str:
        """Format the open commitments into the prompt-ready block that
        prefixes user_content. Returns empty string when there are none,
        so callers can prepend unconditionally. Rendering lives in
        extraction_prompts.format_open_commitments_block (pure,
        unit-tested); this method just fetches and delegates."""
        commits = await self._fetch_open_commitments(user_id)
        return format_open_commitments_block(commits, now=datetime.utcnow())

    async def _apply_resolved_commitments(
        self, user_id: str, resolutions: list[dict[str, Any]],
    ) -> int:
        """Mark resolved commitments as completed. Returns count actually
        resolved. Validates ownership before UPDATE so a hallucinated or
        cross-user patch_id can't accidentally close another user's
        commitment."""
        if not resolutions or not user_id:
            return 0
        subject_key = f"user:{user_id}"
        resolved_count = 0
        for item in resolutions:
            patch_id = (item.get("patch_id") or "").strip()
            evidence = (item.get("evidence") or "").strip()[:300]
            if not patch_id:
                continue
            try:
                # Ownership gate + open-commitment gate in one query.
                # Returns the patch_id only if all conditions hold; lets
                # us avoid two round-trips.
                gated = await self.db.fetchval(
                    """
                    SELECT cp.patch_id::text
                      FROM context_patches cp
                      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                     WHERE cp.patch_id = $1::uuid
                       AND ps.subject_key = $2
                       AND cp.patch_type = 'commitment'
                       AND COALESCE(cp.status, 'active') = 'active'
                       AND cp.completed_at IS NULL
                    """,
                    patch_id, subject_key,
                )
            except Exception as exc:
                # Bad UUID format from the LLM lands here. Log and skip,
                # don't propagate, this is a defensive layer below the
                # JSON schema's enforcement of patch_id being a string.
                logger.warning(
                    "resolved_commitment_lookup_failed",
                    patch_id=patch_id, reason=str(exc)[:200], user_id=user_id,
                )
                continue
            if not gated:
                logger.warning(
                    "resolved_commitment_rejected",
                    patch_id=patch_id, user_id=user_id,
                    reason="not_owned_or_not_open_or_unknown",
                )
                continue
            try:
                # completion_source/evidence stamped into value so apps
                # can distinguish LLM auto-close ('extraction') from
                # tap-to-complete ('app', via POST .../complete) from
                # plain TTL decay (no completed_at at all).
                await self.db.execute(
                    """
                    UPDATE context_patches
                       SET completed_at = NOW(),
                           status = 'archived',
                           updated_at = NOW(),
                           value = CASE WHEN $2::text <> ''
                                   THEN jsonb_set(
                                            jsonb_set(value, '{completion_source}', '"extraction"'),
                                            '{completion_evidence}', to_jsonb($2::text)
                                        )
                                   ELSE jsonb_set(value, '{completion_source}', '"extraction"')
                                   END
                     WHERE patch_id = $1::uuid
                    """,
                    patch_id, evidence,
                )
                resolved_count += 1
                logger.info(
                    "commitment_resolved",
                    patch_id=patch_id, user_id=user_id, evidence=evidence[:120],
                )
            except Exception as exc:
                logger.warning(
                    "resolved_commitment_update_failed",
                    patch_id=patch_id, reason=str(exc)[:200], user_id=user_id,
                )
        return resolved_count

    async def _maybe_alert_llm_failure(self, exc: Exception) -> None:
        """Fire a critical-failure alert when an LLM call fails with an
        operator-actionable HTTP status. Specifically 401/403 (key
        rotated/revoked/billing lapsed) and 402 (budget exhausted).
        Other failure classes (transient 5xx, timeouts, JSON parse) are
        not alerted here, they fall under provider_unreachable which
        needs threshold logic (v2).

        Swallows every downstream error including alerting transport
        failures — alerting must not break the request that triggered
        it. This is the call site that would have caught the May 2026
        OpenRouter 16-day silent outage on day one."""
        try:
            if not isinstance(exc, httpx.HTTPStatusError):
                return
            status = exc.response.status_code
            if status in (401, 403):
                category = "provider_auth_failed"
            elif status == 402:
                category = "provider_budget_exhausted"
            else:
                return
            body_preview = ""
            try:
                body_preview = exc.response.text[:300]
            except Exception:
                pass
            await report_incident(
                self.db,
                category=category,
                subject="openrouter",
                details={
                    "status_code": status,
                    "model": getattr(self.llm, "model", "unknown") if self.llm else "unknown",
                    "response_body": body_preview,
                },
            )
        except Exception as alert_exc:
            logger.warning("alert_dispatch_failed", reason=str(alert_exc)[:200])

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

    async def handle_passive_learning(self, payload: dict[str, Any]):
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

    async def handle_chat_log(self, payload: dict[str, Any]):
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
