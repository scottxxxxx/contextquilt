"""
Context Quilt - Cold Path Worker
Handles Async Memory Consolidation via hosted LLM extraction.

Uses the LLMClient for structured extraction via any OpenAI-compatible API.
Default: Mistral Small 3.1 via OpenRouter ($0.03/$0.11 per M tokens).
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
import httpx
import redis.asyncio as redis
import structlog

# Add src to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from contextquilt.gateway.extraction import classify_fact
from contextquilt.services.alerting import report_incident, sweep_stale_incidents
from contextquilt.services import account_purge
from contextquilt.services.attribution import validate_user_attribution_hint
from contextquilt.services.extraction_prompts import (
    COMMUNICATION_PROFILE_SYSTEM,
    CONVERSATION_SYSTEM,
    MEETING_SUMMARY_SYSTEM,
    TRACE_SYSTEM,
    format_open_commitments_block,
    select_open_commitments,
)
from contextquilt.services.closure_evidence import BELIEVED, classify_closure
from contextquilt.services.deadline_resolver import run_deadline_micropass
from contextquilt.services.extraction_schema import (
    PATCH_TYPES,
    speaker_turn_counts,
    question_attribution,
    meeting_role_signals,
    extraction_patch_backstop,
    cap_entities,
    inject_ownership_entities,
    ENTITY_CAPACITY_KEY,
    EXTRACTION_SCHEMA,
    drop_placeholder_and_self_person_patches,
    drop_placeholder_entities,
    enforce_connection_requirements,
    enforce_connection_vocabulary,
    enforce_owner_gate,
    enforce_owed_to_counterparty,
    enforce_owner_edge_agreement,
    enforce_person_ownership,
    speaker_labels_in,
    self_speaker_label,
    is_placeholder_or_self_person,
    no_collapse_patch_types,
    normalize_cue_list,
    normalize_owner_in_transcript,
    origin_scoped_patch_types,
    sanitize_behavior_observations,
    sanitize_cues,
    sanitize_deadline_dates,
    sanitize_salience,
    sanitize_you_marker_from_patches,
    strip_ephemeral_fields,
    strip_owner_on_self_typed_patches,
    strip_prose_from_person_names,
)
from contextquilt.services.people_network import (
    NODE_CAP as _NETWORK_NODE_CAP,
    build_snapshot as build_network_snapshot,
)


def network_node_cap() -> int:
    """Indirection so the SQL LIMIT and the service cap can never drift."""
    return _NETWORK_NODE_CAP


from contextquilt.services.consolidation import (
    CONSOLIDATION_SYSTEM,
    CLUSTER_OVERFETCH,
    CLUSTER_WINDOW_DAYS,
    QUIET_MEETING_WINDOW,
    MAX_CLUSTERS_PER_USER_PER_CYCLE,
    MAX_SOURCE_TEXTS,
    MAX_TRAJECTORY_PER_USER_PER_CYCLE,
    MAX_USERS_PER_APP_PER_CYCLE,
    MODEL_CHOSEN_LENSES,
    RETIRED_LENSES,
    PROFILE_SYSTEM,
    build_profile_content,
    build_synthesis_content,
    parse_consolidation_rules,
    parse_profile_response,
    parse_synthesis_response,
    remaining_lenses,
    spread_sample,
)
from contextquilt.services.follow_through import (
    FOLLOW_THROUGH_LENS,
    FOLLOW_THROUGH_SYSTEM,
    MAX_FACT_EXAMPLES,
    MIN_JUDGED_ITEMS,
    allowed_numbers,
    build_follow_through_content,
    parse_follow_through_response,
    summarize_follow_through,
)
from contextquilt.services.corrections import (
    COMPLETION_SYSTEM,
    CORRECTION_SYSTEM,
    FALLBACK_PATCH_TYPE,
    MAX_CANDIDATES,
    MAX_CORRECTION_CHARS,
    build_completion_content,
    build_correction_content,
    parse_completion_response,
    parse_correction_response,
)
from contextquilt.services.entity_aliasing import (
    find_alias_candidate,
    is_contested_person_name,
    person_candidates,
)
from contextquilt.services import behavior_extraction
from contextquilt.services import alignment as alignment_svc
from contextquilt.services import described_as
from contextquilt.services import relationship_lenses
from contextquilt.services import who_they_are
from contextquilt.services import trajectory as trajectory_svc
from contextquilt.services.people_identity import (
    build_entity_resolver,
    merge_person_clusters,
    people_vocabulary,
)
from contextquilt.services.person_appearances import observed_capacities
from contextquilt.services.speaker_identities import (
    parse_speaker_identities,
    rewrite_speaker_labels,
)
from contextquilt.services.entity_aliasing import tokenize_name
from contextquilt.services.people_identity import canonical_pair
from contextquilt.services.ingest_modes import is_interaction_allowed
from contextquilt.services.decay_model import (
    DEFAULT_TTLS,
    FRESHNESS_TRACKED_TYPES,
    DEADLINE_ANCHORED_TYPES,
    PERMANENCE_CLASS_DAYS,
    SALIENCE_TTL_SQL,
    TTL_REGISTRY_QUERY,
    staleness_anchor_sql,
)
from contextquilt.services.facet_runtime import get_type_runtime
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

# Extraction caps — backstops against degenerate output, never
# curation. The 2026-07-30 density probe (12 real meetings, uncapped)
# showed natural emission ranges 1→47 with only 0.558 correlation to
# transcript length — the model's density judgment is good (a sparse
# 28K meeting yielded 1 patch beside a dense 25K one yielding 46), so
# no fixed number can be both safe and non-binding. The patch bound is
# therefore length-scaled via extraction_patch_backstop(): floor
# CQ_MAX_PATCHES (24), ceiling 64 — sized so none of the 12 probed
# meetings would have been touched. Dedup tiers + judge downstream are
# the precision stage; extraction is the recall stage.
MAX_FACTS_PER_MEETING = 5
MAX_ACTION_ITEMS_PER_MEETING = 3
MAX_ENTITIES_PER_MEETING = 15
MAX_RELATIONSHIPS_PER_MEETING = 15

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
    no_collapse_types: set[str] | frozenset[str] | None = None,
    origin_scoped_types: set[str] | frozenset[str] | None = None,
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

    `no_collapse_types` are types that declared `collapse_duplicates:
    false`. Dedup is skipped for them entirely: every occurrence inserts.
    Two observations of the same behavior in two meetings are a
    trajectory, and a collapse would keep only the surviving patch's
    origin_id, silently destroying a receipt the profile pass counts.
    Empty/None (every manifest registered before this) → dedup runs
    exactly as before, for every type.

    `origin_scoped_types` are types that declared `origin_scoped: true`.
    They carry origin_id/origin_type without being project-scoped, which
    is what lets a meeting-bound type that belongs to no project keep its
    receipt. Empty/None → the origin stamp follows project scoping alone,
    exactly as before.
    """
    if not patches:
        return 0
    longitudinal_types = longitudinal_types or {}
    no_collapse_types = frozenset(no_collapse_types or ())
    origin_scoped_types = frozenset(origin_scoped_types or ())

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
    # Resolved from the facet runtime (manifest project_scoped flags,
    # SS floor as fallback), so a registered app's types carry project
    # context per THEIR manifest instead of the SS episode list.
    _type_runtime = await get_type_runtime(db.fetch)
    project_scoped_types = _type_runtime.project_scoped_types
    # Which types the item ledger holds, from the same runtime: every
    # completable, plus any type whose manifest declares `ledger_tracked`.
    # Only these record restatement history below, because only an object
    # that can be UNRESOLVED can be molted (restated in a fresh shape
    # while its state never changes).
    #
    # Deliberately not `completable_types`, which is the narrower "a
    # person can owe this" set. The primitive is a thing that keeps
    # coming back without resolving, and a question nobody answered is
    # one of those without being anything anybody owes. Day one the two
    # sets are equal, so this changes no byte of what SS writes.
    ledger_types = frozenset(_type_runtime.ledger_tracked_types)

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

        And, for completables only, it RECORDS THE RESTATEMENT. Before
        this, re-observation stored the fact that it happened (three
        timestamps and a counter) and nothing about what was said, so an
        item that comes back every month as a differently shaped fresh
        commitment was indistinguishable from an item nobody has
        mentioned since. The history it writes is additive: the existing
        text still wins for `value.text`, because this is a record of an
        object's life, not an edit to the fact.
        """
        # A re-ingest of the SAME meeting is the same observation arriving
        # twice, not the fact being observed again, so it must not move any
        # freshness anchor. Same principle as the ledger's same-origin
        # restatement guard below, and the same key. This exists because
        # the 2026-06-10 entity regression has to be repaired by replaying
        # ~176 real meetings: without this, one afternoon of replay
        # re-anchors decay across two months of history in a single write
        # pass, and every stale item on the user's screen reads as fresh.
        # `updated_at` is held back too, not just `last_observed_at`:
        # completables anchor decay on GREATEST(updated_at, deadline_date),
        # so guarding only the freshness column would still extend the life
        # of every commitment in the replay set. Detail merges (deadline,
        # salience, restatements) write their own rows below and are
        # unaffected — a replay that genuinely learns something still
        # records it.
        # Guarded in SQL rather than read-then-branch, for the same reason
        # the restatement guard is: the comparison is against the stored
        # row, and a separate read would be a second trip that can disagree
        # with the write.
        same_origin = str(origin_id) if origin_id else None
        await db.execute(
            """
            UPDATE context_patches
               SET updated_at = $1, last_observed_at = $1
             WHERE patch_id = $2::uuid
               AND ($3::text IS NULL OR COALESCE(origin_id, '') <> $3::text)
            """,
            created_at, existing_id, same_origin,
        )
        await db.execute(
            """
            UPDATE patch_usage_metrics
               SET access_count = access_count + 1, last_accessed_at = $1
             WHERE patch_id = $2::uuid
               AND ($3::text IS NULL OR NOT EXISTS (
                       SELECT 1 FROM context_patches cp
                        WHERE cp.patch_id = $2::uuid
                          AND COALESCE(cp.origin_id, '') = $3::text
                   ))
            """,
            created_at, existing_id, same_origin,
        )
        new_dd = value.get("deadline_date")
        if new_dd:
            # Fill a missing date (unchanged behavior): "I'll ship it"
            # followed by "ship it by Friday" is one fact gaining a date.
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
            # A DIFFERENT date supersedes (new 2026-08-11, the 12a
            # capture signals). Before this, a rescheduled deadline was
            # silently ignored: the fill above only ran when the date was
            # missing, so the patch stayed overdue against the stale date
            # forever. Now the latest statement wins, the displaced date
            # goes to value.deadline_history (capped at 10, oldest
            # dropped) so slip-counting has something to count, and a
            # stale overdue_since clears (the sweep re-stamps if the new
            # date passes too).
            await db.execute(
                """
                UPDATE context_patches
                   SET value = (
                           jsonb_set(
                               jsonb_set(
                                   value,
                                   '{deadline_history}',
                                   CASE WHEN jsonb_array_length(
                                            COALESCE(value->'deadline_history', '[]'::jsonb)) >= 10
                                        THEN (COALESCE(value->'deadline_history', '[]'::jsonb) - 0)
                                        ELSE COALESCE(value->'deadline_history', '[]'::jsonb)
                                   END || jsonb_build_object(
                                           'deadline', value->'deadline',
                                           'deadline_date', value->'deadline_date',
                                           'superseded_at', to_jsonb($4::text))
                               ),
                               '{deadline_date}', to_jsonb($1::text)
                           )
                       ) - 'overdue_since'
                       || jsonb_build_object('deadline', $2::text)
                 WHERE patch_id = $3::uuid
                   AND value->>'deadline_date' IS NOT NULL
                   AND value->>'deadline_date' <> $1
                """,
                new_dd, value.get("deadline") or new_dd, existing_id,
                created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
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
        # The restatement record. Ledger-tracked types only: everything
        # else that dedups is a fact being observed again ("she is based
        # in Lisbon"), where a second observation is corroboration and
        # there is no state that could fail to change. Types outside the
        # ledger take exactly the writes they took before this shipped.
        if patch_type in ledger_types:
            observed_at = (
                created_at.isoformat() if hasattr(created_at, "isoformat")
                else str(created_at)
            )
            await db.execute(
                """
                UPDATE context_patches
                   SET value = jsonb_set(
                           jsonb_set(
                               value,
                               '{restatements}',
                               CASE WHEN jsonb_array_length(
                                        COALESCE(value->'restatements', '[]'::jsonb)) >= 10
                                    THEN (COALESCE(value->'restatements', '[]'::jsonb) - 0)
                                    ELSE COALESCE(value->'restatements', '[]'::jsonb)
                               END || jsonb_build_object(
                                       'observed_at', $2::text,
                                       'text', $3::text,
                                       'owner', $4::text,
                                       'deadline', $5::text,
                                       'deadline_date', $6::text,
                                       'origin_id', $7::text)
                           ),
                           '{restatement_count}',
                           -- Guarded rather than a bare ::int cast: the
                           -- patch edit surface can put anything in the
                           -- value JSONB, and a cast error here would
                           -- take down the whole extraction's storage
                           -- pass, not just one counter.
                           to_jsonb(
                               CASE WHEN value->>'restatement_count' ~ '^[0-9]+$'
                                    THEN (value->>'restatement_count')::int
                                    ELSE 0
                               END + 1
                           )
                       )
                 WHERE patch_id = $1::uuid
                   AND (
                       $7::text IS NULL
                       OR (
                           COALESCE(origin_id, '') <> $7
                           AND COALESCE(
                                   value->'restatements'->-1->>'origin_id', ''
                               ) <> $7
                       )
                   )
                """,
                existing_id, observed_at, text,
                value.get("owner"), value.get("deadline"),
                value.get("deadline_date"),
                # The receipt, and the idempotency key. One meeting can
                # only restate an item once: a re-ingest of the same
                # transcript, or a second extracted phrasing of the same
                # sentence landing on the same patch, must not read as a
                # second hop months later. The patch's OWN origin is
                # excluded for the same reason, since an item first
                # stated in this meeting has not come back yet.
                str(origin_id) if origin_id else None,
            )
            # A handover, stamped once, the first time a restatement
            # names somebody else. `value.owner` is deliberately NOT
            # rewritten: it is what the ledger matches on, so editing it
            # would move the item off the ledger of the person the user
            # is owed by, which is the record they are trying to read.
            # Nothing classifies from this stamp (the restatement owners
            # are the single source of truth, and the read side derives
            # the change from them); it exists so a handover is visible
            # in the patch itself and in delta sync.
            if isinstance(value.get("owner"), str) and value["owner"].strip():
                await db.execute(
                    """
                    UPDATE context_patches
                       SET value = jsonb_set(
                               value, '{owner_restated_at}', to_jsonb($2::text))
                     WHERE patch_id = $1::uuid
                       AND value->>'owner_restated_at' IS NULL
                       AND lower(btrim(COALESCE(value->>'owner', '')))
                           <> lower(btrim($3::text))
                    """,
                    existing_id, observed_at, value["owner"],
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
        # Origin is stamped for project-scoped types, for role-with-parent,
        # and for any type that declared `origin_scoped`. The last one is
        # the split: project scoping says which project a patch belongs
        # to, origin scoping says the patch records a moment and has to
        # remember which meeting it was. A type can now claim the second
        # without the first, which is what keeps a receipt on a patch that
        # belongs to no project.
        patch_origin_id = origin_id if (
            patch_type in project_scoped_types
            or patch_type in origin_scoped_types
            or (patch_type == "role" and patch_project)
        ) else None
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

        # Types that opted out of collapsing skip BOTH dedup tiers, and
        # skip the candidate query with them. There is nothing to ask:
        # the answer would be discarded either way, and a similar
        # observation from a previous meeting is the evidence this type
        # exists to accumulate, not a duplicate of it.
        if patch_type in no_collapse_types:
            await _insert_new_patch(patch, patch_type, value, text)
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
                stub_origin_id = origin_id if (
                    target_type in project_scoped_types
                    or target_type in origin_scoped_types
                ) else None
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
                    ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO UPDATE SET
                        status = 'active'
                    WHERE patch_connections.status <> 'active'
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
                            "UPDATE context_patches SET status = 'archived', completed_at = NOW(), "
                            "value = jsonb_set(value, '{archive_cause}', '\"replaced\"') "
                            "WHERE patch_id = $1::uuid",
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


async def _resolve_merged_forward(db, entity_id):
    """Follow entities.merged_into to the surviving canonical.

    POST /v1/people/{user_id}/merge marks a folded entity with a forward
    pointer instead of deleting it (held client ids must keep resolving,
    and relationships cascade on delete). Every write path that resolves
    an entity by name therefore has to hop that pointer, or the next
    mention of the old surface form re-observes the dead row and rebuilds
    the duplicate the user just resolved.

    Degrades to the input id if the column is absent — the MCP
    deployment's separate Postgres can lag migrations, and entity storage
    must not start failing there because People shipped here.
    """
    current = entity_id
    seen = set()
    try:
        for _ in range(8):
            row = await db.fetchrow(
                "SELECT merged_into FROM entities WHERE entity_id = $1", current
            )
            if row is None or row["merged_into"] is None:
                return current
            nxt = row["merged_into"]
            if nxt in seen:
                logger.warning("entity_merge_cycle", entity_id=str(nxt))
                return current
            seen.add(nxt)
            current = nxt
        logger.warning("entity_merge_chain_too_deep", entity_id=str(current))
        return current
    except Exception as e:
        logger.debug("merged_forward_resolution_skipped", error=str(e)[:120])
        return entity_id


async def store_entities(
    db,
    redis_client,
    user_id: str,
    entities: list[dict],
    metadata: dict | None = None,
    speaker_labels: set | None = None,
    person_entity_type: str = "person",
    speaker_turns: dict | None = None,
    self_label: str | None = None,
    speaker_questions: dict | None = None,
    speaker_role_signals: dict | None = None,
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
    self_key = self_label.strip().lower() if self_label else None
    # Live people, fetched at most ONCE per ingest and only if a person
    # actually turns up, so a meeting full of orgs pays nothing.
    person_roster: list | None = None

    async def _live_person_roster() -> list:
        nonlocal person_roster
        if person_roster is None:
            try:
                rows = await db.fetch(
                    """
                    SELECT entity_id, name FROM entities
                    WHERE user_id = $1 AND entity_type = $2
                      AND merged_into IS NULL AND suppressed_at IS NULL
                    """,
                    user_id, person_entity_type,
                )
                person_roster = [(r["entity_id"], r["name"]) for r in rows]
            except Exception as exc:
                # Never let the guard's own failure block an ingest. No
                # roster means no contest, which is today's behaviour.
                logger.debug("person_roster_unavailable", error=str(exc)[:120])
                person_roster = []
        return person_roster

    for ent in entities:
        name = ent.get("name", "").strip()
        entity_type = ent.get("type", "").strip()
        description = ent.get("description", "")
        # The (you) marker belongs to transcripts, never to entity names:
        # sanitize_you_marker_from_patches covers the patch lane, and prod
        # grew a literal "Scott (you)" entity through this one. Strip it
        # here so the marked form resolves to the canonical row (and gets
        # the self stamp) instead of fragmenting the graph.
        if "(you)" in name.lower():
            name = re.sub(r"\(you\)", "", name, flags=re.IGNORECASE).strip()
        if not name or not entity_type:
            continue
        # Defensive sink guard (same dual-layer pattern as cues): the
        # sanitizer chain covers LLM extraction, but chat/structured
        # lanes reach this sink without it — and this is exactly the
        # lane the 2026-06-30 Speaker-N leak came through.
        if is_placeholder_or_self_person(name):
            continue

        metadata_json = json.dumps(metadata or {})

        async def _record_appearance(entity_id) -> None:
            """Log that this person showed up in this meeting.

            People only, and only when the ingest carried an origin: a
            person named in a chat turn with no meeting behind it has no
            appearance to record. One row per (person, origin), so five
            mentions in one meeting stay one meeting.

            Degrades silently if the table is absent — the MCP
            deployment's Postgres lags migrations, and entity storage must
            not start failing there because People shipped here.
            """
            if entity_type != person_entity_type:
                return
            meta = metadata or {}
            origin_id = meta.get("origin_id")
            if not origin_id:
                return
            # Capacity: what we can honestly assert from this ingest. Being
            # named in the extraction is a mention; being a transcript
            # speaker label is stronger and is what the identity gate reads.
            #
            # `ownership` now lands HERE too, carried on the entity by
            # inject_ownership_entities. It used to be left to the backfill
            # on the reasoning that it is derivable from Postgres at any
            # time, which is true and was still wrong: the backfill only
            # runs when a human remembers to run it, so between runs a
            # person who owned work out of a meeting had no presence in it
            # at all (2026-08-13, origin 866E8E1B).
            #
            # Only ownership and mention are honoured from the entity.
            # `speaker` is a claim about a transcript label and is computed
            # right here from the transcript, so no caller and no model
            # output can assert it.
            # Capacities UNION on conflict rather than overwrite: a second
            # ingest that only mentions someone must not erase the fact
            # that they spoke.
            name_key = name.strip().lower()
            capacities = observed_capacities(
                ent.get(ENTITY_CAPACITY_KEY) if isinstance(ent, dict) else None,
                spoke=bool(speaker_labels and name_key in speaker_labels),
            )
            # Turn count: captured at the only moment it exists (the
            # transcript is derive-then-discard; no backfill is possible,
            # ever). NULL = unknown, never "spoke zero turns".
            turn_count = (speaker_turns or {}).get(name_key)
            # Question counts, captured at the same only-moment turn
            # counts are. The two attribution grades stay in their own
            # columns, never summed: one is a vocative CQ read, the other
            # is a guess from who spoke next. NULL is unknown throughout,
            # including the from_user pair when no speaker label could be
            # identified as the user.
            q = (speaker_questions or {}).get("by_label", {}).get(name_key) or {}
            q_user = (speaker_questions or {}).get("user") or {}
            # Opened / closed / answered, from the same one pass. NULL
            # when no transcript was parsed for this appearance; FALSE is
            # an observation (parsed, and they did not), the same
            # distinction turn_count makes and the same one
            # `capacities = {}` makes about presence.
            r = (speaker_role_signals or {}).get("by_label", {}).get(name_key) or {}
            opened_meeting = r.get("opened")
            closed_meeting = r.get("closed")
            answers_given = r.get("answers_given")
            questions_by_user = q_user.get("asked") if q_user else None
            try:
                # A suppressed entity ("not a person") accumulates no
                # meeting history: SS's condition that appearances stop
                # counting the moment the user disowns the row. The
                # entity row itself keeps absorbing re-observations,
                # which is the point (the durable negative record), but
                # nothing it absorbs is served or counted.
                sup = await db.fetchval(
                    "SELECT suppressed_at FROM entities WHERE entity_id = $1",
                    entity_id,
                )
                if sup is not None:
                    return
                await db.execute(
                    """
                    INSERT INTO person_appearances
                        (user_id, entity_id, origin_id, origin_type, project_id, capacities, turn_count,
                         questions_asked, questions_received_explicit, questions_received_inferred,
                         questions_from_user_explicit, questions_from_user_inferred,
                         meeting_questions_by_user,
                         opened_meeting, closed_meeting, answers_given,
                         first_seen_at, last_seen_at)
                    VALUES ($1, $2, $3, $4, $5, $6::text[], $7, $8, $9, $10, $11, $12, $13,
                        $14, $15, $16,
                        -- THE MEETING'S CLOCK, NEVER THE INGEST'S. Doc 16
                        -- 6.2a states this rule and only the relabel routes
                        -- in main.py implemented it; the ingest path took the
                        -- column default and the conflict branch stamped
                        -- NOW(). That is correct exactly once, on a meeting's
                        -- first ingest, and wrong every other time. Proven on
                        -- 2026-08-15: replaying real meetings to repair the
                        -- entity regression wrote presence rows dated the
                        -- replay, so people last met in July rendered as met
                        -- today, across 23 meetings before it was caught.
                        -- A repair that lies about when is not a repair.
                        --
                        -- Resolution order per doc 16: sibling rows for this
                        -- meeting (already correct), else the meeting's own
                        -- patches (the ingest clock that wrote them), else
                        -- now, which is only reached on a genuinely new
                        -- meeting that produced no patches.
                        COALESCE(
                            (SELECT min(pa2.first_seen_at) FROM person_appearances pa2
                              WHERE pa2.user_id = $1 AND pa2.origin_id = $3),
                            (SELECT min(cp.created_at) FROM context_patches cp
                              WHERE cp.origin_id = $3),
                            NOW()
                        ),
                        COALESCE(
                            (SELECT min(pa2.first_seen_at) FROM person_appearances pa2
                              WHERE pa2.user_id = $1 AND pa2.origin_id = $3),
                            (SELECT min(cp.created_at) FROM context_patches cp
                              WHERE cp.origin_id = $3),
                            NOW()
                        ))
                    ON CONFLICT (user_id, entity_id, origin_id) DO UPDATE SET
                        -- last_seen_at deliberately NOT touched. One row is
                        -- one person in one meeting, so there is no later
                        -- observation to record: a second ingest of the same
                        -- meeting is the same observation arriving twice.
                        -- Same principle as the same-origin guard on patch
                        -- freshness, through the door that one missed.
                        project_id = COALESCE(EXCLUDED.project_id, person_appearances.project_id),
                        capacities = ARRAY(SELECT DISTINCT unnest(
                            person_appearances.capacities || EXCLUDED.capacities)),
                        -- A re-ingest of the same meeting keeps the MAX,
                        -- never sums (five mentions of one meeting stay one
                        -- meeting; two counts of one meeting stay one
                        -- count). NULL never clobbers a known value.
                        turn_count = CASE
                            WHEN EXCLUDED.turn_count IS NULL THEN person_appearances.turn_count
                            ELSE GREATEST(COALESCE(person_appearances.turn_count, 0), EXCLUDED.turn_count)
                        END,
                        -- Every question column follows turn_count's rule
                        -- for exactly the same reason: one meeting counted
                        -- twice is still one meeting.
                        questions_asked = CASE
                            WHEN EXCLUDED.questions_asked IS NULL THEN person_appearances.questions_asked
                            ELSE GREATEST(COALESCE(person_appearances.questions_asked, 0), EXCLUDED.questions_asked)
                        END,
                        questions_received_explicit = CASE
                            WHEN EXCLUDED.questions_received_explicit IS NULL
                                THEN person_appearances.questions_received_explicit
                            ELSE GREATEST(COALESCE(person_appearances.questions_received_explicit, 0),
                                          EXCLUDED.questions_received_explicit)
                        END,
                        questions_received_inferred = CASE
                            WHEN EXCLUDED.questions_received_inferred IS NULL
                                THEN person_appearances.questions_received_inferred
                            ELSE GREATEST(COALESCE(person_appearances.questions_received_inferred, 0),
                                          EXCLUDED.questions_received_inferred)
                        END,
                        questions_from_user_explicit = CASE
                            WHEN EXCLUDED.questions_from_user_explicit IS NULL
                                THEN person_appearances.questions_from_user_explicit
                            ELSE GREATEST(COALESCE(person_appearances.questions_from_user_explicit, 0),
                                          EXCLUDED.questions_from_user_explicit)
                        END,
                        questions_from_user_inferred = CASE
                            WHEN EXCLUDED.questions_from_user_inferred IS NULL
                                THEN person_appearances.questions_from_user_inferred
                            ELSE GREATEST(COALESCE(person_appearances.questions_from_user_inferred, 0),
                                          EXCLUDED.questions_from_user_inferred)
                        END,
                        meeting_questions_by_user = CASE
                            WHEN EXCLUDED.meeting_questions_by_user IS NULL
                                THEN person_appearances.meeting_questions_by_user
                            ELSE GREATEST(COALESCE(person_appearances.meeting_questions_by_user, 0),
                                          EXCLUDED.meeting_questions_by_user)
                        END,
                        -- The booleans take OR rather than GREATEST, and
                        -- NULL still never clobbers: a re-ingest that
                        -- parsed no transcript must not turn an observed
                        -- TRUE back into an unknown, and one that did
                        -- parse must not turn it into FALSE. Two ingests
                        -- of one meeting are one meeting (doc 19.4).
                        opened_meeting = CASE
                            WHEN EXCLUDED.opened_meeting IS NULL THEN person_appearances.opened_meeting
                            ELSE COALESCE(person_appearances.opened_meeting, FALSE) OR EXCLUDED.opened_meeting
                        END,
                        closed_meeting = CASE
                            WHEN EXCLUDED.closed_meeting IS NULL THEN person_appearances.closed_meeting
                            ELSE COALESCE(person_appearances.closed_meeting, FALSE) OR EXCLUDED.closed_meeting
                        END,
                        answers_given = CASE
                            WHEN EXCLUDED.answers_given IS NULL THEN person_appearances.answers_given
                            ELSE GREATEST(COALESCE(person_appearances.answers_given, 0), EXCLUDED.answers_given)
                        END
                    """,
                    user_id, entity_id, str(origin_id),
                    meta.get("origin_type") or "meeting",
                    meta.get("project_id"),
                    capacities,
                    turn_count,
                    q.get("asked"),
                    q.get("received_explicit"),
                    q.get("received_inferred"),
                    q.get("from_user_explicit"),
                    q.get("from_user_inferred"),
                    questions_by_user,
                    opened_meeting, closed_meeting, answers_given,
                )
            except Exception as e:
                logger.debug("person_appearance_skipped", error=str(e)[:120])

        async def _maybe_stamp_self(entity_id) -> None:
            """Record that this entity IS the submitting user (the ego
            link the 13b orbit graph excludes).

            Keep-first: at most one self entity per user (partial unique
            index, migration 35), and an already-stamped DIFFERENT entity
            wins over this observation — a moving ego would silently
            re-shape every graph read, so a conflict is logged for a
            human, never resolved by the write path. Suppressed rows
            never qualify: "not a person" and "is the user" cannot both
            be true, and the durable-no must not be weakened.

            Degrades silently where the columns are absent (the MCP
            deployment's Postgres lags migrations, same contract as
            appearances).
            """
            if not self_key or entity_type != person_entity_type:
                return
            if name.strip().lower() != self_key:
                return
            try:
                stamped = await db.fetchval(
                    """
                    UPDATE entities SET
                        self_at = NOW(),
                        self_source = 'you_marker'
                    WHERE entity_id = $1
                      AND self_at IS NULL
                      AND suppressed_at IS NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM entities
                          WHERE user_id = $2 AND self_at IS NOT NULL
                            AND entity_id <> $1
                      )
                    RETURNING entity_id
                    """,
                    entity_id, user_id,
                )
                if stamped:
                    logger.info(
                        "self_entity_stamped",
                        user_id=user_id, entity_id=str(entity_id),
                        source="you_marker",
                    )
                else:
                    other = await db.fetchval(
                        "SELECT entity_id FROM entities "
                        "WHERE user_id = $1 AND self_at IS NOT NULL "
                        "  AND entity_id <> $2",
                        user_id, entity_id,
                    )
                    if other is not None:
                        logger.warning(
                            "self_entity_conflict",
                            user_id=user_id,
                            stamped_entity_id=str(other),
                            observed_entity_id=str(entity_id),
                        )
            except Exception as e:
                logger.debug("self_entity_stamp_skipped", error=str(e)[:120])

        async def _record_description(entity_id) -> None:
            """Append this meeting's description to the person's series.

            Brian's ask: keep every iteration of who we thought someone
            was, not just the latest. `entities.description` stays the
            CURRENT value (nothing that reads it changes); this is the
            record that used to be destroyed by the overwrite.

            Only a genuinely different description appends. The same
            perception reworded bumps a confirmation count instead, and
            the same meeting arriving twice does nothing at all (doc
            19.4). See services/described_as.py for the decision.

            People only, and it NEVER raises: a series is a nice-to-have
            and an ingest is not. The table can also be absent on the MCP
            deployment's separate Postgres, which lags migrations.
            """
            if entity_type != person_entity_type:
                return
            if not described_as.usable(description):
                return
            meta = metadata or {}
            origin_id = meta.get("origin_id")
            try:
                latest = await db.fetchrow(
                    """
                    SELECT description_id, description, last_origin_id
                    FROM entity_descriptions
                    WHERE user_id = $1 AND entity_id = $2
                    ORDER BY first_observed_at DESC
                    LIMIT 1
                    """,
                    user_id, entity_id,
                )
                verdict = described_as.classify_observation(
                    description, dict(latest) if latest else None, origin_id,
                )
                if verdict["action"] == described_as.IGNORE:
                    return
                if verdict["action"] == described_as.CONFIRM:
                    await db.execute(
                        """
                        UPDATE entity_descriptions
                           SET last_observed_at = NOW(),
                               observation_count = observation_count + 1,
                               last_origin_id = COALESCE($2, last_origin_id)
                         WHERE description_id = $1
                        """,
                        latest["description_id"], origin_id,
                    )
                    return
                await db.execute(
                    """
                    INSERT INTO entity_descriptions (
                        user_id, entity_id, description,
                        first_origin_id, first_origin_type, last_origin_id, source
                    ) VALUES ($1, $2, $3, $4, $5, $4, $6)
                    """,
                    user_id, entity_id, description.strip(),
                    origin_id, meta.get("origin_type"),
                    meta.get("source") or "meeting_summary",
                )
                logger.info(
                    "described_as_changed",
                    user_id=user_id, entity_id=str(entity_id),
                    similarity=verdict["similarity"], reason=verdict["reason"],
                )
            except Exception as exc:
                logger.debug("described_as_skipped", error=str(exc)[:140])

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
            await _record_appearance(entity_id)
            await _maybe_stamp_self(entity_id)
            await _record_description(entity_id)

        # 1. Exact match, case-insensitive (the old ON CONFLICT only
        #    caught exact case, so "lockridge abrams" vs "Lockridge Abrams"
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
            # A merged entity keeps its name (POST /v1/people/{u}/merge
            # writes a forward pointer rather than deleting), so an exact
            # hit can land on a row the user has already folded into
            # someone else. Re-observing the dead row would quietly
            # rebuild the duplicate the merge just resolved.
            await _reobserve(await _resolve_merged_forward(db, row["entity_id"]))
            stored += 1
            continue

        # 1b. CONTESTED NAME GUARD, people only, after the exact match
        # and before every heuristic. An exact full-name hit above is
        # decisive and never reaches here.
        #
        # A surface form that could honestly mean more than one live
        # person resolves to NOBODY: no alias match, no heuristic, and
        # no new row either, because minting a second "Mike" is its own
        # corruption. The entity is dropped and the meeting keeps its
        # patches, whose `value.owner` holds the raw string. Naming the
        # speaker later attaches all of it retroactively.
        #
        # Receipt (2026-08-17): 'Mike' -> Mike DiTroia is a recorded
        # alias, so an EMIDS interview candidate saying "Mike" gave a
        # Kore.ai colleague a meeting he was never in. 17 bare first
        # names on that roster resolve to one person while others share
        # the name.
        if entity_type == person_entity_type:
            roster = await _live_person_roster()
            if is_contested_person_name(name, roster):
                logger.info(
                    "entity_name_contested",
                    user_id=user_id, name=name[:60],
                    candidates=[n for _, n in person_candidates(name, roster)][:6],
                    origin_id=str((metadata or {}).get("origin_id") or "")[:40],
                )
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
                await _reobserve(await _resolve_merged_forward(db, row["entity_id"]))
                stored += 1
                continue

            # 3. Alias heuristic against existing same-type entities.
            #    Only acts on a UNIQUE candidate — ambiguity ("Lockridge"
            #    with both "Lockridge Abrams" and "Lockridge Chen" present)
            #    falls through to a separate entity, as before.
            #
            #    Merged entities are excluded: aliasing a new surface form
            #    onto a folded row would strand it outside the canonical's
            #    neighborhood. (If the merged_into column is absent on a
            #    lagging DB this raises, and the except below falls through
            #    to the plain insert — same degrade as a missing
            #    entity_aliases table.)
            candidate_rows = await db.fetch(
                "SELECT entity_id, name FROM entities "
                "WHERE user_id = $1 AND entity_type = $2 AND merged_into IS NULL",
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
        #    RETURNING on both arms so the appearance can be recorded
        #    against whichever row won.
        new_entity_id = await db.fetchval(
            """
            INSERT INTO entities (user_id, name, entity_type, description, metadata)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id, name, entity_type) DO UPDATE SET
                description = COALESCE(NULLIF(EXCLUDED.description, ''), entities.description),
                last_seen_at = NOW(),
                mention_count = entities.mention_count + 1,
                metadata = entities.metadata || EXCLUDED.metadata
            RETURNING entity_id
            """,
            user_id, name, entity_type, description,
            metadata_json,
        )
        if new_entity_id is not None:
            await _record_appearance(new_entity_id)
            await _maybe_stamp_self(new_entity_id)
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
        # on the canonical "Lockridge Abrams" entity that store_entities
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
    match even though the canonical entity is "Lockridge Abrams".
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


def who_they_are_opening(text: str, words: int = 2) -> str:
    """First words of a served summary, for the collision list."""
    return " ".join((text or "").split()[:words])


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


def resp_content_or(resp):
    """An LLM response's parsed content, tolerant of a client that hands
    back the raw object."""
    return getattr(resp, "content", resp)


def _uuid_or_none(v):
    try:
        return uuid.UUID(str(v)) if v else None
    except (ValueError, AttributeError):
        return None


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
            self.people_network_loop(),
            self.backup_failure_watch_loop(),
            self.provider_health_loop(),
            self.tier_signals_loop(),
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

    async def tier_signals_loop(self):
        """Consume queued tier signals (cq-tier-signals lane).

        The endpoint is record-only; this loop is the processor. For a
        shape-consistent `account_deleted` (event_type AND new_tier
        both say deletion) it runs the full hard purge via
        services/account_purge.py and stamps the signal row as the
        durable deletion receipt. Ordinary tier events are stamped
        recorded_only. Inconsistent deletion-shaped signals are never
        processed destructively — stamped skipped_inconsistent and
        logged loudly for a human.

        Kill switch CQ_ACCOUNT_PURGE_ENABLED stops processing only;
        signals keep recording and are processed on re-enable.
        """
        # Interval literal is local to this coroutine on purpose — the
        # worker's cross-loop-constant NameError crash-looped us once
        # (hotfix 2026-06-12), never share loop constants.
        POLL_INTERVAL_SECONDS = 60
        # Consecutive-failure counters, coroutine-local for the same
        # reason the interval is. Keyed by signal_id, plus the sentinel
        # key for whole-loop failures. Cleared on success so a signal
        # that recovers stops counting toward an alert.
        failures: dict[str, int] = {}
        LOOP_KEY = "tier_signals_loop"

        async def _alert(category: str, subject: str, **details):
            """Alerting must never break the lane it is watching."""
            try:
                await report_incident(
                    self.db, category=category, subject=subject, details=details
                )
            except Exception as alert_exc:
                logger.error("purge_alert_failed", error=str(alert_exc))

        while self.running:
            try:
                if get_settings().cq_account_purge_enabled:
                    rows = await self.db.fetch(
                        """
                        SELECT signal_id, user_id, event_type, new_tier
                        FROM tier_signals
                        WHERE processed_at IS NULL
                        ORDER BY received_at
                        LIMIT 20
                        """
                    )
                    for row in rows:
                        sid = str(row["signal_id"])
                        # Per-signal isolation. Without it, one signal
                        # that always throws starves every signal behind
                        # it forever: the batch is ordered by
                        # received_at, the exception escapes to the outer
                        # handler, and the queue never drains past the
                        # bad row.
                        try:
                            action = account_purge.classify_signal(
                                row["event_type"], row["new_tier"]
                            )
                            detail = action
                            if action == account_purge.ACTION_PURGED:
                                counts = await account_purge.purge_user_data(
                                    self.db, self.redis, row["user_id"]
                                )
                                detail = json.dumps({"action": action, "counts": counts})
                                logger.info(
                                    "account_purged",
                                    user_id=row["user_id"],
                                    signal_id=sid,
                                    counts=counts,
                                )
                            elif action == account_purge.ACTION_INCONSISTENT:
                                logger.warning(
                                    "tier_signal_inconsistent",
                                    user_id=row["user_id"],
                                    signal_id=sid,
                                    event_type=row["event_type"],
                                    new_tier=row["new_tier"],
                                )
                                # One-shot, not retried: the row is about
                                # to be stamped and never looked at
                                # again. Nothing will fix this without a
                                # human, so alert on the first one rather
                                # than waiting for a repeat that cannot
                                # come.
                                await _alert(
                                    "account_purge_inconsistent", sid,
                                    user_id=row["user_id"],
                                    event_type=row["event_type"],
                                    new_tier=row["new_tier"],
                                )
                            await self.db.execute(
                                "UPDATE tier_signals SET processed_at = NOW(), action = $2 WHERE signal_id = $1",
                                row["signal_id"], detail,
                            )
                            failures.pop(sid, None)
                        except Exception as row_exc:
                            failures[sid] = failures.get(sid, 0) + 1
                            logger.error(
                                "tier_signal_processing_failed",
                                signal_id=sid, user_id=row["user_id"],
                                consecutive_failures=failures[sid],
                                error=str(row_exc),
                            )
                            if account_purge.should_alert_for_failures(failures[sid]):
                                await _alert(
                                    "account_purge_failed", sid,
                                    user_id=row["user_id"],
                                    event_type=row["event_type"],
                                    consecutive_failures=failures[sid],
                                    error=str(row_exc)[:400],
                                )
                    failures.pop(LOOP_KEY, None)
            except Exception as e:
                failures[LOOP_KEY] = failures.get(LOOP_KEY, 0) + 1
                logger.error(
                    "tier_signals_loop_error",
                    error=str(e), consecutive_failures=failures[LOOP_KEY],
                )
                # The whole consumer is down, so NOTHING is being
                # deleted. Silence here would look identical to "no
                # deletion requests", which is the failure mode this
                # alert exists to break.
                if account_purge.should_alert_for_failures(failures[LOOP_KEY]):
                    await _alert(
                        "account_purge_failed", LOOP_KEY,
                        consecutive_failures=failures[LOOP_KEY],
                        error=str(e)[:400],
                    )
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

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
                # Which types are deadline-bearing completables comes from
                # the facet runtime per cycle (manifest is_completable,
                # SS floor fallback), so a new app's completables get the
                # overdue stamp without CQ code.
                sweep_completable = (
                    await get_type_runtime(self.db.fetch)
                ).completable_types
                result = await self.db.execute(
                    f"""
                    UPDATE context_patches
                       SET value = jsonb_set(
                               value, '{{overdue_since}}',
                               to_jsonb(to_char((NOW() AT TIME ZONE 'utc')::date, 'YYYY-MM-DD'))
                           ),
                           updated_at = NOW()
                     WHERE patch_type = ANY($1::text[])
                       AND COALESCE(status, 'active') = 'active'
                       AND completed_at IS NULL
                       AND value->>'overdue_since' IS NULL
                       AND {overdue_sql}
                    """,
                    list(sweep_completable)
                )
                stamped = int(result.split()[-1]) if result else 0
                total = await self.db.fetchval(
                    f"""
                    SELECT count(*) FROM context_patches
                     WHERE patch_type = ANY($1::text[])
                       AND COALESCE(status, 'active') = 'active'
                       AND completed_at IS NULL
                       AND {overdue_sql}
                    """,
                    list(sweep_completable)
                )
                if stamped:
                    logger.info("deadline_sweep_stamped", newly_overdue=stamped, total_overdue=total)
                else:
                    logger.debug("deadline_sweep_complete", total_overdue=total)
            except Exception as e:
                logger.error("deadline_sweep_error", error=str(e))
            # Alignment proposals lapse (requirements 4: a proposal
            # expires, a confirmed direction never does). Same cadence,
            # own try, so a missing table on a lagging DB cannot take the
            # overdue stamp down with it.
            try:
                expired = await self.db.execute(
                    """
                    UPDATE alignment_events
                       SET status = 'expired', updated_at = NOW()
                     WHERE status IN ('proposed', 'corrected')
                       AND confirmed_at IS NULL
                       AND superseded_by IS NULL
                       AND expires_at IS NOT NULL AND expires_at < NOW()
                    """
                )
                n = int(expired.split()[-1]) if expired else 0
                if n:
                    logger.info("alignment_proposals_expired", count=n)
            except Exception as e:
                logger.debug("alignment_expiry_skipped", error=str(e)[:120])
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
        # The TTLs, anchors, salience multipliers and SQL fragments live in
        # services/decay_model.py (module-level imports), because the People
        # surface serves `decay_state` derived from the SAME parameters.
        # Changing the predicate there changes both consumers or neither.
        # Run every 6 hours
        DECAY_INTERVAL_SECONDS = 6 * 60 * 60
        # Wait 60 seconds after startup before first run
        await asyncio.sleep(60)

        while self.running:
            try:
                total_archived = 0

                # The type inventory and anchor sets come from the facet
                # runtime (registry-backed, SS floor as fallback), so a
                # registered app's types decay per THEIR manifest. Before
                # this, the loop iterated only the SS names: a TR type
                # with a registry TTL was never visited and never decayed.
                runtime = await get_type_runtime(self.db.fetch)

                # Step 1: Archive patches with explicit permanence_override.
                # These run per-class and cross-cut patch_type.
                for perm_class, ttl_days in PERMANENCE_CLASS_DAYS.items():
                    if ttl_days is None:
                        continue  # permanent/decade never expire
                    # archive_cause says WHY a row without completed_at is
                    # archived; without it a decay expiry is indistinguishable
                    # from every other archival (the 864-unexplained lesson).
                    result = await self.db.execute(
                        """
                        UPDATE context_patches SET status = 'archived', updated_at = NOW(),
                               value = jsonb_set(value, '{archive_cause}', '"decay"')
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
                for patch_type in runtime.decaying_types:
                    ttl_days = DEFAULT_TTLS.get(patch_type)
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
                            TTL_REGISTRY_QUERY,
                            patch_type
                        )
                        if row and row["default_ttl_days"] is not None:
                            ttl_days = row["default_ttl_days"]
                    except Exception:
                        pass  # Registry table may not exist yet

                    if ttl_days is None:
                        # In the decaying inventory but no TTL resolvable
                        # anywhere (registry row went permanent between
                        # snapshot and now, or cache drift). Skipping is
                        # the safe direction: never archive on a guess.
                        continue

                    # Anchor selection and the salience multiplier come from
                    # the shared decay model (self-typed types anchor on
                    # last_observed_at; deadline-bearing completables on
                    # GREATEST(updated_at, deadline_date) so they never
                    # archive before their due date; the access-exemption
                    # window stays at the UNMODIFIED TTL). Membership comes
                    # from the runtime: facet-derived freshness, manifest
                    # completables. For SS names both sets equal the
                    # module constants, so this is byte-identical for them.
                    anchor_sql = staleness_anchor_sql(
                        patch_type,
                        freshness_types=runtime.freshness_tracked_types,
                        deadline_types=runtime.deadline_anchored_types,
                    )

                    result = await self.db.execute(
                        f"""
                        UPDATE context_patches SET status = 'archived', updated_at = NOW(),
                               value = jsonb_set(value, '{{archive_cause}}', '"decay"')
                        WHERE patch_type = $1
                          AND permanence_override IS NULL
                          AND COALESCE(status, 'active') = 'active'
                          AND {anchor_sql} < NOW() - INTERVAL '1 day' * $2 * {SALIENCE_TTL_SQL}
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
                                "last_observed_at" if patch_type in runtime.freshness_tracked_types
                                else "max(updated_at, deadline)" if patch_type in runtime.deadline_anchored_types
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
        store up to MAX_CLUSTERS_PER_USER_PER_CYCLE new derived patches,
        PLUS up to MAX_TRAJECTORY_PER_USER_PER_CYCLE hero cards on their
        own budget (see that constant for why the hero lens is not in the
        shared pool, and what it cost to learn).

        `hero_created` is deliberately not folded into `created`: it is
        counted in the returned total, because a hero card is a patch
        this cycle wrote and the cycle log must not under-report, but it
        must never subtract from the shared pool or the separation is
        cosmetic.
        """
        created = 0
        hero_created = 0
        for rule in rules:
            is_person_rule = rule.get("cluster") == "person"
            # A rule with its OWN budget is not stopped by the shared one.
            # Before 2026-08-27 this break came first for every rule, so a
            # cue rule that spent the pool meant the person branch was
            # never entered and the hero lens never ran at all. The four
            # shared passes inside the branch still respect the pool (they
            # are handed 0 below and return immediately), which is the
            # behaviour that was always intended for them.
            if created >= MAX_CLUSTERS_PER_USER_PER_CYCLE and not is_person_rule:
                break
            if is_person_rule:
                # Four passes over the same rule, sharing one budget. The
                # model-chosen lenses go first because they are the older
                # contract; the computed lenses take whatever slots are
                # left, so a cycle spends at most
                # MAX_CLUSTERS_PER_USER_PER_CYCLE calls per user across
                # these four and none can starve another by more than one
                # cycle's worth. Each remainder is clamped at zero because
                # the branch is now reachable with the pool already spent,
                # and a negative budget is a number no pass should have to
                # reason about.
                created += await self._consolidate_user_people(
                    subject_key, app_id, rule,
                    max(0, MAX_CLUSTERS_PER_USER_PER_CYCLE - created),
                )
                created += await self._derive_follow_through(
                    subject_key, app_id, rule,
                    max(0, MAX_CLUSTERS_PER_USER_PER_CYCLE - created),
                )
                # The contrastive pass runs LAST, and that ordering is
                # deliberate rather than incidental: it is the only pass
                # that needs every OTHER person measured before it can
                # say anything about one of them.
                created += await self._derive_stands_out(
                    subject_key, app_id, rule,
                    max(0, MAX_CLUSTERS_PER_USER_PER_CYCLE - created),
                )
                # The synthesis lens runs on its own model and its own
                # fingerprint gate, so it spends nothing on a person
                # whose inputs have not moved.
                created += await self._derive_who_they_are(
                    subject_key, app_id, rule,
                    max(0, MAX_CLUSTERS_PER_USER_PER_CYCLE - created),
                )
                # The hero lens (16 5.15): how a person is changing
                # against their own past. Arithmetic in
                # services/trajectory.py; the model only writes. It runs
                # on its OWN budget, never the remainder: it was last in
                # a fixed order on a shared pool and went dark for three
                # days without logging a word.
                hero_created += await self._derive_trajectory(
                    subject_key, app_id, rule,
                    MAX_TRAJECTORY_PER_USER_PER_CYCLE,
                )
                continue
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
        # Hero cards are reported but were never charged to the pool.
        return created + hero_created

    async def people_network_loop(self):
        """The 13b orbit graph precompute (daily): one snapshot per user
        with appearances, written whole so the read path serves stored
        bytes. Deterministic end to end (services/people_network), so a
        quiet day rewrites identical positions: the ratified soft goal.
        Degrades to a skipped cycle on lagging DBs (missing table or
        self_at column)."""
        # Coroutine-local on purpose — the worker constants gotcha.
        NETWORK_INTERVAL_SECONDS = 24 * 60 * 60
        await asyncio.sleep(180)

        while self.running:
            try:
                users = await self.db.fetch(
                    "SELECT DISTINCT user_id FROM person_appearances"
                )
                built = 0
                for u in users:
                    try:
                        built += await self._build_network_snapshot(u["user_id"])
                    except Exception as e:
                        logger.warning("network_snapshot_failed",
                                       user_id=u["user_id"], error=str(e)[:150])
                logger.info("network_cycle_complete", snapshots=built)
            except Exception as e:
                logger.error("network_cycle_error", error=str(e)[:200])
            await asyncio.sleep(NETWORK_INTERVAL_SECONDS)

    async def _build_network_snapshot(self, user_id: str) -> int:
        node_rows = await self.db.fetch(
            f"""
            SELECT e.entity_id::text AS entity_id, e.name,
                   count(DISTINCT pa.origin_id) AS meeting_count
            FROM person_appearances pa
            JOIN entities e ON e.entity_id = pa.entity_id
            WHERE pa.user_id = $1
              AND e.merged_into IS NULL AND e.suppressed_at IS NULL
              AND e.self_at IS NULL
            GROUP BY e.entity_id, e.name
            ORDER BY count(DISTINCT pa.origin_id) DESC, e.entity_id
            LIMIT {network_node_cap()}
            """,
            user_id,
        )
        if not node_rows:
            return 0
        ids = [r["entity_id"] for r in node_rows]
        pair_rows = await self.db.fetch(
            """
            SELECT a.entity_id::text AS a, b.entity_id::text AS b,
                   count(DISTINCT a.origin_id) AS weight
            FROM person_appearances a
            JOIN person_appearances b
              ON a.origin_id = b.origin_id AND a.user_id = b.user_id
             AND a.entity_id < b.entity_id
            WHERE a.user_id = $1
              AND a.entity_id = ANY($2::uuid[])
              AND b.entity_id = ANY($2::uuid[])
            GROUP BY a.entity_id, b.entity_id
            HAVING count(DISTINCT a.origin_id) >= 2
            """,
            user_id, ids,
        )
        proj_rows = await self.db.fetch(
            """
            SELECT entity_id::text AS entity_id, project_id, count(*) AS n
            FROM person_appearances
            WHERE user_id = $1 AND entity_id = ANY($2::uuid[])
              AND project_id IS NOT NULL
            GROUP BY entity_id, project_id
            """,
            user_id, ids,
        )
        project_by_node: dict = {}
        for r in proj_rows:
            project_by_node.setdefault(r["entity_id"], {})[r["project_id"]] = r["n"]

        snapshot = build_network_snapshot(
            [dict(r) for r in node_rows],
            [dict(r) for r in pair_rows],
            project_by_node,
            datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        )
        await self.db.execute(
            """
            INSERT INTO people_network_snapshots (user_id, computed_at, version, payload)
            VALUES ($1, NOW(), $2, $3)
            ON CONFLICT (user_id) DO UPDATE SET
                computed_at = NOW(), version = EXCLUDED.version,
                payload = EXCLUDED.payload
            """,
            user_id, snapshot["version"], json.dumps(snapshot),
        )
        return 1

    async def _derive_stands_out(
        self, subject_key: str, app_id: str, rule: dict, budget: int
    ) -> int:
        """The contrastive pass: what is unlike this user's other people.

        Every other lens looks at ONE person and asks what can be said
        about them. That is exactly why they all said the same thing: a
        corpus of commitments and blockers describes dependencies, so
        every person read alone "gates on dependencies", and on
        2026-08-16 three of four people on Scott's pages carried that
        sentence. Accurate about each of them, worthless about any of
        them.

        This pass measures the WHOLE roster first, then asks of one
        person: on which measure are they unlike everybody else. A
        person who is unremarkable gets no card at all, which is the
        point rather than a shortfall.

        The arithmetic is entirely in SQL and the ranking entirely in
        `relationship_lenses`; the model only writes the sentence, and
        may only use numbers it was handed. Same division of labour as
        the follow-through lens, for the same reason: the model may
        identify, it may not count.
        """
        if budget <= 0:
            return 0
        vocab = people_vocabulary(await self._app_manifest(app_id))
        completables = (
            await get_type_runtime(self.db.fetch)
        ).completable_types
        source_types = [t for t in rule["from_types"] if t in completables]
        if not source_types:
            return 0
        user_id = subject_key.split(":", 1)[1] if ":" in subject_key else subject_key
        try:
            self_name = await self.db.fetchval(
                "SELECT name FROM entities WHERE user_id = $1 AND self_at IS NOT NULL",
                user_id,
            )
        except Exception:
            self_name = None
        try:
            rows = await self.db.fetch(
                f"""
                WITH resolver AS (
                    SELECT lower(e.name) AS surface,
                           COALESCE(e.merged_into, e.entity_id) AS canonical_id
                    FROM entities e
                    WHERE e.user_id = $1 AND e.entity_type = 'person'
                    UNION
                    SELECT lower(a.alias) AS surface,
                           COALESCE(e.merged_into, e.entity_id) AS canonical_id
                    FROM entity_aliases a
                    JOIN entities e ON e.entity_id = a.entity_id
                    WHERE a.user_id = $1
                ),
                named AS (
                    SELECT DISTINCT r.surface, r.canonical_id, ce.name AS canonical_name
                    FROM resolver r
                    JOIN entities ce ON ce.entity_id = r.canonical_id
                    WHERE ce.suppressed_at IS NULL AND ce.self_at IS NULL
                ),
                -- The person's own meeting clock. "Gone quiet" is
                -- counted in MEETINGS WITH THEM, never in elapsed days:
                -- doc 16 5.10, because a month of not meeting someone is
                -- not the same claim as three meetings without mention.
                mtg AS (
                    SELECT entity_id,
                           max(last_seen_at) FILTER (WHERE rn = {QUIET_MEETING_WINDOW})
                               AS window_start
                    FROM (
                        SELECT entity_id, last_seen_at,
                               row_number() OVER (PARTITION BY entity_id
                                                  ORDER BY last_seen_at DESC) AS rn
                        FROM person_appearances WHERE user_id = $1
                    ) t GROUP BY entity_id
                ),
                items AS (
                    SELECT DISTINCT n.canonical_id, n.canonical_name, cp.patch_id,
                           COALESCE(cp.status, 'active') AS status,
                           COALESCE(cp.last_observed_at, cp.updated_at,
                                    cp.created_at) AS last_stated,
                           cp.completed_at,
                           cp.value->>'deadline_date' AS due,
                           (cp.value ? 'deadline_history') AS re_dated,
                           (cp.value ? 'owner_restated_at') AS handed,
                           (cp.value ? 'restatement_count') AS restated,
                           m.window_start
                    FROM context_patches cp
                    JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                    JOIN context_patch_acl acl ON acl.patch_id = cp.patch_id
                    JOIN named n ON n.surface = lower(cp.value->>'owner')
                    LEFT JOIN mtg m ON m.entity_id = n.canonical_id
                    WHERE ps.subject_key = $2
                      AND acl.app_id = $3::uuid
                      AND cp.patch_type = ANY($4::text[])
                      AND cp.value->>'shelved_at' IS NULL
                      AND cp.created_at > NOW() - INTERVAL '{CLUSTER_WINDOW_DAYS} days'
                      AND ($5::text IS NULL
                           OR lower(btrim(n.canonical_name)) <> lower(btrim($5)))
                )
                SELECT canonical_id, canonical_name,
                       count(*) AS total_items,
                       count(*) FILTER (WHERE status = 'active') AS open_items,
                       count(*) FILTER (WHERE status = 'active'
                                          AND window_start IS NOT NULL
                                          AND last_stated < window_start) AS quiet_items,
                       -- Items stated INSIDE the window. This is the
                       -- proof that CQ can see the meetings an absence
                       -- is being claimed about; see MIN_RECENT_FOR_QUIET.
                       count(*) FILTER (WHERE status = 'active'
                                          AND window_start IS NOT NULL
                                          AND last_stated >= window_start) AS recent_items,
                       count(*) FILTER (WHERE completed_at IS NOT NULL) AS closed_items,
                       count(*) FILTER (WHERE completed_at IS NOT NULL AND due IS NOT NULL
                                          AND due ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                                          AND completed_at::date > due::date) AS closed_late,
                       count(*) FILTER (WHERE due IS NOT NULL
                                          AND due ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$') AS dated_items,
                       count(*) FILTER (WHERE re_dated) AS re_dated,
                       count(*) FILTER (WHERE handed) AS handed_back,
                       count(*) FILTER (WHERE status = 'active' AND restated) AS restated,
                       array_agg(patch_id) AS patch_ids,
                       -- The specific items behind each fact, not only
                       -- the count. `Fact.patch_ids` has read these five
                       -- keys since the lens shipped and the query
                       -- produced none of them, so every fact carried an
                       -- empty list: a contract with no carrier (19.2)
                       -- inside the code that cites 19.2. Without them a
                       -- claim can say "moves due dates more often than
                       -- others" and can never say WHICH ONE, which is
                       -- the difference between characterising somebody
                       -- and showing the reader the thing.
                       array_agg(patch_id) FILTER (
                           WHERE status = 'active' AND window_start IS NOT NULL
                             AND last_stated < window_start) AS quiet_patch_ids,
                       array_agg(patch_id) FILTER (
                           WHERE completed_at IS NOT NULL AND due IS NOT NULL
                             AND due ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                             AND completed_at::date > due::date) AS late_patch_ids,
                       array_agg(patch_id) FILTER (WHERE re_dated) AS re_dated_patch_ids,
                       array_agg(patch_id) FILTER (WHERE handed) AS handed_patch_ids,
                       array_agg(patch_id) FILTER (
                           WHERE status = 'active' AND restated) AS restated_patch_ids
                FROM items
                GROUP BY canonical_id, canonical_name
                """,
                user_id, subject_key, app_id, source_types, self_name,
            )
        except Exception as exc:
            logger.warning("stands_out_measure_failed",
                           subject=subject_key, reason=str(exc)[:200])
            return 0

        counts_by_person = {
            str(r["canonical_id"]): dict(r) for r in rows
        }
        all_facts = {
            pid: relationship_lenses.facts_for_person(c)
            for pid, c in counts_by_person.items()
        }
        created = 0
        for pid, counts in counts_by_person.items():
            if created >= budget:
                break
            # LEAVE ONE OUT. Unusual means unlike THE OTHERS, and on a
            # roster this size a heavy owner left in their own baseline
            # pulls it toward themselves and hides the very person the
            # measure is loudest about.
            baseline = relationship_lenses.roster_baseline(all_facts, exclude=pid)
            chosen = relationship_lenses.best_fact(counts, baseline)
            if not chosen:
                continue
            if await self._stands_out_taken(
                subject_key, rule["produce_type"], pid,
                chosen["fact"].denominator,
            ):
                continue
            made = await self._write_stands_out(
                subject_key, app_id, rule, pid, counts, chosen,
            )
            if made:
                created += 1
        return created

    async def _stands_out_taken(
        self, subject_key: str, produce_type: str, entity_id: str,
        candidate_denominator: int = 0,
    ) -> bool:
        """The durable no for this lens, keyed on the ENTITY.

        Status-blind like every other lens: an archived card is the
        record of a claim the user rejected and it never comes back.
        Keyed on `source_entity_id` rather than a person patch, because
        that is the identity that does not move when the extractor
        rephrases somebody.

        ONE EXCEPTION, and it is a distinction the other lenses have not
        needed yet: a card CQ itself withdrew is not a user's no. When a
        rule tightens, the cards it would no longer write are retracted,
        and reading those as suppressions would silently ban the person
        from a lens forever over a claim they never saw and never
        rejected. So `archive_cause = 'retracted'` is skipped here, while
        `user_delete` and every other cause still count.

        AND THE THIN SLICE DOES NOT GET TO WIN A RACE. The consolidation
        pass runs once per app id, and this user's items are split across
        two of them by the August flip, so the same person is measured
        twice over different slices of their own record. Measured
        2026-08-16: the pass that sees 28 of Sukumar's items wrote his
        card at 08:59, and the pass that sees 86 was then blocked by this
        very check, so a 28 item slice beat an 86 item slice purely by
        finishing first and he lost the strongest card on the roster.

        So an ACTIVE card standing on FEWER items than the candidate does
        not block it: the richer card is written and the thinner one is
        retracted, which is the same preference the read already applies
        (evidence beats recency) moved to where it decides what exists
        rather than what shows. A user's no still blocks unconditionally,
        because that is a decision rather than a measurement.
        """
        rows = await self.db.fetch(
            """
            SELECT d.patch_id,
                   COALESCE(d.status, 'active') AS status,
                   COALESCE((d.value->'facts'->>'denominator')::int, 0) AS den
            FROM context_patches d
            JOIN patch_subjects dps ON dps.patch_id = d.patch_id
            WHERE dps.subject_key = $1
              AND d.patch_type = $2
              AND d.origin_mode = 'derived'
              AND d.value->>'lens' = $3
              AND d.value->>'source_entity_id' = $4
              AND COALESCE(d.value->>'archive_cause', '') <> 'retracted'
            """,
            subject_key, produce_type, relationship_lenses.LENS, str(entity_id),
        )
        if not rows:
            return False
        # An archived card is a decision (a user's no), never a
        # measurement, so it blocks whatever the candidate stands on.
        if any(r["status"] != "active" for r in rows):
            return True
        if not any(r["den"] < int(candidate_denominator or 0) for r in rows):
            return True
        # Every live card here stands on less evidence than the
        # candidate. Withdraw them so the richer one can be written; the
        # retraction cause keeps this from reading as a user's no later.
        for row in rows:
            await self.db.execute(
                """
                UPDATE context_patches
                SET status = 'archived', updated_at = NOW(),
                    value = jsonb_set(value, '{archive_cause}',
                                      '"retracted"'::jsonb)
                WHERE patch_id = $1
                """,
                row["patch_id"],
            )
        logger.info("stands_out_thin_card_retracted",
                    subject=subject_key, entity=str(entity_id),
                    replaced=[r["den"] for r in rows],
                    candidate_denominator=int(candidate_denominator or 0))
        return False

    async def _write_stands_out(
        self, subject_key: str, app_id: str, rule: dict,
        entity_id: str, counts: dict, chosen: dict,
    ) -> bool:
        """One contrast call, checked, then the provenance write."""
        person_name = counts["canonical_name"]
        facts = relationship_lenses.served_facts(chosen, person_name)
        # What has already been said about this user's OTHER people on
        # this lens. A card that reads like the last card teaches the
        # reader to stop reading them, and the live prose lenses proved
        # it: six claims with two distinct opening words between them,
        # and two people carrying byte-identical text. The writer cannot
        # avoid a collision it cannot see, so it is shown them.
        used_claims = [
            r["text"] for r in await self.db.fetch(
                """
                SELECT DISTINCT cp.value->>'text' AS text
                FROM context_patches cp
                JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                WHERE ps.subject_key = $1
                  AND cp.origin_mode = 'derived'
                  AND cp.value->>'lens' = $2
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND COALESCE(cp.value->>'source_entity_id', '') <> $3
                ORDER BY 1
                LIMIT 12
                """,
                subject_key, relationship_lenses.LENS, str(entity_id),
            ) if r["text"]
        ]
        source_ids = [str(p) for p in (counts.get("patch_ids") or [])]
        examples = await self.db.fetch(
            "SELECT value->>'text' AS text FROM context_patches "
            "WHERE patch_id = ANY($1::uuid[]) ORDER BY created_at ASC LIMIT $2",
            source_ids, MAX_FACT_EXAMPLES,
        )
        base_content = relationship_lenses.build_stands_out_content(
            person_name, facts, [dict(e) for e in examples],
            used_claims=used_claims,
        )
        defects: list = []
        claim = None
        content = base_content
        # ONE bounded retry, and only when the correction changes the
        # PROMPT. A pinned temperature makes a repeat of the same
        # question return the same answer (#240), so a blind retry is
        # waste; telling the writer that its claim was N characters and
        # must get shorter is a different question. Two recoverable
        # defects qualify: too long, and collided with another person's
        # opening. A character verdict or an invented number is a
        # judgement problem, and asking again invites argument.
        for attempt in range(2):
            try:
                response = await self.llm.extract(
                    system_prompt=relationship_lenses.STANDS_OUT_SYSTEM,
                    user_content=content,
                )
                claim = relationship_lenses.parse_stands_out_response(
                    response.content,
                    relationship_lenses.allowed_numbers(facts),
                    person_name=person_name, defects=defects, facts=facts,
                    used_claims=used_claims,
                )
            except Exception as exc:
                logger.warning("stands_out_failed", subject=subject_key,
                               person=person_name, reason=str(exc)[:200])
                return False
            if claim or attempt:
                break
            note = relationship_lenses.retry_note(
                defects[0] if defects else "",
                relationship_lenses.rejected_lengths(
                    response.content).get("claim") or "",
            )
            if not note:
                break
            content = f"{base_content}\n\n{note}"
            defects = []
        if not claim:
            # Carry the EVIDENCE, not just the verdict. A bare
            # `defect=claim_too_long` cost three deploys of guessing at
            # what the model had actually written, and the answer was
            # visible the moment anyone looked. Same rule as doc 19.9's
            # logging note: a count with the offending value makes
            # somebody go looking, a bare label does not.
            over = relationship_lenses.rejected_lengths(response.content)
            logger.info("stands_out_card_rejected", subject=subject_key,
                        person=person_name,
                        defect=defects[0] if defects else "declined",
                        claim_chars=over.get("claim_chars"),
                        do_chars=over.get("do_chars"),
                        claim=over.get("claim"))
            return False
        # Post-check, same reason as every other lens: the call is
        # seconds of wall clock and the durable no must not lose a race
        # with a suppression.
        if await self._stands_out_taken(
            subject_key, rule["produce_type"], entity_id,
            chosen["fact"].denominator,
        ):
            return False
        await self._write_person_insight(
            subject_key, app_id, rule, str(entity_id), person_name,
            source_ids, relationship_lenses.LENS, claim["text"], claim["do"],
            facts=facts, entity_id=entity_id,
        )
        logger.info(
            "stands_out_insight_created", subject=subject_key,
            person=person_name, fact=chosen["fact"].key,
            direction=chosen["direction"], gap_points=chosen["gap_points"],
            them=f"{facts['numerator']}/{facts['denominator']}",
            roster=f"{facts['roster_numerator']}/{facts['roster_denominator']}",
        )
        return True

    async def _derive_who_they_are(
        self, subject_key: str, app_id: str, rule: dict, budget: int
    ) -> int:
        """Who they are: one synthesis per person across stated roles and
        the description series, regenerated only when the inputs change.

        Scott, 2026-08-21: "average it out to get the best comprehensive
        summary that goes beyond a simply title." The title (#301) is a
        rule, this is the model step, and it is the ONLY model step in
        the description story: the 08-13 experiment showed cross-meeting
        synthesis is what a model adds and that Haiku fails it invisibly,
        so this lens runs on its own model (CQ_WHO_THEY_ARE_MODEL, Sonnet
        by default) behind its own kill switch (CQ_WHO_THEY_ARE_ENABLED).

        The arithmetic is in `services/who_they_are`; the model writes
        from inputs it was handed and the parse refuses anything that
        drops the stated role, invents a number, or opens with the name.
        A card carries the fingerprint of its inputs; a person whose
        roles and perceptions have not moved costs nothing per cycle.
        """
        if budget <= 0:
            return 0
        if os.getenv("CQ_WHO_THEY_ARE_ENABLED", "true").lower() in ("0", "false", "no"):
            return 0
        model = os.getenv("CQ_WHO_THEY_ARE_MODEL") or who_they_are.DEFAULT_MODEL
        vocab = people_vocabulary(await self._app_manifest(app_id))
        if not vocab.stated_role_type:
            role_type = None
        else:
            role_type = vocab.stated_role_type
        user_id = subject_key.split(":", 1)[1] if ":" in subject_key else subject_key
        try:
            people = await self.db.fetch(
                """
                SELECT e.entity_id, e.name, e.mention_count,
                       e.first_seen_at, e.last_seen_at,
                       (SELECT array_agg(DISTINCT a.alias) FROM entity_aliases a
                         WHERE a.entity_id = e.entity_id) AS aliases,
                       (SELECT count(*) FROM entity_descriptions d
                         WHERE d.entity_id = e.entity_id) AS perceptions,
                       (SELECT count(*) FROM person_appearances pa
                         WHERE pa.entity_id = e.entity_id) AS meetings,
                       (SELECT array_agg(DISTINCT pr.name) FROM person_appearances pa
                         JOIN projects pr ON pr.project_id = pa.project_id
                         WHERE pa.entity_id = e.entity_id) AS projects
                FROM entities e
                WHERE e.user_id = $1 AND e.entity_type = $2
                  AND e.merged_into IS NULL AND e.suppressed_at IS NULL
                  AND e.self_at IS NULL
                ORDER BY e.last_seen_at DESC NULLS LAST
                LIMIT 400
                """,
                user_id, vocab.person_entity_type,
            )
        except Exception as exc:
            logger.debug("who_they_are_roster_unavailable", error=str(exc)[:140])
            return 0
        created = 0
        for person in people:
            if created >= budget:
                break
            eid = str(person["entity_id"])
            names = [person["name"]] + list(person["aliases"] or [])
            keys = [n.strip().lower() for n in names if n and n.strip()]
            roles = []
            if role_type:
                roles = [dict(r) for r in await self.db.fetch(
                    """
                    SELECT cp.patch_id, cp.value->>'text' AS text, cp.origin_id,
                           cp.created_at AS stated_at,
                           COALESCE(pr.name, cp.project) AS project
                    FROM context_patches cp
                    JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                    LEFT JOIN projects pr ON pr.project_id = cp.project_id
                    WHERE ps.subject_key = $1 AND cp.patch_type = $2
                      AND COALESCE(cp.status, 'active') = 'active'
                      AND LOWER(cp.value->>'text') LIKE ANY($3::text[])
                    ORDER BY cp.created_at DESC
                    LIMIT 8
                    """,
                    subject_key, role_type, [k + "%" for k in keys],
                )]
            percs = [dict(r) for r in await self.db.fetch(
                """
                SELECT description, first_origin_id, observation_count,
                       first_observed_at, last_observed_at
                FROM entity_descriptions
                WHERE user_id = $1 AND entity_id = $2::uuid
                ORDER BY first_observed_at DESC
                LIMIT 20
                """,
                user_id, eid,
            )]
            if not who_they_are.eligible(roles, percs):
                continue
            facts = who_they_are.build_facts(
                person["name"], roles, percs,
                int(person["meetings"] or 0), person["first_seen_at"],
                person["last_seen_at"], list(person["projects"] or []),
            )
            # Fingerprint gate: the standing card for this person, if any.
            existing = await self.db.fetch(
                """
                SELECT d.patch_id, COALESCE(d.status, 'active') AS status,
                       d.value->'facts'->>'fingerprint' AS fp
                FROM context_patches d
                JOIN patch_subjects dps ON dps.patch_id = d.patch_id
                WHERE dps.subject_key = $1 AND d.origin_mode = 'derived'
                  AND d.value->>'lens' = $2 AND d.value->>'source_entity_id' = $3
                  AND COALESCE(d.value->>'archive_cause', '') <> 'replaced'
                """,
                subject_key, who_they_are.LENS, eid,
            )
            # A user's no (archived, not by replacement) is a decision and
            # blocks regeneration outright, same as every other lens.
            if any(r["status"] != "active" for r in existing):
                continue
            if any(r["fp"] == facts["fingerprint"] for r in existing):
                continue
            used_openings = [
                who_they_are_opening(r["text"]) for r in await self.db.fetch(
                    """
                    SELECT cp.value->>'text' AS text FROM context_patches cp
                    JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                    WHERE ps.subject_key = $1 AND cp.origin_mode = 'derived'
                      AND cp.value->>'lens' = $2
                      AND COALESCE(cp.status, 'active') = 'active'
                      AND COALESCE(cp.value->>'source_entity_id', '') <> $3
                    ORDER BY cp.created_at DESC LIMIT 12
                    """,
                    subject_key, who_they_are.LENS, eid,
                ) if r["text"]
            ]
            base_content = who_they_are.build_content(facts, used_openings)
            content = base_content
            defects: list = []
            parsed = None
            response = None
            # ONE bounded retry, and only when the correction changes the
            # prompt (same discipline as the stands_out lens). The first
            # prod cycle (2026-08-21 23:09Z) rejected 5 of 5: four over
            # length, one stated role "dropped" because the role text
            # ended in a period; and the raw answers showed Sonnet
            # opening with the name despite the instruction. A retry
            # that names the defect turns most of those into cards.
            for attempt in range(2):
                try:
                    response = await self.llm.extract(
                        system_prompt=who_they_are.SYSTEM,
                        user_content=content, model=model,
                    )
                    parsed = who_they_are.parse_response(response.content, facts, defects)
                except Exception as exc:
                    logger.warning("who_they_are_failed", subject=subject_key,
                                   person=person["name"], reason=str(exc)[:200])
                    parsed = None
                    break
                if parsed or attempt:
                    break
                defect = defects[0] if defects else ""
                if defect.split(":")[0] not in who_they_are.RETRYABLE:
                    break
                note = who_they_are.retry_note(
                    defect, facts, who_they_are.summary_chars(response.content))
                if not note:
                    break
                content = f"{base_content}\n\n{note}"
                defects = []
            if not parsed:
                if response is None:
                    continue
                logger.info("who_they_are_rejected", subject=subject_key,
                            person=person["name"],
                            defect=defects[0] if defects else "declined",
                            summary_chars=who_they_are.summary_chars(response.content),
                            raw=(json.dumps(response.content) if isinstance(response.content, dict)
                                 else str(response.content or ""))[:700])
                continue
            # Replace the prior card: a synthesis supersedes, it does not
            # accumulate, and the cause says so, so it never reads as a
            # user's no later.
            for r in existing:
                await self.db.execute(
                    """
                    UPDATE context_patches
                    SET status = 'archived', updated_at = NOW(),
                        value = jsonb_set(value, '{archive_cause}', '"replaced"'::jsonb)
                    WHERE patch_id = $1
                    """,
                    r["patch_id"],
                )
            source_ids = [r["patch_id"] for r in roles if r.get("patch_id")]
            await self._write_person_insight(
                subject_key, app_id, rule, "", person["name"],
                [str(x) for x in source_ids], who_they_are.LENS,
                parsed["summary"], parsed["trajectory"] or "",
                facts=facts, entity_id=eid,
                extra={
                    "trajectory": parsed["trajectory"],
                    "sources": parsed["sources"],
                    "generated_at": datetime.utcnow().isoformat(),
                    "model": getattr(response, "model", None) or model,
                    "output_language": parsed.get("output_language"),
                },
            )
            created += 1
            logger.info("who_they_are_created", subject=subject_key,
                        person=person["name"], roles=len(roles),
                        perceptions=len(percs), model=model,
                        replaced=len(existing))
        return created

    async def _derive_trajectory(
        self, subject_key: str, app_id: str, rule: dict, budget: int
    ) -> int:
        """The 5.15 hero card: on what measure is this person unlike
        THEMSELVES earlier. Windows split by MEETING SEQUENCE (never by
        elapsed time; CQ holds no meeting dates), gates and the pick in
        services/trajectory.py, the model writes and may state only
        numbers it was handed.

        TWO measures are wired, not three. `questions_to_you` is in the
        MEASURES map and is deliberately not constructible: migration 37
        attributes a question the user RECEIVES to the user block in
        aggregate, never to the asker's row, so "questions they addressed
        to you" per person does not exist in storage. Building it from
        `questions_asked` (any addressee) would be a different claim
        wearing this one's label. Same reason working_with.your_half is
        not served: the user has no appearance rows, so your-side turns
        and questions have no source. Both need capture-side columns and
        can never be backfilled; recorded in 5.15.
        """
        if budget <= 0:
            return 0
        if os.getenv("CQ_TRAJECTORY_ENABLED", "true").lower() in ("0", "false", "no"):
            return 0
        model = os.getenv("CQ_TRAJECTORY_MODEL") or who_they_are.DEFAULT_MODEL
        vocab = people_vocabulary(await self._app_manifest(app_id))
        completables = (await get_type_runtime(self.db.fetch)).completable_types
        user_id = subject_key.split(":", 1)[1] if ":" in subject_key else subject_key
        resolve_person_entity = await self._person_entity_resolver(user_id)
        try:
            people = await self.db.fetch(
                """
                SELECT e.entity_id, e.name,
                       (SELECT count(DISTINCT pa.origin_id) FROM person_appearances pa
                         WHERE pa.entity_id = e.entity_id) AS meetings
                FROM entities e
                WHERE e.user_id = $1 AND e.entity_type = $2
                  AND e.merged_into IS NULL AND e.suppressed_at IS NULL
                  AND e.self_at IS NULL
                ORDER BY e.last_seen_at DESC NULLS LAST
                LIMIT 200
                """,
                user_id, vocab.person_entity_type,
            )
        except Exception as exc:
            logger.debug("trajectory_roster_unavailable", error=str(exc)[:140])
            return 0
        # STARVATION IS SAID OUT LOUD, ruled 2026-08-27. This pass used to
        # be handed the remainder of a shared budget and could be given
        # zero, in which case it returned on its first person having
        # logged nothing at all. Three days of no cards looked exactly
        # like three days of nobody qualifying, and the only reason the
        # difference was ever found was somebody asking where one card
        # had gone. An absence is evidence only if the contradicting
        # result had a channel to arrive through (doc 19.10), so this is
        # that channel. It logs at INFO, not debug: a pass that cannot
        # run is not a detail.
        if budget <= 0:
            logger.info("trajectory_budget_exhausted", subject=subject_key,
                        budget=budget, created=0, people_unexamined=len(people),
                        reason="no_budget_on_entry")
            return 0
        created = 0
        for index, person in enumerate(people):
            if created >= budget:
                # The count is what was OBSERVED, never a claim that the
                # remainder would have produced cards: most of a roster
                # never qualifies, and this line must not read as a
                # backlog it has not measured.
                logger.info("trajectory_budget_exhausted", subject=subject_key,
                            budget=budget, created=created,
                            people_unexamined=len(people) - index,
                            reason="budget_reached")
                break
            if int(person["meetings"] or 0) < trajectory_svc.MIN_SPAN_MEETINGS:
                continue
            eid = str(person["entity_id"])
            rows = await self.db.fetch(
                """
                SELECT pa.origin_id, pa.turn_count,
                       max(pa.last_seen_at) AS seen
                FROM person_appearances pa
                WHERE pa.user_id = $1 AND pa.entity_id = $2::uuid
                  AND pa.origin_id IS NOT NULL
                GROUP BY pa.origin_id, pa.turn_count
                ORDER BY max(pa.last_seen_at) DESC
                """,
                user_id, eid,
            )
            # One row per meeting, newest first; a meeting with several
            # rows keeps the highest turn count (the merge rule the
            # upsert itself uses).
            per_meeting: dict = {}
            newest_first: list = []
            for r in rows:
                oid = str(r["origin_id"])
                if oid not in per_meeting:
                    per_meeting[oid] = r["turn_count"]
                    newest_first.append(oid)
                elif r["turn_count"] is not None:
                    prev = per_meeting[oid]
                    per_meeting[oid] = r["turn_count"] if prev is None else max(prev, r["turn_count"])
            split = trajectory_svc.split_meetings(newest_first)
            if not split:
                continue
            earlier_ids, recent_ids = split
            span_ids = list(earlier_ids) + list(recent_ids)

            def turn_window(ids):
                counted = [oid for oid in ids if per_meeting.get(oid) is not None]
                total = sum(int(per_meeting[oid]) for oid in counted)
                return trajectory_svc.Window(total, len(counted), ids)

            # closed_late: dated items this person owns from the span's
            # meetings that CLOSED, late = closed after the date. Owner
            # resolved through the entity graph, the identity lesson of
            # the insight rework (owner text alone missed 97 of 110).
            items = await self.db.fetch(
                f"""
                SELECT cp.patch_id, cp.origin_id, cp.value->>'owner' AS owner,
                       (cp.value->>'deadline_date')::date AS due,
                       cp.completed_at::date AS closed
                FROM context_patches cp
                JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                WHERE ps.subject_key = $1
                  AND cp.patch_type = ANY($2::text[])
                  AND cp.origin_id = ANY($3::text[])
                  AND cp.value->>'deadline_date' ~ '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                  AND cp.completed_at IS NOT NULL
                """,
                subject_key, list(completables), span_ids,
            )
            owned: dict = {}
            for it in items:
                who = resolve_person_entity(it["owner"] or "")
                if who != eid:
                    continue
                oid = str(it["origin_id"])
                den, num, pids = owned.get(oid, (0, 0, []))
                late = 1 if (it["closed"] and it["due"] and it["closed"] > it["due"]) else 0
                pids = pids + [str(it["patch_id"])]
                owned[oid] = (den + 1, num + late, pids)

            def closed_window(ids):
                den = sum(owned.get(oid, (0, 0, []))[0] for oid in ids)
                num = sum(owned.get(oid, (0, 0, []))[1] for oid in ids)
                receipts = [oid for oid in ids if oid in owned]
                return trajectory_svc.Window(num, den, receipts)

            windows = {
                "speaking_turns": (turn_window(earlier_ids), turn_window(recent_ids)),
                "closed_late": (closed_window(earlier_ids), closed_window(recent_ids)),
            }
            # The live card, if any, BEFORE the gates run: its measure is
            # judged against the hold floors (hysteresis, Scott's ruling
            # 2026-08-26), and if nothing qualifies any more it is
            # archived rather than left standing on numbers that are no
            # longer true. Same rows feed the durable-no and fingerprint
            # checks below.
            existing = await self.db.fetch(
                """
                SELECT d.patch_id, COALESCE(d.status, 'active') AS status,
                       d.value->'facts'->>'fingerprint' AS fp,
                       d.value->'facts'->>'measure_key' AS measure_key
                FROM context_patches d
                JOIN patch_subjects dps ON dps.patch_id = d.patch_id
                WHERE dps.subject_key = $1 AND d.origin_mode = 'derived'
                  AND d.value->>'lens' = $2 AND d.value->>'source_entity_id' = $3
                  AND COALESCE(d.value->>'archive_cause', '') NOT IN ('replaced', 'lapsed')
                """,
                subject_key, trajectory_svc.LENS, eid,
            )
            live = [r for r in existing if r["status"] == "active"]
            held_key = live[0]["measure_key"] if live else None
            chosen = trajectory_svc.best_change(windows, held_key=held_key)
            if not chosen:
                for r in live:
                    # LAPSED, not replaced: nothing succeeds it. The card
                    # said something the arithmetic no longer supports
                    # even at the hold floor, so it comes down. A lapsed
                    # card is not a durable no; the person can earn a
                    # new one at the entry floor.
                    await self.db.execute(
                        """
                        UPDATE context_patches
                        SET status = 'archived', updated_at = NOW(),
                            value = jsonb_set(value, '{archive_cause}', '"lapsed"'::jsonb)
                        WHERE patch_id = $1
                        """,
                        r["patch_id"],
                    )
                    logger.info("trajectory_lapsed", subject=subject_key,
                                person=person["name"], measure=r["measure_key"])
                continue
            key = chosen["measure_key"]
            buckets = []
            for oid in span_ids:
                if key == "speaking_turns":
                    tc = per_meeting.get(oid)
                    buckets.append({"origin_id": oid,
                                    "numerator": int(tc or 0),
                                    "denominator": 1 if tc is not None else 0})
                else:
                    den, num, _ = owned.get(oid, (0, 0, []))
                    buckets.append({"origin_id": oid, "numerator": num, "denominator": den})
            facts = trajectory_svc.served_trajectory(
                chosen, person["name"], series=buckets, supersedes=[],
            )
            fingerprint = hashlib.sha256(
                ("|".join(recent_ids) + "::" + json.dumps(
                    [facts["earlier"], facts["recent"], key], sort_keys=True
                )).encode()
            ).hexdigest()[:16]
            facts["fingerprint"] = fingerprint
            if any(r["status"] != "active" for r in existing):
                continue  # the durable no, this lens only
            if any(r["fp"] == fingerprint for r in existing):
                continue  # inputs unchanged; the person costs nothing
            content = trajectory_svc.build_trajectory_content(person["name"], facts)
            permitted = trajectory_svc.allowed_numbers(facts)
            defects: list = []
            parsed = None
            response = None
            for attempt in range(2):
                try:
                    response = await self.llm.extract(
                        system_prompt=trajectory_svc.TRAJECTORY_SYSTEM,
                        user_content=content, model=model,
                    )
                    parsed = trajectory_svc.parse_trajectory_response(
                        response.content, permitted=permitted,
                        person_name=person["name"], defects=defects, facts=facts,
                    )
                except Exception as exc:
                    logger.warning("trajectory_failed", subject=subject_key,
                                   person=person["name"], reason=str(exc)[:200])
                    parsed = None
                    break
                if parsed or attempt:
                    break
                note = trajectory_svc.retry_note(
                    defects[0] if defects else "",
                    (response.content or {}).get("text", "") if isinstance(response.content, dict) else "",
                )
                if not note:
                    break
                content = content + "\n\n" + note
                defects = []
            if not parsed:
                if response is not None:
                    logger.info("trajectory_rejected", subject=subject_key,
                                person=person["name"], measure=key,
                                defect=defects[0] if defects else "declined")
                continue
            for r in existing:
                await self.db.execute(
                    """
                    UPDATE context_patches
                    SET status = 'archived', updated_at = NOW(),
                        value = jsonb_set(value, '{archive_cause}', '"replaced"'::jsonb)
                    WHERE patch_id = $1
                    """,
                    r["patch_id"],
                )
            source_ids = sorted({p for oid in span_ids for p in owned.get(oid, (0, 0, []))[2]})[:20] if key == "closed_late" else []
            await self._write_person_insight(
                subject_key, app_id, rule, "", person["name"],
                source_ids, trajectory_svc.LENS,
                parsed["text"], parsed["do"],
                facts=facts, entity_id=eid,
                extra={
                    "narrative": parsed.get("narrative") or "",
                    "display_order": trajectory_svc.DISPLAY_ORDER,
                    "generated_at": datetime.utcnow().isoformat(),
                    "model": getattr(response, "model", None) or model,
                },
            )
            created += 1
            logger.info("trajectory_created", subject=subject_key,
                        person=person["name"], measure=key,
                        direction=chosen["direction"], model=model,
                        replaced=len(existing))
        return created

    async def _person_entity_resolver(self, user_id: str):
        """Surface form -> canonical entity id, for this user.

        The same resolution the quilt read uses to stamp
        `owner_entity_id`: exact name, then recorded alias, then
        merged_into, and NO heuristic leg. Built once per user per pass
        rather than per cluster, and degrades to "resolves nothing" on a
        database without the alias table so a profile pass can never
        crash on an identity lookup.
        """
        try:
            entity_rows = await self.db.fetch(
                "SELECT entity_id, name, merged_into FROM entities "
                "WHERE user_id = $1 AND entity_type = 'person'",
                user_id,
            )
            alias_rows = await self.db.fetch(
                "SELECT entity_id, alias FROM entity_aliases WHERE user_id = $1",
                user_id,
            )
        except Exception as exc:
            logger.warning("person_entity_resolver_unavailable",
                           user=user_id, reason=str(exc)[:200])
            return lambda _surface: None
        return build_entity_resolver(
            [dict(r) for r in entity_rows], [dict(r) for r in alias_rows]
        )

    async def _consolidate_user_people(
        self, subject_key: str, app_id: str, rule: dict, budget: int
    ) -> int:
        """The profile pass (16a / 12a): person-keyed clusters.

        A cluster is (user, app, person patch) whose owns-edge items of
        rule.from_types span >= min_meetings DISTINCT meetings (the
        receipts gate) with >= min_patches members. Three deliberate
        differences from the cue pass:

        - The idempotency check ignores status, PER LENS: a user-deleted
          insight (hold-to-suppress rides DELETE /patches, archive_cause
          user_delete) is a durable no for the lens it carried, never
          re-derived. It is not a no for the person's other lenses,
          which is what lets 16a stack more than one card. The SQL gate
          counts DISTINCT lens stamps rather than naming one, because
          the model picks the lens AFTER the call; the authoritative
          per-lens refusal is the post-check in
          `_synthesize_person_cluster`.
        - The SELF person is excluded: 16a's lenses are about the
          counterparty; insights about the user belong to the self-typed
          machinery.
        - Sources must carry origin_ids, because the receipts ARE the
          meetings.

        A person missing both lenses gains at most one per cycle (one
        cluster row, one call), so the second card lands on the next
        consolidation pass rather than in the same one.
        """
        if budget <= 0:
            return 0
        vocab = people_vocabulary(await self._app_manifest(app_id))
        user_id = subject_key.split(":", 1)[1] if ":" in subject_key else subject_key
        try:
            self_name = await self.db.fetchval(
                "SELECT name FROM entities WHERE user_id = $1 AND self_at IS NOT NULL",
                user_id,
            )
        except Exception:
            self_name = None  # pre-migration-35 DB: no exclusion basis
        resolve_person_entity = await self._person_entity_resolver(user_id)
        clusters = await self.db.fetch(
            f"""
            SELECT op.patch_id AS person_patch_id,
                   op.value->>'text' AS person_name,
                   op.created_at AS created_at,
                   array_agg(DISTINCT cp.patch_id) AS patch_ids,
                   count(DISTINCT cp.origin_id) AS meeting_count
            FROM patch_connections pc
            JOIN context_patches op ON op.patch_id = pc.from_patch_id
            JOIN context_patches cp ON cp.patch_id = pc.to_patch_id
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            JOIN context_patch_acl acl ON acl.patch_id = cp.patch_id
            WHERE ps.subject_key = $1
              AND acl.app_id = $2::uuid
              AND pc.connection_label = $7
              AND COALESCE(pc.status, 'active') = 'active'
              AND op.patch_type = $8
              AND COALESCE(op.status, 'active') = 'active'
              AND cp.patch_type = ANY($3::text[])
              AND COALESCE(cp.status, 'active') = 'active'
              AND cp.origin_id IS NOT NULL
              AND cp.created_at > NOW() - INTERVAL '{CLUSTER_WINDOW_DAYS} days'
              AND ($9::text IS NULL
                   OR lower(btrim(op.value->>'text')) <> lower(btrim($9)))
              -- durable-no idempotency, PER LENS: every lens already
              -- stamped for this person counts, INCLUDING archived
              -- (user-deleted) ones, so a suppressed lens stays
              -- suppressed. The person leaves the candidate set only
              -- once the stamps cover this call's lens vocabulary.
              --
              -- MODEL-CHOSEN lenses only. A computed lens is derived by
              -- its own pass and can never come out of this call, so
              -- counting its stamp here would hold a person in the
              -- candidate set for a lens this query cannot produce, and
              -- burn a cluster slot every cycle to decline it.
              AND (
                  SELECT count(DISTINCT d.value->>'lens')
                  FROM context_patches d
                  JOIN patch_subjects dps ON dps.patch_id = d.patch_id
                  WHERE dps.subject_key = $1
                    AND d.patch_type = $4
                    AND d.origin_mode = 'derived'
                    AND d.value->>'lens' = ANY($12::text[])
                    AND d.value->>'source_person' = op.patch_id::text
                    AND COALESCE(d.value->>'archive_cause', '') <> 'retracted'
              ) < $11
            GROUP BY op.patch_id, op.value->>'text', op.created_at
            HAVING count(DISTINCT cp.patch_id) >= $5
               AND count(DISTINCT cp.origin_id) >= $6
            ORDER BY count(DISTINCT cp.origin_id) DESC, op.patch_id ASC
            LIMIT $10
            """,
            subject_key, app_id, rule["from_types"], rule["produce_type"],
            rule["min_patches"], rule["min_meetings"],
            vocab.ownership_label, vocab.person_type, self_name,
            budget * CLUSTER_OVERFETCH,
            len(MODEL_CHOSEN_LENSES), sorted(MODEL_CHOSEN_LENSES),
        )
        # One human, one cluster. The rows above are keyed on person
        # PATCH and the extractor mints one per surface form, so a
        # colleague arrives as several rows holding a fraction of their
        # record each. Merge before spending an LLM call on any of them.
        #
        # The SQL lens gate stays per-patch and is deliberately the
        # PERMISSIVE side of this: a second surface form carries no lens
        # stamps of its own, so it always passes and the fully-covered
        # human is dropped below instead, once the stamps of every form
        # can be read together. That is also why the query over-fetches:
        # merging only ever removes rows, and the budget must be spent
        # on humans rather than on forms.
        merged = merge_person_clusters(
            [dict(c) for c in clusters], resolve_person_entity
        )
        created = 0
        for cluster in merged:
            if created >= budget:
                break
            person_patch_ids = cluster["person_patch_ids"]
            taken = await self._taken_lenses(
                subject_key, rule["produce_type"], person_patch_ids
            )
            if not remaining_lenses(taken):
                continue
            made = await self._synthesize_person_cluster(
                subject_key, app_id, rule,
                cluster["person_patch_id"], cluster["person_name"],
                cluster["patch_ids"],
                person_patch_ids=person_patch_ids,
                entity_id=cluster["entity_id"],
            )
            if made:
                created += 1
        return created

    async def _taken_lenses(
        self, subject_key: str, produce_type: str,
        person_patch_ids: "str | list",
    ) -> set:
        """Every lens already stamped for this person, in ANY status.

        No status predicate, deliberately: an archived (user-deleted)
        insight is the record of a lens the user said no to, and reading
        only active rows would re-derive the card they suppressed.

        Takes every one of the person's surface-form patch ids, not one.
        A card stamps `source_person` with whichever form was current
        when it was derived, so reading a single form lets a suppressed
        lens come back under a different spelling of the same human, and
        lets a second card be derived for a lens they already carry.
        A bare string is still accepted so existing callers do not have
        to know about the fan-out.

        A card CQ RETRACTED is not a user's no, and is skipped here for
        the same reason the contrastive lens skips it (#255): a rule or
        prompt change withdraws cards CQ would no longer write, and
        reading those as suppressions bans the person from that lens
        forever over a claim they never saw. Learned the hard way an hour
        after building the distinction and applying it to one lens only:
        six prose cards were retracted to clear repeated openings and the
        next cycle created NOTHING, because this check still counted
        them. `user_delete` and every other cause still block.
        """
        ids = (
            [person_patch_ids]
            if isinstance(person_patch_ids, str)
            else [str(p) for p in person_patch_ids or ()]
        )
        if not ids:
            return set()
        rows = await self.db.fetch(
            """
            SELECT DISTINCT d.value->>'lens' AS lens
            FROM context_patches d
            JOIN patch_subjects dps ON dps.patch_id = d.patch_id
            WHERE dps.subject_key = $1
              AND d.patch_type = $2
              AND d.origin_mode = 'derived'
              AND d.value->>'source_person' = ANY($3::text[])
              AND COALESCE(d.value->>'archive_cause', '') <> 'retracted'
            """,
            subject_key, produce_type, ids,
        )
        return {r["lens"] for r in rows if r["lens"]}

    async def _synthesize_person_cluster(
        self, subject_key: str, app_id: str, rule: dict,
        person_patch_id: str, person_name: str, source_patch_ids: list,
        person_patch_ids: "list | None" = None,
        entity_id: "str | None" = None,
    ) -> bool:
        """One profile call + provenance write. Two invariants are
        re-checked here against fetched state (sanitizer-style second
        layer): the claim never ships on fewer distinct meetings than
        the rule demands, whatever the cluster query said, and it never
        ships on a lens this person already carries in any status.

        The lens check has to live here because the lens does not exist
        until the model answers. The prompt is told which lenses are
        taken, but a prompt is a hint and this is an invariant, so the
        answer is checked on the way in and a repeat lens is declined
        without a write."""
        # Every surface form of this human, so the durable no is read
        # across all of them. Falls back to the primary alone for callers
        # that predate the merge (the computed-lens pass, and tests).
        lens_key_ids = [str(p) for p in (person_patch_ids or [person_patch_id])]
        rows = await self.db.fetch(
            """
            SELECT value->>'text' AS text, origin_id, created_at
            FROM context_patches
            WHERE patch_id = ANY($1::uuid[])
            ORDER BY created_at ASC
            """,
            source_patch_ids,
        )
        dated = [
            (r["created_at"].date().isoformat(), r["text"])
            for r in rows if r["text"]
        ]
        distinct_origins = {r["origin_id"] for r in rows if r["origin_id"]}
        if len(dated) < rule["min_patches"] or len(distinct_origins) < rule["min_meetings"]:
            return False
        taken_lenses = await self._taken_lenses(
            subject_key, rule["produce_type"], lens_key_ids
        )
        if not remaining_lenses(taken_lenses):
            return False
        # What has already been said about this user's OTHER people on
        # the prose lenses. Spans BOTH of them deliberately: this pass
        # does not know which lens it will produce until the model
        # answers, and the convergence measured in production crossed
        # the lens boundary anyway ("Responds to" opened five cards,
        # "Gates forward" three, across both).
        used_claims = [
            r["text"] for r in await self.db.fetch(
                """
                SELECT DISTINCT cp.value->>'text' AS text
                FROM context_patches cp
                JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                WHERE ps.subject_key = $1
                  AND cp.origin_mode = 'derived'
                  AND cp.value->>'lens' = ANY($2::text[])
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND NOT (cp.value->>'source_person' = ANY($3::text[]))
                ORDER BY 1
                LIMIT 12
                """,
                subject_key, sorted(MODEL_CHOSEN_LENSES), lens_key_ids,
            ) if r["text"]
        ]
        base_content = build_profile_content(
            person_name, dated, rule.get("guidance"),
            taken_lenses=taken_lenses, used_claims=used_claims,
        )
        defects: list = []
        profile = None
        content = base_content
        # One bounded retry on a recoverable defect, same reasoning as
        # the contrastive lens: a corrective changes the PROMPT, so the
        # second attempt is a different question rather than another
        # roll of a pinned temperature.
        for attempt in range(2):
            try:
                response = await self.llm.extract(
                    system_prompt=PROFILE_SYSTEM, user_content=content,
                )
                profile = parse_profile_response(
                    response.content, person_name=person_name,
                    defects=defects, used_claims=used_claims,
                )
            except Exception as exc:
                logger.warning("profile_synthesis_failed",
                               subject=subject_key, person=person_name,
                               reason=str(exc)[:200])
                return False
            if profile or attempt:
                break
            note = relationship_lenses.retry_note(
                defects[0] if defects else "",
                relationship_lenses.rejected_lengths(
                    response.content).get("claim") or "",
            )
            if not note:
                break
            content = f"{base_content}\n\n{note}"
            defects = []
        if not profile:
            # A rejected FORMAT is not a model declining, it is a model
            # answering in a shape the card cannot hold, and a run of
            # them would silently stop the pass. Different level, so the
            # difference is visible without reading every debug line.
            if defects:
                logger.info("profile_card_rejected", subject=subject_key,
                            person=person_name, defect=defects[0])
            else:
                logger.debug("profile_declined", subject=subject_key,
                             person=person_name)
            return False

        # The post-check. Re-read rather than trusting the pre-call set:
        # the LLM call is seconds of wall clock, and this is the one
        # place the per-lens durable no is actually enforced.
        taken_lenses = await self._taken_lenses(
            subject_key, rule["produce_type"], lens_key_ids
        )
        if profile["lens"] in taken_lenses:
            logger.debug(
                "profile_lens_already_taken",
                subject=subject_key, person=person_name, lens=profile["lens"],
            )
            return False

        await self._write_person_insight(
            subject_key, app_id, rule, person_patch_id, person_name,
            source_patch_ids, profile["lens"], profile["text"], profile["do"],
            entity_id=entity_id,
        )
        logger.info(
            "profile_insight_created",
            subject=subject_key, person=person_name,
            lens=profile["lens"], sources=len(source_patch_ids),
            meetings=len(distinct_origins),
        )
        return True

    async def _write_person_insight(
        self, subject_key: str, app_id: str, rule: dict,
        person_patch_id: str, person_name: str, source_patch_ids: list,
        lens: str, text: str, do: str, facts: Optional[dict] = None,
        entity_id: Optional[str] = None, extra: Optional[dict] = None,
    ) -> str:
        """The provenance-carrying write, shared by every lens pass.

        One write path on purpose: the model-chosen lenses and the
        computed one differ in how they reach a claim and in nothing
        else. Both are derived patches with source_patch_ids, an
        `informs` edge from every source, and a lens stamp, because the
        durable no, the receipts read and the decay band all key on
        those and would drift the moment a second writer existed.

        `facts` is the computed lens's arithmetic, stored beside the
        claim so the numbers behind a sentence stay auditable after the
        fact, and null for a lens that has none.
        """
        patch_id = str(uuid.uuid4())
        now = datetime.utcnow()
        value: dict = {
            "text": text,
            "do": do,
            "lens": lens,
            "source_person": person_patch_id,
            "about_person": person_name,
        }
        # Where this card sits in the stack. ShoulderSurf sorts by
        # whether a lens is NAMED rather than against a fixed list, so a
        # lens this build considers primary would otherwise take an
        # arbitrary position. An order that carries meaning has to ship
        # as a field. Absent means "after the ordered ones", which is
        # what every lens predating this does.
        if lens == relationship_lenses.LENS:
            value["display_order"] = relationship_lenses.DISPLAY_ORDER
        if entity_id:
            # The identity that does not move. `source_person` is a
            # PATCH id and which patch wins is an accident of extraction
            # history, which is what made a person's own page fail to
            # find their own cards (#249). Stamped alongside rather than
            # instead of, so nothing already keyed on source_person
            # changes shape.
            value["source_entity_id"] = str(entity_id)
        if facts:
            value["facts"] = facts
        if extra:
            # Lens-specific fields (who_they_are: trajectory, sources,
            # generated_at, model). Merged last so a lens cannot
            # overwrite the provenance keys above by accident.
            for k, v in extra.items():
                value.setdefault(k, v)
        value_json = json.dumps(value)
        async with self.db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO context_patches (
                        patch_id, patch_name, patch_type, value,
                        origin_mode, source_prompt, confidence, persistence,
                        status, created_at, updated_at, last_observed_at,
                        source_patch_ids
                    ) VALUES ($1, $2, $3, $4, 'derived', 'profile_pass', 0.7,
                              $5, 'active', $6, $6, $6, $7)
                    """,
                    patch_id, f"profile_{patch_id[:8]}",
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
                        VALUES ($1::uuid, $2::uuid, 'informs', 'consolidated_into', 'profile source')
                        ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO NOTHING
                        """,
                        src, patch_id,
                    )
        return patch_id

    async def _derive_follow_through(
        self, subject_key: str, app_id: str, rule: dict, budget: int
    ) -> int:
        """The computed lens (16a lens 3): a delivery record, then a call.

        Everything that decides the verdict happens before the model is
        involved. For each candidate person the pass fetches the items
        they own that carried a due date which has come due, computes on
        time / late / still open / due date moves in
        services/follow_through.py, and declines IN CODE when the counts
        are too thin. Only then does a call happen, and its only job is
        writing the numbers up in the house voice.

        That ordering is the point. The model-chosen lenses decline on
        this corpus because they are asked to find interaction style in
        records of task assignment; arithmetic over the same records
        cannot decline, and what it produces is more useful before a
        meeting anyway.

        Two gates hold the line, same shape as the other pass:
        min_meetings distinct meetings behind the counts (the receipts
        invariant) and MIN_JUDGED_ITEMS judged items. The durable no is
        the same machinery too: a stamp for this lens in ANY status
        removes the person from the candidate set forever.
        """
        if budget <= 0:
            return 0
        # Retired: the card it produced duplicated OPEN LOOPS, and
        # what_stands_out computes the same closure fact with the roster
        # comparison attached. Existing cards keep rendering until
        # something regenerates them; only new derivation stops here.
        if FOLLOW_THROUGH_LENS in RETIRED_LENSES:
            return 0
        vocab = people_vocabulary(await self._app_manifest(app_id))
        # Which of the rule's types can even carry a due date is the
        # facet runtime's answer, not a hardcoded pair of SS type names:
        # a completable is a completable because a manifest said so.
        completables = (
            await get_type_runtime(self.db.fetch)
        ).completable_types
        source_types = [t for t in rule["from_types"] if t in completables]
        if not source_types:
            return 0
        user_id = subject_key.split(":", 1)[1] if ":" in subject_key else subject_key
        try:
            self_name = await self.db.fetchval(
                "SELECT name FROM entities WHERE user_id = $1 AND self_at IS NOT NULL",
                user_id,
            )
        except Exception:
            self_name = None  # pre-migration-35 DB: no exclusion basis
        resolve_person_entity = await self._person_entity_resolver(user_id)
        # The candidate query counts the judged set the same way
        # follow_through.judge_item does, so the SQL gate and the Python
        # gate agree; the Python one is still the authority, sanitizer
        # style, and can only ever shrink the set (an unparseable date,
        # a row the fetch no longer sees).
        judged_sql = """
            cp.value->>'deadline_date' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
            AND cp.origin_id IS NOT NULL
            AND (
                cp.completed_at IS NOT NULL
                OR (COALESCE(cp.status, 'active') = 'active'
                    AND cp.value->>'shelved_at' IS NULL
                    AND (cp.value->>'deadline_date')::date
                        < (NOW() AT TIME ZONE 'utc')::date)
            )
        """
        candidates = await self.db.fetch(
            f"""
            SELECT op.patch_id AS person_patch_id,
                   op.value->>'text' AS person_name
            FROM patch_connections pc
            JOIN context_patches op ON op.patch_id = pc.from_patch_id
            JOIN context_patches cp ON cp.patch_id = pc.to_patch_id
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            JOIN context_patch_acl acl ON acl.patch_id = cp.patch_id
            WHERE ps.subject_key = $1
              AND acl.app_id = $2::uuid
              AND pc.connection_label = $6
              AND COALESCE(pc.status, 'active') = 'active'
              AND op.patch_type = $7
              AND COALESCE(op.status, 'active') = 'active'
              AND cp.patch_type = ANY($3::text[])
              AND cp.created_at > NOW() - INTERVAL '{CLUSTER_WINDOW_DAYS} days'
              AND {judged_sql}
              AND ($8::text IS NULL
                   OR lower(btrim(op.value->>'text')) <> lower(btrim($8)))
              -- The durable no, this lens only. Status-blind, like the
              -- model pass: an archived card is the record of a claim
              -- the user rejected and it never comes back.
              AND NOT EXISTS (
                  SELECT 1 FROM context_patches d
                  JOIN patch_subjects dps ON dps.patch_id = d.patch_id
                  WHERE dps.subject_key = $1
                    AND d.patch_type = $4
                    AND d.origin_mode = 'derived'
                    AND d.value->>'lens' = $9
                    AND d.value->>'source_person' = op.patch_id::text
                    AND COALESCE(d.value->>'archive_cause', '') <> 'retracted'
              )
            GROUP BY op.patch_id, op.value->>'text', op.created_at
            HAVING count(DISTINCT cp.patch_id) >= $10
               AND count(DISTINCT cp.origin_id) >= $5
            ORDER BY count(DISTINCT cp.patch_id) DESC, op.patch_id ASC
            LIMIT $11
            """,
            subject_key, app_id, source_types, rule["produce_type"],
            rule["min_meetings"], vocab.ownership_label, vocab.person_type,
            self_name, FOLLOW_THROUGH_LENS, MIN_JUDGED_ITEMS,
            budget * CLUSTER_OVERFETCH,
        )
        # Same merge as the model pass, and it matters MORE here. This
        # lens ships a count inside the claim, so a person split across
        # two surface forms did not merely get two cards, the arithmetic
        # on each was computed over a fraction of their items and the
        # served number was wrong. Merging is the correctness fix; the
        # de-duplication is a side effect.
        merged = merge_person_clusters(
            [dict(c) for c in candidates], resolve_person_entity
        )
        created = 0
        for candidate in merged:
            if created >= budget:
                break
            made = await self._derive_person_follow_through(
                subject_key, app_id, rule, source_types,
                vocab.ownership_label,
                candidate["person_patch_id"], candidate["person_name"],
                person_patch_ids=candidate["person_patch_ids"],
                entity_id=candidate["entity_id"],
            )
            if made:
                created += 1
        return created

    async def _derive_person_follow_through(
        self, subject_key: str, app_id: str, rule: dict, source_types: list,
        ownership_label: str, person_patch_id: str, person_name: str,
        person_patch_ids: "list | None" = None,
        entity_id: "str | None" = None,
    ) -> bool:
        """One person's delivery record: compute, gate, write it up."""
        owner_ids = [str(p) for p in (person_patch_ids or [person_patch_id])]
        rows = await self.db.fetch(
            f"""
            -- DISTINCT because a merged person is several patch ids and
            -- one item can carry an owns edge from more than one of
            -- them; without it the same item would be counted twice
            -- inside a served number.
            SELECT DISTINCT cp.patch_id, cp.origin_id, cp.completed_at,
                   COALESCE(cp.status, 'active') AS status,
                   cp.value->>'text' AS text,
                   cp.value->>'deadline_date' AS deadline_date,
                   cp.value->>'overdue_since' AS overdue_since,
                   cp.value->>'shelved_at' AS shelved_at,
                   jsonb_array_length(
                       COALESCE(cp.value->'deadline_history', '[]'::jsonb)
                   ) AS deadline_history
            FROM patch_connections pc
            JOIN context_patches cp ON cp.patch_id = pc.to_patch_id
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            WHERE ps.subject_key = $1
              AND pc.from_patch_id = ANY($2::uuid[])
              AND pc.connection_label = $3
              AND COALESCE(pc.status, 'active') = 'active'
              AND cp.patch_type = ANY($4::text[])
              AND cp.created_at > NOW() - INTERVAL '{CLUSTER_WINDOW_DAYS} days'
            """,
            subject_key, owner_ids, ownership_label, source_types,
        )
        summary = summarize_follow_through(
            [dict(r) for r in rows],
            today=datetime.utcnow().date(),
            min_items=MIN_JUDGED_ITEMS,
            min_meetings=rule["min_meetings"],
        )
        if summary is None:
            # The decline that costs nothing. No call was spent, because
            # the thing being judged was never a judgement call.
            logger.debug("follow_through_below_gate",
                         subject=subject_key, person=person_name)
            return False
        facts, items = summary["facts"], summary["items"]
        defects: list = []
        try:
            response = await self.llm.extract(
                system_prompt=FOLLOW_THROUGH_SYSTEM,
                user_content=build_follow_through_content(
                    person_name, facts,
                    # A spread across the record, not its oldest slice.
                    spread_sample(items, MAX_FACT_EXAMPLES),
                ),
            )
            claim = parse_follow_through_response(
                response.content, allowed_numbers(facts),
                person_name=person_name, defects=defects,
            )
        except Exception as exc:
            logger.warning("follow_through_failed", subject=subject_key,
                           person=person_name, reason=str(exc)[:200])
            return False
        if not claim:
            # This lens has the tightest brief (a real count inside the
            # claim ceiling), so a run of format rejections is the thing
            # most likely to stall it. It says which one, at a level that
            # shows up.
            if defects:
                logger.info("follow_through_card_rejected",
                            subject=subject_key, person=person_name,
                            defect=defects[0])
            else:
                logger.debug("follow_through_declined",
                             subject=subject_key, person=person_name)
            return False
        # The post-check, for the same reason the model pass has one: the
        # call is seconds of wall clock and the durable no is the thing
        # that must not lose a race with a suppression.
        if FOLLOW_THROUGH_LENS in await self._taken_lenses(
            subject_key, rule["produce_type"], owner_ids
        ):
            logger.debug("follow_through_lens_already_taken",
                         subject=subject_key, person=person_name)
            return False
        # The receipts ARE the counted items: every source id here is a
        # row the arithmetic actually counted, so tapping through from
        # the card lands on the meetings behind the number.
        await self._write_person_insight(
            subject_key, app_id, rule, person_patch_id, person_name,
            [i["patch_id"] for i in items],
            FOLLOW_THROUGH_LENS, claim["text"], claim["do"], facts=facts,
            entity_id=entity_id,
        )
        logger.info(
            "follow_through_insight_created",
            subject=subject_key, person=person_name,
            items=facts["judged_items"], meetings=facts["meetings"],
            on_time=facts["closed_on_time"], late=facts["closed_late"],
            open_past_due=facts["open_past_due"],
        )
        return True

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
                        ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO UPDATE SET
                        status = 'active'
                    WHERE patch_connections.status <> 'active'
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

        # Chat completion (contract item 10): close an open completable.
        # Immediate, same reasoning.
        if task_type == "completion":
            await self.handle_completion(payload)
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
                    ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO UPDATE SET
                        status = 'active'
                    WHERE patch_connections.status <> 'active'
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

    async def _app_manifest(self, app_id) -> dict | None:
        """Latest registered manifest for an app, or None. Cold path
        only; any failure (legacy non-UUID id, no schema) degrades to
        None = the SS floor everywhere this feeds."""
        try:
            app_uuid = str(uuid.UUID(str(app_id)))
        except (ValueError, AttributeError, TypeError):
            return None
        try:
            row = await self.db.fetchrow(
                "SELECT manifest FROM app_schemas WHERE app_id = $1::uuid "
                "ORDER BY version DESC LIMIT 1",
                app_uuid,
            )
        except Exception:
            return None
        if row is None:
            return None
        manifest = row["manifest"]
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except (ValueError, TypeError):
                return None
        return manifest if isinstance(manifest, dict) else None

    async def _correction_vocabulary(self, app_id) -> tuple[set, str]:
        """(allowed patch types, unmatched-correction fallback type) for
        this app. No manifest -> the SS floor (PATCH_TYPES, takeaway).
        A manifest without `correction_fallback_type` still falls back
        to takeaway even when off-manifest: a slightly alien type beats
        losing a user-stated fact, and the warning log names the gap so
        the app knows to declare the key."""
        manifest = await self._app_manifest(app_id)
        if manifest is None:
            return set(PATCH_TYPES), FALLBACK_PATCH_TYPE
        declared = {
            pt.get("domain_type") for pt in manifest.get("patch_types") or []
            if isinstance(pt, dict) and pt.get("domain_type")
        }
        if not declared:
            return set(PATCH_TYPES), FALLBACK_PATCH_TYPE
        fallback = manifest.get("correction_fallback_type")
        if not (isinstance(fallback, str) and fallback in declared):
            if fallback:
                logger.warning("correction_fallback_undeclared",
                               app_id=str(app_id), fallback=fallback)
            fallback = FALLBACK_PATCH_TYPE
            if FALLBACK_PATCH_TYPE not in declared:
                logger.warning("correction_fallback_off_manifest",
                               app_id=str(app_id))
        return declared, fallback

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
        allowed_types, fallback_type = await self._correction_vocabulary(app_id)
        try:
            response = await self.llm.extract(
                system_prompt=CORRECTION_SYSTEM,
                user_content=build_correction_content(
                    correction_text,
                    [{"patch_id": str(r["patch_id"]), "patch_type": r["patch_type"], "text": r["text"]}
                     for r in candidates],
                    today.isoformat(),
                    scope_label=project or project_id,
                    allowed_types=sorted(allowed_types),
                ),
            )
            parsed = parse_correction_response(
                response.content, set(by_id.keys()), meeting_date=today,
                allowed_types=allowed_types, fallback_type=fallback_type,
            )
        except Exception as exc:
            logger.error("correction_failed", user_id=user_id, reason=str(exc)[:200])
            return
        if not parsed:
            logger.warning("correction_unparseable", user_id=user_id,
                           correction=correction_text[:100])
            return
        matched_id, value = parsed
        new_type = value.pop("_new_type", fallback_type)

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
                                jsonb_set(
                                    jsonb_set(value, '{corrected_by}', to_jsonb($2::text)),
                                    '{correction_source}', '"user_chat"'
                                ),
                                '{archive_cause}', '"corrected"'
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
                        ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO UPDATE SET
                        status = 'active'
                    WHERE patch_connections.status <> 'active'
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

    async def handle_completion(self, payload: dict[str, Any]):
        """Chat completion (contract item 10): the user said something is
        done. Match against OPEN completables (in-block first, same
        candidates-with-ids pattern as corrections), then close via the
        EXISTING machinery — completed_at + completion_source='user_chat'
        + the user's sentence as evidence — so the patch flows the delta
        `completed` array exactly like tap-to-complete. Unmatched
        completions are dropped, never stored: inventing a patch to
        close would manufacture memory.
        """
        user_id = payload.get("user_id")
        statement = (payload.get("content") or "").strip()[:MAX_CORRECTION_CHARS]
        if not user_id or not statement:
            logger.warning("completion_missing_fields", user_id=user_id)
            return
        metadata = payload.get("metadata") or {}
        context_block = payload.get("context_block") or ""
        project_id = metadata.get("project_id")
        project = metadata.get("project")
        subject_key = f"user:{user_id}"

        completable = (await get_type_runtime(self.db.fetch)).completable_types
        scope_sql = ""
        params: list = [subject_key, list(completable)]
        if project_id:
            scope_sql = "AND (cp.project_id = $3 OR cp.project_id IS NULL)"
            params.append(project_id)
        elif project:
            scope_sql = "AND (cp.project = $3 OR cp.project IS NULL)"
            params.append(project)
        rows = await self.db.fetch(
            f"""
            SELECT cp.patch_id, cp.patch_type, cp.value->>'text' AS text
            FROM context_patches cp
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            WHERE ps.subject_key = $1
              AND cp.patch_type = ANY($2::text[])
              AND COALESCE(cp.status, 'active') = 'active'
              AND cp.completed_at IS NULL
              {scope_sql}
            ORDER BY cp.created_at DESC, cp.patch_id ASC
            LIMIT 40
            """,
            *params,
        )
        in_block = [r for r in rows if r["text"] and r["text"] in context_block]
        others = [r for r in rows if r not in in_block]
        candidates = (in_block + others)[:MAX_CANDIDATES]
        by_id = {str(r["patch_id"]): r for r in candidates}
        if not candidates:
            logger.info("completion_no_candidates", user_id=user_id,
                        statement=statement[:100])
            return

        try:
            response = await self.llm.extract(
                system_prompt=COMPLETION_SYSTEM,
                user_content=build_completion_content(
                    statement,
                    [{"patch_id": str(r["patch_id"]), "patch_type": r["patch_type"], "text": r["text"]}
                     for r in candidates],
                    datetime.utcnow().date().isoformat(),
                    scope_label=project or project_id,
                ),
            )
            parsed = parse_completion_response(response.content, set(by_id.keys()))
        except Exception as exc:
            logger.error("completion_failed", user_id=user_id, reason=str(exc)[:200])
            return
        if not parsed:
            logger.info("completion_unmatched", user_id=user_id,
                        statement=statement[:100], candidates=len(candidates))
            return
        patch_id, evidence = parsed
        evidence = evidence or statement[:300]

        # Identical close semantics to the extraction auto-close and the
        # app complete endpoint — only the source differs.
        await self.db.execute(
            """
            UPDATE context_patches
               SET completed_at = NOW(),
                   status = 'archived',
                   updated_at = NOW(),
                   value = jsonb_set(
                               jsonb_set(value, '{completion_source}', '"user_chat"'),
                               '{completion_evidence}', to_jsonb($2::text)
                           )
             WHERE patch_id = $1::uuid
               AND completed_at IS NULL
            """,
            patch_id, evidence,
        )
        logger.info("completion_applied", user_id=user_id, patch_id=patch_id,
                    patch_type=by_id[patch_id]["patch_type"],
                    in_block=by_id[patch_id] in in_block, evidence=evidence[:120])

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
                    # Same two manifest facts the extraction lane reads.
                    # Neither structured manifest registered today declares
                    # either key, so both sets are empty and this lane is
                    # unchanged; a structured app that wants the behavior
                    # should not have to ask for a code change to get it.
                    no_collapse_types=no_collapse_patch_types(manifest),
                    origin_scoped_types=origin_scoped_patch_types(manifest),
                )
                entities_stored = await store_entities(
                    conn, self.redis, user_id, entities, metadata,
                    person_entity_type=people_vocabulary(manifest).person_entity_type,
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

    async def _apply_speaker_identities(
        self, user_id: str, transcript: str, metadata: dict, person_entity_type: str,
    ) -> tuple[str, list[dict]]:
        """Resolve each answered label to a stored canonical name and
        rewrite the transcript. Never raises; an entry that cannot be
        resolved leaves its label untouched for today's matching."""
        entries = parse_speaker_identities((metadata or {}).get("speaker_identities"))
        if not entries:
            return transcript, []
        mapping: dict[str, str] = {}
        applied: list[dict] = []
        for e in entries:
            try:
                if e["entity_id"]:
                    eid = await _resolve_merged_forward(self.db, uuid.UUID(e["entity_id"]))
                    row = await self.db.fetchrow(
                        """
                        SELECT entity_id, name FROM entities
                        WHERE entity_id = $1 AND user_id = $2
                          AND entity_type = $3 AND suppressed_at IS NULL
                        """,
                        eid, user_id, person_entity_type,
                    )
                    if row is None:
                        logger.warning(
                            "speaker_identity_unresolved", user_id=user_id,
                            label=e["label"][:60], entity_id=e["entity_id"][:40],
                            reason="unknown_entity",
                        )
                        continue
                    canonical, status = row["name"], "linked"
                else:
                    canonical, status = await self._create_identified_person(
                        user_id, e["name"], person_entity_type,
                    )
                mapping[e["label"]] = canonical
                applied.append({"label": e["label"], "name": canonical, "status": status})
            except Exception as exc:
                logger.warning(
                    "speaker_identity_unresolved", user_id=user_id,
                    label=e["label"][:60], reason=str(exc)[:120],
                )
        text, counts = rewrite_speaker_labels(transcript, mapping)
        for a in applied:
            a["replacements"] = counts.get(a["label"], 0)
        logger.info(
            "speaker_identities_applied", user_id=user_id,
            applied=applied, sent=len(entries),
        )
        return text, applied

    async def _create_identified_person(
        self, user_id: str, name: str, person_entity_type: str,
    ) -> tuple[str, str]:
        """The create_new half, under the same rules as reassign-speaker
        (doc 16 5.16): an exact match on the stored name resolves (a
        two-plus-token exact match is decisive by ruling; a bare taken
        name resolves here rather than failing, because the client was
        told to ask again and a map cannot be asked); otherwise create
        the person and stamp Keep separate against every live person
        sharing the first token, which is the list the device showed.
        Returns (canonical name, "linked" | "created")."""
        row = await self.db.fetchrow(
            """
            SELECT entity_id, name FROM entities
            WHERE user_id = $1 AND entity_type = $2 AND LOWER(name) = LOWER($3)
            LIMIT 1
            """,
            user_id, person_entity_type, name,
        )
        if row is not None:
            eid = await _resolve_merged_forward(self.db, row["entity_id"])
            live = await self.db.fetchrow(
                "SELECT name FROM entities WHERE entity_id = $1", eid
            )
            if len(tokenize_name(name)) == 1:
                logger.warning(
                    "speaker_identity_bare_name_taken", user_id=user_id,
                    name=name[:60],
                )
            return (live["name"] if live else row["name"]), "linked"

        first = tokenize_name(name)[:1]
        roster = await self.db.fetch(
            """
            SELECT entity_id, name FROM entities
            WHERE user_id = $1 AND entity_type = $2
              AND merged_into IS NULL AND suppressed_at IS NULL
            """,
            user_id, person_entity_type,
        )
        sharing = [
            str(r["entity_id"]) for r in roster
            if first and tokenize_name(r["name"] or "")[:1] == first
        ]
        new_id = str(await self.db.fetchval(
            """
            INSERT INTO entities
                (user_id, name, entity_type, description,
                 confirmed_at, confirmation_source)
            VALUES ($1, $2, $3, '', NOW(), 'speaker_identity')
            RETURNING entity_id
            """,
            user_id, name, person_entity_type,
        ))
        for other in sharing:
            lo, hi = canonical_pair(new_id, other)
            await self.db.execute(
                """
                INSERT INTO entity_separations
                    (user_id, entity_id_lo, entity_id_hi, source)
                VALUES ($1, $2::uuid, $3::uuid, 'speaker_identity')
                ON CONFLICT (user_id, entity_id_lo, entity_id_hi) DO NOTHING
                """,
                user_id, lo, hi,
            )
        logger.info(
            "speaker_identity_person_created", user_id=user_id,
            name=name[:60], separated_from=len(sharing),
        )
        return name, "created"

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
        # for the lookback window + cap, and select_open_commitments for
        # why this meeting's project gets reserved slots rather than a
        # sort key.
        open_commits_block = await self._build_open_commitments_block(
            user_id, metadata.get("project_id")
        )

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
        # Weekday included: the coverage eval (2026-07-30) caught the
        # model resolving "by Friday" off by one day from a bare ISO
        # date — models are unreliable at date→weekday math, and every
        # weekday-relative deadline depends on it.
        meeting_date_line = (
            f"Meeting date: {meeting_date.isoformat()} ({meeting_date.strftime('%A')})\n\n"
            if meeting_date else ""
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

            # Which of this app's types and labels carry People semantics
            # (doc 16 5.9), resolved once: the ownership-entity injection
            # below and the entity sink both need it, and reading it twice
            # invites the two of them to disagree about what a person is.
            people_vocab = people_vocabulary(await self._app_manifest(app_id))

            # Live speaker labels the user answered on the device
            # (metadata.speaker_identities): rewrite the bracketed label
            # to the chosen person's canonical name BEFORE the model
            # reads it, so every downstream path hits the exact match.
            # See services/speaker_identities.py for why a rewrite.
            effective_summary, _identities_applied = await self._apply_speaker_identities(
                user_id, effective_summary, metadata,
                people_vocab.person_entity_type,
            )

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

            # Apply the length-scaled patch backstop to LLM output BEFORE the
            # enforcer runs. The cap exists to bound LLM-output noise; the
            # enforcer's job is structural completeness (every named owner
            # must have a person patch + owns edge). Capping the
            # post-enforcer list silently drops the synthetic person
            # patches it appends, which silently breaks PR #84 for any
            # meeting where the LLM emitted patches at the backstop
            # on its own. Cap first; then enforce.
            raw_patches = response.content.get("patches") or []
            patch_backstop = extraction_patch_backstop(
                len(summary), floor=_settings.cq_max_patches
            )
            if len(raw_patches) > patch_backstop:
                logger.warning(
                    "extraction_capped",
                    type="patches",
                    original=len(raw_patches),
                    capped=patch_backstop,
                    transcript_chars=len(summary),
                )
                response.content["patches"] = raw_patches[:patch_backstop]

            # Guardrail 12b at capture time: a behavioral observation
            # cites conduct, never character. Runs BEFORE the ownership
            # enforcer on purpose, so a dropped verdict never gets a
            # person patch and an owns edge minted for it; the sanitizer
            # also strips edges the model emitted itself, because an
            # unresolved owns target makes the Pass-2 resolver synthesize
            # a stub carrying the same text.
            sanitize_behavior_observations(response.content)
            if (bo := response.content.get("_behavior_observations_sanitized")):
                logger.info(
                    "behavior_observations_sanitized",
                    user_id=user_id,
                    dropped=bo["count"],
                    dropped_detail=bo["dropped"][:20],
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

            # Owner-edge agreement — the mirror of enforce_person_ownership
            # above. That one adds the missing owns edge for a named owner;
            # this drops owns edges from everyone who is not that owner.
            # `owns` is the only person-to-item label SS defines, so any
            # other involvement the extractor cannot name (a counterparty,
            # someone supplying a precondition) lands as a second owner and
            # is indistinguishable from the real one downstream. Runs after
            # vocabulary enforcement so edge directions are already
            # normalized.
            enforce_owner_edge_agreement(response.content, user_label=user_label)
            if (oe := response.content.get("_owner_edge_agreement_enforced")):
                logger.info(
                    "owner_edge_agreement_enforced",
                    user_id=user_id,
                    dropped=len(oe.get("dropped", [])),
                    dropped_detail=oe.get("dropped", []),
                    model=response.model,
                )

            # Counterparty agreement, the same idea applied to the other
            # new edge: `owed_to` is the only label that can say the (you)
            # speaker owes a named person something, so a wrong one reads
            # as an obligation the user does not have. Drops edges pointing
            # at the item's own owner, at the (you) speaker (already
            # carried by `owns`, and the self person patch is dropped by
            # design), and at diarization placeholders. Runs after
            # vocabulary enforcement so reversed edges have already been
            # flipped onto the item.
            enforce_owed_to_counterparty(response.content, user_label=user_label)
            if (ot := response.content.get("_owed_to_enforced")):
                logger.info(
                    "owed_to_counterparty_enforced",
                    user_id=user_id,
                    dropped=len(ot.get("dropped", [])),
                    dropped_detail=ot.get("dropped", [])[:20],
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
            strip_owner_on_self_typed_patches(response.content, user_label=user_label)
            if (so := response.content.get("_self_typed_owner_stripped")):
                # Instrumentation only, nothing changed behaviourally. The
                # third_party count is the one to watch: those are genuine
                # attributions ("Brightwell prefers X") that the manifest wants as
                # a held_by edge and that we currently delete. Logged
                # because the strip destroys the evidence, so stored rows
                # cannot tell us whether the model still attempts them.
                logger.info(
                    "self_typed_owner_stripped",
                    user_id=user_id,
                    self_or_placeholder=so.get("self_or_placeholder", 0),
                    third_party=so.get("third_party", 0),
                    third_party_detail=so.get("third_party_detail", []),
                    model=response.model,
                )
            strip_prose_from_person_names(response.content)
            drop_placeholder_and_self_person_patches(
                response.content, user_label=user_label
            )
            # Presence for the people who own the work. enforce_person_ownership
            # above guarantees the person PATCH; person_appearances is written
            # from the ENTITIES array, so without this an owner can come out of
            # a meeting with three commitments and no presence in it at all.
            # Deliberately placed here: after every pass that prunes or
            # rewrites a person patch name (so the names are final), and
            # BEFORE drop_placeholder_entities, so the placeholder pass still
            # cleans up after this rather than ahead of it.
            inject_ownership_entities(
                response.content,
                person_patch_type=people_vocab.person_type,
                person_entity_type=people_vocab.person_entity_type,
                ownership_label=people_vocab.ownership_label,
                user_label=user_label,
            )
            if (oi := response.content.get("_ownership_entities_injected")):
                logger.info(
                    "ownership_entities_injected",
                    user_id=user_id,
                    injected=oi["injected"],
                    owners=len(oi["owners"]),
                    model=response.model,
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

            # Deadline micro-pass: one small focused call that ONLY
            # resolves spoken deadlines against a rendered calendar
            # table. The main call resolves weekday-relative dates off
            # by one (measured 2026-07-30; the weekday hint on the
            # Meeting date line did not fix it). Runs after the
            # sanitizers so its output passes the same plausibility
            # gate; failure leaves the main call's dates untouched.
            if get_settings().cq_deadline_micropass_enabled:
                await run_deadline_micropass(
                    llm, response.content.get("patches") or [], meeting_date
                )

            timestamp = payload.get("timestamp")
            project = metadata.get("project") if metadata else None
            project_id = metadata.get("project_id") if metadata else None
            origin_id = metadata.get("origin_id") if metadata else None
            origin_type = metadata.get("origin_type") if metadata else None

            # HONOUR A PROJECT DECISION MADE WHILE THIS INGEST WAS STILL
            # RUNNING. An ingest is multi-phase and takes about twenty
            # seconds; assigning a project the moment a meeting ends
            # lands inside that window, and on 2026-08-28 it did: the
            # rescope moved the 3 patches that existed and the 12 written
            # eight seconds later stayed unscoped forever.
            #
            # Only when this payload carries NO project of its own. A
            # payload that names one is the newer statement and wins. A
            # row whose project_id is NULL is an EXPLICIT unassignment
            # and must leave this ingest unscoped rather than fall
            # through to "never stated" (migration 43).
            if origin_id and not project_id:
                try:
                    decided = await self.db.fetchrow(
                        """
                        SELECT project_id, project FROM origin_project_assignments
                         WHERE user_id = $1 AND origin_id = $2
                           AND origin_type = $3
                        """,
                        user_id, str(origin_id),
                        str(origin_type) if origin_type else "meeting",
                    )
                except Exception:
                    decided = None      # table may lag on the MCP deployment
                if decided is not None:
                    project_id = decided["project_id"]
                    project = decided["project"] or project
                    logger.info("origin_project_adopted", origin_id=str(origin_id),
                                project_id=project_id, user_id=user_id)

            # THE DAY THE MEETING HAPPENED, recorded rather than spent.
            #
            # Until 2026-08-27 this timestamp built the `Meeting date:`
            # prompt line and was dropped, so every date CQ held was an
            # ingest clock and no surface could honestly bucket by month
            # (doc 21, ruled by Scott). It is written here and read by
            # nothing yet; the point is that history starts accruing now,
            # because a meeting's date is like the transcript, available
            # exactly once.
            #
            # Failure is swallowed on purpose. This is bookkeeping beside
            # the ingest, never a reason to lose an extraction, and the
            # table may not exist on the MCP deployment's lagging
            # Postgres, which is the same degradation entity_aliases and
            # patch_cues already take.
            # Gated on the PARSED date (computed above for the prompt's
            # `Meeting date:` line), never on the raw string: that parse
            # already handles the Z suffix and yields None on anything it
            # cannot read, and slicing the string instead would write a
            # confident wrong day for any format it does not expect.
            if origin_id and meeting_date:
                try:
                    await self.db.execute(
                        """
                        INSERT INTO meeting_origins
                            (user_id, origin_id, origin_type, meeting_date)
                        VALUES ($1, $2, $3, $4::date)
                        ON CONFLICT (user_id, origin_id) DO UPDATE
                           SET meeting_date = EXCLUDED.meeting_date,
                               origin_type  = COALESCE(EXCLUDED.origin_type,
                                                       meeting_origins.origin_type),
                               updated_at   = NOW()
                         WHERE meeting_origins.meeting_date
                               IS DISTINCT FROM EXCLUDED.meeting_date
                        """,
                        user_id, str(origin_id),
                        str(origin_type) if origin_type else None,
                        meeting_date,
                    )
                except Exception as exc:
                    logger.debug("meeting_origin_not_recorded",
                                 origin_id=str(origin_id), error=str(exc)[:140])

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
                    # Manifest-declared storage behavior. Both sets are
                    # empty for every manifest that predates them, so the
                    # sink behaves exactly as before for those apps.
                    no_collapse_types=no_collapse_patch_types(resolved_manifest),
                    origin_scoped_types=origin_scoped_patch_types(resolved_manifest),
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
            #
            # The cap bounds LLM-output noise, so an ownership-carrying
            # entity is exempt from it: those are structural, and truncating
            # one deletes a person's presence in the meeting. Same reasoning
            # as "cap first, then enforce" on the patch backstop above, in
            # the only shape available here (the injection has already run).
            entities, entities_dropped = cap_entities(entities, MAX_ENTITIES_PER_MEETING)
            if entities_dropped:
                logger.warning(
                    "extraction_capped", type="entities",
                    original=len(entities) + entities_dropped,
                    capped=len(entities),
                )
            if len(relationships) > MAX_RELATIONSHIPS_PER_MEETING:
                logger.warning("extraction_capped", type="relationships", original=len(relationships), capped=MAX_RELATIONSHIPS_PER_MEETING)
                relationships = relationships[:MAX_RELATIONSHIPS_PER_MEETING]

            entities_stored = await store_entities(
                self.db, self.redis, user_id, entities, metadata,
                # Who actually spoke, so the appearance carries the capacity
                # the identity gate needs. Read off the same normalized
                # transcript the extraction saw.
                speaker_labels=speaker_labels_in(effective_summary, owner_speaker_label),
                speaker_turns=speaker_turn_counts(effective_summary, owner_speaker_label),
                # Who the questions in this room were asked OF, from the
                # same normalized transcript, in the same one pass. The
                # transcript is gone after this; there is no second
                # chance at this signal for this meeting, ever.
                speaker_questions=question_attribution(
                    effective_summary, owner_speaker_label
                ),
                # Who opened the room, who closed it, who answered its
                # questions. Same transcript, same one pass, same
                # never-backfillable constraint (doc 21 stage 2).
                speaker_role_signals=meeting_role_signals(
                    effective_summary, owner_speaker_label
                ),
                # Which entity type IS a person comes from the app's people
                # vocabulary (doc 16 5.9); the SS floor when undeclared.
                # This closes slice 2's recorded limit: a custom-named
                # person entity type now accumulates appearances.
                person_entity_type=people_vocab.person_entity_type,
                # The ego link (13b): whichever identity signal named the
                # user — structured metadata or the inline (you) marker —
                # stamps their resolved entity as self. Both land here as
                # a plain name; the marker parse is the fallback because
                # the metadata path is the stronger, gated signal.
                self_label=owner_speaker_label
                or self_speaker_label(effective_summary),
            )
            relationships_stored = await store_relationships(
                self.db, user_id, relationships, metadata
            )

            # Apply commitment resolutions reported by the LLM. Validates
            # patch ownership and that the patch is actually an open
            # commitment before marking completed. Unknown or cross-user
            # patch_ids are dropped with a warning.
            commitments_believed = await self._apply_resolved_commitments(
                user_id, response.content.get("resolved_commitments") or [],
                origin_id=origin_id,
            )

            logger.info(
                "meeting_summary_complete",
                user_id=user_id,
                facts_stored=facts_stored,
                actions_stored=actions_stored,
                entities_stored=entities_stored,
                relationships_stored=relationships_stored,
                commitments_believed=commitments_believed,
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

            # Behavior observations: their own call, for the reason doc
            # 19.5 records. Inline as one of fifteen types this produced
            # 4 observations across 8 meetings on Haiku and 0 on Sonnet;
            # a dedicated call with the same cheap model produced 48.
            # Runs only when the manifest declares the type, so an app
            # that never asked for it never pays for the call.
            await self._extract_behavior_observations(
                user_id, summary, app_id, origin_id, origin_type,
                timestamp, project, project_id, user_label,
                resolved_manifest,
            )

            # Alignment events (design e6ee7ae8, phase 1): its own call,
            # after everything above is stored, because it needs this
            # meeting's decision patch ids and the project's active set
            # as inputs. Inert without a project. Never raises.
            await self._extract_alignment_events(
                user_id, effective_summary, app_id, origin_id, origin_type,
                timestamp, project_id, meeting_date_line,
            )

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

    async def _fetch_open_commitments(
        self, user_id: str, project_id: str | None = None
    ) -> list[dict[str, Any]]:
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

            # This meeting's project, newest first, INDEPENDENT of the
            # overdue window. Separate query rather than another ORDER BY
            # key because the slots are reserved, not merely preferred:
            # see select_open_commitments for the measurement that
            # settles why a sort key cannot fix this. Same `=` match the
            # quilt route uses on project_id (text, case-sensitive), so
            # the read and the write agree on what "this project" means.
            project_rows = []
            if project_id:
                project_rows = await self.db.fetch(
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
                       AND cp.project_id = $2
                       AND cp.created_at >= NOW() - INTERVAL '{int(self.OPEN_COMMITS_LOOKBACK_DAYS)} days'
                     ORDER BY cp.created_at DESC
                     LIMIT $3
                    """,
                    subject_key, project_id, self.OPEN_COMMITS_MAX_INJECTED,
                )

            def _row(r):
                return {
                    "patch_id": r["patch_id"], "text": r["text"],
                    "created_at": r["created_at"], "deadline_date": r["deadline_date"],
                }

            return select_open_commitments(
                [_row(r) for r in project_rows],
                [_row(r) for r in rows],
                self.OPEN_COMMITS_MAX_INJECTED,
            )
        except Exception as exc:
            logger.warning("open_commitments_fetch_failed", reason=str(exc)[:200], user_id=user_id)
            return []

    async def _build_open_commitments_block(
        self, user_id: str, project_id: str | None = None
    ) -> str:
        """Format the open commitments into the prompt-ready block that
        prefixes user_content. Returns empty string when there are none,
        so callers can prepend unconditionally. Rendering lives in
        extraction_prompts.format_open_commitments_block (pure,
        unit-tested); this method just fetches and delegates.

        `project_id` is the meeting's own project. Absent (a meeting with
        no project) degrades to exactly the previous behaviour."""
        commits = await self._fetch_open_commitments(user_id, project_id)
        return format_open_commitments_block(commits, now=datetime.utcnow())

    async def _apply_resolved_commitments(
        self, user_id: str, resolutions: list[dict[str, Any]],
        origin_id: str | None = None,
    ) -> int:
        """Act on the closures the LLM reported. Returns the count CLOSED.

        Validates ownership before any write so a hallucinated or
        cross-user patch_id can't touch another user's commitment.

        NOTHING CLOSES HERE ANY MORE. Every reported resolution becomes
        a BELIEF a human answers.

        This used to archive every reported resolution on the spot, and
        measured across all 167 of them on prod, most were not evidence
        of anything being finished: items closed because the promise was
        made AGAIN, or because somebody set a date, both of which doc 16
        5.12 already rules are not advances.

        A first pass kept a "confident" band that still auto-closed, 13
        of the 167. Reading all 13 by hand against the item they closed
        found at least two wrong, so the band labelled confident was
        running 15% to 46% wrong on exactly the population marked
        trustworthy. 13-of-167 measures ABSTENTION, not accuracy, and it
        reads like rigour, which is what made it dangerous.

        A wrong close is not a lost obligation, it is a FABRICATED
        DELIVERY: the row archives out of the ledger population and
        reappears in `completed_they_owe` as something that person
        handed over. So the unreadable, forward-looking and ambiguous
        cases now stay OPEN carrying a belief stamp, and the app asks a
        human. Only unambiguous completion language closes by itself,
        and the app is told so it can offer a one tap reopen.

        Never the reverse: nothing here re-opens or un-believes an item.
        A human's answer outranks a later meeting's guess, so vouch and
        complete are the only things that clear a belief.
        """
        if not resolutions or not user_id:
            return 0
        subject_key = f"user:{user_id}"
        believed_count = 0
        for item in resolutions:
            patch_id = (item.get("patch_id") or "").strip()
            evidence = (item.get("evidence") or "").strip()[:300]
            if not patch_id:
                continue
            try:
                # Ownership gate + open-commitment gate in one query.
                # Returns the patch_id only if all conditions hold; lets
                # us avoid two round-trips.
                # Owner comes back too: the evidence classifier needs it
                # to ask whether the evidence is even about the person who
                # owed the thing.
                gated = await self.db.fetchrow(
                    """
                    SELECT cp.patch_id::text AS patch_id,
                           cp.value->>'owner' AS owner
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
            verdict = classify_closure(gated["owner"], evidence)
            try:
                # NOTHING AUTO-CLOSES ANY MORE, and the band no longer
                # decides that. It survives only as a hint for the app's
                # card ORDER, so strong evidence reaches a quick yes
                # first.
                #
                # Why the confident band was collapsed on 2026-08-18:
                # it was 13 of 167, which reads as rigour and is actually
                # a measure of how often the classifier ABSTAINS, not of
                # how often it is right when it does not. Reading all 13
                # by hand against the item they closed found at least two
                # wrong, so precision on the band labelled "confident"
                # was somewhere between 85% and 54%:
                #
                #   item     Validate SDK deployment for history and search
                #   evidence "...deployed to Dev and QA. Joy TO VALIDATE in QA."
                #
                #   item     DELIVER asset management hardware POC
                #   evidence "shared analysis; RECOMMENDS ... as separate POC"
                #
                # Both failures are about the VERB, not the nouns, which
                # is why a shared-token check would not have caught
                # either. And both of the last two items were closed by
                # the SAME piece of evidence: one person reporting work,
                # closing two items, one of which was not done.
                #
                # The costs are not symmetric. A wrong close fabricates
                # delivery history, in the artifact people are least
                # likely to question. A missed close leaves an item open
                # and someone chases it. Eleven good auto-closes over
                # five months do not buy two fabricated delivery records.
                await self.db.execute(
                    """
                    UPDATE context_patches
                       SET updated_at = NOW(),
                           value = jsonb_set(
                               jsonb_set(
                                   jsonb_set(
                                       jsonb_set(
                                           jsonb_set(value,
                                               '{believed_complete_at}',
                                               to_jsonb(to_char(NOW() AT TIME ZONE 'UTC',
                                                   'YYYY-MM-DD"T"HH24:MI:SS"Z"'))),
                                           '{believed_complete_evidence}',
                                           to_jsonb($2::text)),
                                       '{believed_complete_reasons}',
                                       $3::jsonb),
                                   '{believed_complete_origin_id}',
                                   to_jsonb($4::text)),
                               '{believed_evidence_strength}',
                               to_jsonb($5::text))
                     WHERE patch_id = $1::uuid
                    """,
                    patch_id, evidence,
                    json.dumps(verdict["reasons"]),
                    str(origin_id or ""),
                    verdict["band"],
                )
                believed_count += 1
                logger.info(
                    "commitment_believed_complete",
                    patch_id=patch_id, user_id=user_id,
                    strength=verdict["band"], reasons=verdict["reasons"],
                    evidence=evidence[:120],
                )
            except Exception as exc:
                logger.warning(
                    "resolved_commitment_update_failed",
                    patch_id=patch_id, reason=str(exc)[:200], user_id=user_id,
                )
        if believed_count:
            logger.info(
                "commitments_believed_complete_batch",
                user_id=user_id, believed=believed_count,
            )
        # Beliefs RAISED, not closures. Nothing closes on this path any
        # more, so returning a permanently-zero "resolved" count would be
        # a metric that reads the same whether the pass worked or never
        # ran, which is the exact instrument failure this change came
        # from.
        return believed_count

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

    async def _extract_behavior_observations(
        self, user_id: str, transcript: str, app_id, origin_id, origin_type,
        timestamp, project, project_id, user_label, manifest,
    ) -> int:
        """One dedicated call for how people conducted themselves.

        Writes through `store_connected_patches`, the same sink the main
        extraction uses, deliberately: ownership edges, origin stamping,
        ACLs, dedup and the manifest's per-type storage keys all live
        there, and a second writer with its own path would be a second
        source of truth about the same person.

        Inert unless the app's manifest declares a `behavior` type, so
        an app that never asked for it never pays for the call.

        Never raises. This runs after the meeting's real extraction has
        already been stored, so a failure here costs one call and the
        user still has everything the main pass produced.
        """
        try:
            if not behavior_extraction.worth_a_call(transcript):
                return 0
            declared = {
                t.get("domain_type")
                for t in (manifest or {}).get("patch_types", [])
                if isinstance(t, dict)
            }
            if "behavior" not in declared:
                return 0
            guidance = None
            for t in (manifest or {}).get("patch_types", []):
                if isinstance(t, dict) and t.get("domain_type") == "behavior":
                    rules = t.get("extraction_rules") or {}
                    guidance = rules.get("guidance") or t.get("description")
                    break

            llm = await self._get_llm_for_app(app_id)
            defects: list = []
            response = await llm.extract(
                system_prompt=behavior_extraction.BEHAVIOR_SYSTEM,
                user_content=behavior_extraction.build_behavior_content(
                    transcript, guidance
                ),
            )
            patches = behavior_extraction.parse_behavior_response(
                response.content, user_label=user_label, defects=defects,
            )
            if not patches:
                logger.info("behavior_observations_none", user_id=user_id,
                            origin=origin_id,
                            defect=defects[0] if defects else "empty")
                return 0

            stored = await store_connected_patches(
                self.db, user_id, patches, "behavior_observations", app_id,
                timestamp, project, project_id, origin_id, origin_type,
                user_label=user_label, llm=llm,
                no_collapse_types=no_collapse_patch_types(manifest),
                origin_scoped_types=origin_scoped_patch_types(manifest),
            )
            logger.info(
                "behavior_observations_stored", user_id=user_id,
                origin=origin_id, emitted=len(patches), stored=stored,
                cost_usd=getattr(response, "cost_usd", None),
            )
            return stored
        except Exception as exc:
            logger.warning("behavior_observations_failed", user_id=user_id,
                           origin=origin_id, reason=str(exc)[:200])
            return 0

    ALIGNMENT_ACTIVE_SET_MAX = 40

    async def _extract_alignment_events(
        self, user_id: str, transcript: str, app_id, origin_id, origin_type,
        timestamp, project_id, meeting_date_line: str = "",
    ) -> int:
        """Phase 1 of the Alignment Layer (services/alignment.py has the
        rules). Detect supersession by id against the project's active
        decision set, guard the shared copy in code (one regeneration,
        then drop), derive the impact receipt from referencing open
        items, store. Inert without a project or without a decision
        patch from this meeting. Never raises."""
        try:
            if not project_id or not origin_id or not transcript:
                return 0
            subject_key = f"user:{user_id}"
            todays = await self.db.fetch(
                """
                SELECT cp.patch_id::text AS id, cp.value->>'text' AS text,
                       cp.value->>'owner' AS owner
                  FROM context_patches cp
                  JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                 WHERE ps.subject_key = $1 AND cp.patch_type = 'decision'
                   AND cp.origin_id = $2
                   AND COALESCE(cp.status, 'active') = 'active'
                 ORDER BY cp.created_at
                """,
                subject_key, origin_id,
            )
            if not todays:
                return 0
            # The active set: live alignment events on this project
            # (what the record currently believes) plus active decision
            # patches from OTHER meetings on this project (what the
            # extraction recorded before any record existed). Deltas,
            # not history: capped, newest first.
            prior_events = await self.db.fetch(
                """
                SELECT event_id::text AS id, statement AS text,
                       to_char(proposed_at, 'YYYY-MM-DD') AS date, topic
                  FROM alignment_events
                 WHERE user_id = $1 AND project_id = $2
                   AND status IN ('proposed','confirmed','corrected')
                   AND superseded_by IS NULL
                 ORDER BY proposed_at DESC LIMIT $3
                """,
                user_id, project_id, self.ALIGNMENT_ACTIVE_SET_MAX,
            )
            prior_patches = await self.db.fetch(
                """
                SELECT cp.patch_id::text AS id, cp.value->>'text' AS text,
                       to_char(cp.created_at, 'YYYY-MM-DD') AS date
                  FROM context_patches cp
                  JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                 WHERE ps.subject_key = $1 AND cp.patch_type = 'decision'
                   AND cp.project_id = $2
                   AND COALESCE(cp.origin_id, '') <> $3
                   AND COALESCE(cp.status, 'active') = 'active'
                 ORDER BY cp.created_at DESC LIMIT $4
                """,
                subject_key, project_id, origin_id, self.ALIGNMENT_ACTIVE_SET_MAX,
            )
            active = [dict(r) for r in prior_events] + [dict(r) for r in prior_patches]
            if not active:
                return 0   # nothing to supersede; first decisions are not changes
            event_ids = {r["id"] for r in prior_events}
            meeting_date = meeting_date_line.replace("Meeting date:", "").strip() or (
                timestamp.strftime("%Y-%m-%d") if hasattr(timestamp, "strftime") else str(timestamp)[:10]
            )
            today_iso = meeting_date[:10]

            llm = await self._get_llm_for_app(app_id)
            content = alignment_svc.build_alignment_content(
                meeting_date, [dict(r) for r in todays], active, transcript,
            )
            defects: list = []
            resp = await llm.extract(system_prompt=alignment_svc.ALIGNMENT_SYSTEM, user_content=content)
            events = alignment_svc.parse_alignment_response(
                resp.content, [r["id"] for r in todays], [a["id"] for a in active], transcript, defects,
            )
            # THE GUARD REJECTS, THEN REGENERATES ONCE. Never softens.
            if any(e["guard_hit"] for e in events):
                hits = [e["guard_hit"] for e in events if e["guard_hit"]]
                logger.info("alignment_guard_rejected", user_id=user_id, origin=origin_id, terms=hits[:5])
                retry = await llm.extract(
                    system_prompt=alignment_svc.ALIGNMENT_SYSTEM,
                    user_content=content + (
                        "\n\nYour previous answer was rejected because shared text must not "
                        f"describe a person. Do not use: {', '.join(hits[:5])}. Rewrite."
                    ),
                )
                events = alignment_svc.parse_alignment_response(
                    resp_content_or(retry), [r["id"] for r in todays], [a["id"] for a in active], transcript, defects,
                )
                events = [e for e in events if not e["guard_hit"]]
            if not events:
                logger.info("alignment_events_none", user_id=user_id, origin=origin_id,
                            defect=defects[0] if defects else "empty")
                return 0

            stored = 0
            # Proposal clock: the ingest timestamp, made tz-aware so the
            # column and the 72h expiry agree on a zone.
            proposed_at = timestamp if isinstance(timestamp, datetime) else datetime.utcnow()
            if proposed_at.tzinfo is None:
                proposed_at = proposed_at.replace(tzinfo=timezone.utc)
            existing_topic_rows = [dict(r) for r in await self.db.fetch(
                "SELECT topic, supersedes, superseded_patch_ids, status FROM alignment_events WHERE user_id = $1 AND project_id = $2",
                user_id, project_id,
            )]
            for e in events:
                sup_events = [i for i in e["supersedes_ids"] if i in event_ids]
                sup_patches = [i for i in e["supersedes_ids"] if i not in event_ids]
                referencing = await self._alignment_referencing_items(
                    subject_key, project_id, sup_patches + [e["new_decision_id"]], today_iso,
                )
                impact = alignment_svc.derive_impact(referencing, today_iso)
                change_count = alignment_svc.topic_change_count(existing_topic_rows, e["topic"]) + 1
                instruction = alignment_svc.private_instruction(e["topic"], change_count)
                evidence = (
                    [{"origin_id": origin_id, "quote": e["evidence_quote"], "matched": e.get("evidence_matched")}]
                    if e["evidence_quote"] else []
                )
                eid = str(uuid.uuid4())
                await self.db.execute(
                    """
                    INSERT INTO alignment_events (
                        event_id, user_id, app_id, project_id, origin_id, origin_type,
                        topic, statement, rationale, decision_owner, implementation_owner,
                        status, confidence, supersedes, source_patch_ids, superseded_patch_ids,
                        impact, evidence, shippable, proposed_at, expires_at, private_instruction)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                            'proposed', $12, $13::uuid[], $14::uuid[], $15::uuid[],
                            $16::jsonb, $17::jsonb, $18, $19, $20, $21)
                    """,
                    eid, user_id, _uuid_or_none(app_id), project_id, origin_id, origin_type,
                    e["topic"], e["statement"], e["rationale"], e["decision_owner"], e["implementation_owner"],
                    e["confidence"], sup_events, [e["new_decision_id"]], sup_patches,
                    json.dumps(impact), json.dumps(evidence), bool(e["shippable"]),
                    proposed_at, proposed_at + timedelta(hours=alignment_svc.PROPOSAL_TTL_HOURS), instruction,
                )
                existing_topic_rows.append({"topic": e["topic"], "supersedes": sup_events, "superseded_patch_ids": sup_patches, "status": "proposed"})
                stored += 1
            logger.info(
                "alignment_events_stored", user_id=user_id, origin=origin_id, project_id=project_id,
                stored=stored, shippable=sum(1 for e in events if e["shippable"]),
                cost_usd=getattr(resp, "cost_usd", None),
            )
            return stored
        except Exception as exc:
            logger.warning("alignment_events_failed", user_id=user_id, origin=origin_id, reason=str(exc)[:200])
            return 0

    async def _alignment_referencing_items(self, subject_key: str, project_id: str, decision_patch_ids: list, today_iso: str) -> list:
        """Open items that reference the superseded decisions: connected
        by an edge in either direction, or sharing a cue with one of them.
        Completables and deliverables only; the receipt is about work."""
        ids = [i for i in decision_patch_ids if i]
        if not ids:
            return []
        rows = await self.db.fetch(
            """
            WITH refs AS (
                SELECT to_patch_id AS pid FROM patch_connections
                 WHERE from_patch_id = ANY($2::uuid[]) AND COALESCE(status, 'active') = 'active'
                UNION SELECT from_patch_id FROM patch_connections
                 WHERE to_patch_id = ANY($2::uuid[]) AND COALESCE(status, 'active') = 'active'
                UNION SELECT pc2.patch_id FROM patch_cues pc1
                       JOIN patch_cues pc2 ON pc2.cue = pc1.cue AND pc2.patch_id <> pc1.patch_id
                      WHERE pc1.patch_id = ANY($2::uuid[])
                      GROUP BY pc2.patch_id HAVING count(DISTINCT pc1.cue) >= 2
            )
            SELECT DISTINCT cp.patch_id::text AS patch_id, cp.patch_type, cp.value->>'text' AS text,
                   cp.value->>'owner' AS owner, cp.value->>'deadline_date' AS deadline_date
              FROM context_patches cp
              JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
              JOIN refs ON refs.pid = cp.patch_id
             WHERE ps.subject_key = $1
               AND cp.patch_type IN ('commitment','blocker','deliverable')
               AND COALESCE(cp.status, 'active') = 'active'
               AND cp.completed_at IS NULL
               AND (cp.project_id = $3 OR cp.project_id IS NULL)
               AND NOT (cp.patch_id = ANY($2::uuid[]))
             LIMIT 6
            """,
            subject_key, ids, project_id,
        )
        out = []
        for r in rows:
            d = r["deadline_date"]
            out.append({
                "patch_id": r["patch_id"], "patch_type": r["patch_type"], "text": r["text"],
                "owner": r["owner"], "deadline_date": d,
                "overdue": bool(d and re.match(r"^\d{4}-\d{2}-\d{2}$", d) and d < today_iso),
            })
        return out

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
