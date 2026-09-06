"""
Context Quilt - Hot Path API (FastAPI)
Implements 'Zero-Latency' Context Enrichment & MCP Endpoints
"""

from fastapi import FastAPI, HTTPException, Depends, Header, Request, Query, status, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional, Dict, Any, Union
import asyncio
import hashlib
import os
import redis.asyncio as redis
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
import sys

import structlog

logger = structlog.get_logger()

# Add src/ to sys.path so `from contextquilt.X` and `from dashboard.X`
# resolve unambiguously. Mirrors `src/worker.py:23`. Without this,
# uvicorn (loading us as `src.main`) leaves PYTHONPATH=/app only —
# any `from contextquilt.X` imports inside auth.py, dashboard/router.py,
# or the services modules would fail with ModuleNotFoundError. Worse,
# if both `from src.contextquilt.X` and `from contextquilt.X` resolve,
# they load as two different modules and the Settings singleton splits
# (rotation in the dashboard wouldn't propagate to the LLM clients).
# One canonical bare-prefix path eliminates both issues.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Populate os.environ from Secret Manager before Settings builds.
# No-op when CQ_GCP_PROJECT is unset (local dev), so this is safe at
# import time on every entry point.
from contextquilt.secrets import ensure_secrets_in_env
ensure_secrets_in_env()
from contextquilt.config import get_settings

from dashboard.router import router as dashboard_router
from contextquilt.routers.app_schemas import router as app_schemas_router
from contextquilt.services.recall_scorer import score_patches
from contextquilt.services import insight_cards
from contextquilt.services import headlines as headlines_svc
from contextquilt.services import alignment as alignment_svc
from contextquilt.services import item_ledger
from contextquilt.services import decay_model
from contextquilt.services import people_signals
from contextquilt.services import people_i18n
from contextquilt.services import woven_digest as woven_digest_svc
from contextquilt.services import facet_runtime
from contextquilt.services.consolidation import (
    CLUSTER_WINDOW_DAYS,
    build_insight_readiness,
    manifest_declares_person_insights,
    person_insight_rule,
)
from contextquilt.services.extraction_schema import is_user_reference
from contextquilt.services.completion_time import (
    CompletedAtError,
    parse_completed_at,
)
from contextquilt.services.recall_formatter import (
    format_people_scope,
    CHARS_PER_TOKEN,
    format_category_grouped,
    format_flat_ranked_with_stats,
    resolve_max_age_days,
    resolve_token_budget,
)
from contextquilt.services.people_signals import (
    compute_person_signals,
    compute_question_totals,
    last_seen_in,
    presence_anchor,
)
from contextquilt.services.person_appearances import (
    SPEAKER_METRICS,
    plan_speaker_map,
    reassignment_presence,
    reassignment_presence_target,
)
from contextquilt.services.people_network import (
    MIN_SHARED_MEETINGS as NETWORK_MIN_SHARED,
    NODE_CAP as NETWORK_NODE_CAP,
    SNAPSHOT_VERSION as NETWORK_SNAPSHOT_VERSION,
)
from contextquilt.services import described_as
from contextquilt.services import who_they_are
from contextquilt.services import trajectory as trajectory_svc
from contextquilt.services import project_meetings
from contextquilt.services import project_resolve
from contextquilt.services import project_roster
from contextquilt.services.entity_aliasing import person_candidates, tokenize_name
from contextquilt.services.people_identity import (
    IdentityRequestError,
    STATED_TITLE_SQL,
    SUPERSEDE_PRIOR_STATED_ROLE_SQL,
    apply_stated_titles,
    candidate_payload,
    describes_target,
    owner_names_multiple,
    owner_is_placeholder,
    owned_by_self_verdict,
    canonical_pair,
    capability_report,
    choose_surviving_person_patch,
    is_self_owned,
    manifest_declares_owed_to,
    merge_project_rollups,
    normalise_merge_request,
    owner_keys,
    resolve_identity_source,
    separation_conflicts,
    validate_person_name,
    DEFAULT_PEOPLE_VOCABULARY,
    PeopleVocabulary,
    build_entity_resolver,
    people_vocabulary,
    stated_roles_payload,
)
from contextquilt.services.cue_matching import build_cue_fetch, match_cues
from contextquilt.services.recall_scope import (
    build_conduct_fetch, build_flat_fetch, build_scoped_count,
)
from contextquilt.services import origin_project
from contextquilt.services.entity_match import (
    BARE_NAME_CANDIDATES_SQL, bare_terms, disambiguate_bare_names, match_entity_names,
    owner_tokens,
)
from contextquilt.services.recall_signals import (
    build_coverage_line,
    build_signal_lines,
    extract_unmatched_mentions,
    memory_signals_enabled,
)

import asyncpg
import uuid
import secrets
from fastapi.security import OAuth2PasswordRequestForm
import auth

_settings = get_settings()

# Initialize FastAPI app
app = FastAPI(
    title="Context Quilt API",
    description="Intelligent AI Gateway & Memory Layer",
    version="3.10.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Database Connection Pool
db_pool = None

@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(_settings.database_url)

@app.on_event("shutdown")
async def shutdown():
    if db_pool:
        await db_pool.close()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Dashboard Static Files
# Ensure the directory exists
dashboard_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "dashboard")
if os.path.exists(dashboard_path):
    app.mount("/dashboard", StaticFiles(directory=dashboard_path, html=True), name="dashboard")

# Include Dashboard Router
app.include_router(dashboard_router)

# Include App Schema Registration Router (admin-authenticated)
app.include_router(app_schemas_router)

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard/")

# Redis Connection (Working Memory).
# Use the composed URL from Settings — REDIS_URL wins if set, otherwise
# host/port/password are composed identically to the previous inline build.
redis_client = redis.from_url(_settings.redis_url, decode_responses=True)

# ============================================
# Models
# ============================================

class EnrichRequest(BaseModel):
    """Request to enrich a prompt template"""
    user_id: str = Field(..., description="Target User ID")
    template: str = Field(..., description="Prompt template with [[placeholders]]")
    context: Optional[Dict[str, Any]] = Field(default=None, description="Additional context to merge")
    format: Optional[str] = Field(default="text", description="Output format: 'text' (default) or 'json'")

class EnrichResponse(BaseModel):
    """Enriched prompt response"""
    enriched_prompt: Optional[str] = None
    variables: Optional[Dict[str, Any]] = None
    used_variables: List[str]
    missing_variables: List[str]

class MemoryUpdate(BaseModel):
    """Memory update request (MCP Tool / Trace Log)"""
    user_id: str
    interaction_type: str = Field(..., description="'chat_log', 'tool_call', 'trace', 'meeting_summary', 'structured_patches', 'correction', or 'completion'")
    agent_id: Optional[str] = None
    
    # For 'tool_call' (Active Learning)
    fact: Optional[str] = None
    category: Optional[str] = None
    confidence: Optional[float] = None
    
    # For 'trace' (Passive Learning)
    # Supports "Internal Monologue" and "Tool Inputs/Outputs"
    input: Optional[Dict[str, Any]] = None
    execution_trace: Optional[List[Dict[str, Any]]] = None
    output: Optional[Dict[str, Any]] = None
    
    # For 'chat_log' (Legacy/Simple)
    messages: Optional[List[Dict[str, Any]]] = None

    # For 'meeting_summary' (ShoulderSurf via CloudZap)
    summary: Optional[str] = None

    # For 'query' (user query + optional LLM response)
    content: Optional[str] = None
    response: Optional[str] = None

    # For 'structured_patches' (apps that already emit typed signals, e.g.
    # Tech Rehearsal). Pre-typed patches go straight to the store, skipping
    # LLM extraction; validated against the app's registered manifest. Raw
    # transcript fields above (summary/content/messages) are rejected on this
    # path — structured ingest carries only distilled, typed signals.
    patches: Optional[List[Dict[str, Any]]] = None
    entities: Optional[List[Dict[str, Any]]] = None
    relationships: Optional[List[Dict[str, Any]]] = None

    # For 'correction' (context-flow contract item 9): the user's
    # correction text rides in `content`; context_block optionally carries
    # the recall block that was injected when the user corrected it —
    # candidate matching prefers what the user was actually looking at.
    # Never persisted; never the model's response.
    context_block: Optional[str] = None

    # Optional timestamp for backdating (e.g. historical import)
    timestamp: Optional[str] = None

    # Generic metadata — app-defined key-value pairs (e.g., origin_id, origin_type, project)
    # CQ stores these alongside extracted facts for filtering and grouping.
    metadata: Optional[Dict[str, Any]] = None

    # Memory classification (for decay system)
    patch_type: Optional[str] = None  # 'identity', 'preference', 'trait', 'context', 'relationship'
    persistence: Optional[str] = None # 'permanent', 'sticky', 'ephemeral', 'decaying'
    source: Optional[str] = None      # 'explicit', 'inferred', 'external', 'system'

class RecallRequest(BaseModel):
    """Request to recall relevant context from the graph"""
    user_id: str = Field(..., description="User ID")
    text: str = Field(..., description="Query or transcript text to match entities against")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Optional hints: project_id/project (scope), locale (grouped-mode labels), token_budget (int, flat-mode context size, default 700, clamped 100-2000), max_age_days (int >= 1: serve meeting-bound memory observed in the last N UTC days only; universal self-disclosure types exempt; absent = no window), memory_signals (truthy: append explicit metamemory gap lines to the context block)")
    max_hops: Optional[int] = Field(default=2, description="Graph traversal depth")
    output_format: Optional[str] = Field(
        default="flat",
        description="'flat' (query-scoped ranked list, default) or 'grouped' (category-grouped block)",
    )
    max_patches: Optional[int] = Field(
        default=15,
        description="Upper bound on patches surfaced in the output (flat mode). Default 15.",
    )

class RecallResponse(BaseModel):
    """Context recalled from the graph"""
    context: str
    matched_entities: List[str]
    matched_patch_ids: List[str] = []
    matched_cues: List[str] = []
    patch_count: int
    communication_style: Optional[str] = None
    timing_ms: Optional[Dict[str, float]] = None
    # What this recall could not use, as MEETINGS (GP, 2026-08-23: the
    # two real memory moments an upgrade can name). Absent when CQ did
    # not compute it (no project scope, or the condition does not
    # apply); present with zeros when it applies and nothing was kept
    # out. Definitions ride on the wire.
    excluded: Optional[Dict[str, Any]] = None

# ============================================
# Recall constants
# ============================================

# Redis TTL for the entity index. Recall lazily rehydrates from Postgres on
# miss and slides the TTL forward on every hit, so a steady stream of recall
# calls keeps the cache warm indefinitely. Prewarm uses the same value.
ENTITY_INDEX_TTL = 7200  # 2 hours

# Entity index contents: canonical names plus recorded aliases, so a
# query mentioning a surface form ("S. Abrams") still matches the
# canonical entity ("Lockridge Abrams"). Every entity_index builder must use
# this — a name-only rebuild silently breaks alias matching.
# Suppressed entities ("not a person") are excluded along with their
# aliases: their names must stop matching in recall, or the assistant
# keeps greeting ASR garbage the user explicitly disowned. Merged
# entities stay IN the index on purpose (their names are aliases of a
# canonical); suppression is the opposite claim.
ENTITY_INDEX_NAMES_SQL = """
    SELECT name FROM entities WHERE user_id = $1 AND suppressed_at IS NULL
    UNION
    SELECT a.alias FROM entity_aliases a
    JOIN entities e ON e.entity_id = a.entity_id
    WHERE a.user_id = $1 AND e.suppressed_at IS NULL
"""

# Cue index contents: distinct active-patch cues (associative-retrieval
# topic phrases from patch_cues, written by the extraction pipeline).
# Lives in its own Redis set (cue_index:{user_id}) rather than inside
# entity_index — entity members also drive the formatter header and
# graph traversal, which cues must never do. Cues are stored lowercase
# by sanitize_cues, so matching against lowered request text is direct.
CUE_INDEX_NAMES_SQL = """
    SELECT DISTINCT pc.cue
    FROM patch_cues pc
    JOIN patch_subjects ps ON ps.patch_id = pc.patch_id
    JOIN context_patches cp ON cp.patch_id = pc.patch_id
    WHERE ps.subject_key = 'user:' || $1
      AND COALESCE(cp.status, 'active') = 'active'
"""

# Short-TTL cache on the rendered RecallResponse body (context, matched
# entities, patch ids, comm style). Same (user_id + request shape) within
# the TTL window returns byte-identical output, which is required for
# upstream prompt caching (Anthropic 5-min cache_control) to actually
# hit. Trade-off: cold-path patch writes that land mid-window won't be
# visible to recall for up to RECALL_RENDER_CACHE_TTL seconds. Kept short
# so meeting-level extractions surface quickly. If staleness becomes a
# problem before this is merged, swap to a per-user version-bump key
# that the worker invalidates on patch insert/update.
RECALL_RENDER_CACHE_TTL = 30  # seconds

# Recall renders slower than this (ms, server-side) log a warning with
# the full phase breakdown. GP's recall budget is 500ms (ratified
# 2026-07-18 after a 200ms timeout silently cost a turn its block);
# this threshold surfaces the tail while it is still well inside that
# budget, so degradation is visible before it costs anything.
RECALL_SLOW_RENDER_MS = int(os.getenv("CQ_RECALL_SLOW_MS", "150"))

# ---------------------------------------------------------------
# Recall timings: every step key is a DELTA for the step it names, and
# the parts are made to add up out loud.
#
# What this is for, 2026-08-27. A recall took 740ms and its line read
# `redis_entity_lookup: 247.04`. That key was measured from the START OF
# THE REQUEST while every neighbour was a delta, so it silently included
# a vocabulary lookup that can touch Postgres and the render-cache read,
# and the vocabulary lookup had no key of its own at all. I read the
# label, believed Redis was slow, told another team a mechanism built on
# it, and had to retract. The number was real; the NAME was wrong, and
# nothing in the line could have revealed that.
#
# So: one key per step, each measuring only itself, each ending in `_ms`
# so the rule is checkable rather than remembered; and `unaccounted_ms`,
# which is the wall clock minus the parts. A gap now has somewhere to
# appear instead of hiding inside whichever key happens to start at t0.
# An instrument that cannot show its own blind spot is how a wrong
# attribution survives.
RECALL_STEP_TIMINGS = (
    "vocab_lookup_ms",
    "render_cache_lookup_ms",
    "entity_index_ms",
    "cue_index_ms",
    "postgres_entities_and_graph_ms",
    "postgres_patches_ms",
    "score_and_format_ms",
    "working_memory_ms",
    "render_cache_write_ms",
)

# Measured INSIDE one of the steps above. Summing these would double
# count, so they are reported and never added.
RECALL_NESTED_TIMINGS = ("entity_index_rehydrated_ms",)


def _stamp_recall_total(timings: dict, t0: float) -> dict:
    """Stamp `total` and the time the step keys do not account for.

    Called at every site that ends a recall, including the early
    returns, because a blind spot that only appears on the slow path is
    the one you will be reading when it matters.
    """
    timings["total"] = round((time.monotonic() - t0) * 1000, 2)
    parts = sum(
        float(timings.get(k) or 0) for k in RECALL_STEP_TIMINGS
    )
    timings["unaccounted_ms"] = round(timings["total"] - parts, 2)
    return timings

# ============================================
# i18n — Recall section labels by locale
# ============================================

_RECALL_LABELS = {
    "en": {
        "project": "Project",
        "people": "People",
        "connections": "Connections",
        "about_you": "About you",
        "decisions": "Decisions",
        "commitments": "Open commitments",
        "blockers": "Blockers",
        "roles": "Roles",
        "key_facts": "Key facts",
        "goals": "Goals",
    },
    "es": {
        "project": "Proyecto",
        "people": "Personas",
        "connections": "Conexiones",
        "about_you": "Sobre ti",
        "decisions": "Decisiones",
        "commitments": "Compromisos abiertos",
        "blockers": "Bloqueadores",
        "roles": "Roles",
        "key_facts": "Datos clave",
        "goals": "Objetivos",
    },
    "fr": {
        "project": "Projet",
        "people": "Personnes",
        "connections": "Connexions",
        "about_you": "À propos de vous",
        "decisions": "Décisions",
        "commitments": "Engagements en cours",
        "blockers": "Blocages",
        "roles": "Rôles",
        "key_facts": "Faits clés",
        "goals": "Objectifs",
    },
    "pt": {
        "project": "Projeto",
        "people": "Pessoas",
        "connections": "Conexões",
        "about_you": "Sobre você",
        "decisions": "Decisões",
        "commitments": "Compromissos abertos",
        "blockers": "Bloqueios",
        "roles": "Funções",
        "key_facts": "Fatos importantes",
        "goals": "Metas",
    },
    "ja": {
        "project": "プロジェクト",
        "people": "メンバー",
        "connections": "関係",
        "about_you": "あなたについて",
        "decisions": "決定事項",
        "commitments": "未完了のコミットメント",
        "blockers": "ブロッカー",
        "roles": "役割",
        "key_facts": "重要な事実",
        "goals": "目標",
    },
}

def _recall_labels(locale: str) -> dict:
    """Get recall section labels for a locale. Falls back to English."""
    return _RECALL_LABELS.get(locale[:2].lower(), _RECALL_LABELS["en"])


# ============================================
# Helpers
# ============================================

async def verify_application_access(
    request: Request,
    token: Optional[str] = Depends(auth.oauth2_scheme),
    x_app_id: Optional[str] = Header(None, alias="X-App-ID")
):
    """
    Verify Application Access via JWT (Strict) or App ID Header (Legacy).
    Enforces auth if 'enforce_auth' is True for the app.
    """
    # 1. Check for valid JWT (Strict Mode)
    if token:
        try:
            token_data = auth.verify_token(token, HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            ))
            return token_data.app_id
        except Exception:
             # Same reasoning as the token endpoint: the gateway turns our
             # 401 into a 502, so this log is the only place a bad or expired
             # bearer token is identifiable as an auth problem rather than as
             # the gateway being down.
             logger.warning("auth_bearer_rejected", path=str(request.url.path)[:120])
             raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # 2. Fallback to Legacy Mode (X-App-ID)
    app_id_to_check = x_app_id

    if not app_id_to_check:
        raise HTTPException(status_code=400, detail="X-App-ID header or Bearer Token required")

    # Verify the app_id exists in the applications table.
    # Legacy string IDs (e.g., "cloudzap") that aren't registered are rejected.
    try:
        if not db_pool:
            return app_id_to_check

        row = await db_pool.fetchrow(
            "SELECT app_id, enforce_auth FROM applications WHERE app_id = $1",
            app_id_to_check
        )

        if not row:
            # App ID not registered — reject
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unknown application. Register via POST /v1/auth/register.",
            )

        if row['enforce_auth']:
            # App requires JWT — X-App-ID alone is not enough
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required for this application. Use Bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return app_id_to_check
    except HTTPException:
        raise  # Re-raise our own HTTP exceptions
    except Exception as e:
        # DB error — fail closed for security
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to verify application access",
        )

async def get_working_memory(user_id: str) -> Dict[str, Any]:
    """Fetch hydrated profile from Redis"""
    key = f"active_context:{user_id}"
    data = await redis_client.get(key)
    if data:
        return json.loads(data)
    return {}

# ============================================
# Endpoints
# ============================================

@app.post("/v1/enrich", response_model=EnrichResponse, tags=["Hot Path"])
async def enrich_context(
    request: EnrichRequest,
    app_id: str = Depends(verify_application_access)
):
    """
    Context Substitution Endpoint.
    Replaces [[placeholders]] in the template with values from Working Memory.
    Supports returning raw JSON variables if format='json'.
    """
    # 1. Fetch Context (Fast Redis Lookup)
    profile = await get_working_memory(request.user_id)
    
    # 2. Parse Template
    # Matches [[variable]] or [[variable|default]]
    pattern = r"\[\[(.*?)(?:\|(.*?))?\]\]"
    matches = re.findall(pattern, request.template)
    
    enriched_text = request.template
    used_vars = []
    missing_vars = []
    resolved_variables = {}
    
    # 3. Substitute Values
    for var_name, default_val in matches:
        var_name = var_name.strip()
        val = profile.get("variables", {}).get(var_name)
        
        if val is not None:
            replacement = str(val)
            used_vars.append(var_name)
            resolved_variables[var_name] = val
        elif default_val:
            replacement = default_val
            used_vars.append(f"{var_name} (default)")
            resolved_variables[var_name] = default_val
        else:
            replacement = "" # Default to empty string if not found
            missing_vars.append(var_name)
            resolved_variables[var_name] = None
            
        # Replace in text (handle potential regex special chars in replacement if needed, but simple replace is safer here)
        # Reconstruct the exact match string to replace
        full_match = f"[[{var_name}|{default_val}]]" if default_val else f"[[{var_name}]]"
        enriched_text = enriched_text.replace(full_match, replacement)
        
    if request.format == "json":
        return EnrichResponse(
            enriched_prompt=None,
            variables=resolved_variables,
            used_vars=used_vars, # Note: Pydantic will map this to used_variables alias if configured, but here field name is used_variables
            used_variables=used_vars,
            missing_variables=missing_vars
        )
    
    return EnrichResponse(
        enriched_prompt=enriched_text,
        used_variables=used_vars,
        missing_variables=missing_vars
    )

from pathlib import Path as _Path

from contextquilt.services.schema_validator import validate_manifest

# Starter manifests per app archetype, baked into the image. Served to
# developers so a new app starts from a linted, working example instead
# of reverse-engineering another app's manifest.
MANIFEST_TEMPLATES_DIR = _Path(__file__).resolve().parent.parent / "templates" / "manifests"


def _load_manifest_template(name: str) -> Optional[Dict[str, Any]]:
    # Resolve against the directory listing — never join raw user input
    # into a filesystem path.
    for path in sorted(MANIFEST_TEMPLATES_DIR.glob("*.json")):
        if path.stem == name:
            try:
                return json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                return None
    return None


@app.post("/v1/schema/validate", tags=["App Schemas"])
async def validate_own_schema(
    manifest: Dict[str, Any],
    app_id: str = Depends(verify_application_access),
):
    """
    Lint a manifest WITHOUT registering it.

    Runs the exact validator the registration endpoint uses, against the
    calling app's identity, and returns every error at once. Nothing is
    written — iterate here until `valid` is true, then have the operator
    register it via the admin-gated POST /v1/apps/{app_id}/schema.

    Note: the manifest's app_id field must equal YOUR app id (templates
    ship with a REPLACE-WITH-YOUR-APP-ID placeholder).
    """
    is_valid, errors = validate_manifest(manifest, app_id)
    return {
        "valid": is_valid,
        "errors": errors,
        "app_id": app_id,
        "summary": {
            "ingest_mode": manifest.get("ingest_mode") or "extraction (default)",
            "patch_types": len(manifest.get("patch_types") or []),
            "connection_labels": len(manifest.get("connection_labels") or []),
            "entity_types": len(manifest.get("entity_types") or []),
            "longitudinal_types": [
                pt.get("domain_type")
                for pt in (manifest.get("patch_types") or [])
                if isinstance(pt, dict) and pt.get("longitudinal") is True
            ],
        },
    }


@app.get("/v1/schema/templates", tags=["App Schemas"])
async def list_manifest_templates(app_id: str = Depends(verify_application_access)):
    """
    List the starter manifest templates (one per app archetype).

    Fetch a full template via GET /v1/schema/templates/{name}, replace
    the app_id placeholder and adapt the domain types, lint it via
    POST /v1/schema/validate, then have the operator register it.
    """
    out = []
    for path in sorted(MANIFEST_TEMPLATES_DIR.glob("*.json")):
        try:
            m = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "name": path.stem,
            "display_name": m.get("display_name"),
            "ingest_mode": m.get("ingest_mode") or "extraction (default)",
            "description": m.get("description"),
        })
    return {"templates": out}


@app.get("/v1/schema/templates/{name}", tags=["App Schemas"])
async def get_manifest_template(
    name: str,
    app_id: str = Depends(verify_application_access),
):
    """Return one starter manifest template verbatim."""
    template = _load_manifest_template(name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"No manifest template named {name!r}.")
    return template


@app.get("/v1/schema", tags=["App Schemas"])
async def get_own_schema(app_id: str = Depends(verify_application_access)):
    """
    Return the caller's own registered schema.

    Uses app JWT auth (or X-App-ID legacy) to infer which app is asking
    and returns that app's current manifest. Clients use this to:
      - Build UI pickers data-driven (e.g., connection label matrix)
      - Detect drift between the manifest bundled at build time and
        what's registered server-side
      - Discover the current CQ-assigned revision for diff tooling

    404 if no schema has been registered for the calling app.
    """
    import uuid as _uuid
    try:
        app_uuid = _uuid.UUID(app_id)
    except (ValueError, AttributeError, TypeError):
        # Legacy non-UUID app IDs never have registered schemas
        raise HTTPException(
            status_code=404,
            detail="No schema registered for this application.",
        )

    row = await db_pool.fetchrow(
        """
        SELECT version, manifest, registered_at, registered_by
        FROM app_schemas
        WHERE app_id = $1
        ORDER BY version DESC
        LIMIT 1
        """,
        app_uuid,
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No schema registered for this application.",
        )
    manifest = row["manifest"]
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    return {
        "app_id": app_id,
        "version": row["version"],
        "registered_at": row["registered_at"].isoformat(),
        "registered_by": row["registered_by"],
        "manifest": manifest,
    }


async def _bump_patch_access(patch_ids: List[str]) -> None:
    """Record a recall hit on patch_usage_metrics for the returned patches.

    This is the read-side half of the usage feedback loop. The decay
    worker exempts patches whose last_accessed_at is recent from TTL
    archival — but until this existed, recall never wrote to the table
    (only the worker's dedup path did), so a patch recalled daily could
    still be archived as if nobody used it.

    Scheduled via asyncio.create_task AFTER the response is built —
    never on the hot path. Best-effort: failures are swallowed, recall
    must never break on metrics bookkeeping.
    """
    if not patch_ids:
        return
    try:
        await db_pool.execute(
            """
            UPDATE patch_usage_metrics
               SET access_count = access_count + 1,
                   last_accessed_at = NOW()
             WHERE patch_id = ANY($1::uuid[])
            """,
            patch_ids,
        )
    except Exception:
        # Best-effort, matching the render-cache error posture above.
        pass


@app.post("/v1/recall", response_model=RecallResponse, tags=["Hot Path"])
async def recall_context(
    request: RecallRequest,
    app_id: str = Depends(verify_application_access),
):
    """
    Intelligent Recall: Send text, get relevant context back.

    Matches entity names in the text against the user's entity graph,
    traverses relationships 1-2 hops deep, and returns a formatted
    context block ready to inject into an LLM prompt.

    No LLM call — this is pure graph traversal. Target: <10ms.
    """
    user_id = request.user_id
    text_lower = request.text.lower()
    t0 = time.monotonic()
    timings = {}

    # Render-cache lookup. Identical request shape within
    # RECALL_RENDER_CACHE_TTL returns the previously rendered body so that
    # upstream prompt caches (Anthropic cache_control) see a byte-stable
    # prefix. timing_ms is rebuilt fresh on every call so the cache is
    # observable in dashboards.
    vocab_t = time.monotonic()
    recall_vocab = await _people_vocab_cached(app_id)
    timings["vocab_lookup_ms"] = round((time.monotonic() - vocab_t) * 1000, 2)
    cache_key_payload = {
        "text": request.text,
        "metadata": request.metadata or {},
        # Two apps whose vocabularies disagree about which entity type
        # is "a person" must not share a rendered header (doc 18 keeps
        # their subject spaces apart, but the key must not rely on it).
        "person_entity_type": recall_vocab.person_entity_type,
        "max_hops": request.max_hops if request.max_hops is not None else 2,
        "output_format": request.output_format or "flat",
        "max_patches": request.max_patches if request.max_patches is not None else 15,
    }
    cache_key_hash = hashlib.sha256(
        json.dumps(cache_key_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    render_cache_key = f"recall:render:{user_id}:{cache_key_hash}"
    cache_t = time.monotonic()
    cached_blob = None
    try:
        cached_blob = await redis_client.get(render_cache_key)
    except Exception:
        # Cache is best-effort — never fail recall on Redis hiccups.
        cached_blob = None
    timings["render_cache_lookup_ms"] = round((time.monotonic() - cache_t) * 1000, 2)
    if cached_blob:
        try:
            cached = json.loads(cached_blob)
            _stamp_recall_total(timings, t0)
            timings["render_cache_hit"] = 1
            # A cache hit is still a recall — the patches were served to
            # the caller, so they count as accessed.
            asyncio.create_task(
                _bump_patch_access(cached.get("matched_patch_ids", []))
            )
            return RecallResponse(
                context=cached["context"],
                matched_entities=cached["matched_entities"],
                matched_patch_ids=cached.get("matched_patch_ids", []),
                matched_cues=cached.get("matched_cues", []),
                patch_count=cached["patch_count"],
                communication_style=cached.get("communication_style"),
                excluded=cached.get("excluded"),
                timing_ms=timings,
            )
        except (json.JSONDecodeError, KeyError):
            # Corrupt cache entry — drop it and fall through to a fresh build.
            try:
                await redis_client.delete(render_cache_key)
            except Exception:
                pass

    # Step 1: Find matching entities from Redis index (fast)
    entity_index_key = f"entity_index:{user_id}"
    entity_t = time.monotonic()
    known_entities = await redis_client.smembers(entity_index_key)

    # Cache-miss self-heal. Without this, recall silently degrades to empty
    # ~ENTITY_INDEX_TTL after the last prewarm — a session that runs longer
    # than the TTL stops returning entity matches.
    if not known_entities:
        rehydrate_t = time.monotonic()
        rows = await db_pool.fetch(ENTITY_INDEX_NAMES_SQL, user_id)
        if rows:
            names = [r["name"] for r in rows]
            await redis_client.delete(entity_index_key)
            await redis_client.sadd(entity_index_key, *names)
            await redis_client.expire(entity_index_key, ENTITY_INDEX_TTL)
            known_entities = set(names)
            timings["entity_index_rehydrated_ms"] = round((time.monotonic() - rehydrate_t) * 1000, 2)
    else:
        # Sliding TTL — keep an actively used cache from expiring under a long meeting.
        await redis_client.expire(entity_index_key, ENTITY_INDEX_TTL)

    # Renamed from `redis_entity_lookup`: it never bounded only Redis, and
    # it was the label that produced a wrong attribution on 2026-08-27.
    timings["entity_index_ms"] = round((time.monotonic() - entity_t) * 1000, 2)

    # Word-boundary match, sorted (services/entity_match.py). The bare
    # substring test this replaced let "RV" answer to "interview" and any
    # two-letter name to any word containing it (2026-09-04); the sort is
    # the byte-stable ordering prompt caching downstream depends on (GP
    # TestFlight, May 2026).
    matched_names = match_entity_names(known_entities or (), text_lower)

    # Also check metadata hints
    if request.metadata:
        for val in request.metadata.values():
            if isinstance(val, str) and val not in matched_names:
                # Check if this metadata value is a known entity
                if known_entities and val in known_entities:
                    matched_names.append(val)

    # Cue index — associative retrieval. Cues are topic phrases attached
    # to patches at extraction time ("pricing model"), so text that names
    # a topic but no entity still recalls the right patches. Same lazy
    # rehydrate + sliding TTL as the entity index; sorted iteration for
    # byte-stable output (same gotcha as matched_names above).
    cue_index_key = f"cue_index:{user_id}"
    cue_t = time.monotonic()
    known_cues = await redis_client.smembers(cue_index_key)
    if not known_cues:
        try:
            cue_rows_idx = await db_pool.fetch(CUE_INDEX_NAMES_SQL, user_id)
        except Exception:
            # patch_cues may not exist on a lagging DB (MCP runs the same
            # code against its own Postgres) — recall degrades to
            # entity-only matching, never errors.
            cue_rows_idx = []
        if cue_rows_idx:
            cue_values = [r["cue"] for r in cue_rows_idx]
            await redis_client.delete(cue_index_key)
            await redis_client.sadd(cue_index_key, *cue_values)
            await redis_client.expire(cue_index_key, ENTITY_INDEX_TTL)
            known_cues = set(cue_values)
    else:
        await redis_client.expire(cue_index_key, ENTITY_INDEX_TTL)
    # Untimed until 2026-08-27, so its cost landed in no key at all and
    # `unaccounted_ms` would have carried it. Same self-heal shape as the
    # entity index, so the same right to a number of its own.
    timings["cue_index_ms"] = round((time.monotonic() - cue_t) * 1000, 2)

    # Word-boundary matched, not a bare substring: see cue_matching for
    # what `if cue in text_lower` was actually matching on prod.
    matched_cues = match_cues(known_cues, text_lower)

    # Project scope decides whether an entity-less query is still answerable.
    # A scope-shaped question ("anyone have any commitments?") has no entity
    # names but, given a project_id, can still pull the right patches via
    # the project-scoped fetch in step 4.
    recall_project_id = request.metadata.get("project_id") if request.metadata else None
    recall_project = request.metadata.get("project") if request.metadata else None
    has_project_scope = bool(recall_project_id or recall_project)

    signals_enabled = memory_signals_enabled(request.metadata)

    # Everything the store indexes under — entity names, aliases, cues.
    # Metamemory suppression checks against this: a mention covered by a
    # cue is not a memory gap.
    known_index_terms = set(known_entities or ()) | set(known_cues or ())

    if not matched_names and not has_project_scope and not matched_cues:
        # Metamemory (opt-in): an empty result is the most dangerous
        # place to stay silent — the downstream LLM fills the silence
        # with confabulated context. Say "checked, nothing there"
        # explicitly instead. Not render-cached, same as the other
        # empty-ish bodies: cheap to recompute, and we don't want a
        # transient gap pinned for the cache TTL.
        if signals_enabled:
            signal_lines = build_signal_lines(
                extract_unmatched_mentions(request.text, known_index_terms),
                nothing_matched=True,
            )
            _stamp_recall_total(timings, t0)
            return RecallResponse(
                context="\n".join(signal_lines),
                matched_entities=[],
                patch_count=0,
                timing_ms=timings,
            )
        # This exit never stamped a total, before or after #334, so it
        # returned step timings with no clock to check them against and
        # no `unaccounted_ms` at all. Found by running a real recall on
        # prod rather than by reading the tests, which is the whole
        # argument for going and looking at the artifact.
        _stamp_recall_total(timings, t0)
        return RecallResponse(context="", matched_entities=[], patch_count=0, timing_ms=timings)

    # Step 2 & 3: entity rows + graph traversal — only meaningful when we
    # actually matched entities. The project-scope fallthrough skips this and
    # goes straight to the patch fetch.
    entity_rows: list = []
    rel_rows: list = []
    if matched_names:
        t1 = time.monotonic()
        # Alias-aware: a matched name may be a recorded surface form
        # ("S. Abrams"), so resolve it to the canonical entity and let
        # graph traversal see the full relationship neighborhood.
        #
        # ALSO merge-aware, and that half was missing until 2026-08-07.
        # A merge marks the folded entity with a forward pointer instead
        # of deleting it, and the dead row keeps its own name and its own
        # description. This query matched that row by name, so after
        # merging four spellings of one person the header rendered
        # "People: Vijay Rayudu (...); Vijay R (...); Vijay Rayud" and
        # the model was told one human was three. That is precisely the
        # split brain a merge exists to resolve, surviving in the recall
        # lane because only the WRITE path hopped the pointer
        # (worker._resolve_merged_forward). Two predicates for one
        # concept, and only one of them maintained.
        #
        # Resolving forward rather than just excluding folded rows: a
        # merge records the loser's name as an alias on the survivor, so
        # the canonical usually matches anyway and excluding would be
        # enough. Usually is not a guarantee. If that alias is ever
        # missing, excluding silently DROPS the match, while resolving
        # substitutes the survivor and DISTINCT collapses the pair.
        #
        # Depth 8 matches the write path's cap. A cycle cannot outrun it,
        # and a chain deeper than 8 keeps the last id reached rather than
        # failing the recall.
        entity_rows = await db_pool.fetch(
            """
            WITH RECURSIVE matched AS (
                SELECT DISTINCT e.entity_id
                FROM entities e
                LEFT JOIN entity_aliases a ON a.entity_id = e.entity_id
                WHERE e.user_id = $1 AND (e.name = ANY($2) OR a.alias = ANY($2))
            ),
            walk AS (
                SELECT m.entity_id AS start_id, m.entity_id AS current_id, 0 AS depth
                FROM matched m
                UNION ALL
                SELECT w.start_id, e.merged_into, w.depth + 1
                FROM walk w
                JOIN entities e ON e.entity_id = w.current_id
                WHERE e.merged_into IS NOT NULL AND w.depth < 8
            ),
            survivor AS (
                SELECT DISTINCT ON (start_id) start_id, current_id
                FROM walk ORDER BY start_id, depth DESC
            )
            SELECT DISTINCT e.entity_id, e.name, e.entity_type, e.description
            FROM survivor s
            JOIN entities e ON e.entity_id = s.current_id
            ORDER BY e.entity_id
            """,
            user_id, matched_names
        )

        # A bare first name is the contested form (#434 stopped the write
        # side trusting it; this is the read side). When the recall has a
        # project and a single-token match could mean several people, the
        # people with presence in that project are the answer, and a
        # namesake from another project leaves the header.
        terms = bare_terms(matched_names)
        if terms and recall_project_id:
            try:
                candidates = await db_pool.fetch(
                    BARE_NAME_CANDIDATES_SQL, user_id, terms, recall_project_id,
                    recall_vocab.person_entity_type,
                )
                if candidates:
                    entity_rows = disambiguate_bare_names(
                        entity_rows, matched_names, candidates,
                        person_entity_type=recall_vocab.person_entity_type,
                    )
            except Exception as exc:
                # Lagging DB or a vocabulary without a person type: the
                # lookup's own answer stands, and the miss is audible.
                logger.warning("bare_name_disambiguation_failed",
                               user_id=user_id, terms=terms, error=str(exc)[:160])

        # PRECEDENCE, on the read side: a role the person STATED beats
        # the description a meeting INFERRED. This shipped on the person
        # detail route and nowhere else, so a stated title showed on the
        # page while the block kept repeating the inference to every AI
        # surface. Runs AFTER disambiguation deliberately: the title
        # belongs to whichever person that resolved to, and doing it
        # first would title a namesake who then left the header.
        #
        # Measured on real data, and re-measured after the query grew
        # its second matching leg: 7ms median at the 1 to 4 matched
        # names a real header carries, 12ms at a pathological 12. Fails
        # open, because a header carrying yesterday's description is a
        # worse block and a broken recall is no block at all.
        if entity_rows and recall_vocab.stated_role_type:
            try:
                title_rows = await db_pool.fetch(
                    STATED_TITLE_SQL,
                    f"user:{user_id}",
                    [r["name"] for r in entity_rows if r["name"]],
                    recall_vocab.stated_role_type,
                )
                if title_rows:
                    entity_rows = apply_stated_titles(
                        entity_rows, title_rows,
                        person_entity_type=recall_vocab.person_entity_type,
                    )
            except Exception as exc:
                logger.warning("stated_title_lookup_failed",
                               user_id=user_id, error=str(exc)[:160])

        if entity_rows:
            entity_ids = [row["entity_id"] for row in entity_rows]
            max_hops = request.max_hops or 2
            rel_rows = await db_pool.fetch(
                """
                WITH RECURSIVE graph AS (
                    -- Seed: relationships from/to matched entities
                    SELECT r.from_entity_id, r.to_entity_id, r.relationship_type, r.context,
                           1 as depth
                    FROM relationships r
                    WHERE r.user_id = $1
                      AND (r.from_entity_id = ANY($2) OR r.to_entity_id = ANY($2))

                    UNION

                    -- Hop: follow edges from discovered entities
                    SELECT r.from_entity_id, r.to_entity_id, r.relationship_type, r.context,
                           g.depth + 1
                    FROM relationships r
                    JOIN graph g ON (r.from_entity_id = g.to_entity_id OR r.from_entity_id = g.from_entity_id
                                  OR r.to_entity_id = g.from_entity_id OR r.to_entity_id = g.to_entity_id)
                    WHERE r.user_id = $1 AND g.depth < $3
                )
                SELECT g.from_entity_id, g.to_entity_id, g.relationship_type, g.context,
                       MIN(g.depth) AS depth,
                       e1.name as from_name, e1.entity_type as from_type,
                       e2.name as to_name, e2.entity_type as to_type
                FROM graph g
                JOIN entities e1 ON g.from_entity_id = e1.entity_id
                JOIN entities e2 ON g.to_entity_id = e2.entity_id
                GROUP BY g.from_entity_id, g.to_entity_id, g.relationship_type, g.context,
                         e1.name, e1.entity_type, e2.name, e2.entity_type
                -- Nearest edges first: the formatter renders the first five,
                -- and ordered by id a two-hop walk through a hub (the user's
                -- own entity) served another project's reporting lines
                -- under a chat that never mentioned them (2026-09-04).
                ORDER BY depth, e1.name, e2.name, g.relationship_type
                """,
                user_id, entity_ids, max_hops
            )
        timings["postgres_entities_and_graph_ms"] = round((time.monotonic() - t1) * 1000, 2)

    # Step 4: Get patches for this user
    t2 = time.monotonic()
    subject_key = f"user:{user_id}"

    # One runtime snapshot for the whole recall: the universal-leg and
    # overdue-guarantee type filters and the scorer's facet/freshness/
    # deadline sets all read the SAME registered-manifest facts (cached;
    # no per-call registry round trip). For SS every set equals the
    # pinned floor, so every byte of SS output is unchanged.
    type_runtime = await facet_runtime.get_type_runtime(db_pool.fetch)

    # Recall age window (tier contract, 2026-08-21). metadata.max_age_days
    # bounds meeting-bound memory to the last N days: a row is served when
    # its most recent observation, COALESCE(last_observed_at, created_at),
    # falls on or after (today_utc - N). Universal self-disclosure types
    # are exempt (a preference does not expire on day 31). One predicate,
    # applied to EVERY leg below including the overdue guarantee, the cue
    # leg and the coverage denominator, so "showing N of M" is M as this
    # tier sees it. NULL means no window, and the predicate short-circuits
    # to TRUE, so an unwindowed request returns exactly the pre-window
    # rows. Day-bucketed like the scorer's clock: byte-stable within a
    # UTC day. The number is the gateway's per-tier dial; CQ never
    # defaults it. Same SQL text either way, so the planner sees one shape.
    # Does this database carry the ingest's project record? Probed once
    # per process; the MCP deployment can lag migrations and a leg naming
    # a missing table would 500 the hot path (services/origin_project.py).
    include_assignments = await origin_project.assignments_available(db_pool.fetch)

    max_age_days = resolve_max_age_days(request.metadata)
    universal_types = list(type_runtime.universal_recall_types)
    AGE = (
        "AND ({d}::int IS NULL OR cp.patch_type = ANY({u}::text[]) "
        "OR COALESCE(cp.last_observed_at, cp.created_at)::date "
        ">= ((NOW() AT TIME ZONE 'utc')::date - {d}::int))"
    )

    # Step 4a: Flat patch query (works for both V1 and V2 patches)
    # cp.patch_id is the secondary sort everywhere — created_at ties on
    # microsecond-equal inserts (workers batching) gave undefined order,
    # which silently drove different rendered strings for identical inputs.
    if recall_project_id or recall_project:
        # Two windows, one round trip, and the same admission rule the cue
        # leg uses. See services/recall_scope.py for the 2026-09-04
        # measurement that motivated both: a bare `project_id IS NULL`
        # clause let every origin-scoped row from every other meeting in,
        # and one LIMIT across the lot let the newest unrelated meeting
        # evict the project's own decisions.
        flat_sql, flat_args = build_flat_fetch(
            subject_key, universal_types, max_age_days, AGE.format(d="$4", u="$3"),
            recall_project_id=recall_project_id, recall_project=recall_project,
            include_assignments=include_assignments,
        )
        fact_rows = await db_pool.fetch(flat_sql, *flat_args)
    else:
        # No project context — only return universal patches (traits, preferences).
        # Project-scoped patches (commitments, blockers, decisions) from other projects
        # would be irrelevant noise in an unrelated session.
        fact_rows = await db_pool.fetch(
            """
            SELECT cp.patch_id, cp.value, cp.patch_type, cp.source_prompt,
                   cp.created_at, cp.last_observed_at
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND cp.patch_type = ANY($2::text[])
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY cp.created_at DESC, cp.patch_id ASC
            LIMIT 20
            """,
            subject_key, list(type_runtime.universal_recall_types)
        )

    # Step 4b: Traverse patch connections from project patches (Connected Quilt V2)
    connected_rows = []
    project_patch = None
    if recall_project_id or recall_project:
        if recall_project_id:
            project_patch = await db_pool.fetchrow(
                """
                SELECT cp.patch_id FROM context_patches cp
                JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
                WHERE ps.subject_key = $1 AND cp.patch_type = 'project'
                  AND cp.project_id = $2
                  AND COALESCE(cp.status, 'active') = 'active'
                LIMIT 1
                """,
                subject_key, recall_project_id
            )
        else:
            # Fallback: fuzzy match on project name
            project_patch = await db_pool.fetchrow(
                """
                SELECT cp.patch_id FROM context_patches cp
                JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
                WHERE ps.subject_key = $1 AND cp.patch_type = 'project'
                  AND LOWER(cp.value->>'text') LIKE '%' || LOWER($2) || '%'
                  AND COALESCE(cp.status, 'active') = 'active'
                LIMIT 1
                """,
                subject_key, recall_project
            )
        if project_patch:
            connected_rows = await db_pool.fetch(
                """
                SELECT cp.patch_id, cp.value, cp.patch_type, cp.source_prompt,
                       cp.created_at, cp.last_observed_at
                FROM patch_connections pc
                JOIN context_patches cp ON pc.from_patch_id = cp.patch_id
                WHERE pc.to_patch_id = $1 AND pc.connection_role = 'parent'
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND COALESCE(pc.status, 'active') = 'active'
                  {AGE}
                ORDER BY cp.created_at DESC, cp.patch_id ASC
                """.replace("{AGE}", AGE.format(d="$3", u="$2")),
                project_patch["patch_id"], universal_types, max_age_days
            )

    # Overdue guarantee: an overdue commitment/blocker in this project
    # must surface even when it's older than the latest-20 window above —
    # it's the single most "needs attention" item in the quilt, and
    # before this it could age out of recall entirely while still open.
    # Day-grain condition (flips at UTC midnight) keeps rendered output
    # byte-stable within a day, consistent with the scorer's clock.
    #
    # The guarantee is age-capped at 30 days past deadline. Without the
    # cap, a dead-but-unclosed item is guaranteed into every scoped
    # recall forever: the fetch bumps last_accessed_at, the bump exempts
    # it from decay, and the loop never ends (observed on prod: a
    # 2024-deadline commitment at 199 accesses). Items past the cap can
    # still surface through the normal recency/entity/cue legs; they
    # just lose the guaranteed slot, so decay can eventually run from
    # GREATEST(updated_at, deadline_date).
    overdue_rows: list = []
    if recall_project_id or recall_project:
        proj_col, proj_val = (
            ("cp.project_id", recall_project_id) if recall_project_id
            else ("cp.project", recall_project)
        )
        overdue_rows = await db_pool.fetch(
            f"""
            SELECT cp.patch_id, cp.value, cp.patch_type, cp.source_prompt,
                   cp.created_at, cp.last_observed_at
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1 AND {proj_col} = $2
              AND cp.patch_type = ANY($3::text[])
              AND COALESCE(cp.status, 'active') = 'active'
              AND cp.completed_at IS NULL
              AND cp.value->>'deadline_date' ~ '^\\d{{4}}-\\d{{2}}-\\d{{2}}$'
              AND (cp.value->>'deadline_date')::date < (NOW() AT TIME ZONE 'utc')::date
              AND (cp.value->>'deadline_date')::date >= ((NOW() AT TIME ZONE 'utc')::date - 30)
              {AGE.format(d='$5', u='$4')}
            ORDER BY cp.value->>'deadline_date' ASC, cp.patch_id ASC
            LIMIT 5
            """,
            subject_key, proj_val, list(type_runtime.completable_types),
            universal_types, max_age_days
        )

    # Conduct guarantee: a named person's conduct reaches the candidate
    # set even when it is older than the latest-20 window. Measured on
    # 2026-09-06: one person had 35 conduct rows across six meetings and
    # the block rendered the shallowest one, because everything richer
    # predated the two most recent meetings. Bounded, project-scoped, and
    # it only ADDS candidates; the scorer and the flat cap still decide
    # what renders, and conduct ranks below the person's own rules.
    conduct_rows: list = []
    if type_runtime.conduct_types and matched_names and has_project_scope:
        try:
            conduct_sql, conduct_args = build_conduct_fetch(
                subject_key, type_runtime.conduct_types,
                owner_tokens(matched_names), universal_types, max_age_days,
                AGE.format(d="$4", u="$3"),
                recall_project_id=recall_project_id, recall_project=recall_project,
                include_assignments=include_assignments,
            )
            conduct_rows = await db_pool.fetch(conduct_sql, *conduct_args)
        except Exception as exc:
            # The block is still served without it, exactly as before.
            logger.warning("conduct_guarantee_failed", user_id=user_id,
                           error=str(exc)[:160])

    # Cue fetch leg: patches indexed under a matched cue surface directly,
    # outside the latest-20 window — this is the associative-recall path
    # ("the pricing model" pulls the pricing commitments even when no
    # entity name was spoken). Scoped to the caller's project when there
    # is one, on the flat leg's own predicate: see cue_matching for what
    # an unscoped cue leg served on 2026-08-30 and what is still open.
    cue_rows: list = []
    if matched_cues:
        cue_sql, cue_args = build_cue_fetch(
            subject_key, matched_cues, universal_types, max_age_days,
            AGE.format(d="$4", u="$3"),
            recall_project_id=recall_project_id,
            recall_project=recall_project,
            include_assignments=include_assignments,
        )
        try:
            cue_rows = await db_pool.fetch(cue_sql, *cue_args)
        except Exception:
            cue_rows = []  # lagging DB — see cue-index rehydrate above

    # Merge flat rows + connected + overdue + cue-matched, deduplicate by value text
    all_patches = list(fact_rows)
    seen_texts = {(row["value"] if isinstance(row["value"], str) else json.dumps(row["value"])) for row in fact_rows}
    for row in list(connected_rows) + list(overdue_rows) + list(conduct_rows) + list(cue_rows):
        key = row["value"] if isinstance(row["value"], str) else json.dumps(row["value"])
        if key not in seen_texts:
            all_patches.append(row)
            seen_texts.add(key)

    timings["postgres_patches_ms"] = round((time.monotonic() - t2) * 1000, 2)
    t3 = time.monotonic()

    # Step 5: Score and format the context block.
    #
    # Output mode (PR 4):
    #   - "flat" (default): relevance-ranked flat list, query-scoped,
    #     compact enough to drop into an LLM prompt as-is.
    #   - "grouped": category-grouped block with section headers, the
    #     pre-PR-4 shape. Retained for apps that want it.

    # Rank all patches against the query first. Cue-fetched patches get an
    # explicit boost — their text may share no words with the query (that
    # is the point of a cue), so keyword overlap alone would strand them
    # below the flat-mode cap.
    scored = score_patches(
        all_patches, request.text, matched_names,
        cue_matched_patch_ids={str(r["patch_id"]) for r in cue_rows},
        facet_by_type=type_runtime.facet_by_type,
        freshness_types=type_runtime.freshness_tracked_types,
        deadline_types=frozenset(type_runtime.completable_types),
        completable_types=frozenset(type_runtime.completable_types),
        conduct_types=type_runtime.conduct_types,
    )

    # Metamemory signals (opt-in): explicit gap lines appended below the
    # block. Deterministic from (text, entity index, scope), so they are
    # as byte-stable as the rest of the render; metadata.memory_signals
    # is part of the render-cache key, so flagged and unflagged callers
    # never share a cached body.
    signal_lines: List[str] = []
    if signals_enabled:
        # "No stored project memory" must mean exactly that: no project
        # patch, no project-scoped rows, no overdue completables. A
        # project can hold commitments without a project-type patch, so
        # project_patch alone is not evidence of absence.
        project_scope_missing = False
        if has_project_scope and project_patch is None and not overdue_rows and not connected_rows:
            if recall_project_id:
                scoped_hit = any(
                    r["project_id"] is not None and str(r["project_id"]) == str(recall_project_id)
                    for r in fact_rows
                )
            else:
                scoped_hit = any(r["project"] == recall_project for r in fact_rows)
            project_scope_missing = not scoped_hit
        signal_lines = build_signal_lines(
            extract_unmatched_mentions(request.text, known_index_terms),
            project_scope_label=recall_project or recall_project_id,
            project_scope_missing=project_scope_missing,
        )
    signal_block = "\n".join(signal_lines)

    # Coverage denominator (contract commitment E): how many active
    # patches this project scope actually holds. One indexed COUNT, only
    # on scoped requests; failure degrades to no coverage line.
    scoped_total = 0
    if has_project_scope:
        try:
            count_sql, count_args = build_scoped_count(
                subject_key, universal_types, max_age_days, AGE.format(d="$4", u="$3"),
                recall_project_id=recall_project_id, recall_project=recall_project,
                include_assignments=include_assignments,
            )
            scoped_total = await db_pool.fetchval(count_sql, *count_args) or 0
        except Exception:
            scoped_total = 0

    # The `excluded` block (GP #773, the memory upgrade moments). Two
    # indexed COUNTs at most, only on project-scoped requests, never a
    # second recall. Counted in MEETINGS because the copy says meetings.
    #   by_window: active meeting-bound rows in this project OUTSIDE the
    #     tier window (the AGE predicate inverted), as distinct origins,
    #     plus the oldest observation so "the one from May" is sayable.
    #     Present only when a window was sent.
    #   by_scope: on a people-scoped (Free) request, the meetings in this
    #     project holding memory the People render cannot use. This is
    #     scope size, not "matches that scored", because the people lane
    #     skips every memory leg and the scored set does not exist; the
    #     definition says so on the wire rather than in a docstring.
    # Day-bucketed like everything else here: byte-stable within a UTC
    # day, and it rides in the render cache with the context.
    excluded: Optional[dict] = None
    if has_project_scope:
        try:
            scope_col = "project_id" if recall_project_id else "project"
            scope_val = recall_project_id or recall_project
            excluded = {}
            if max_age_days is not None:
                row = await db_pool.fetchrow(
                    f"""
                    SELECT count(DISTINCT cp.origin_id) AS meetings,
                           min(COALESCE(cp.last_observed_at, cp.created_at)) AS oldest
                    FROM context_patches cp
                    JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                    WHERE ps.subject_key = $1 AND cp.{scope_col} = $2
                      AND COALESCE(cp.status, 'active') = 'active'
                      AND cp.origin_id IS NOT NULL
                      AND NOT (cp.patch_type = ANY($3::text[]))
                      AND COALESCE(cp.last_observed_at, cp.created_at)::date
                          < ((NOW() AT TIME ZONE 'utc')::date - $4::int)
                    """,
                    subject_key, scope_val, universal_types, max_age_days,
                )
                excluded["by_window"] = {
                    "meetings": int(row["meetings"] or 0),
                    "oldest": row["oldest"].isoformat() if row and row["oldest"] else None,
                    "max_age_days": max_age_days,
                    "definition": (
                        "Meetings in this project whose memory is older than max_age_days "
                        "and was therefore not available to this recall. Universal "
                        "self-disclosure types are never windowed and are not counted."
                    ),
                }
            if (request.metadata or {}).get("recall_scope") == "people":
                # Plain string + replace for BOTH placeholders: an f-string
                # here interpolated {AGE} before the placeholder swap and
                # shipped literal "{d}::int" to Postgres (syntax error,
                # degraded to no block; GP's proof caught it).
                n = await db_pool.fetchval(
                    """
                    SELECT count(DISTINCT cp.origin_id)
                    FROM context_patches cp
                    JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                    WHERE ps.subject_key = $1 AND cp.{SCOPE_COL} = $2
                      AND COALESCE(cp.status, 'active') = 'active'
                      AND cp.origin_id IS NOT NULL
                      {AGE}
                    """.replace("{SCOPE_COL}", scope_col).replace("{AGE}", AGE.format(d="$4", u="$3")),
                    subject_key, scope_val, universal_types, max_age_days,
                )
                excluded["by_scope"] = {
                    "meetings": int(n or 0),
                    "definition": (
                        "Meetings in this project that hold memory a people-scoped recall "
                        "cannot use. A count of the scope, not of matches: the people lane "
                        "runs no memory leg, so no scored set exists to subtract from."
                    ),
                }
            if not excluded:
                excluded = None
        except Exception as exc:
            logger.debug("recall_excluded_unavailable", error=str(exc)[:120])
            excluded = None

    # Cap for flat output — avoids runaway context blocks for users
    # with large quilts.
    flat_cap = request.max_patches or 15
    # The cap is applied to RENDERED rows inside the formatter now, not by
    # slicing here. A conduct row that folds into a person's capsule was
    # taking a slot in this slice and then leaving the list, so the block
    # shrank from 14 rows to 5 on prod (2026-09-06) the moment a person's
    # whole conduct history reached the candidate set. Grouped output is
    # unchanged: it always received the full set.
    scored_for_output = scored

    if request.output_format == "grouped":
        locale = request.metadata.get("locale", "en") if request.metadata else "en"
        labels = _recall_labels(locale)
        try:
            context = format_category_grouped(
                scored_for_output, entity_rows, rel_rows, labels,
                person_entity_type=recall_vocab.person_entity_type,
            )
        except Exception as fmt_exc:  # pragma: no cover — defensive; test coverage is flat-mode
            # An empty block and a formatter that raised are the same
            # observable to every caller, which is how a KeyError on a
            # missing locale label served "" to grouped-mode recalls for
            # months without anyone seeing it. Keep the safety, lose the
            # silence (same argument as the role-semantics decline log).
            logger.warning(
                "recall_grouped_format_failed",
                error=str(fmt_exc), locale=locale, user_id=user_id,
                patch_count=len(scored_for_output),
            )
            context = ""
    else:
        try:
            # Token budget (GP contract): metadata.token_budget, clamped,
            # default 700 tokens. Converted to the formatter's char cap at
            # ~4 chars/token. Lives in metadata, so it's automatically part
            # of the render-cache key above — two budgets never share a
            # cached body.
            token_budget = resolve_token_budget(request.metadata)
            # Signal + coverage lines ride inside the same token budget —
            # reserve their length so everything stays under it. Coverage
            # length isn't known until after formatting; reserve a fixed
            # 64 chars whenever a scoped total exists.
            trailing_reserve = (len(signal_block) + 2 if signal_block else 0) + (64 if scoped_total else 0)
            context, rendered_count = format_flat_ranked_with_stats(
                scored_for_output, entity_rows, rel_rows,
                max_chars=token_budget * CHARS_PER_TOKEN - trailing_reserve,
                person_entity_type=recall_vocab.person_entity_type,
                conduct_types=type_runtime.conduct_types,
                max_rows=flat_cap,
            )
            # Contract commitment E — truncation must be visible.
            coverage = build_coverage_line(rendered_count, scoped_total)
            if coverage:
                signal_block = f"{coverage}\n{signal_block}" if signal_block else coverage
        except Exception:
            # Emergency fallback: empty string, the endpoint still returns
            # matched entities and patch ids so callers aren't blocked.
            context = ""

    if signal_block:
        context = f"{context}\n\n{signal_block}" if context else signal_block
    timings["score_and_format_ms"] = round((time.monotonic() - t3) * 1000, 2)
    t4 = time.monotonic()

    # People-scoped lane (boundary piece 4, decision 2026-08-11): the
    # free-tier render. GP passes metadata.recall_scope="people" per
    # entitlement (GP owns tiering; CQ serves capability). The render is
    # EXACTLY what the People tab shows, assembled by the same
    # _people_core the tab is served from: header, relations, and each
    # matched person's ledger. Every memory leg is skipped: no fact
    # fetch, no cues, no metamemory, no communication profile (the
    # style model is memory-derived and on no free screen). The render
    # cache key includes metadata, so scoped and full renders never
    # share a body. Unknown scope values fall through to the full lane
    # (open vocabulary, additive rule).
    if (request.metadata or {}).get("recall_scope") == "people":
        # Four values since person_rule joined the tuple; this lane was
        # left unpacking three and 500'd on every Free people-scoped
        # recall, which GP degraded silently (found by GP proving #325
        # through the proxied path, 2026-08-24).
        p_vocab, p_owed, _, _ = await _people_read_context(db_pool, app_id)
        person_ids = [
            str(r["entity_id"]) for r in entity_rows
            if r["entity_type"] == p_vocab.person_entity_type
        ]
        people_rows = []
        if person_ids:
            p_core = await _people_core(
                db_pool, user_id, person_ids,
                owed_to_available=p_owed, include_completed=True,
                vocab=p_vocab,
            )
            people_rows = p_core["people"]
        context, ledger_ids, ledger_total = format_people_scope(
            people_rows, entity_rows, rel_rows,
            person_entity_type=p_vocab.person_entity_type,
        )
        t5 = time.monotonic()
        if context:
            try:
                await redis_client.set(
                    render_cache_key,
                    json.dumps({
                        "context": context,
                        "matched_entities": matched_names,
                        "matched_patch_ids": ledger_ids,
                        "matched_cues": [],
                        "patch_count": ledger_total,
                        "communication_style": None,
                        "excluded": excluded,
                    }),
                    ex=RECALL_RENDER_CACHE_TTL,
                )
            except Exception:
                pass
        timings["render_cache_write_ms"] = round((time.monotonic() - t5) * 1000, 2)
        _stamp_recall_total(timings, t0)
        timings["render_cache_hit"] = 0
        # Served ledger items count as recall access (decay exemption),
        # same as the full lane.
        asyncio.create_task(_bump_patch_access(ledger_ids))
        return RecallResponse(
            context=context,
            matched_entities=matched_names,
            matched_patch_ids=ledger_ids,
            excluded=excluded,
            matched_cues=[],
            patch_count=ledger_total,
            communication_style=None,
            timing_ms=timings,
        )

    # Look up communication profile and format as a natural language hint.
    # The calling gateway decides whether to inject this (e.g., only for chat modes).
    comm_style = None
    profile = await get_working_memory(user_id)
    if profile:
        cp = profile.get("variables", {}).get("communication_profile")
        if cp and isinstance(cp, dict):
            # Build a natural language style hint from the scores
            style_parts = []
            v = cp.get("verbosity")
            if v is not None:
                style_parts.append("concise" if v < 0.4 else "detailed" if v > 0.6 else "moderate-length")
            d = cp.get("directness")
            if d is not None:
                style_parts.append("direct" if d > 0.6 else "diplomatic" if d < 0.4 else "balanced")
            f = cp.get("formality")
            if f is not None:
                style_parts.append("formal" if f > 0.6 else "casual" if f < 0.4 else "semi-formal")
            t = cp.get("technical_level")
            if t is not None:
                style_parts.append("highly technical" if t > 0.7 else "non-technical" if t < 0.3 else "moderately technical")
            w = cp.get("warmth")
            if w is not None:
                style_parts.append("warm and friendly" if w > 0.6 else "businesslike" if w < 0.4 else "professional")
            if style_parts:
                comm_style = f"This user communicates in a {', '.join(style_parts)} style."
    timings["working_memory_ms"] = round((time.monotonic() - t4) * 1000, 2)

    # Collect matched patch IDs from all_patches, ranked by "relevance-ish" order.
    #
    # NOTE: This is deliberately NOT a real scoring algorithm (no BM25, no
    # embedding similarity, no query-patch relevance model). It's a heuristic
    # ordering designed to put the most "nudge-worthy" patches first for UI
    # preview chips (e.g., SS "Almost Had It" teaser). If we ever add true
    # semantic scoring (pgvector cosine, reranker model), replace this block.
    #
    # Heuristic:
    #   + 100 if patch text contains any matched entity name (strongest signal)
    #   + type priority (below): actionable types first, passive types last
    #   + tiebreaker: preserve original DB order (newest first via created_at DESC)
    #
    # Type priority rationale:
    #   Actionable work items (commitment, blocker, decision) — "what needs to happen"
    #   Roles & people — "who is involved"
    #   Container (project) — "what it belongs to"
    #   User traits/preferences — "background context about the user"
    #   Short-term observations (takeaway, experience, identity) — "passing context"
    TYPE_PRIORITY = {
        "commitment": 50, "blocker": 45, "decision": 40, "role": 30,
        "person": 25, "project": 20, "trait": 15, "preference": 10,
        "takeaway": 5, "experience": 5, "identity": 5,
    }
    matched_lower = [n.lower() for n in matched_names]

    scored = []
    seen_ids = set()
    for idx, row in enumerate(all_patches):
        pid = str(row["patch_id"]) if row.get("patch_id") else None
        if not pid or pid in seen_ids:
            continue
        seen_ids.add(pid)

        # Parse text from value
        v = row["value"]
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:
                v = {}
        text = (v.get("text", "") if isinstance(v, dict) else "").lower()

        score = TYPE_PRIORITY.get(row["patch_type"], 0)
        # Entity-match boost: +100 if any matched entity appears in the patch text
        if any(name in text for name in matched_lower):
            score += 100
        # Tiebreaker: preserve original order (newer first from created_at DESC)
        scored.append((score, -idx, pid))

    scored.sort(key=lambda t: (-t[0], -t[1]))
    matched_patch_ids = [pid for _, _, pid in scored]

    patch_count = len(fact_rows) + len(rel_rows)

    # Best-effort write to the render cache. Skip the empty-context case
    # (cheap to recompute and we don't want to cache "the user has no
    # entities yet" through transient prewarm gaps).
    t5 = time.monotonic()
    if context:
        try:
            await redis_client.set(
                render_cache_key,
                json.dumps({
                    "context": context,
                    "matched_entities": matched_names,
                    "matched_patch_ids": matched_patch_ids,
                    "matched_cues": matched_cues,
                    "patch_count": patch_count,
                    "communication_style": comm_style,
                    "excluded": excluded,
                }),
                ex=RECALL_RENDER_CACHE_TTL,
            )
        except Exception:
            pass

    timings["render_cache_write_ms"] = round((time.monotonic() - t5) * 1000, 2)
    _stamp_recall_total(timings, t0)
    timings["render_cache_hit"] = 0

    # Tail visibility: the 2026-07-18 timeout was only discovered
    # forensically. Anything slow logs its full phase breakdown so the
    # tail names itself in real time.
    if timings["total"] >= RECALL_SLOW_RENDER_MS:
        logger.warning(
            "recall_slow_render",
            user_id=user_id,
            text_len=len(request.text),
            matched_entities=len(matched_names),
            scoped=has_project_scope,
            timings=timings,
        )

    # Off-hot-path usage bookkeeping — see _bump_patch_access.
    asyncio.create_task(_bump_patch_access(matched_patch_ids))

    return RecallResponse(
        context=context,
        matched_entities=matched_names,
        matched_patch_ids=matched_patch_ids,
        matched_cues=matched_cues,
        patch_count=patch_count,
        communication_style=comm_style,
        excluded=excluded,
        timing_ms=timings,
    )


@app.get("/v1/profile/{user_id}", tags=["MCP Resource"])
async def get_profile(
    user_id: str,
    keys: Optional[List[str]] = Query(None),
    app_id: str = Depends(verify_application_access)
):
    """
    MCP Resource: Retrieve User State.
    Agents can query this to get the raw profile or specific keys.
    """
    profile = await get_working_memory(user_id)
    
    if not profile:
        return {}
        
    if keys:
        # Filter by requested keys
        filtered_vars = {k: v for k, v in profile.get("variables", {}).items() if k in keys}
        return {"user_id": user_id, "variables": filtered_vars}
        
    return profile

@app.post("/v1/memory", tags=["MCP Tool"])
async def update_memory(
    update: MemoryUpdate,
    app_id: str = Depends(verify_application_access)
):
    """
    MCP Tool: Update Memory State.
    Accepts:
    - 'tool_call': Direct fact insertion (Active Learning)
    - 'trace': Full execution trace (Passive Learning)
    - 'chat_log': Simple conversation history
    """
    # Push to Redis Stream for Async Worker (Cold Path)
    stream_key = "memory_updates"
    payload = update.dict(exclude_none=True)
    payload["app_id"] = app_id
    # Use provided timestamp or default to now
    if not payload.get("timestamp"):
        payload["timestamp"] = datetime.utcnow().isoformat()
    
    # Add to stream
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})
    
    return {"status": "queued", "message": "Memory update received for async processing"}

@app.post("/v1/prewarm", tags=["Ops"])
async def prewarm_cache(
    user_id: str,
    app_id: str = Depends(verify_application_access)
):
    """
    Synchronous Cache Hydration.
    Warms profile + entity index from Postgres into Redis.
    Call at session start so the first recall hits a warm cache.
    Typically completes in <50ms.
    """
    # Warm profile
    row = await db_pool.fetchrow(
        "SELECT variables, last_updated, display_name, email FROM profiles WHERE user_id = $1",
        user_id
    )
    if row:
        variables = row["variables"]
        if isinstance(variables, str):
            variables = json.loads(variables)
        profile_data = {
            "variables": variables,
            "last_updated": row["last_updated"].isoformat() if row["last_updated"] else "now",
            "display_name": row["display_name"],
            "email": row["email"],
        }
        await redis_client.set(f"active_context:{user_id}", json.dumps(profile_data), ex=3600)

    # Warm entity index (canonical names + aliases)
    entity_rows = await db_pool.fetch(ENTITY_INDEX_NAMES_SQL, user_id)
    entity_key = f"entity_index:{user_id}"
    if entity_rows:
        names = [r["name"] for r in entity_rows]
        await redis_client.delete(entity_key)
        await redis_client.sadd(entity_key, *names)
        await redis_client.expire(entity_key, ENTITY_INDEX_TTL)

    # Warm cue index (associative-retrieval topic phrases)
    try:
        cue_rows = await db_pool.fetch(CUE_INDEX_NAMES_SQL, user_id)
    except Exception:
        cue_rows = []  # patch_cues absent on a lagging DB (MCP) — degrade
    cue_key = f"cue_index:{user_id}"
    if cue_rows:
        cue_values = [r["cue"] for r in cue_rows]
        await redis_client.delete(cue_key)
        await redis_client.sadd(cue_key, *cue_values)
        await redis_client.expire(cue_key, ENTITY_INDEX_TTL)

    return {
        "status": "warm",
        "profile": row is not None,
        "entities": len(entity_rows) if entity_rows else 0,
        "cues": len(cue_rows) if cue_rows else 0,
    }

@app.get("/health", tags=["Ops"])
async def health():
    return {"status": "healthy", "version": "3.10.0"}

@app.post("/v1/auth/register", response_model=auth.ApplicationResponse, tags=["Authentication"])
async def register_application(app_data: auth.ApplicationCreate):
    from contextquilt.services.key_encryption import encrypt_key

    client_secret = secrets.token_urlsafe(32)
    secret_hash = auth.get_password_hash(client_secret)

    # Encrypt the user's LLM key if provided
    encrypted_llm_key = encrypt_key(app_data.llm_api_key) if app_data.llm_api_key else None

    try:
        row = await db_pool.fetchrow(
            """
            INSERT INTO applications (app_name, client_secret_hash, llm_api_key_encrypted, llm_base_url, llm_model)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING app_id, created_at
            """,
            app_data.app_name, secret_hash, encrypted_llm_key,
            app_data.llm_base_url, app_data.llm_model
        )
        return {
            "app_id": str(row['app_id']),
            "app_name": app_data.app_name,
            "client_secret": client_secret,
            "created_at": row['created_at'],
            "llm_key_provided": app_data.llm_api_key is not None,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/auth/token", response_model=auth.Token, tags=["Authentication"])
async def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends()):
    # client_id is in form_data.username
    # client_secret is in form_data.password
    
    try:
        row = await db_pool.fetchrow(
            "SELECT app_id, client_secret_hash FROM applications WHERE app_id = $1",
            form_data.username
        )
        
        if not row or not auth.verify_password(form_data.password, row['client_secret_hash']):
            # Logged because CQ is the ONLY place this failure is visible as
            # an auth failure. The gateway translates our 401 into a 502 at
            # its edge, deliberately, so a wrong secret reads downstream as
            # "the gateway is broken" rather than "the credential is wrong".
            # Nothing downstream can tell those apart; we can.
            #
            # Never log the submitted secret. The app id is safe and is the
            # only field that identifies WHICH credential is wrong, which is
            # the whole question during a cutover.
            logger.warning(
                "auth_token_rejected",
                client_id=str(form_data.username)[:64],
                reason="unknown_app" if not row else "secret_mismatch",
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect client_id or client_secret",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        access_token = auth.create_access_token(
            data={"sub": str(row['app_id'])},
            expires_delta=timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        logger.info("auth_token_issued", client_id=str(row['app_id']))
        return {"access_token": access_token, "token_type": "bearer", "expires_in": auth.ACCESS_TOKEN_EXPIRE_MINUTES * 60}
    except HTTPException:
        raise
    except Exception as e:
        # KNOWN DEFECT, logged rather than fixed here on purpose: this arm
        # turns ANY failure into a credential error, so a database outage
        # currently presents as "Incorrect client_id or client_secret" and
        # sends everyone hunting the wrong thing. Changing the status code is
        # a behaviour change on a live auth path and there is a credential
        # cutover imminent, so it lands separately. The log line is what makes
        # the difference visible in the meantime.
        logger.error(
            "auth_token_backend_error",
            client_id=str(form_data.username)[:64],
            error_type=type(e).__name__,
            error=str(e)[:200],
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect client_id or client_secret",
            headers={"WWW-Authenticate": "Bearer"},
        )

@app.get("/v1/auth/apps", tags=["Authentication"])
async def list_applications():
    rows = await db_pool.fetch("SELECT app_id, app_name, enforce_auth, created_at FROM applications ORDER BY created_at DESC")
    # Convert UUIDs and Datetimes to strings for JSON serialization
    results = []
    for row in rows:
        r = dict(row)
        r['app_id'] = str(r['app_id'])
        r['created_at'] = r['created_at'].isoformat()
        results.append(r)
    return results

# ============================================
# User Quilt CRUD (App-scoped access control)
# ============================================
# Auth model: The calling app authenticates via JWT or X-App-ID.
# CQ trusts the app to vouch for the user_id.
# ACL is enforced per-patch: the app can only see/edit patches it created.
# This is provider-agnostic — CQ doesn't care if the user logged in
# via Apple, Google, email, or anything else. That's the app's job.

class PatchConnectionResponse(BaseModel):
    to_patch_id: str
    role: str              # parent, depends_on, resolves, replaces, informs
    label: Optional[str] = None  # belongs_to, blocked_by, works_on, owns, etc.
    context: Optional[str] = None

class QuiltPatchResponse(BaseModel):
    patch_id: str
    fact: str
    category: str
    participants: List[str] = []
    owner: Optional[str] = None
    deadline: Optional[str] = None
    deadline_date: Optional[str] = None  # structured YYYY-MM-DD, when the extractor resolved one
    patch_type: str = ""
    source: str = ""
    created_at: Optional[str] = None
    project: Optional[str] = None
    project_id: Optional[str] = None
    origin_id: Optional[str] = None
    origin_type: Optional[str] = None
    permanence_override: Optional[str] = None
    permanence_override_source: Optional[str] = None
    # Shelved items STAY in these arrays (they are still active — recall
    # still knows them); these stamps are how a client's ledger/triage
    # excludes them. A tombstone would be indistinguishable from decay.
    shelved_at: Optional[str] = None
    shelved_source: Optional[str] = None
    # Server-resolved person entity for the item's owner (owns-edge
    # person first, then value.owner by name/alias), so a client links
    # an owner chip into the People tab with zero entity matching of
    # its own (SS's ask, boundary decision 2026-08-11). Null = CQ
    # cannot tell, never "no owner"; `owner` remains the raw string.
    owner_entity_id: Optional[str] = None
    # Whether the item is the SUBMITTING USER'S OWN obligation, computed
    # once here with the canonical rule (owns-edge resolution first,
    # owner text fallback, ownerless-on-own-quilt counts as theirs per
    # reassign-speaker's to_self contract) against the ego link
    # (entities.self_at), so clients can never drift from the server on
    # whose item something is. Null = CQ cannot tell (no self entity, or
    # not a completable); false = someone else's. The obligation-surfacing
    # design (2026-08-11 amendment) renders null as absence, never as
    # "you owe 0".
    owned_by_self: Optional[bool] = None
    # live | aging | stale for completables, from the SAME decay_model
    # the worker's archival and the People ledger consume, so a triage
    # queue on any surface (person card, project detail) reads the same
    # band for the same row. Null = decay does not apply here (not a
    # completable). Day-bucketed: stable within a UTC day.
    decay_state: Optional[str] = None
    # THE THREE A TRIAGE HEADER NEEDS, all of which existed in the value
    # blob and none of which reached a client until 2026-08-18.
    #
    # Measured on ABM the day this shipped: 472 open items carrying 99
    # overdue, 55 high salience and 23 restated, and the app could see
    # none of it. A client asked to build "what needs attention" had
    # literally nothing to sort on but a raw deadline.
    #
    # `overdue_since` is stamped by the worker's deadline sweep, so it is
    # the server's own answer rather than a client re-deriving "past
    # today" against its own clock and drifting across timezones.
    overdue_since: Optional[str] = None
    # low | high, absent means normal. Set by extraction, and it also
    # stretches or shrinks the decay TTL, so a client showing it is
    # showing the same thing that governs the item's lifetime.
    salience: Optional[str] = None
    # How many times this item came back WITHOUT closing. Monotonic, and
    # it survives the restatements array's cap. A thing said four times
    # and still open is the strongest "this is not moving" signal we
    # hold; doc 16 5.12 is the ruling on what it does and does not mean.
    restatement_count: Optional[int] = None
    # Does `owner` name more than one live person? Three valued, matching
    # the null-means-cannot-tell convention on this surface.
    #
    # SS needs it because a project view must tell "Pradeep & Suresh"
    # from "Steven": the first cannot be assigned to anybody without
    # lying, so it lives in the project permanently, while the second is
    # a resolvable gap. Rendering "Which Pradeep & Suresh?" is not
    # merely unhelpful, it is incoherent.
    #
    # CQ does NOT know this at extraction time; the model writes `owner`
    # as free text and never says how many humans are in it. What CQ has
    # that a client does not is the ROSTER, so True means two or more
    # parts were CONFIRMED against live people, never inferred from
    # punctuation. Null is "looks compound, cannot confirm", which is
    # where a client-side heuristic belongs: the server proves what it
    # can, the client presents what it cannot, and no name heuristic
    # goes near the identity path.
    owner_names_multiple: Optional[bool] = None
    owner_is_placeholder: Optional[bool] = None
    # The believed-completion question (#279/#284: nothing auto-closes,
    # every meeting-detected closure is a card a human answers). These
    # were stamped by the worker and served on the People detail, and
    # NEVER on this route, so the project-section confirm queue and the
    # meeting-review card, both fed by /v1/quilt, starved by
    # construction. Found 2026-08-19 by the local two-meeting
    # simulation: the worker logged the stamp and the quilt served
    # null. GP's 08-17 middle-hop proof was real but proved the fields
    # SURVIVE the hop, not that the origin emits them here (rule 5:
    # name which side each claim was proved on).
    believed_complete_at: Optional[str] = None
    # The quote from the meeting. Load-bearing, not decoration: one
    # line of evidence is what makes the confirm answer obvious.
    believed_complete_evidence: Optional[str] = None
    believed_complete_reasons: Optional[List[str]] = None
    # The meeting that produced the belief, so the quote can be traced.
    believed_complete_origin_id: Optional[str] = None
    # confident | believed. A presentation ordering hint only; it must
    # never render as a badge (the auto-close lesson with typography).
    believed_evidence_strength: Optional[str] = None
    connections: List[PatchConnectionResponse] = []

class MeetingGroup(BaseModel):
    """Patches anchored to one meeting (origin). Groups are ordered
    newest meeting first; patches inside a group are in capture order."""
    origin_id: str
    origin_type: Optional[str] = None
    patches: List[QuiltPatchResponse] = []

class QuiltResponse(BaseModel):
    user_id: str
    facts: List[QuiltPatchResponse]
    action_items: List[QuiltPatchResponse]
    deleted: List[str] = []        # patch_ids removed since `since` timestamp (all causes)
    completed: List[str] = []      # subset of `deleted` that was resolved (completed_at set),
                                   # as opposed to decayed/archived — lets the app show "done"
                                   # instead of items silently vanishing
    meetings: Optional[List[MeetingGroup]] = None  # present only with ?group_by=origin;
                                   # originless (user-scoped) patches stay in the flat
                                   # arrays only
    server_time: Optional[str] = None  # use as `since` on next request
    # Whether `limit` bit, and what the real total was.
    #
    # `limit` applied a SQL LIMIT and returned fewer rows with nothing
    # saying so. GhostPour found the consequence from the far side: they
    # pass a cap, build a cross-meeting topic tracker on the result, and
    # the artifact is confidently wrong about the one thing it exists to
    # measure. Their words, worth keeping: a silent cap means a busy
    # project undercounts.
    #
    # Recall already solved this for its own truncation with a coverage
    # line ("showing N of M stored patches for this project"), which is
    # contract commitment E and always on. Same principle, structured
    # because this surface is read by code rather than a model.
    #
    # Absent when no `limit` was passed, so a caller that never caps
    # sees no change at all.
    truncated: Optional[bool] = None
    total_available: Optional[int] = None

class PatchUpdate(BaseModel):
    fact: Optional[str] = None
    category: Optional[str] = None
    owner: Optional[str] = None
    project_id: Optional[str] = None
    # One of: permanent | decade | year | quarter | month | week | day,
    # or "" to CLEAR the override back to the type default. NOT null:
    # null means "not supplied" here, as it does for every field on this
    # route, and the block below is not even entered for it. Said
    # explicitly because the wire strips a null before CQ sees it (GP's
    # proxy filters `if v is not None` on this route among five), so a
    # client that sent null hoping to clear would get a 200 and no
    # change, invisibly, from both ends. Verified 2026-09-02.
    permanence_override: Optional[str] = None
    permanence_override_source: Optional[str] = None    # 'user' or 'app'; defaults to 'user' when the API is called without explicit source
    # THE DUE DATE, EDITABLE. Until now nothing could change it after the
    # fact, so an item created with the wrong date, or with none, was
    # stuck that way forever, by anyone, on any surface.
    #
    # "" CLEARS it back to undated, matching permanence_override's
    # convention on this same route rather than inventing a second one.
    # None means "not supplied" and leaves the stored value alone.
    #
    # WHAT THIS MUST NEVER TOUCH IS `value.deadline_history`. That array
    # is written by the WORKER on the re-observation path and it means
    # THE PERSON MOVED THEIR OWN DEADLINE, observed in a room; the item
    # ledger derives its `re_dated` mode by counting it. If a user edit
    # appended to it, a colleague's card would report that they pushed a
    # deadline when in fact the app's user moved it in the UI, which is a
    # served claim about something nobody observed (doc 16 5.13).
    #
    # The precedent is already one function away: `_stated_days` in
    # item_ledger refuses to anchor on `updated_at` because "an admin
    # edit, a vouch or a shelve moves that, and none of them is anybody
    # saying the item out loud." An edit changes what is TRUE. It does
    # not create evidence about anybody's conduct.
    deadline_date: Optional[str] = None                 # YYYY-MM-DD to set, "" to clear, omit to leave unchanged
    # THE SAME FIELD AS `category`, UNDER THE NAME THE CLIENT ACTUALLY
    # SENDS. GP's proxy audit (2026-08-30) found their model named
    # `category` while SS's updatePatch sends `patch_type`, so the key
    # was dropped on their hop and every patch-type edit was a silent
    # no-op: 200 back, nothing changed.
    #
    # THEIR FIX ALONE DOES NOT CLOSE IT. Once they stop dropping the key
    # it arrives here, and this model would have ignored it for the same
    # reason under a different roof, so the edit would still no-op and
    # the second fix would look like the first one failing. Three
    # components, two names, and the mismatch survives any one of them
    # being corrected alone.
    #
    # Both names are accepted rather than renaming either: `category` is
    # the shipped name on this route and something may send it, and a
    # rename to fix a compatibility bug is how you get a third name.
    patch_type: Optional[str] = None
    # A deliberate tile refresh with no text change. Scott ruled
    # 2026-09-02 that an unchanged Save sends nothing on any edit screen,
    # so a stale headline (worded by a rule that no longer applies) needs
    # its own trigger rather than an incidental re-save. Recomputes the
    # headline from the STORED text exactly as a fact edit does, touches
    # nothing else, and moves neither origin_mode nor updated_at, because
    # a headline is presentation and a bump would extend decay on a patch
    # nobody re-observed. A flag on this route rather than a new
    # sub-path, so no GP route change; GP's typed proxy must pass it.
    refresh_headline: Optional[bool] = None

class PatchCompletionRequest(BaseModel):
    evidence: Optional[str] = None  # short free-text note on what completed it (e.g. "user tapped done")
    # ISO 8601 date or datetime: when the thing was ACTUALLY finished,
    # user-declared. Absent means the server clock, which is the
    # "completed today" default. Future values are rejected with a 422
    # whose body names the field and the reason. Scott's ruling
    # 2026-08-19: default today, overridable.
    completed_at: Optional[str] = None


class TierChangeEvent(BaseModel):
    """App-fired account/tier lifecycle signal (cq-tier-signals lane).

    Contract (GP, 2026-07-25): fired only on real subscription state
    transitions, never idempotent re-verifications or same-tier
    renewals. Vocabulary: upgrade, downgrade, trial_start,
    trial_to_paid, cancellation, expire, refund, account_deleted.
    `occurred_at` is GP server time at the transition and forms the
    idempotency key with user_id — account_deleted is delivered
    at-least-once (GP durable outbox retries until our 202), ordinary
    events fire-and-forget. Ordinary events are record-only today;
    account_deleted is the deletion request for everything CQ holds
    for the user (new_tier is the literal 'deleted')."""
    event_type: str
    old_tier: Optional[str] = None
    new_tier: Optional[str] = None
    occurred_at: Optional[str] = None

# The SS floor for completable types. Runtime callers use
# _completable_types(), which resolves the registered manifests'
# `completable` flags through the facet runtime (registry-backed) and
# can only WIDEN this set. The constant remains as the degraded-mode
# fallback and for error messages when the registry is unreachable.
COMPLETABLE_PATCH_TYPES = facet_runtime.FALLBACK_COMPLETABLE_TYPES


def _as_optional_int(value):
    """A non negative int from an int or a numeric string, else None.

    `value.restatement_count` crosses the wire as text from a `->>`
    select and as an int from a jsonb one, and a hand-written or
    backfilled value could be neither. A read route returns absent
    rather than raising on any of those. Mirrors item_ledger._as_int,
    which serves the same field to the People surfaces.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            n = int(value.strip())
        except (ValueError, AttributeError):
            return None
        return n if n >= 0 else None
    return None


def _as_str_list(value):
    """A list of strings, else None.

    `value.believed_complete_reasons` is written by the worker as a
    list of strings, but a hand-edited or backfilled value could be
    anything. A read route serves absent rather than raising, and
    non-string members are dropped rather than coerced (a reason that
    is not a string is not a reason).
    """
    if not isinstance(value, list):
        return None
    strings = [v for v in value if isinstance(v, str)]
    return strings if strings or value == [] else None


async def _completable_types() -> tuple:
    """Completable types per the registered manifests (cached snapshot)."""
    return (await facet_runtime.get_type_runtime(db_pool.fetch)).completable_types

# The person-detail history leg serves this many completed items, newest
# completion first; `total` beside the array counts the WHOLE population
# so the cap is self-describing rather than silent (the coverage-line
# rule: a truncated list must say it truncated).
COMPLETED_HISTORY_CAP = 20


@app.post("/v1/users/{user_id}/tier-change", status_code=202, tags=["Lifecycle"])
async def record_tier_change(
    user_id: str,
    event: TierChangeEvent,
    app_id: str = Depends(verify_application_access),
):
    """
    Durable inbox for app-fired account/tier lifecycle events.

    RECORD-ONLY by design: the endpoint validates shape, persists the
    signal, and returns 202. Processing (for `account_deleted`, the
    full account purge) runs as a separate consumer keyed on
    `processed_at IS NULL`, so a signal that arrives before its
    processor ships is queued, never lost. This endpoint went live
    ahead of the purge wiring precisely so the sender's fires stop
    404ing (observed: GP test fire 2026-07-25T16:17Z).

    Unknown event_type values are recorded too (raw payload kept) —
    the contract vocabulary is app-side; dropping an unrecognized
    lifecycle signal is worse than storing it unprocessed.
    """
    et = (event.event_type or "").strip().lower()
    if not et:
        raise HTTPException(status_code=422, detail="event_type is required")

    # occurred_at: parse leniently — a malformed timestamp must not cost
    # us the signal (the raw string survives in raw_payload either way).
    occurred_dt = None
    if event.occurred_at:
        try:
            occurred_dt = datetime.fromisoformat(event.occurred_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            occurred_dt = None

    payload = json.dumps(event.model_dump() if hasattr(event, "model_dump") else event.dict())

    # Idempotent on (user_id, occurred_at): account_deleted arrives
    # at-least-once from GP's durable outbox, ordinary events may
    # best-effort double-fire. A duplicate returns the original
    # signal_id with the same 202, so the sender's retry loop settles.
    row = await db_pool.fetchrow(
        """
        INSERT INTO tier_signals (user_id, app_id, event_type, old_tier, new_tier, occurred_at, raw_payload)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (user_id, occurred_at) WHERE occurred_at IS NOT NULL
        DO NOTHING
        RETURNING signal_id, received_at
        """,
        user_id, app_id, et, event.old_tier, event.new_tier, occurred_dt, payload,
    )
    duplicate = row is None
    if duplicate:
        row = await db_pool.fetchrow(
            "SELECT signal_id, received_at FROM tier_signals WHERE user_id = $1 AND occurred_at = $2",
            user_id, occurred_dt,
        )
    logger.info(
        "tier_signal_recorded",
        user_id=user_id, app_id=app_id, event_type=et,
        old_tier=event.old_tier, new_tier=event.new_tier,
        occurred_at=event.occurred_at, duplicate=duplicate,
        signal_id=str(row["signal_id"]),
    )
    return {
        "status": "recorded",
        "signal_id": str(row["signal_id"]),
        "received_at": row["received_at"].isoformat(),
        "duplicate": duplicate,
        "processing": "queued",
    }

@app.get("/v1/quilt/{user_id}", response_model=QuiltResponse, tags=["Quilt"])
async def get_user_quilt(
    user_id: str,
    category: Optional[str] = Query(None, description="Filter by patch_type"),
    since: Optional[str] = Query(None, description="ISO 8601 timestamp — return only patches created/updated after this time, plus IDs of patches deleted since then"),
    origin_id: Optional[str] = Query(None, description="Meeting view: only patches anchored to this meeting (origin), in capture order. Only episodic/project-scoped types carry an origin — user-scoped types (trait, preference, person, project, org) are meeting-free by design and never appear here."),
    group_by: Optional[str] = Query(None, description="'origin' adds a `meetings` array grouping origin-anchored patches by meeting (newest meeting first, capture order inside). Flat arrays are unchanged."),
    project_id: Optional[str] = Query(None, description="Project rundown view (context-flow contract, 2026-07): only patches carrying this stable project id. Combine with group_by=origin for a complete per-meeting project dossier. NOTE: meeting views stay keyed on origin_id per the SS contract; this filter serves the gateway's rundown route and only works when ingest stamped project_id (see docs/architecture/13)."),
    limit: Optional[int] = Query(None, ge=1, le=500, description="Cap the patches array (applied after ordering). For prompt injection use — a large project must not blow the caller's prompt budget."),
    order: Optional[str] = Query(None, description="'attention' ranks open work by what needs looking at (overdue, then high salience, then items that keep coming back, then soonest due) instead of by recency. Pair it with `limit` so a capped project view returns the important N rather than an arbitrary N. Omit for the existing recency order; `origin_id` meeting views ignore it and stay in capture order."),
    max_age_days: Optional[int] = Query(None, ge=1, le=3650, description="Tier recall window, same contract as recall's metadata.max_age_days: only meeting-bound patches whose most recent observation (last_observed_at, else created_at) falls within the last N UTC days; universal self-disclosure types are exempt. `total_available` counts inside the window. For the gateway's rundown/dossier leg into a prompt; a sync caller never passes it, so delta sync is untouched."),
    app_id: str = Depends(verify_application_access),
):
    """
    Get patches CQ knows about a user.

    Without `since`: returns all active patches (full sync).
    With `since`: returns only patches created or updated after that timestamp,
    plus a `deleted` array of patch_ids that were removed or archived since then.
    Pass the returned `server_time` as `since` on the next request.

    Meeting views (SS contract, 2026-06-10): `origin_id=<meeting UUID>`
    returns that meeting's full patch set with no relevance ranking, in
    deterministic capture order — it's a browse surface. `group_by=origin`
    adds a server-side grouped-by-meeting shape for project-level views.
    Both keyed on the meeting UUID, never project_id (SS's call, due to
    their CloudKit project-id drift).
    """
    # Reject an unknown order rather than falling back to recency. A
    # client that ships `order=priority` would otherwise get a plausible
    # list, in the wrong order, with a 200 and nothing to notice.
    if order is not None and order not in ("attention",):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "UNKNOWN_ORDER",
                "message": "order must be 'attention' or omitted",
                "received": order[:40],
            },
        )

    subject_key = f"user:{user_id}"
    server_time = datetime.utcnow()

    # Check if app_id is a valid UUID for ACL join; legacy X-App-ID
    # values (e.g. "cloudzap") are not UUIDs, so skip ACL filtering
    # and return all patches (no ACL row = open access).
    import uuid as _uuid
    try:
        app_uuid = _uuid.UUID(app_id)
        acl_join = "LEFT JOIN context_patch_acl acl ON cp.patch_id = acl.patch_id AND acl.app_id = $2"
        acl_where = "AND (acl.can_read = TRUE OR acl.patch_id IS NULL)"
        params: list = [subject_key, app_uuid]
    except (ValueError, AttributeError):
        acl_join = ""
        acl_where = ""
        params = [subject_key]

    # Build query with optional ACL enforcement
    since_filter = ""
    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            since_filter = f" AND cp.updated_at > ${len(params) + 1}"
            params.append(since_dt)
        except ValueError:
            pass

    query = f"""
        SELECT cp.patch_id, cp.patch_name, cp.patch_type, cp.value,
               cp.origin_mode, cp.source_prompt, cp.created_at, cp.updated_at,
               cp.project,
               cp.project_id, cp.origin_id, cp.origin_type,
               cp.permanence_override, cp.permanence_override_source, cp.status
        FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
        {acl_join}
        WHERE ps.subject_key = $1
          AND COALESCE(cp.status, 'active') = 'active'
          {acl_where}
          {since_filter}
    """

    if category:
        query += f" AND cp.patch_type = ${len(params) + 1}"
        params.append(category)

    if origin_id:
        query += f" AND cp.origin_id = ${len(params) + 1}"
        params.append(origin_id)

    if project_id:
        query += f" AND cp.project_id = ${len(params) + 1}"
        params.append(project_id)

    # Tier recall window on the dossier leg (2026-08-21). The chat
    # context flow has TWO CQ legs, /v1/recall and this rundown, and a
    # window applied to one of them is a window with a hole in it: a Plus
    # user would lose March from the recall block and get it back from the
    # dossier. Same predicate as recall, same exemption, same day bucket,
    # applied BEFORE the count so total_available is the windowed
    # population. Absent = untouched, which is every sync caller.
    if max_age_days is not None:
        window_runtime = await facet_runtime.get_type_runtime(db_pool.fetch)
        query += (
            f" AND (cp.patch_type = ANY(${len(params) + 1}::text[])"
            f" OR COALESCE(cp.last_observed_at, cp.created_at)::date"
            f" >= ((NOW() AT TIME ZONE 'utc')::date - ${len(params) + 2}::int))"
        )
        params.append(list(window_runtime.universal_recall_types))
        params.append(max_age_days)

    # Meeting view = capture order (oldest first), per the SS contract:
    # a browse surface wants deterministic ordering, not ranking. The
    # default full-sync order stays newest-first. patch_id tiebreak in
    # both modes — microsecond-equal batch inserts gave undefined order.
    if origin_id:
        query += " ORDER BY cp.created_at ASC, cp.patch_id ASC"
    elif order == "attention":
        # OPT IN, and deliberately not the default: GP's rundown route
        # reads this endpoint into a prompt, and reordering underneath a
        # caller who never asked would change their bytes for free.
        #
        # Exists because a capped rundown was returning AN arbitrary N
        # rather than the important N. On ABM the day this shipped, 472
        # open items carried 99 overdue and 55 high salience, and a
        # client asking for the top 40 by recency would surface almost
        # none of them.
        #
        # No casts anywhere in here on purpose. `restatement_count` is
        # text from a `->>` select and could be a hand-written value, and
        # an ORDER BY that raises takes down a read route for whoever
        # happens to hold one bad row. The regex answers the only
        # question the sort needs, "has this come back at all", without
        # trusting the contents. ISO dates sort correctly as text.
        query += (
            " ORDER BY"
            " (cp.value->>'overdue_since' IS NOT NULL) DESC,"
            " (cp.value->>'salience' = 'high') DESC,"
            " (cp.value->>'restatement_count' ~ '^[1-9]') DESC,"
            " (cp.value->>'deadline_date') ASC NULLS LAST,"
            " cp.created_at DESC, cp.patch_id ASC"
        )
    else:
        query += " ORDER BY cp.created_at DESC, cp.patch_id ASC"

    # Count BEFORE the cap, so the caller can tell an exhausted list from
    # a truncated one. Only when a cap was actually passed: a caller that
    # never limits pays nothing for a question it did not ask.
    total_available: Optional[int] = None
    if limit:
        total_available = await db_pool.fetchval(
            f"SELECT count(*) FROM ({query}) AS unlimited", *params
        )
        query += f" LIMIT ${len(params) + 1}"
        params.append(limit)

    rows = await db_pool.fetch(query, *params)

    # No cap means the rows ARE the population, so the count is free and
    # the field is always present. GP asked for this (2026-08-19) and the
    # reason is better than the convenience: the one field that says "you
    # are seeing a partial view" used to disappear exactly when a caller
    # stopped asking for a partial view, so the day anything server side
    # caps a response, a consumer that had been reading total_available
    # would see absence and have no way to tell it apart from the absence
    # it has always seen. Absence cannot carry that news. A number can.
    if total_available is None:
        total_available = len(rows)

    # Find patches deleted or archived since the timestamp. completed_at
    # separates "resolved" (worker auto-close or app-initiated completion)
    # from "decayed" (TTL archival) — both land in `deleted` for backward
    # compat, resolved ones additionally in `completed`.
    deleted_ids = []
    completed_ids = []
    if since_dt:
        deleted_rows = await db_pool.fetch(
            """
            SELECT cp.patch_id, cp.completed_at FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND cp.status IN ('archived', 'completed')
              AND cp.updated_at > $2
            """,
            subject_key, since_dt
        )
        deleted_ids = [str(r["patch_id"]) for r in deleted_rows]
        completed_ids = [str(r["patch_id"]) for r in deleted_rows if r["completed_at"] is not None]

    # Collect all patch IDs to fetch connections in one query
    patch_ids = [row["patch_id"] for row in rows]

    # Fetch all outgoing connections for these patches
    connections_by_patch: dict[str, list] = {}
    if patch_ids:
        conn_rows = await db_pool.fetch(
            """SELECT pc.from_patch_id, pc.to_patch_id,
                      pc.connection_role, pc.connection_label, pc.context
               FROM patch_connections pc
               WHERE pc.from_patch_id = ANY($1::uuid[])
                 AND COALESCE(pc.status, 'active') = 'active'""",
            patch_ids,
        )
        for cr in conn_rows:
            from_id = str(cr["from_patch_id"])
            if from_id not in connections_by_patch:
                connections_by_patch[from_id] = []
            connections_by_patch[from_id].append(
                PatchConnectionResponse(
                    to_patch_id=str(cr["to_patch_id"]),
                    role=cr["connection_role"] or "",
                    label=cr["connection_label"],
                    context=cr["context"],
                )
            )

    facts = []
    action_items = []

    completable = await _completable_types()

    # owner_entity_id inputs, all set-based: the owns-edge person text
    # per item (same deterministic pick as the People surface), and the
    # user's person entities + aliases for the name resolver.
    vocab = await _people_vocab_cached(app_id)
    completable_ids = [
        row["patch_id"] for row in rows if row["patch_type"] in completable
    ]
    owner_text_by_item: dict = {}
    if completable_ids:
        owns_rows = await db_pool.fetch(
            """
            SELECT DISTINCT ON (pc.to_patch_id)
                   pc.to_patch_id, op.value->>'text' AS person_text
            FROM patch_connections pc
            JOIN context_patches op ON op.patch_id = pc.from_patch_id
            JOIN context_patches item ON item.patch_id = pc.to_patch_id
            WHERE pc.to_patch_id = ANY($1::uuid[])
              AND pc.connection_label = $2
              AND COALESCE(pc.status, 'active') = 'active'
            ORDER BY pc.to_patch_id,
                     (lower(btrim(op.value->>'text'))
                      = lower(btrim(item.value->>'owner'))) DESC NULLS LAST,
                     pc.created_at, pc.connection_id
            """,
            completable_ids, vocab.ownership_label,
        )
        owner_text_by_item = {
            str(r["to_patch_id"]): r["person_text"] for r in owns_rows
        }
    entity_rows = await db_pool.fetch(
        "SELECT entity_id, name, merged_into FROM entities "
        "WHERE user_id = $1 AND entity_type = $2",
        user_id, vocab.person_entity_type,
    )
    alias_rows = await db_pool.fetch(
        "SELECT a.alias, a.entity_id FROM entity_aliases a "
        "JOIN entities e ON e.entity_id = a.entity_id "
        "WHERE a.user_id = $1 AND e.entity_type = $2",
        user_id, vocab.person_entity_type,
    )
    resolve_owner_entity = build_entity_resolver(
        [dict(r) for r in entity_rows], [dict(r) for r in alias_rows]
    )
    try:
        _self_row = await db_pool.fetchval(
            "SELECT entity_id FROM entities "
            "WHERE user_id = $1 AND self_at IS NOT NULL",
            user_id,
        )
        self_entity_id = str(_self_row) if _self_row else None
    except Exception:
        # Pre-migration-35 DB (the MCP deployment lags migrations):
        # owned_by_self serves null everywhere, the honest answer, and
        # the quilt route itself must never fail over this column.
        self_entity_id = None

    # Decay-band inputs for completables, guarded so a lagging DB (the
    # MCP deployment misses tables and columns) degrades to bands
    # without registry TTLs or access exemption rather than failing the
    # core quilt route. Same parameters the worker archival and the
    # People ledger use (services/decay_model.py is the single source).
    registry_ttls: dict = {}
    for _pt in completable:
        try:
            _ttl_row = await db_pool.fetchrow(
                decay_model.TTL_REGISTRY_QUERY, _pt
            )
            if _ttl_row and _ttl_row["default_ttl_days"] is not None:
                registry_ttls[_pt] = _ttl_row["default_ttl_days"]
        except Exception:
            pass
    last_accessed_by_id: dict = {}
    if completable_ids:
        try:
            _acc_rows = await db_pool.fetch(
                "SELECT patch_id, last_accessed_at FROM patch_usage_metrics "
                "WHERE patch_id = ANY($1::uuid[])",
                completable_ids,
            )
            last_accessed_by_id = {
                str(r["patch_id"]): r["last_accessed_at"] for r in _acc_rows
            }
        except Exception:
            pass

    def _decay_state(pid, value, row):
        if row["patch_type"] not in completable:
            return None
        try:
            return decay_model.decay_state(
                row["patch_type"],
                updated_at=row["updated_at"],
                created_at=row["created_at"],
                deadline_date=value.get("deadline_date"),
                salience=value.get("salience"),
                permanence_override=row.get("permanence_override"),
                registry_ttl_days=registry_ttls.get(row["patch_type"]),
                last_accessed_at=last_accessed_by_id.get(pid),
            )
        except Exception:
            return None

    def _owned_by_self(pid, value, patch_type):
        """Null when CQ cannot tell (no ego link, or not a completable);
        otherwise the same verdict the insights follow-up rate uses, so
        the quilt chips and the Memory-tab aggregates can never disagree
        about whose item something is."""
        if self_entity_id is None or patch_type not in completable:
            return None
        owner_entity = resolve_owner_entity(
            owner_text_by_item.get(pid)
        ) or resolve_owner_entity(value.get("owner"))
        # The verdict itself lives in people_identity so it can be tested
        # against inputs instead of grepped. "Speaker 3" abstains there:
        # it is somebody, so the ownerless rule must not hand it to the
        # user, and it is nobody CQ can name, so False would be a
        # confident answer CQ has not earned. The insights follow-up rate
        # keeps saying not-self for the same input, which is the same
        # substance for a rate (in or out of the numerator and the
        # denominator together) without the distinction a per-item chip
        # needs.
        return owned_by_self_verdict(
            owner_entity,
            self_entity_id,
            owner_text_by_item.get(pid) or value.get("owner"),
            value.get("owner"),
        )

    for row in rows:
        value = row["value"]
        if isinstance(value, str):
            value = json.loads(value)

        pid = str(row["patch_id"])
        patch = QuiltPatchResponse(
            patch_id=pid,
            fact=value.get("text", ""),
            category=row["patch_type"] or "",
            participants=value.get("participants", []),
            owner=value.get("owner"),
            deadline=value.get("deadline"),
            deadline_date=value.get("deadline_date"),
            patch_type=row["patch_type"] or "",
            source=row["source_prompt"] or "",
            created_at=row["created_at"].isoformat() if row["created_at"] else None,
            project=row["project"],
            project_id=row.get("project_id"),
            origin_id=row.get("origin_id"),
            origin_type=row.get("origin_type"),
            permanence_override=row.get("permanence_override"),
            permanence_override_source=row.get("permanence_override_source"),
            shelved_at=value.get("shelved_at"),
            shelved_source=value.get("shelved_source"),
            owner_entity_id=(
                resolve_owner_entity(owner_text_by_item.get(pid))
                or resolve_owner_entity(value.get("owner"))
                if row["patch_type"] in completable else None
            ),
            owned_by_self=_owned_by_self(pid, value, row["patch_type"]),
            decay_state=_decay_state(pid, value, row),
            overdue_since=value.get("overdue_since"),
            salience=value.get("salience"),
            # Text from a `->>` select, int from a jsonb one, and a
            # hand-written value could be neither. Absent beats raising
            # on a read route.
            restatement_count=_as_optional_int(value.get("restatement_count")),
            owner_names_multiple=(
                owner_names_multiple(value.get("owner"), resolve_owner_entity)
                if row["patch_type"] in completable else None
            ),
            owner_is_placeholder=(
                owner_is_placeholder(value.get("owner"))
                if row["patch_type"] in completable else None
            ),
            believed_complete_at=(
                value.get("believed_complete_at")
                if row["patch_type"] in completable else None
            ),
            believed_complete_evidence=(
                value.get("believed_complete_evidence")
                if row["patch_type"] in completable else None
            ),
            believed_complete_reasons=(
                _as_str_list(value.get("believed_complete_reasons"))
                if row["patch_type"] in completable else None
            ),
            believed_complete_origin_id=(
                value.get("believed_complete_origin_id")
                if row["patch_type"] in completable else None
            ),
            believed_evidence_strength=(
                value.get("believed_evidence_strength")
                if row["patch_type"] in completable else None
            ),
            connections=connections_by_patch.get(pid, []),
        )

        if row["patch_type"] in completable:
            action_items.append(patch)
        else:
            facts.append(patch)

    # Grouped-by-meeting shape. Originless (user-scoped) patches stay in
    # the flat arrays only. Groups: newest meeting first; patches inside
    # each group in capture order. Rows arrive created_at DESC (or ASC in
    # origin_id mode), so sort explicitly rather than relying on row order.
    meetings = None
    if group_by == "origin":
        by_origin: Dict[str, dict] = {}
        for row in rows:
            oid = row.get("origin_id")
            if not oid:
                continue
            pid = str(row["patch_id"])
            group = by_origin.setdefault(oid, {
                "origin_type": row.get("origin_type"),
                "rows": [],
            })
            group["rows"].append((row["created_at"], pid))

        patches_by_id = {p.patch_id: p for p in facts + action_items}
        meetings = []
        for oid, group in by_origin.items():
            ordered = sorted(group["rows"], key=lambda t: (t[0], t[1]))
            meetings.append(MeetingGroup(
                origin_id=oid,
                origin_type=group["origin_type"],
                patches=[patches_by_id[pid] for _, pid in ordered if pid in patches_by_id],
            ))
        # Newest meeting first — keyed by the group's latest capture time.
        meetings.sort(key=lambda m: max(r[0] for r in by_origin[m.origin_id]["rows"]), reverse=True)

    return QuiltResponse(
        user_id=user_id,
        facts=facts,
        action_items=action_items,
        deleted=deleted_ids,
        completed=completed_ids,
        meetings=meetings,
        server_time=server_time.isoformat() + "Z",
        # Both always present now. `truncated` answers the only question a
        # caller building a count on this actually has, and
        # `total_available` is what they should have counted: the rows
        # that matched BEFORE any cap, which on an uncapped read is the
        # rows themselves and on a delta is the size of that delta.
        truncated=total_available > len(rows),
        total_available=total_available,
    )


@app.patch("/v1/quilt/{user_id}/patches/{patch_id}", tags=["Quilt"])
async def update_patch(
    user_id: str,
    patch_id: str,
    update: PatchUpdate,
    app_id: str = Depends(verify_application_access),
):
    """
    Update a fact or action item. User corrects something CQ got wrong.
    Requires write access via ACL.
    """
    _require_patch_uuid(patch_id)
    # Verify the patch belongs to this user
    subject_key = f"user:{user_id}"
    row = await db_pool.fetchrow(
        """
        SELECT cp.patch_id, cp.value, cp.patch_type
        FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
        WHERE cp.patch_id = $1 AND ps.subject_key = $2
        """,
        patch_id, subject_key
    )
    if not row:
        raise HTTPException(status_code=404, detail="Patch not found for this user")

    # Check write ACL (skip for legacy non-UUID app IDs — no ACL = open access)
    import uuid as _uuid
    try:
        app_uuid = _uuid.UUID(app_id)
        acl = await db_pool.fetchrow(
            "SELECT can_write FROM context_patch_acl WHERE patch_id = $1 AND app_id = $2",
            patch_id, app_uuid
        )
        if acl and not acl["can_write"]:
            raise HTTPException(status_code=403, detail="Write access denied for this patch")
    except (ValueError, AttributeError):
        pass  # Legacy app_id, no ACL enforcement

    # Build update
    value = row["value"]
    if isinstance(value, str):
        value = json.loads(value)

    fact_changed = False
    headline_job = False
    if update.fact is not None:
        fact_changed = (update.fact or "").strip() != str(value.get("text") or "").strip()
        value["text"] = update.fact
        # THE HEADLINE DESCRIBES THE OLD WORDING. Scott edited "in the
        # next couple of days" to "weeks" on 2026-09-02 and the tile
        # kept reading "Travel to mother's location in days", because
        # the headline is derived from the fact and nothing here
        # touched it. A fact that already passes every headline rule is
        # its own headline (free); otherwise the stale line is retired
        # NOW, so no surface can show it, and the worker rewrites it as
        # a cold-path job. Recomputed whenever a fact is sent, not only
        # when it differs, so a re-save is a repair.
        own = headlines_svc.self_headline(update.fact)
        if own is not None:
            value["headline"] = own
        else:
            value.pop("headline", None)
            headline_job = True
        # THE SPOKEN DEADLINE DESCRIBES THE OLD WORDING TOO. "next
        # couple of days" served beside a fact that now says weeks is
        # a contradiction on the wire. Only when the text actually
        # changed and the caller did not set a date: the string moves
        # to `prior_deadline` as the receipt of what was heard, and
        # `deadline_date` is untouched (the caller owns that field).
        if fact_changed and update.deadline_date is None and value.get("deadline"):
            value["prior_deadline"] = value.pop("deadline")
    elif update.refresh_headline:
        own = headlines_svc.self_headline(str(value.get("text") or ""))
        if own is not None:
            value["headline"] = own
        else:
            value.pop("headline", None)
            headline_job = True
    if update.owner is not None:
        value["owner"] = update.owner if update.owner else None

    # The due date. Sets, or clears on "".
    #
    # Unlike the create route this REFUSES a malformed value instead of
    # dropping it, and the difference is deliberate. On create, refusing
    # would lose the task the user just typed, and the date was minted by
    # the client. Here the item already exists and is safe, the user is
    # deliberately editing one field, and silently keeping the old date
    # while answering 200 would tell them the edit worked when it did
    # not. A no-op reported as success is the worse failure on this
    # route.
    #
    # `deadline_history` is NOT touched, on purpose. See PatchUpdate.
    if update.deadline_date is not None:
        if update.deadline_date == "":
            value.pop("deadline_date", None)
        elif _valid_calendar_day(update.deadline_date):
            value["deadline_date"] = update.deadline_date
        else:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INVALID_DEADLINE_DATE",
                    "message": (
                        f"deadline_date {update.deadline_date!r} is not a valid "
                        "YYYY-MM-DD calendar day. Send \"\" to clear the date, "
                        "or omit the field to leave it unchanged."
                    ),
                },
            )

    # Either spelling, then the stored type. `category` first only
    # because it is the older of the two; they are the same field and a
    # caller sending both contradictory values has a bug either way.
    new_type = update.category or update.patch_type or row["patch_type"]

    # A refresh-only call writes `value` and nothing else: no
    # origin_mode flip, because a refresh is not the user declaring
    # anything, and no updated_at bump, because a headline is
    # presentation and a bump would extend decay on a patch nobody
    # re-observed (the headline lane makes the same argument).
    refresh_only = bool(
        update.refresh_headline
        and update.fact is None and update.owner is None
        and update.deadline_date is None
        and not update.category and not update.patch_type
    )
    if refresh_only:
        await db_pool.execute(
            "UPDATE context_patches SET value = $1 WHERE patch_id = $2",
            json.dumps(value), patch_id,
        )
    else:
        await db_pool.execute(
            """
            UPDATE context_patches
            SET value = $1, patch_type = $2, origin_mode = 'declared', updated_at = $3
            WHERE patch_id = $4
            """,
            json.dumps(value), new_type, datetime.utcnow(), patch_id
        )
    if headline_job:
        # Enqueued rather than awaited: this is a write route, not the
        # cold path, and a model call here would hold the user's edit.
        # Failure must not fail the edit, so it is guarded and logged.
        try:
            await redis_client.xadd("memory_updates", {"data": json.dumps({
                "user_id": user_id, "app_id": app_id,
                # The worker's router reads `interaction_type` (or
                # `type`), never `task_type`; the first version of this
                # sent only `task_type` and the job was routed as an
                # untyped task and ignored (2026-09-02). Both keys, like
                # the description-correction enqueue beside it.
                "interaction_type": "headline_patch",
                "task_type": "headline_patch", "patch_id": patch_id,
            })})
        except Exception as exc:
            logger.warning("headline_patch_enqueue_failed", user_id=user_id,
                           patch_id=patch_id, error=str(exc)[:200])

    # Update project_id if requested (move patch to a different project)
    if update.project_id is not None:
        project_row = await db_pool.fetchrow(
            "SELECT name FROM projects WHERE project_id = $1", update.project_id
        )
        project_name = project_row["name"] if project_row else None
        await db_pool.execute(
            "UPDATE context_patches SET project_id = $1, project = $2, updated_at = $3 WHERE patch_id = $4",
            update.project_id, project_name, datetime.utcnow(), patch_id
        )

    # Update permanence_override if supplied. "" CLEARS the override back
    # to the type default; null is "not supplied" and does not enter here.
    if update.permanence_override is not None or update.permanence_override_source is not None:
        valid_classes = {"permanent", "decade", "year", "quarter", "month", "week", "day"}
        valid_sources = {"user", "app"}

        new_override = update.permanence_override
        if new_override == "":
            new_override = None
        if new_override is not None and new_override not in valid_classes:
            raise HTTPException(
                status_code=400,
                detail=f'permanence_override must be one of {sorted(valid_classes)}, or "" to clear it. null means "not supplied" and changes nothing.',
            )

        new_source = update.permanence_override_source or ("user" if new_override is not None else None)
        if new_source is not None and new_source not in valid_sources:
            raise HTTPException(
                status_code=400,
                detail=f'permanence_override_source must be one of {sorted(valid_sources)}, or "" to clear it. null means "not supplied" and changes nothing.',
            )

        await db_pool.execute(
            """
            UPDATE context_patches
            SET permanence_override = $1,
                permanence_override_source = $2,
                updated_at = $3
            WHERE patch_id = $4
            """,
            new_override, new_source, datetime.utcnow(), patch_id,
        )

    # Trigger cache refresh
    stream_key = "memory_updates"
    payload = {"type": "hydrate", "user_id": user_id, "timestamp": datetime.utcnow().isoformat()}
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})

    return {
        "status": "updated",
        "patch_id": patch_id,
        # Echoed so the caller compares rather than assumes, and so a
        # cleared date is distinguishable from an untouched one.
        "deadline_date": value.get("deadline_date"),
    }


@app.post("/v1/quilt/{user_id}/patches/{patch_id}/complete", tags=["Quilt"])
async def complete_patch(
    user_id: str,
    patch_id: str,
    completion: Optional[PatchCompletionRequest] = None,
    app_id: str = Depends(verify_application_access),
):
    """
    Mark a completable patch (commitment, blocker) as done.

    App-initiated counterpart to the worker's LLM-driven auto-close —
    lets the app build tap-to-complete UI instead of waiting for the
    user to mention completion in a later meeting. Applies the same
    gates as the worker's resolver: ownership via patch_subjects,
    completable type, currently open. Requires write access via ACL.

    The patch is archived with completed_at set, so it leaves recall
    and the active quilt, and shows up in the quilt delta's `completed`
    array (distinct from decayed patches in `deleted`).
    """
    _require_patch_uuid(patch_id)
    subject_key = f"user:{user_id}"
    row = await db_pool.fetchrow(
        """
        SELECT cp.patch_id, cp.patch_type, cp.completed_at,
               COALESCE(cp.status, 'active') AS status
        FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
        WHERE cp.patch_id = $1 AND ps.subject_key = $2
        """,
        patch_id, subject_key
    )
    if not row:
        raise HTTPException(status_code=404, detail="Patch not found for this user")
    completable = await _completable_types()
    if row["patch_type"] not in completable:
        raise HTTPException(
            status_code=400,
            detail=f"Patch type '{row['patch_type']}' is not completable "
                   f"(completable types: {', '.join(completable)})",
        )
    if row["completed_at"] is not None or row["status"] != "active":
        raise HTTPException(status_code=409, detail="Patch is already completed or archived")

    # Check write ACL (skip for legacy non-UUID app IDs — no ACL = open access)
    import uuid as _uuid
    try:
        app_uuid = _uuid.UUID(app_id)
        acl = await db_pool.fetchrow(
            "SELECT can_write FROM context_patch_acl WHERE patch_id = $1 AND app_id = $2",
            patch_id, app_uuid
        )
        if acl and not acl["can_write"]:
            raise HTTPException(status_code=403, detail="Write access denied for this patch")
    except (ValueError, AttributeError):
        pass  # Legacy app_id, no ACL enforcement

    evidence = ((completion.evidence if completion else None) or "").strip()[:300]

    # User-declared completion time (absent = server clock = today).
    # Validated in pure logic; the 422 body carries code, field, reason
    # and the received value so the device has the words to explain it.
    try:
        completed_at_override = parse_completed_at(
            completion.completed_at if completion else None
        )
    except CompletedAtError as exc:
        raise HTTPException(status_code=422, detail=exc.detail())

    # The open-state predicate is repeated in the UPDATE so a concurrent
    # completion (e.g. worker auto-close racing a user tap) can't double-
    # apply; the loser of the race gets 409. A backdated completed_at is
    # safe for delta sync because the `completed` array keys on
    # updated_at, which stays NOW(). $2 carries the value stamps below.
    stamps = {"completion_source": "app"}
    if evidence:
        stamps["completion_evidence"] = evidence
    if completed_at_override is not None:
        # A backdated completed_at is a declaration, not an observation;
        # the stamp keeps the two distinguishable on the record.
        stamps["completed_at_source"] = "user"
    completed_row = await db_pool.fetchrow(
        """
        UPDATE context_patches
           SET completed_at = COALESCE($3::timestamptz, NOW()),
               status = 'archived',
               updated_at = NOW(),
               value = value || $2::jsonb
         WHERE patch_id = $1
           AND COALESCE(status, 'active') = 'active'
           AND completed_at IS NULL
        RETURNING completed_at
        """,
        patch_id, json.dumps(stamps), completed_at_override
    )
    if not completed_row:
        raise HTTPException(status_code=409, detail="Patch is already completed or archived")

    # Trigger cache refresh so recall stops surfacing the patch promptly
    # (the render cache alone would age it out within RECALL_RENDER_CACHE_TTL).
    stream_key = "memory_updates"
    payload = {"type": "hydrate", "user_id": user_id, "timestamp": datetime.utcnow().isoformat()}
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})

    return {
        "status": "completed",
        "patch_id": patch_id,
        "completed_at": completed_row["completed_at"].isoformat(),
    }


def _require_patch_uuid(patch_id: str) -> None:
    """422 for a patch id that is not a UUID, before it reaches SQL.

    The hardening promised to GP (2026-08-07): their chat surface mints
    synthetic `cta:`-prefixed ids for call-to-action rows that never
    were CQ patches. One of those reaching a patch verb used to die as
    an asyncpg cast error (a 500 that reads as a CQ outage); now it is a
    422 that says what actually happened. Applies to every route taking
    a {patch_id} path segment, so the contract is one rule rather than
    per-verb trivia.
    """
    try:
        uuid.UUID(patch_id)
    except (ValueError, AttributeError, TypeError):
        if patch_id.startswith("cta:"):
            message = (
                f"'{patch_id}' is a synthetic call-to-action id, not a CQ patch id. "
                "CTA rows are not patches; nothing about them can be completed, "
                "corrected, or triaged here."
            )
        else:
            message = f"'{patch_id}' is not a patch id (expected a UUID)."
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_PATCH_ID", "message": message},
        )


async def _load_open_completable(user_id: str, patch_id: str):
    """Shared gate for the triage write paths (vouch / shelve / un-shelve).

    Same checks as `complete_patch`: the patch must belong to this user
    (404), be a completable type (400), and be open (409). Returns the row
    with the current shelved stamp so callers can enforce their own state
    transition.
    """
    _require_patch_uuid(patch_id)
    subject_key = f"user:{user_id}"
    row = await db_pool.fetchrow(
        """
        SELECT cp.patch_id, cp.patch_type, cp.completed_at,
               COALESCE(cp.status, 'active') AS status,
               cp.value->>'shelved_at' AS shelved_at
        FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
        WHERE cp.patch_id = $1 AND ps.subject_key = $2
        """,
        patch_id, subject_key
    )
    if not row:
        raise HTTPException(status_code=404, detail="Patch not found for this user")
    completable = await _completable_types()
    if row["patch_type"] not in completable:
        raise HTTPException(
            status_code=400,
            detail=f"Patch type '{row['patch_type']}' is not completable "
                   f"(completable types: {', '.join(completable)})",
        )
    if row["completed_at"] is not None or row["status"] != "active":
        raise HTTPException(status_code=409, detail="Patch is already completed or archived")
    return row


async def _check_patch_write_acl(patch_id: str, app_id: str):
    """Write ACL, same shape as complete/delete (no ACL row = open access)."""
    import uuid as _uuid
    try:
        app_uuid = _uuid.UUID(app_id)
        acl = await db_pool.fetchrow(
            "SELECT can_write FROM context_patch_acl WHERE patch_id = $1 AND app_id = $2",
            patch_id, app_uuid
        )
        if acl and not acl["can_write"]:
            raise HTTPException(status_code=403, detail="Write access denied for this patch")
    except (ValueError, AttributeError):
        pass  # Legacy app_id, no ACL enforcement


async def _hydrate_refresh(user_id: str):
    payload = {"type": "hydrate", "user_id": user_id, "timestamp": datetime.utcnow().isoformat()}
    await redis_client.xadd("memory_updates", {"data": json.dumps(payload)})


@app.post("/v1/quilt/{user_id}/patches/{patch_id}/vouch", tags=["Quilt"])
async def vouch_patch(
    user_id: str,
    patch_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    "Still live": the user deliberately vouches an open item is current.

    Bumps `updated_at`, which extends the item's decay clock (completables
    anchor on GREATEST(updated_at, deadline_date)) — that extension is the
    point of the tap. The vouch is ALSO stamped explicitly
    (`value.last_vouched_at`, `value.vouch_source`) because a plain bump is
    indistinguishable from an incidental recall touch: without the stamp,
    the strongest signal in the app's triage would be recorded the same way
    as a passing glance, and the signal would not survive.

    Vouching does NOT clear a shelf: the two states are orthogonal writes
    and un-shelving is `DELETE .../shelve`.

    IT DOES CLEAR A BELIEVED COMPLETION, and that is the whole answer to
    "looks done, confirm?". A meeting guessed the item was finished, the
    person who knows says it is still live, and that answer must outrank
    the guess. The stamps move to `prior_believed_*` rather than being
    dropped, on the uncomplete route's discipline: the guess and its
    correction are both facts, and a later pass that silently re-believed
    the same item would look identical to a first belief without them.

    The counterpart is `POST .../complete`, which is how a user CONFIRMS
    the belief; it closes the item with `completion_source='app'`, so a
    human answer is never recorded as the machine's.

    Any request body is accepted and ignored (the gateway forwards bodies
    untyped).
    """
    await _load_open_completable(user_id, patch_id)
    await _check_patch_write_acl(patch_id, app_id)

    vouched_row = await db_pool.fetchrow(
        """
        UPDATE context_patches
           SET updated_at = NOW(),
               value = jsonb_set(
                           jsonb_set(
                               (CASE WHEN value ? 'believed_complete_at'
                               THEN value
                                    || jsonb_build_object(
                                        'prior_believed_complete_at',
                                        value->'believed_complete_at',
                                        'prior_believed_complete_evidence',
                                        value->'believed_complete_evidence',
                                        'believed_rejected_at',
                                        to_jsonb(to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')))
                               ELSE value END)
                               - 'believed_complete_at'
                               - 'believed_complete_evidence'
                               - 'believed_complete_reasons'
                               - 'believed_complete_origin_id'
                               - 'believed_evidence_strength',
                               '{last_vouched_at}', to_jsonb(to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))),
                           '{vouch_source}', '"app"'
                       )
         WHERE patch_id = $1
           AND COALESCE(status, 'active') = 'active'
           AND completed_at IS NULL
        RETURNING value->>'last_vouched_at' AS last_vouched_at,
                  (value ? 'believed_rejected_at') AS cleared_belief
        """,
        patch_id
    )
    if not vouched_row:
        raise HTTPException(status_code=409, detail="Patch is already completed or archived")

    await _hydrate_refresh(user_id)
    return {
        "status": "vouched",
        "patch_id": patch_id,
        "last_vouched_at": vouched_row["last_vouched_at"],
        # True when this tap also answered a "looks done, confirm?" with
        # a no. The client needs to tell the two apart: one is a routine
        # "still live", the other retires a card.
        "cleared_believed_completion": bool(vouched_row["cleared_belief"]),
    }


@app.post("/v1/quilt/{user_id}/patches/{patch_id}/shelve", tags=["Quilt"])
async def shelve_patch(
    user_id: str,
    patch_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    "Let it go": out of the ledger, still known to the assistant,
    reversible.

    The patch STAYS active — recall still finds it, so the assistant can
    still answer "did Vijay ever owe me the hardware POC?". What changes:
    `value.shelved_at` + `value.shelved_source` are stamped, the People
    ledger and its counts stop carrying the item, and it reaches clients
    as a normal patch update rather than a tombstone. Archiving instead
    would flow it through the delta `deleted` array — the same array a
    DECAYED item flows in — making a deliberate user action
    indistinguishable from the passage of time.

    Decay still applies: shelving is the user declining to act, not
    asserting the item is immortal. (The `updated_at` bump this write
    carries restarts the item's TTL, so a shelved item gets one final
    grace window from the shelve, then archives on its own.)

    409 when already shelved. Reverse with `DELETE .../shelve`.
    """
    row = await _load_open_completable(user_id, patch_id)
    if row["shelved_at"] is not None:
        raise HTTPException(status_code=409, detail="Patch is already shelved")
    await _check_patch_write_acl(patch_id, app_id)

    shelved_row = await db_pool.fetchrow(
        """
        UPDATE context_patches
           SET updated_at = NOW(),
               value = jsonb_set(
                           jsonb_set(value, '{shelved_at}', to_jsonb(to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'))),
                           '{shelved_source}', '"app"'
                       )
         WHERE patch_id = $1
           AND COALESCE(status, 'active') = 'active'
           AND completed_at IS NULL
           AND value->>'shelved_at' IS NULL
        RETURNING value->>'shelved_at' AS shelved_at
        """,
        patch_id
    )
    if not shelved_row:
        raise HTTPException(status_code=409, detail="Patch is already shelved, completed, or archived")

    await _hydrate_refresh(user_id)
    return {
        "status": "shelved",
        "patch_id": patch_id,
        "shelved_at": shelved_row["shelved_at"],
    }


@app.delete("/v1/quilt/{user_id}/patches/{patch_id}/shelve", tags=["Quilt"])
async def unshelve_patch(
    user_id: str,
    patch_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    Un-shelve: clears the shelved stamps, so the item re-enters the
    People ledger and its counts on the next read. 409 when the patch is
    not currently shelved.
    """
    row = await _load_open_completable(user_id, patch_id)
    if row["shelved_at"] is None:
        raise HTTPException(status_code=409, detail="Patch is not shelved")
    await _check_patch_write_acl(patch_id, app_id)

    unshelved = await db_pool.fetchrow(
        """
        UPDATE context_patches
           SET updated_at = NOW(),
               value = value - 'shelved_at' - 'shelved_source'
         WHERE patch_id = $1
           AND COALESCE(status, 'active') = 'active'
           AND completed_at IS NULL
           AND value->>'shelved_at' IS NOT NULL
        RETURNING patch_id
        """,
        patch_id
    )
    if not unshelved:
        raise HTTPException(status_code=409, detail="Patch is not shelved, or is completed or archived")

    await _hydrate_refresh(user_id)
    return {"status": "unshelved", "patch_id": patch_id}


@app.post("/v1/quilt/{user_id}/patches/{patch_id}/uncomplete", tags=["Quilt"])
async def uncomplete_patch(
    user_id: str,
    patch_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    Reverse a completion: the item was marked done and it is not.

    This is the correction verb for ALL THREE completion lanes, not just a
    mistapped checkbox: extraction auto-close is an LLM deciding from a
    transcript that something resolved, and the chat-completion lane is an
    LLM matching a statement to a patch. Both can be wrong, and before this
    route the only fix was an operator editing prod by hand. (A wrongly
    completed item DOES partially self-heal without this: dedup only
    matches active patches, so restating the item in a later meeting
    creates a fresh open copy. But that copy has no lineage, the false
    completion stays on the record, and the user waits for a meeting.)

    Restores `status = 'active'` and clears `completed_at`, so the item
    re-enters recall, the ledger, and the active quilt, and REAPPEARS in
    the next delta as a normal patch update (its tombstone stops being
    served: `deleted[]`/`completed[]` are computed from the current row,
    not from history). The original completion is preserved for audit as
    `value.prior_completed_at` / `prior_completion_source` /
    `prior_completion_evidence`, alongside `uncompleted_at` +
    `uncompletion_source`, so "completed then reopened" is never
    indistinguishable from "never completed".

    The `updated_at` bump restarts the decay clock: a restored item gets a
    full TTL window rather than inheriting the neglect that preceded its
    wrong completion.

    409 when the patch is not completed. Works on any completed
    completable regardless of which lane closed it.
    """
    _require_patch_uuid(patch_id)
    subject_key = f"user:{user_id}"
    row = await db_pool.fetchrow(
        """
        SELECT cp.patch_id, cp.patch_type, cp.completed_at,
               COALESCE(cp.status, 'active') AS status
        FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
        WHERE cp.patch_id = $1 AND ps.subject_key = $2
        """,
        patch_id, subject_key
    )
    if not row:
        raise HTTPException(status_code=404, detail="Patch not found for this user")
    completable = await _completable_types()
    if row["patch_type"] not in completable:
        raise HTTPException(
            status_code=400,
            detail=f"Patch type '{row['patch_type']}' is not completable "
                   f"(completable types: {', '.join(completable)})",
        )
    if row["completed_at"] is None:
        raise HTTPException(status_code=409, detail="Patch is not completed")
    await _check_patch_write_acl(patch_id, app_id)

    # The audit stamps read the OLD row (RHS expressions see pre-UPDATE
    # values); `- 'archive_cause'` covers the replaces lifecycle, which
    # sets completed_at with a cause and no completion_source. A null
    # prior_completion_source is honest: it means the completion predates
    # source stamping or came from a lifecycle archive.
    restored = await db_pool.fetchrow(
        """
        UPDATE context_patches
           SET status = 'active',
               completed_at = NULL,
               updated_at = NOW(),
               value = (CASE WHEN value ? 'believed_complete_at'
                        THEN value || jsonb_build_object(
                                    'prior_believed_complete_at',
                                    value->'believed_complete_at',
                                    'prior_believed_complete_evidence',
                                    value->'believed_complete_evidence')
                        ELSE value END
                        - 'believed_complete_at'
                        - 'believed_complete_evidence'
                        - 'believed_complete_reasons'
                        - 'believed_complete_origin_id'
                        - 'believed_evidence_strength'
                        - 'completion_source' - 'completion_evidence'
                        - 'archive_cause' - 'completed_at_source')
                       || jsonb_build_object(
                              'uncompleted_at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                              'uncompletion_source', 'app',
                              'prior_completed_at', to_char(completed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
                              'prior_completion_source', value->>'completion_source',
                              'prior_completion_evidence', value->>'completion_evidence',
                              'prior_completed_at_source', value->>'completed_at_source'
                          )
         WHERE patch_id = $1
           AND completed_at IS NOT NULL
        RETURNING value->>'uncompleted_at' AS uncompleted_at,
                  value->>'prior_completion_source' AS prior_completion_source
        """,
        patch_id
    )
    if not restored:
        raise HTTPException(status_code=409, detail="Patch is not completed")

    await _hydrate_refresh(user_id)
    return {
        "status": "uncompleted",
        "patch_id": patch_id,
        "uncompleted_at": restored["uncompleted_at"],
        "prior_completion_source": restored["prior_completion_source"],
    }


FOLLOW_UP_MIN_BASIS = 5


@app.get("/v1/quilt/{user_id}/insights", tags=["Quilt"])
async def quilt_insights(
    user_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    ABOUT-YOU aggregates for the Memory tab (design 10c) that a client
    can never reconstruct from sync: completion history leaves the sync
    surface, so a fresh install has no basis for a follow-up rate.

    `follow_up` is the rate of the USER'S OWN completable items that
    reached an observable resolution: completed / (completed +
    open-and-overdue). Items that decayed without an observed resolution
    are EXCLUDED from the claim, because decay means unobserved, not
    unfulfilled; they are reported separately as `unresolved` so the
    exclusion is visible rather than silent.

    "The user's own" resolves through the ego link (entities.self_at,
    migration 35) with the SAME edge-first owner resolution the quilt
    action items serve, so this number and the owner chips can never
    disagree about whose item something is. No self entity, or fewer
    than FOLLOW_UP_MIN_BASIS resolved items, serves `follow_up: null` —
    a percentage over two data points is a coin flip wearing a ring
    chart, and null renders as not-tracked (the house rule: 0 and
    cannot-tell are different claims).

    Deadline comparisons bucket to the UTC day, so the response is
    stable within a day for a quiet quilt.
    """
    self_entity = await db_pool.fetchval(
        "SELECT entity_id FROM entities "
        "WHERE user_id = $1 AND self_at IS NOT NULL",
        user_id,
    )
    if self_entity is None:
        return {"user_id": user_id, "self_entity_id": None, "follow_up": None}

    vocab = await _people_vocab_cached(app_id)
    rows = await db_pool.fetch(
        """
        SELECT cp.patch_id, cp.value, COALESCE(cp.status, 'active') AS status,
               cp.completed_at,
               (cp.value->>'deadline_date') AS deadline_date
        FROM context_patches cp
        JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
        WHERE ps.subject_key = $1 AND cp.patch_type = 'commitment'
        """,
        f"user:{user_id}",
    )
    ids = [r["patch_id"] for r in rows]
    owner_text_by_item: dict = {}
    if ids:
        owns_rows = await db_pool.fetch(
            """
            SELECT DISTINCT ON (pc.to_patch_id)
                   pc.to_patch_id, op.value->>'text' AS person_text
            FROM patch_connections pc
            JOIN context_patches op ON op.patch_id = pc.from_patch_id
            JOIN context_patches item ON item.patch_id = pc.to_patch_id
            WHERE pc.to_patch_id = ANY($1::uuid[])
              AND pc.connection_label = $2
              AND COALESCE(pc.status, 'active') = 'active'
            ORDER BY pc.to_patch_id,
                     (lower(btrim(op.value->>'text'))
                      = lower(btrim(item.value->>'owner'))) DESC NULLS LAST,
                     pc.created_at, pc.connection_id
            """,
            ids, vocab.ownership_label,
        )
        owner_text_by_item = {
            str(r["to_patch_id"]): r["person_text"] for r in owns_rows
        }
    entity_rows = await db_pool.fetch(
        "SELECT entity_id, name, merged_into FROM entities "
        "WHERE user_id = $1 AND entity_type = $2",
        user_id, vocab.person_entity_type,
    )
    alias_rows = await db_pool.fetch(
        "SELECT a.alias, a.entity_id FROM entity_aliases a "
        "JOIN entities e ON e.entity_id = a.entity_id "
        "WHERE a.user_id = $1 AND e.entity_type = $2",
        user_id, vocab.person_entity_type,
    )
    resolve_owner_entity = build_entity_resolver(
        [dict(r) for r in entity_rows], [dict(r) for r in alias_rows]
    )

    today = datetime.utcnow().date().isoformat()
    completed = overdue_open = unresolved = 0
    for r in rows:
        value = r["value"]
        if isinstance(value, str):
            value = json.loads(value)
        owner_entity = (
            resolve_owner_entity(owner_text_by_item.get(str(r["patch_id"])))
            or resolve_owner_entity(value.get("owner"))
        )
        # An ownerless commitment on the user's own quilt is the (you)
        # speaker's by the extraction contract (owner is stripped on
        # self-owned items; reassign-speaker to_self clears it the same
        # way), so it counts as the user's alongside explicit self hits.
        is_self = owner_entity == str(self_entity) or (
            owner_entity is None and not value.get("owner")
        )
        if not is_self:
            continue
        if r["completed_at"] is not None:
            completed += 1
        elif r["status"] == "active":
            if r["deadline_date"] and r["deadline_date"] < today:
                overdue_open += 1
        else:
            unresolved += 1

    basis = completed + overdue_open
    if basis < FOLLOW_UP_MIN_BASIS:
        return {
            "user_id": user_id,
            "self_entity_id": str(self_entity),
            "follow_up": None,
        }
    return {
        "user_id": user_id,
        "self_entity_id": str(self_entity),
        "follow_up": {
            "completed": completed,
            "overdue_open": overdue_open,
            "rate": round(completed / basis, 2),
            "unresolved": unresolved,
        },
    }


@app.delete("/v1/quilt/{user_id}/patches/{patch_id}", tags=["Quilt"])
async def delete_patch(
    user_id: str,
    patch_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    Delete a fact or action item. User removes something CQ got wrong.
    Requires delete access via ACL (or no ACL entry = open access).

    ARCHIVES rather than hard-deleting (boundary decision, 2026-08-11).
    The old hard delete had no tombstone: `deleted[]` is computed from
    archived rows, so a deletion never reached other devices until the
    daily full sync, and for a person patch it silently cascaded every
    owns/works_on/owed_to edge away. Now the row archives with
    `archive_cause: "user_delete"`, the `updated_at` bump puts it in the
    next delta's `deleted[]`, and edges stay in place but stop being
    served (every read filters to active patches).

    CONTRACT (SS's condition, stated so it cannot drift): a patch
    archived by user delete is excluded from recall and EVERY serving
    path, not just the tab. Deletion must be real from the user's seat
    even though the row survives until account purge. Today that holds
    because every serving read filters `COALESCE(status,'active') =
    'active'`; any future read that relaxes that filter must exclude
    `archive_cause = 'user_delete'` rows explicitly.

    Idempotent: deleting an already-archived patch returns the same
    success without overwriting the original archive cause. Hard
    deletion remains only on the account-purge paths, where leaving
    rows behind defeats the purpose.
    """
    _require_patch_uuid(patch_id)
    subject_key = f"user:{user_id}"
    row = await db_pool.fetchrow(
        """
        SELECT cp.patch_id
        FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
        WHERE cp.patch_id = $1 AND ps.subject_key = $2
        """,
        patch_id, subject_key
    )
    if not row:
        raise HTTPException(status_code=404, detail="Patch not found for this user")

    # Check delete ACL (skip for legacy non-UUID app IDs — no ACL = open access)
    import uuid as _uuid
    try:
        app_uuid = _uuid.UUID(app_id)
        acl = await db_pool.fetchrow(
            "SELECT can_delete FROM context_patch_acl WHERE patch_id = $1 AND app_id = $2",
            patch_id, app_uuid
        )
        if acl and not acl["can_delete"]:
            raise HTTPException(status_code=403, detail="Delete access denied for this patch")
    except (ValueError, AttributeError):
        pass  # Legacy app_id, no ACL enforcement

    # Archive, never hard-delete. patch_subjects is deliberately KEPT:
    # the delta's `deleted[]` query joins through it, so removing the
    # subject row would orphan the tombstone this change exists to
    # serve. The WHERE guard keeps a second delete (or a delete racing
    # decay) from overwriting the original archive cause.
    await db_pool.execute(
        """
        UPDATE context_patches
           SET status = 'archived', updated_at = NOW(),
               value = jsonb_set(value, '{archive_cause}', '"user_delete"')
         WHERE patch_id = $1
           AND COALESCE(status, 'active') = 'active'
        """,
        patch_id,
    )

    # Trigger cache refresh
    stream_key = "memory_updates"
    payload = {"type": "hydrate", "user_id": user_id, "timestamp": datetime.utcnow().isoformat()}
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})

    return {"status": "deleted", "patch_id": patch_id}


@app.delete("/v1/quilt/{user_id}", tags=["Quilt"])
async def delete_all_patches(
    user_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    Delete ALL patches, entities, and relationships for a user.
    Use for testing or when a user requests full data deletion.
    """
    subject_key = f"user:{user_id}"

    # Delete all patches and related records for this user
    patch_ids = await db_pool.fetch(
        "SELECT patch_id FROM patch_subjects WHERE subject_key = $1", subject_key
    )
    count = len(patch_ids)

    for row in patch_ids:
        pid = row["patch_id"]
        # Hard delete, like account purge. Migration 32 archives connections
        # so ordinary removals stay auditable; erasing a user's data is the
        # one case where leaving rows behind defeats the purpose.
        await db_pool.execute("DELETE FROM patch_connections WHERE from_patch_id = $1 OR to_patch_id = $1", pid)
        await db_pool.execute("DELETE FROM patch_usage_metrics WHERE patch_id = $1", pid)
        await db_pool.execute("DELETE FROM context_patch_acl WHERE patch_id = $1", pid)

    await db_pool.execute("DELETE FROM patch_subjects WHERE subject_key = $1", subject_key)
    await db_pool.execute(
        """DELETE FROM context_patches WHERE patch_id IN (
            SELECT patch_id FROM patch_subjects WHERE subject_key = $1
        ) OR patch_id NOT IN (SELECT patch_id FROM patch_subjects)""",
        subject_key
    )

    # Also clear entities and relationships
    entity_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM entities WHERE user_id = $1", user_id
    )
    await db_pool.execute("DELETE FROM relationships WHERE user_id = $1", user_id)
    await db_pool.execute("DELETE FROM entities WHERE user_id = $1", user_id)

    # Clear Redis caches
    await redis_client.delete(f"entity_index:{user_id}")
    await redis_client.delete(f"active_context:{user_id}")

    return {
        "status": "deleted",
        "user_id": user_id,
        "patches_deleted": count,
        "entities_deleted": entity_count,
    }


# ============================================
# Patch Create
# ============================================

# Aligned to the v5 SS manifest (init-db/11_shouldersurf_schema.json).
# `identity` and `experience` are retired per the v1 taxonomy decision —
# see docs/memos/patch-taxonomy-simplification.md and worker.py's
# DEFAULT_PERSISTENCE for the matching list on the extraction path.
VALID_PATCH_TYPES = {
    "trait", "preference", "goal", "constraint",
    "person", "org", "project", "deliverable",
    "role", "decision", "commitment", "blocker",
    "takeaway", "event",
}

PATCH_PERSISTENCE = {
    "trait": "sticky", "preference": "sticky",
    "goal": "sticky", "constraint": "sticky",
    "role": "sticky", "person": "sticky", "org": "sticky",
    "project": "sticky", "deliverable": "sticky",
    "decision": "sticky", "commitment": "sticky", "blocker": "sticky",
    "takeaway": "decaying", "event": "decaying",
}

PROJECT_SCOPED_TYPES = {
    "goal", "constraint", "deliverable",
    "role", "decision", "commitment", "blocker",
    "takeaway", "event",
}


class PatchConnectionInput(BaseModel):
    # extra="forbid" is the promise made to GP (2026-08-07): an unknown
    # key on a CONNECTION object is a 422, never silently dropped. Their
    # `relationship` -> `label` rename shipped a payload where the real
    # field name was ignored and the edge landed with label NULL; a
    # loud contract beats a quiet default. Forbid is scoped to the
    # connection shapes only — top-level request models stay tolerant so
    # additive evolution keeps working.
    model_config = ConfigDict(extra="forbid")

    target_patch_id: str
    role: str  # parent, depends_on, resolves, replaces, informs
    label: Optional[str] = None  # belongs_to, blocked_by, owns, works_on, etc.
    context: Optional[str] = None


class PatchCreate(BaseModel):
    type: str = Field(..., description="Patch type: trait, preference, goal, constraint, person, org, project, deliverable, role, decision, commitment, blocker, takeaway, event")
    text: str = Field(..., description="The patch content")
    owner: Optional[str] = Field(default=None, description="Owner name (for commitment, blocker, decision)")
    project_id: Optional[str] = Field(default=None, description="Project UUID to scope this patch to")
    connections: Optional[List[PatchConnectionInput]] = Field(default=None, description="Optional connections to existing patches")
    # IDEMPOTENCY KEY, minted by the client and stable across its retries.
    #
    # The write this route serves is a human tapping Add. SS's account of
    # the failure is the specification: the user taps, the network stalls,
    # they see nothing, they tap again. Without a key the second tap is a
    # second row in somebody's ledger, and the client's only safe response
    # to an ambiguous write is to STOP RETRYING and park the item for a
    # human — which is honest and a worse product than it needs to be.
    #
    # With a key, a repeat POST returns the row the first one created,
    # with 200 and `created: false`, so an ambiguous write becomes an
    # ordinary retry.
    #
    # Optional: the extractor and every existing caller write without one
    # and are unaffected.
    client_id: Optional[str] = Field(
        default=None,
        description="Client-minted idempotency key (e.g. a UUID). A repeat POST with the same value returns the existing patch with created=false instead of creating a second one.",
    )
    # THE DAY THIS IS DUE, as a plain YYYY-MM-DD.
    #
    # Scott ruled the date optional (relayed by SS 2026-08-30). Optional
    # is the operative word: an item WITHOUT one is a legitimate item,
    # not a degraded one, so this never becomes required.
    #
    # It matters more than a display string. A completable with no
    # deadline_date can never be overdue, never reaches the project
    # recall guarantee's five-overdue slot, and anchors decay on
    # updated_at instead of GREATEST(updated_at, deadline_date). So an
    # item without one is TRACKED BUT NEVER CHASED, and that is the whole
    # reason this field exists.
    #
    # Day granularity, deliberately, and it matches what every consumer
    # already reads: eight separate call sites cast
    # value->>'deadline_date' to ::date behind a
    # `^\d{4}-\d{2}-\d{2}$` regex guard. A value that misses that
    # shape is not a slightly-wrong date, it is invisible to every one of
    # them, silently. Hence validation here rather than storage here.
    #
    # No companion `deadline` free-text is accepted: that field means
    # "as spoken in the room", and nobody spoke this one.
    deadline_date: Optional[str] = Field(
        default=None,
        description="Optional due date as a plain calendar day, YYYY-MM-DD, in the user's own timezone. No time component and no zone suffix. Stored and echoed back verbatim, never normalised.",
    )


# The exact shape every downstream ::date cast guards on. Written as a
# regex AND a real calendar parse, because the regex alone accepts
# 2026-02-31 and 2026-13-01, which pass the guard, reach the cast, and
# raise there instead of being skipped.
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _valid_calendar_day(raw: object) -> bool:
    """True only for a string that is BOTH the guarded shape and a real day."""
    if not isinstance(raw, str) or not _ISO_DAY.match(raw):
        return False
    try:
        date.fromisoformat(raw)
    except ValueError:
        return False
    return True


async def _existing_client_id_patch(client_id: str, subject_key: str):
    """The active patch already holding this idempotency key, for THIS
    subject, or None.

    The subject check is not decoration. The unique index is global,
    because the key is a client-minted UUID and `patch_subjects` is a
    separate table an index cannot reach into. So a key colliding across
    users is possible in principle, and returning the row without
    checking would hand one user another user's patch in an echo. A
    mismatch is refused by the caller instead.
    """
    return await db_pool.fetchrow(
        """
        SELECT cp.patch_id
        FROM context_patches cp
        JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
        WHERE cp.client_id = $1 AND ps.subject_key = $2
          AND COALESCE(cp.status, 'active') = 'active'
        LIMIT 1
        """,
        client_id, subject_key,
    )


async def _created_patch_response(
    user_id: str, app_id: str, patch_id: str, patch_type: str,
    *, created: bool, connections: list, warnings: list | None = None,
    superseded: list | None = None,
):
    """The create echo, rendered by the READ route rather than beside it.

    SS asked for the created item back "exactly as /v1/quilt would later
    render it, so the client compares rather than assumes". The tempting
    way to do that is to build a QuiltPatchResponse here. That would be a
    SECOND renderer for one wire shape, and the two would drift the first
    time a field is added to one and not the other, silently, with the
    create path telling the client something the read path does not.

    So this asks the actual read route. `get_user_quilt` is a plain async
    function and `since` already exists, so a one-second delta returns a
    handful of rows and the item is picked out of them. The echo is then
    identical to /v1/quilt BY CONSTRUCTION rather than by a test that
    could go stale.

    THE ECHO MUST NEVER FAIL THE WRITE. The patch is committed by the
    time this runs; a slow or failed render is a thinner response, never
    a 500 for a create that succeeded and never a signal to the client to
    retry. On any failure the item is simply absent and `item_rendered`
    says so, which is the honest shape: the client refetches rather than
    believing a fabricated echo.
    """
    body = {
        "status": "created" if created else "exists",
        "created": created,
        "patch_id": patch_id,
        "type": patch_type,
        "connections": connections,
        "item": None,
        "item_rendered": False,
        # Present and empty when nothing was dropped, so a caller can
        # test the key rather than its absence.
        "warnings": list(warnings or []),
        # Which prior USER-STATED rows this write archived. Same
        # convention: present and empty when none, so a client tests the
        # key. A 200 says the write was processed, never that it did
        # what the caller meant, and "did my old title actually go away"
        # is exactly the question a correction UI has to answer.
        "superseded_patch_ids": list(superseded or []),
    }
    try:
        since = (datetime.utcnow() - timedelta(seconds=5)).isoformat()
        # EVERY parameter is passed explicitly, and that is load bearing.
        # These are FastAPI route parameters whose defaults are `Query(None)`
        # OBJECTS, not None. FastAPI substitutes the real value per request,
        # but a DIRECT call does not: an omitted `category` would arrive as a
        # truthy Query instance and silently filter the echo to nothing.
        # Verified by running it, not by reading the signature.
        quilt = await get_user_quilt(
            user_id=user_id,
            category=None,
            since=since,
            origin_id=None,
            group_by=None,
            project_id=None,
            limit=None,
            order=None,
            max_age_days=None,
            app_id=app_id,
        )
        # QuiltResponse splits its rows across TWO arrays, `facts` and
        # `action_items` (completables land in the second). There is no
        # `patches` field. Both are searched because the create route
        # accepts every VALID_PATCH_TYPES value, not only completables,
        # so which array a new patch lands in depends on its type.
        for bucket in (quilt.action_items or []), (quilt.facts or []):
            for candidate in bucket:
                if str(candidate.patch_id) == str(patch_id):
                    body["item"] = candidate
                    body["item_rendered"] = True
                    break
            if body["item_rendered"]:
                break
    except Exception as exc:
        logger.warning("create_patch_echo_failed",
                       patch_id=patch_id, error=str(exc)[:200])
    return body


@app.post("/v1/quilt/{user_id}/patches", tags=["Quilt"])
async def create_patch(
    user_id: str,
    patch: PatchCreate,
    app_id: str = Depends(verify_application_access),
):
    """
    Create a patch manually. Origin is 'declared' (user-created, not extracted).
    Returns the new patch_id so the caller can immediately wire up connections.
    """
    if patch.type not in VALID_PATCH_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid patch type '{patch.type}'. Must be one of: {', '.join(sorted(VALID_PATCH_TYPES))}"
        )

    patch_id = str(uuid.uuid4())
    subject_key = f"user:{user_id}"
    now = datetime.utcnow()
    persistence = PATCH_PERSISTENCE.get(patch.type, "decaying")

    # Idempotency fast path. The unique index below is what makes a
    # concurrent double-tap impossible; this lookup is what makes a
    # SEQUENTIAL retry cheap, which is the common case (the user taps,
    # gives up, and taps again seconds later).
    if patch.client_id:
        prior = await _existing_client_id_patch(patch.client_id, subject_key)
        if prior is not None:
            return await _created_patch_response(
                user_id, app_id, str(prior["patch_id"]), patch.type,
                created=False, connections=[],
            )

    # Build value JSON
    value = {"text": patch.text}
    if patch.owner:
        value["owner"] = patch.owner

    # A malformed date is DROPPED, not stored, and never fails the write.
    #
    # Storing it would be worse than dropping it: every consumer guards
    # its ::date cast with the same regex, so a bad value is silently
    # skipped by all of them while sitting in the row looking like a
    # deadline. The item would read as dated and behave as undated.
    #
    # Refusing the whole write would be worse still. The user typed a
    # task; losing the task because the date was malformed is a bad
    # trade, and the client mints the date itself so a bad one is its bug
    # to fix, not the user's to retype. So: keep the item, drop the date,
    # and SAY SO in the response rather than leave the caller to infer it
    # from an absence.
    deadline_warning = None
    if patch.deadline_date is not None:
        if _valid_calendar_day(patch.deadline_date):
            value["deadline_date"] = patch.deadline_date
        else:
            deadline_warning = (
                f"deadline_date {patch.deadline_date!r} is not a valid "
                "YYYY-MM-DD calendar day and was not stored. The item was "
                "created without a date."
            )
            logger.warning("create_patch_deadline_rejected",
                           value=str(patch.deadline_date)[:40])

    # Resolve project scope
    #
    # A `project_id` THAT RESOLVES TO NOTHING IS SAID OUT LOUD, and the
    # item is still created. Same trade as the malformed deadline above,
    # for the same reason: the user typed a decision and losing it
    # because the scope was wrong is a bad trade. What is NOT acceptable
    # is doing it silently, which is what happened until now.
    #
    # The row keeps the id it was sent, so the patch stores an id with a
    # NULL name. Every consumer joins `projects` to render a scope, so it
    # reads as unscoped everywhere while sitting in the row looking
    # scoped, which is precisely the "dated and behaves undated" shape
    # the deadline comment describes.
    #
    # The lookup is an EXACT match and `projects.project_id` is TEXT, not
    # uuid (init-db/06_projects_and_meetings.sql), so case matters. On
    # prod today every project carrying real data is uppercase, matching
    # Swift's `uuidString`; the only lowercase rows are three test
    # artifacts with zero patches, and two ids are not UUID-shaped at all
    # ("CUE-SMOKE-PROJ-1", "smoke-test-0610"), which is why this stays a
    # string compare rather than becoming a uuid cast that would 500 on
    # them. A client that lowercases would silently scope to nothing, and
    # this warning is what makes that audible on the first write instead
    # of on the first missing rundown.
    project_name = None
    project_id = patch.project_id
    project_warning = None
    if project_id:
        project_row = await db_pool.fetchrow(
            "SELECT name FROM projects WHERE project_id = $1", project_id
        )
        project_name = project_row["name"] if project_row else None
        if project_row is None:
            project_warning = (
                f"project_id {project_id!r} did not match a known project and "
                "no project name was resolved. The item was created; it will "
                "read as unscoped until the id is corrected. The match is "
                "exact and case-sensitive."
            )
            logger.warning("create_patch_project_unresolved",
                           project_id=str(project_id)[:60],
                           patch_type=patch.type)
    elif patch.type not in PROJECT_SCOPED_TYPES:
        project_id = None

    patch_project = project_name if patch.type in PROJECT_SCOPED_TYPES else None
    patch_project_id = project_id if patch.type in PROJECT_SCOPED_TYPES else None

    # ON CONFLICT closes the window the fast path above cannot. Two taps
    # in flight together both miss the SELECT; only one survives the
    # unique index, and the loser is told which row won rather than
    # getting an error for a write that did what the user meant.
    inserted = await db_pool.fetchval(
        """
        INSERT INTO context_patches (
            patch_id, patch_name, patch_type, value,
            origin_mode, source_prompt, confidence, persistence,
            project, project_id, status, created_at, updated_at, last_observed_at,
            client_id
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
        ON CONFLICT (client_id) WHERE client_id IS NOT NULL DO NOTHING
        RETURNING patch_id
        """,
        patch_id, f"declared_{patch_id[:8]}", patch.type, json.dumps(value),
        "declared", "manual", 1.0, persistence,
        patch_project, patch_project_id, "active", now, now, now,
        patch.client_id,
    )
    if inserted is None:
        # The concurrent tap won. Serve ITS row, so both requests agree
        # on which patch exists rather than one of them 500ing on a write
        # that succeeded.
        prior = await _existing_client_id_patch(patch.client_id, subject_key)
        if prior is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CLIENT_ID_TAKEN",
                    "message": (
                        "This client_id already belongs to a patch on another "
                        "subject. Idempotency keys must be unique per client."
                    ),
                },
            )
        return await _created_patch_response(
            user_id, app_id, str(prior["patch_id"]), patch.type,
            created=False, connections=[],
        )

    await db_pool.execute(
        "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
        patch_id, subject_key
    )

    # ACL — declared patches get full access for the creating app
    import uuid as _uuid
    try:
        app_uuid = _uuid.UUID(app_id)
        await db_pool.execute(
            """
            INSERT INTO context_patch_acl (patch_id, app_id, can_read, can_write, can_delete)
            VALUES ($1, $2, TRUE, TRUE, TRUE)
            """,
            patch_id, app_uuid
        )
    except (ValueError, AttributeError):
        pass  # Legacy app_id, no ACL

    # Create connections if provided
    created_connections = []
    if patch.connections:
        for conn in patch.connections:
            # Verify target patch belongs to this user
            target_row = await db_pool.fetchrow(
                "SELECT cp.patch_id FROM context_patches cp JOIN patch_subjects ps ON cp.patch_id = ps.patch_id WHERE cp.patch_id = $1 AND ps.subject_key = $2",
                conn.target_patch_id, subject_key
            )
            if not target_row:
                continue  # Skip invalid connections silently

            await db_pool.execute(
                """
                INSERT INTO patch_connections (from_patch_id, to_patch_id, connection_role, connection_label, context)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO UPDATE SET
                    connection_label = EXCLUDED.connection_label,
                    context = EXCLUDED.context,
                    status = 'active'
                """,
                patch_id, conn.target_patch_id, conn.role, conn.label, conn.context
            )

            # Lifecycle: "replaces" role archives the target
            if conn.role == "replaces":
                await db_pool.execute(
                    "UPDATE context_patches SET status = 'archived', updated_at = NOW(), "
                    "value = jsonb_set(value, '{archive_cause}', '\"replaced\"') "
                    "WHERE patch_id = $1",
                    conn.target_patch_id
                )

            created_connections.append({
                "to": conn.target_patch_id, "role": conn.role, "label": conn.label
            })

    # ONE user-stated title at a time. A user correcting a title twice
    # should not leave two live roles racing on created_at, and
    # `stated_roles.items` accumulating every attempt turns the receipt
    # into a pile. CQ owns this rather than the client, because the
    # client's version is a read-modify-write over three hops whose
    # partial failure leaves two live titles or none.
    #
    # ONLY a prior DECLARED role is archived, never an extracted one:
    # a role a meeting recorded is an observation, and a user stating
    # their title today is not grounds to delete what was observed last
    # month. The predicate is in the SQL and it is the load-bearing part.
    superseded: list = []
    person_patch_id = describes_target(created_connections)
    if person_patch_id:
        try:
            vocab = await _people_vocab_cached(app_id)
            if vocab.stated_role_type and patch.type == vocab.stated_role_type:
                rows = await db_pool.fetch(
                    SUPERSEDE_PRIOR_STATED_ROLE_SQL,
                    patch_id, subject_key, patch.type, person_patch_id,
                )
                superseded = [str(r["patch_id"]) for r in rows]
                if superseded:
                    logger.info("stated_role_superseded", user_id=user_id,
                                patch_id=patch_id, person_patch_id=person_patch_id,
                                superseded=superseded)
        except Exception as exc:
            # The new title is already written and is the newest, so it
            # wins on created_at regardless. A failure here leaves an
            # extra row in `items`, which is untidy and not wrong.
            logger.warning("stated_role_supersede_failed",
                           user_id=user_id, patch_id=patch_id,
                           error=str(exc)[:160])

    # Trigger cache refresh
    stream_key = "memory_updates"
    payload = {"type": "hydrate", "user_id": user_id, "timestamp": now.isoformat()}
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})

    # Every warning this route can raise, collected in one place. A list
    # rather than a single slot because a caller can get both at once (a
    # bad date AND an unresolvable project), and a client that reads only
    # the first would act on half of what went wrong.
    return await _created_patch_response(
        user_id, app_id, patch_id, patch.type,
        created=True, connections=created_connections,
        warnings=[w for w in (deadline_warning, project_warning) if w],
        superseded=superseded,
    )


# ============================================
# Patch Connections CRUD
# ============================================

class ConnectionCreate(BaseModel):
    # Same forbid contract as PatchConnectionInput (the GP promise): an
    # unknown key here is a misspelled or renamed field, and dropping it
    # silently writes an edge missing the thing the caller said.
    model_config = ConfigDict(extra="forbid")

    from_patch_id: str
    to_patch_id: str
    role: str  # parent, depends_on, resolves, replaces, informs
    label: Optional[str] = None  # belongs_to, blocked_by, owns, works_on, etc.
    context: Optional[str] = None

@app.post("/v1/quilt/{user_id}/connections", tags=["Quilt"])
async def create_connection(
    user_id: str,
    conn: ConnectionCreate,
    app_id: str = Depends(verify_application_access),
):
    """Create a connection between two patches."""
    subject_key = f"user:{user_id}"

    # Verify both patches belong to this user
    for pid in (conn.from_patch_id, conn.to_patch_id):
        row = await db_pool.fetchrow(
            "SELECT cp.patch_id FROM context_patches cp JOIN patch_subjects ps ON cp.patch_id = ps.patch_id WHERE cp.patch_id = $1 AND ps.subject_key = $2",
            pid, subject_key
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"Patch {pid} not found for this user")

    await db_pool.execute(
        """
        INSERT INTO patch_connections (from_patch_id, to_patch_id, connection_role, connection_label, context)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (from_patch_id, to_patch_id, connection_role) DO UPDATE SET
            connection_label = EXCLUDED.connection_label,
            context = EXCLUDED.context,
            status = 'active'
        """,
        conn.from_patch_id, conn.to_patch_id, conn.role, conn.label, conn.context
    )

    # Lifecycle: "replaces" role archives the target
    if conn.role == "replaces":
        await db_pool.execute(
            "UPDATE context_patches SET status = 'archived', updated_at = NOW(), "
            "value = jsonb_set(value, '{archive_cause}', '\"replaced\"') "
            "WHERE patch_id = $1",
            conn.to_patch_id
        )

    return {"status": "created", "from": conn.from_patch_id, "to": conn.to_patch_id, "role": conn.role}


@app.delete("/v1/quilt/{user_id}/connections", tags=["Quilt"])
async def delete_connection(
    user_id: str,
    from_patch_id: str = Query(...),
    to_patch_id: str = Query(...),
    role: str = Query(...),
    app_id: str = Depends(verify_application_access),
):
    """Delete a connection between two patches."""
    result = await db_pool.execute(
        "UPDATE patch_connections SET status = 'archived' "
        "WHERE from_patch_id = $1 AND to_patch_id = $2 AND connection_role = $3 "
        "AND COALESCE(status, 'active') = 'active'",
        from_patch_id, to_patch_id, role
    )
    deleted = int(result.split()[-1]) if result else 0

    if deleted == 0:
        raise HTTPException(status_code=404, detail="Connection not found")

    return {"status": "deleted", "from": from_patch_id, "to": to_patch_id, "role": role}


# ============================================
# Speaker / Entity Rename
# ============================================

class SpeakerRename(BaseModel):
    old_name: str
    new_name: str

@app.post("/v1/quilt/{user_id}/rename-speaker", tags=["Quilt"])
async def rename_speaker(
    user_id: str,
    rename: SpeakerRename,
    app_id: str = Depends(verify_application_access),
):
    """
    Rename a speaker across entities, relationships, and the Redis entity index.
    Called by ShoulderSurf when a user renames "Speaker 4" to "Ramkumar".

    The app is responsible for updating patch text separately via PATCH /v1/quilt.
    This endpoint handles the graph layer (entities + relationships).

    PRESENCE, AND WHERE IT CANNOT FOLLOW. `person_appearances` is keyed
    on entity_id, so the two branches below behave differently and only
    one of them can be made honest:

    * Old name IS an entity: the rename is IN PLACE. entity_id never
      changes, so every appearance the person already had still points at
      them and their presence anchor is untouched. Nothing to do, and
      nothing here may start deleting and recreating that row
      (test_reassign_speaker_presence pins it).
    * Old name was an unnamed placeholder: a NEW entity is created, and
      the request carries no meeting id anywhere, so there is no meeting
      to attach an appearance to. CQ will not guess at one by matching
      patch text or owner strings, which is inference dressed as a
      record. The new entity therefore starts with NO appearances and
      serves a null `last_present_at` until an extraction observes them.
      This is the ONE remaining ambiguous null in the People surface;
      docs/architecture/16-people.md 6.2a names it and says what the
      request would have to carry to close it.

    Reassigning a meeting's utterances (POST /v1/quilt/{u}/reassign-speaker)
    DOES record presence, because every from_label carries its meeting.
    """
    old = rename.old_name
    new = rename.new_name

    # Update entity name if it exists, or create it if the old name was an unnamed speaker
    existing = await db_pool.fetchrow(
        "SELECT entity_id FROM entities WHERE user_id = $1 AND name = $2", user_id, old
    )
    if existing:
        await db_pool.execute(
            "UPDATE entities SET name = $1, last_seen_at = NOW() WHERE user_id = $2 AND name = $3",
            new, user_id, old
        )
    else:
        # Old name was never an entity (unnamed speaker) — create the entity with the real name
        await db_pool.execute(
            """INSERT INTO entities (user_id, name, entity_type, description)
            VALUES ($1, $2, 'person', $3)
            ON CONFLICT (user_id, name, entity_type) DO UPDATE SET last_seen_at = NOW()""",
            user_id, new, f"Identified from speaker rename (was {old})"
        )

    # Update relationship context that mentions the old name
    await db_pool.execute(
        "UPDATE relationships SET context = REPLACE(context, $1, $2) WHERE user_id = $3 AND context LIKE '%' || $1 || '%'",
        old, new, user_id
    )

    # Rebuild Redis entity index so recall matches the new name
    entity_index_key = f"entity_index:{user_id}"
    all_names = await db_pool.fetch(ENTITY_INDEX_NAMES_SQL, user_id)
    if all_names:
        await redis_client.delete(entity_index_key)
        await redis_client.sadd(entity_index_key, *[r["name"] for r in all_names])
        await redis_client.expire(entity_index_key, 7200)

    return {"status": "renamed", "old_name": old, "new_name": new}


# ============================================
# Speaker Reassign — bulk fix attribution after diarization gets it wrong
# ============================================

class FromLabel(BaseModel):
    label: str
    meeting_id: str

class ReassignSpeakerRequest(BaseModel):
    from_labels: List[FromLabel]
    to_person_id: Optional[str] = None
    to_self: Optional[bool] = None
    # The third target, additive: name a speaker CQ has no id for. The
    # server resolves it (bind to the matching person, create them if
    # there is none), because name-to-identity resolution is CQ's job and
    # a client re-implementing it is the duplication doc 16 argues
    # against. `to_person_id` stays the precise form and nothing about it
    # changes; this is what makes ONE meeting-scoped verb cover both
    # naming an unknown speaker and correcting a misattribution.
    to_name: Optional[str] = None
    # Set after the caller has SEEN the candidates and chosen "someone
    # new". Without it a contested name is refused, which is the point;
    # with it the caller is asserting this is a different person and CQ
    # records what they say. It never hard-requires a distinguishing
    # surname: refusing to record a real colleague because we wanted a
    # tidier graph is the wrong trade, and sometimes you genuinely only
    # know "Mike".
    create_new: Optional[bool] = None


def _reassign_error(code: str, message: str, **extra) -> HTTPException:
    detail = {"code": code, "message": message}
    detail.update(extra)
    return HTTPException(status_code=422, detail=detail)


async def _meeting_presence_anchors(
    conn, user_id: str, subject_key: str, origin_ids: List[str],
) -> dict:
    """origin_id -> (timestamp, project_id) for dating a presence row.

    person_appearances runs on the INGEST clock, so a presence recorded
    after the fact has to be dated by the meeting, never by NOW(). The
    meeting's other appearance rows are the best anchor (they were
    stamped when it was ingested, and first_seen_at is the half nothing
    re-bumps); its own patches are the fallback for a meeting with no
    appearances at all. A meeting with neither has no honest date, and
    the caller skips it rather than inventing one.

    Same rule `backfill_person_appearances.py` states in its header:
    importing at wall-clock time would tell the app every meeting
    happened today.
    """
    rows = await conn.fetch(
        """
        WITH sib AS (
            SELECT origin_id,
                   MIN(first_seen_at) AS anchor,
                   -- A meeting carries one project in practice; MIN keeps
                   -- the pick deterministic if it ever carries two.
                   MIN(project_id) AS project_id
            FROM person_appearances
            WHERE user_id = $1 AND origin_id = ANY($2::text[])
            GROUP BY origin_id
        ), pat AS (
            SELECT cp.origin_id,
                   MIN(cp.created_at) AS anchor,
                   MIN(cp.project_id) AS project_id
            FROM context_patches cp
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            WHERE ps.subject_key = $3
              AND cp.origin_type = 'meeting'
              AND cp.origin_id = ANY($2::text[])
            GROUP BY cp.origin_id
        )
        SELECT m.origin_id,
               COALESCE(sib.anchor, pat.anchor) AS anchor,
               COALESCE(sib.project_id, pat.project_id) AS project_id
        FROM unnest($2::text[]) AS m(origin_id)
        LEFT JOIN sib ON sib.origin_id = m.origin_id
        LEFT JOIN pat ON pat.origin_id = m.origin_id
        """,
        user_id, list(origin_ids), subject_key,
    )
    return {r["origin_id"]: (r["anchor"], r["project_id"]) for r in rows}


async def _upsert_speaker_appearance(
    conn, user_id: str, entity_id, origin_id: str, anchor, project_id,
    turn_count,
) -> None:
    """Record that this person SPOKE in this meeting.

    The merge route's upsert discipline, shared by both write paths so
    they cannot drift: earliest first_seen wins, latest last_seen wins,
    capacities UNION (a person recorded by ownership who also spoke ends
    up carrying both), turn_count MAX with NULL never clobbering a known
    value.
    """
    await conn.execute(
        """
        INSERT INTO person_appearances
            (user_id, entity_id, origin_id, origin_type,
             project_id, first_seen_at, last_seen_at,
             capacities, turn_count)
        VALUES ($1, $2::uuid, $3, 'meeting', $4, $5, $5,
                ARRAY['speaker'], $6)
        ON CONFLICT (user_id, entity_id, origin_id) DO UPDATE SET
            first_seen_at = LEAST(person_appearances.first_seen_at,
                                  EXCLUDED.first_seen_at),
            last_seen_at  = GREATEST(person_appearances.last_seen_at,
                                     EXCLUDED.last_seen_at),
            project_id    = COALESCE(person_appearances.project_id,
                                     EXCLUDED.project_id),
            turn_count    = CASE
                WHEN EXCLUDED.turn_count IS NULL THEN person_appearances.turn_count
                ELSE GREATEST(COALESCE(person_appearances.turn_count, 0),
                              EXCLUDED.turn_count)
            END,
            capacities    = ARRAY(SELECT DISTINCT unnest(
                                person_appearances.capacities
                                || EXCLUDED.capacities))
        """,
        user_id, str(entity_id), origin_id, project_id, anchor, turn_count,
    )


@app.post("/v1/quilt/{user_id}/reassign-speaker", tags=["Quilt"])
async def reassign_speaker(
    user_id: str,
    req: ReassignSpeakerRequest,
    app_id: str = Depends(verify_application_access),
):
    """
    Bulk-reassign patches/entities from one or more diarization labels to a
    target person (or to the submitting user). Covers the case where a single
    user fragments across N diarized labels in one meeting and the user wants
    to merge them into a single attribution.

    Three targets, exactly one per request: `to_person_id` (a person CQ
    already has), `to_name` (a speaker the user is naming, resolved or
    created server side), `to_self`. Every from_label carries its own
    meeting, so all three are MEETING SCOPED, which is what a post-save
    speaker rename actually is.

    Authorization: GP's proxy enforces user.id == path user_id before
    forwarding. CQ trusts the (app, user_id) pair — same pattern as
    rename-speaker.

    PRESENCE FOLLOWS THE REASSIGNMENT. Utterances moving to a person is
    direct evidence that person SPOKE in that meeting, so a
    `speaker`-grade appearance is upserted for each meeting where
    anything actually moved (see docs/architecture/16-people.md 6.2a).
    Without it CQ served a null `last_present_at` for someone the user
    had just told it was in the room, which the client could not tell
    apart from "not present".

    NOT here, deliberately: the patch TEXT rewrite. The app still owns
    that (PATCH /v1/quilt), as the rename-speaker docstring has always
    said. Moving it server side is a real improvement and a separate
    decision, tracked in doc 16 6.2a rather than bundled in.

    See contract: docs design pinned 2026-04-26.
    """
    # ------------------------------------------------------------
    # 1. exactly-one target + non-empty validation
    # ------------------------------------------------------------
    targets_given = sum(
        1 for t in (req.to_self, req.to_person_id, (req.to_name or "").strip()) if t
    )
    if targets_given != 1:
        raise _reassign_error(
            "INVALID_TARGET",
            "Provide exactly one of to_self=true, to_person_id or to_name",
        )
    if not req.from_labels:
        raise _reassign_error("EMPTY_FROM_LABELS", "from_labels must not be empty")
    for fl in req.from_labels:
        if not fl.label or not fl.meeting_id:
            raise _reassign_error(
                "INVALID_LABEL_FORMAT",
                "Every from_labels entry must have non-empty label and meeting_id",
            )

    # ------------------------------------------------------------
    # 2. Validate all referenced meetings are known to this user's
    #    quilt. There is no separate `meetings` table — the meeting
    #    UUID lives on context_patches as `origin_id` (with
    #    `origin_type='meeting'`). A meeting is considered "known"
    #    if at least one patch exists for it under this user's
    #    subject_key. All-or-nothing: no partial writes if any
    #    meeting_id is unknown.
    #
    #    The Pydantic field stays named `meeting_id` because that's
    #    the SS-facing public name — we just translate it to the
    #    canonical `origin_id` column at the SQL boundary.
    # ------------------------------------------------------------
    subject_key = f"user:{user_id}"
    meeting_ids = list({fl.meeting_id for fl in req.from_labels})
    found_rows = await db_pool.fetch(
        """
        SELECT DISTINCT cp.origin_id
        FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
        WHERE cp.origin_id = ANY($1::text[])
          AND cp.origin_type = 'meeting'
          AND ps.subject_key = $2
        """,
        meeting_ids, subject_key,
    )
    found = {r["origin_id"] for r in found_rows}
    missing_meetings = sorted(set(meeting_ids) - found)
    if missing_meetings:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "MEETING_NOT_FOUND",
                "missing_meeting_ids": missing_meetings,
            },
        )

    # ------------------------------------------------------------
    # 3. Resolve target.
    #    to_person_id  -> look up entity name; 404 if not found.
    #    to_name       -> validated here, resolved-or-created INSIDE the
    #                     transaction below, so a failed reassignment
    #                     never strands a half-made person.
    #    to_self       -> clear_owner; the owner field is removed from
    #                     value (since (you) speaker attribution is
    #                     implicit via subject_key=user:{user_id}).
    # ------------------------------------------------------------
    clear_owner = bool(req.to_self)
    requested_name: Optional[str] = None
    if req.to_name and not req.to_person_id and not req.to_self:
        try:
            # Same gate as POST /v1/people: a placeholder is refused here
            # too, or naming a speaker becomes the hole straight through
            # drop_placeholder_entities that endpoint refuses to be.
            requested_name = validate_person_name(req.to_name)
        except IdentityRequestError as e:
            raise _identity_error(e)

    if req.to_person_id:
        target_row = await db_pool.fetchrow(
            "SELECT name, suppressed_at, "
            # Presence lands on the CANONICAL row when the caller holds a
            # pre-merge id: the People list reads merged_into IS NULL, so
            # an appearance written to a folded row would be presence
            # nobody can see, which is the null anchor this fix exists to
            # remove. The owner STRING stays the id's own name, unchanged
            # from before, and the ledger still matches it because a
            # merge leaves the folded name behind as an alias.
            "       COALESCE(merged_into, entity_id) AS presence_entity_id "
            "FROM entities WHERE entity_id = $1::uuid AND user_id = $2",
            req.to_person_id, user_id,
        )
        if not target_row:
            raise HTTPException(
                status_code=404,
                detail={"code": "PERSON_NOT_FOUND", "person_id": req.to_person_id},
            )
        target_owner: Optional[str] = target_row["name"]
        # A suppressed entity ("not a person") accumulates no meeting
        # history, SS's condition, enforced identically on the ingest
        # path (worker._record_appearance). The patches still move; only
        # the presence write is withheld.
        person_presence_id = (
            None if target_row["suppressed_at"] is not None
            else target_row["presence_entity_id"]
        )
        self_entity_id = None
    elif requested_name is not None:
        # Filled in inside the transaction.
        target_owner = None
        person_presence_id = None
        self_entity_id = None
    else:
        target_owner = None  # to_self: clear the owner field
        # to_self presence lands on the ego entity and nowhere else. No
        # ego link stamped means no honest target, so nothing is written
        # and the response says so; this route never mints an ego stamp
        # (migration 35 is keep-first for a reason).
        person_presence_id = None
        self_entity_id = await db_pool.fetchval(
            "SELECT entity_id FROM entities "
            "WHERE user_id = $1 AND self_at IS NOT NULL AND suppressed_at IS NULL",
            user_id,
        )

    # ------------------------------------------------------------
    # 4. All-or-nothing transaction.
    # ------------------------------------------------------------
    patches_updated = 0
    labels_skipped = 0
    entities_merged = 0
    appearances_recorded = 0
    resolved_person: Optional[dict] = None
    # One entry per (label, meeting) processed, for the presence rule.
    outcomes: List[dict] = []

    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        async with conn.transaction():
            # to_name: resolve or create the person the user just named,
            # through the SAME path POST /v1/people uses, so a name typed
            # onto a speaker and the same name typed into the "+" sheet
            # cannot produce two different people.
            if requested_name is not None:
                # The meetings being relabelled give the picker its
                # strongest ordering signal: a Mike already in THIS
                # project is the likelier answer. Ranking only, never
                # resolution, which is the line Scott drew.
                scope_projects = [
                    r["project_id"] for r in await conn.fetch(
                        """
                        SELECT DISTINCT project_id FROM context_patches
                        WHERE origin_id = ANY($1::text[])
                          AND project_id IS NOT NULL
                        """,
                        meeting_ids,
                    )
                ]
                person = await _resolve_or_create_person(
                    conn, user_id, app_id, requested_name, "",
                    resolve_identity_source("speaker_reassign"), vocab,
                    datetime.utcnow(), "speaker_reassign",
                    create_new=bool(req.create_new),
                    scope_project_ids=scope_projects,
                )
                target_owner = person["name"]
                person_presence_id = person["entity_id"]
                resolved_person = {
                    "entity_id": person["entity_id"],
                    "name": person["name"],
                    "patch_id": person["patch_id"],
                    "status": "created" if person["created"] else "exists",
                    # Every person the picker offered and the user
                    # declined; SS keys its local merge veto off
                    # (entity_id, each). Empty unless create_new escaped
                    # a bare exact hit.
                    "separated_from": person["separated_from"],
                }

            presence_entity_id = reassignment_presence_target(
                person_presence_id, clear_owner, self_entity_id,
            )

            for fl in req.from_labels:
                if clear_owner:
                    update_sql = """
                        UPDATE context_patches
                        SET value = value - 'owner', updated_at = NOW()
                        WHERE origin_id = $1
                          AND origin_type = 'meeting'
                          AND value->>'owner' = $2
                          AND patch_id IN (
                              SELECT patch_id FROM patch_subjects WHERE subject_key = $3
                          )
                          AND COALESCE(status, 'active') = 'active'
                    """
                    result = await conn.execute(update_sql, fl.meeting_id, fl.label, subject_key)
                else:
                    update_sql = """
                        UPDATE context_patches
                        SET value = jsonb_set(value, '{owner}', to_jsonb($1::text)),
                            updated_at = NOW()
                        WHERE origin_id = $2
                          AND origin_type = 'meeting'
                          AND value->>'owner' = $3
                          AND patch_id IN (
                              SELECT patch_id FROM patch_subjects WHERE subject_key = $4
                          )
                          AND COALESCE(status, 'active') = 'active'
                    """
                    result = await conn.execute(
                        update_sql, target_owner, fl.meeting_id, fl.label, subject_key,
                    )

                # asyncpg returns command tags like "UPDATE 5" — count is the tail.
                count = int(result.rsplit(" ", 1)[-1])
                if count == 0:
                    labels_skipped += 1
                else:
                    patches_updated += count
                outcomes.append({
                    "origin_id": fl.meeting_id,
                    "patches_moved": count,
                    # Deliberately no turn count. This form moves PATCHES,
                    # and patch ownership is not a partition of a
                    # meeting's turns, so carrying the label's count over
                    # would be inference. It also leaves the source row
                    # standing (see below), and two rows each claiming the
                    # same 41 turns would credit one person's speech to
                    # two people. NULL is unknown, which is true.
                    # /speaker-map is the form that states the whole
                    # meeting and can therefore move measurements.
                    "turn_count": None,
                })

            # --------------------------------------------------------
            # 4b. Presence follows the utterances. Whoever these lines
            # belong to was demonstrably SPEAKING in that meeting, so the
            # appearance is created if absent and merged into if present,
            # on the merge route's upsert discipline (earliest first_seen,
            # latest last_seen, capacities union, MAX turn count and NULL
            # never clobbering a known one).
            #
            # The SOURCE label's appearance is deliberately LEFT ALONE.
            # A reassignment says whose voice this was, not that nobody
            # was there: the row records a speaker CQ observed in the
            # transcript, and removing it would assert an absence from
            # the same evidence that proved a presence, against
            # migration 31's own rule. It also holds turn and question
            # counts that can never be reconstructed, and there is no
            # undo. The one path that still drops a source appearance is
            # step 5's cleanup, and only for a label entity with no graph
            # anchor at all, where the FK cascade takes it.
            #
            # The timestamps are the MEETING's ingest anchor, never
            # NOW(). person_appearances runs on the ingest clock (the
            # sibling rows for this meeting, or the meeting's own patches
            # when it has no sibling), and stamping NOW() here would tell
            # the People list the user met this person today because they
            # fixed a label today. That is the exact "Last met 6 hours
            # ago" defect this work came from.
            # --------------------------------------------------------
            writes = reassignment_presence(outcomes)
            if presence_entity_id is not None and writes:
                anchors = await _meeting_presence_anchors(
                    conn, user_id, subject_key,
                    [w["origin_id"] for w in writes],
                )
                for w in writes:
                    anchor, project_id = anchors.get(w["origin_id"], (None, None))
                    if anchor is None:
                        # No clock to date this presence by. Skipping is
                        # the honest move: a row dated NOW() would read as
                        # a meeting that happened today.
                        continue
                    await _upsert_speaker_appearance(
                        conn, user_id, presence_entity_id, w["origin_id"],
                        anchor, project_id, w["turn_count"],
                    )
                    appearances_recorded += 1

            # --------------------------------------------------------
            # 5. Entity cleanup. For to_person_id, drop entities whose
            # name matches one of the from_labels AND have no remaining
            # graph relationships. Conservative — only deletes truly
            # orphaned label-derived entities (e.g., "Speaker 3"
            # entities that were created from misattributed speech and
            # have no other anchors).
            #
            # For to_self, skip entity cleanup: the ego link lives on a
            # person entity the user shares with everyone else in the
            # graph, and a from_label is never that row.
            #
            # The target is excluded outright: a label that happens to
            # carry the target's own name would otherwise delete the
            # person just reassigned to, and cascade away the appearance
            # written above with it.
            # --------------------------------------------------------
            # AND NEVER AN ENTITY THAT WAS PRESENT SOMEWHERE THIS REQUEST
            # DID NOT NAME. 2026-09-02: "Kartik" was renamed to "Kartik
            # Patnaik" in ONE meeting. The bare-name entity had no
            # relationship edge, so this cleanup deleted it, and
            # migration 30's ON DELETE CASCADE took its four appearance
            # rows, three of them in meetings the request never
            # mentioned, turn and question counts included. Seven active
            # patches in those meetings still say owner "Kartik" and now
            # point at nobody. "No graph anchor" cannot tell a real
            # person recorded under a first name from a stale label;
            # presence in a meeting outside the request can, and it is
            # exact. Scott's ruling the same day: suggest it is the same
            # person, never assume. Such an entity is LEFT on the roster
            # for the client's duplicate matcher to propose.
            if not clear_owner:
                from_label_names = list({fl.label for fl in req.from_labels})
                merge_result = await conn.execute(
                    """
                    DELETE FROM entities
                    WHERE user_id = $1
                      AND name = ANY($2::text[])
                      AND ($3::uuid IS NULL OR entity_id <> $3::uuid)
                      AND entity_id NOT IN (
                          SELECT from_entity_id FROM relationships
                          WHERE user_id = $1 AND from_entity_id IS NOT NULL
                          UNION
                          SELECT to_entity_id FROM relationships
                          WHERE user_id = $1 AND to_entity_id IS NOT NULL
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM person_appearances pa
                          WHERE pa.entity_id = entities.entity_id
                            AND pa.origin_id <> ALL($4::text[])
                      )
                    """,
                    user_id, from_label_names,
                    str(presence_entity_id) if presence_entity_id else None,
                    [str(m) for m in meeting_ids],
                )
                entities_merged = int(merge_result.rsplit(" ", 1)[-1])

    # ------------------------------------------------------------
    # 6. Rebuild Redis entity index when we removed any entities, so
    #    recall stops matching on the dropped names.
    # ------------------------------------------------------------
    # A to_name that created someone adds a name recall has to match, so
    # the index is rebuilt for that too.
    if entities_merged > 0 or (resolved_person or {}).get("status") == "created":
        entity_index_key = f"entity_index:{user_id}"
        all_names = await db_pool.fetch(ENTITY_INDEX_NAMES_SQL, user_id)
        await redis_client.delete(entity_index_key)
        if all_names:
            await redis_client.sadd(entity_index_key, *[r["name"] for r in all_names])
            await redis_client.expire(entity_index_key, 7200)

    return {
        "patches_updated": patches_updated,
        "connections_updated": 0,  # v1: patch attribution changes; connection structure unchanged
        "entities_merged": entities_merged,
        # The honest name for the same number: nothing was joined, these
        # label-named entities were DELETED. `entities_merged` keeps
        # serving until clients move (doc 16 section 5.13).
        "entities_deleted": entities_merged,
        "labels_skipped": labels_skipped,
        # How many (person, meeting) presence rows this call wrote or
        # merged into. A count of what happened, with no cause attached:
        # zero can mean nothing moved, or that a to_self call found no ego
        # link to hang presence on, and `presence_entity_id` is the field
        # that separates those without CQ narrating either.
        "appearances_recorded": appearances_recorded,
        "presence_entity_id": str(presence_entity_id) if presence_entity_id else None,
        # Who `to_name` resolved to, so the client can bind the id it did
        # not have. Null on the other two lanes, which already know their
        # target.
        "resolved_person": resolved_person,
    }


# ============================================
# Speaker map: the meeting's speaker set as it now stands
# ============================================

class SpeakerMapEntry(BaseModel):
    label: str
    to_person_id: Optional[str] = None
    to_name: Optional[str] = None
    to_self: Optional[bool] = None
    # "Nobody". Explicit rather than inferred from three absent siblings,
    # because this is the field that REMOVES a presence: a client that
    # forgot to fill in a target must get a 422, never a deletion.
    to_nobody: Optional[bool] = None


class SpeakerMapRequest(BaseModel):
    meeting_id: str
    labels: List[SpeakerMapEntry]
    # Must be literally true. Removal works by ABSENCE from the resulting
    # speaker set, which is only sound if the caller really did send every
    # label in the meeting, and CQ cannot verify that from the outside.
    # Requiring the assertion at the call site means a half-wired lane
    # fails loudly instead of quietly deleting presence it was never told
    # about.
    labels_are_complete: bool = False


@app.post("/v1/quilt/{user_id}/speaker-map", tags=["Quilt"])
async def set_speaker_map(
    user_id: str,
    req: SpeakerMapRequest,
    app_id: str = Depends(verify_application_access),
):
    """Declare who spoke in a meeting, as it now stands, and sync presence.

    THE STATE, NOT THE OPERATION. "What is the inverse of a
    reassignment" has no answer: an undo can mean "I mislabelled that, it
    was never him", which makes the appearance false, or "I want the raw
    labels back on screen", which makes it true and reverting it
    destructive. No client and no server can tell those apart from an
    undo signal, so nothing here is keyed on an operation. The caller
    sends the resulting mapping and CQ diffs it against what it holds.

    That dissolves the whole family: an undo is the post-undo mapping, a
    block-scoped segment edit is the post-edit mapping (so segment ranges
    are never modelled), a consolidation is a mapping with one fewer
    speaker. And it is IDEMPOTENT: sending the same mapping twice writes
    nothing the second time, which is what makes it safe on relabel lanes
    nobody has found yet, since an unwired lane then fails by omission
    rather than by writing something false.

    Presence only, deliberately. This route never rewrites `value.owner`
    and never touches patch text. Speaking and owning are different
    claims (work gets assigned in absentia), so a mapping of who spoke
    must not silently re-own anybody's commitments. Use
    POST /v1/quilt/{u}/reassign-speaker for attribution, this for
    presence; they compose.

    GP SEQUENCING: this is a NEW route, so the gateway carries it before
    CQ can call it live. GP's edge declares exact paths, no prefixes and
    no wildcards, and CQ's own socket cannot see a route-table miss.
    """
    # ------------------------------------------------------------
    # 1. Validation. Every refusal names the label it refused.
    # ------------------------------------------------------------
    if not req.labels:
        raise _reassign_error("EMPTY_LABELS", "labels must not be empty")
    if not req.labels_are_complete:
        raise _reassign_error(
            "INCOMPLETE_MAPPING",
            "labels_are_complete must be true: removal works by absence "
            "from the mapping, so a partial mapping would delete presence "
            "it never described",
        )
    seen_labels = set()
    for e in req.labels:
        key = (e.label or "").strip().lower()
        if not key:
            raise _reassign_error("INVALID_LABEL_FORMAT", "label must not be empty")
        if key in seen_labels:
            raise _reassign_error(
                "DUPLICATE_LABEL", "each label may appear once", label=e.label,
            )
        seen_labels.add(key)
        given = sum(1 for t in (
            e.to_person_id, (e.to_name or "").strip(), e.to_self, e.to_nobody,
        ) if t)
        if given != 1:
            raise _reassign_error(
                "INVALID_LABEL_TARGET",
                "each label needs exactly one of to_person_id, to_name, "
                "to_self or to_nobody",
                label=e.label,
            )
        if e.to_name:
            try:
                validate_person_name(e.to_name)
            except IdentityRequestError as exc:
                raise _identity_error(exc)

    subject_key = f"user:{user_id}"
    known = await db_pool.fetchval(
        """
        SELECT EXISTS(
            SELECT 1 FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE cp.origin_id = $1 AND cp.origin_type = 'meeting'
              AND ps.subject_key = $2
        )
        """,
        req.meeting_id, subject_key,
    )
    if not known:
        raise HTTPException(
            status_code=404,
            detail={"code": "MEETING_NOT_FOUND",
                    "missing_meeting_ids": [req.meeting_id]},
        )

    resolved: List[dict] = []
    created_any = False
    appearances_recorded = 0
    capacities_reduced = 0
    appearances_removed = 0

    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        async with conn.transaction():
            # ----------------------------------------------------
            # 2. Resolve every label to an entity, or to nobody, or
            #    to nothing at all.
            # ----------------------------------------------------
            for e in req.labels:
                entry = {
                    "label": e.label, "entity_id": None, "name": None,
                    "patch_id": None, "status": None,
                }
                if e.to_person_id:
                    row = await conn.fetchrow(
                        "SELECT name, suppressed_at, "
                        "       COALESCE(merged_into, entity_id) AS presence_entity_id "
                        "FROM entities WHERE entity_id = $1::uuid AND user_id = $2",
                        e.to_person_id, user_id,
                    )
                    if not row:
                        raise HTTPException(
                            status_code=404,
                            detail={"code": "PERSON_NOT_FOUND",
                                    "person_id": e.to_person_id,
                                    "label": e.label},
                        )
                    if row["suppressed_at"] is not None:
                        # "Not a person" keeps accumulating no history.
                        # Unresolved rather than an error, and it costs
                        # the removal half (see below).
                        entry["status"] = "unresolved"
                    else:
                        entry.update(
                            entity_id=str(row["presence_entity_id"]),
                            name=row["name"], status="exists",
                        )
                elif e.to_name:
                    person = await _resolve_or_create_person(
                        conn, user_id, app_id, validate_person_name(e.to_name),
                        "", resolve_identity_source("speaker_map"), vocab,
                        datetime.utcnow(), "speaker_map",
                    )
                    created_any = created_any or person["created"]
                    entry.update(
                        entity_id=person["entity_id"], name=person["name"],
                        patch_id=person["patch_id"],
                        status="created" if person["created"] else "exists",
                    )
                elif e.to_self:
                    ego = await conn.fetchval(
                        "SELECT entity_id FROM entities WHERE user_id = $1 "
                        "AND self_at IS NOT NULL AND suppressed_at IS NULL",
                        user_id,
                    )
                    # No ego link means the user's own presence has no
                    # honest home, and this route does not mint one.
                    entry["status"] = "exists" if ego else "unresolved"
                    entry["entity_id"] = str(ego) if ego else None
                else:
                    entry["status"] = "nobody"
                resolved.append(entry)

            targets = {r["entity_id"] for r in resolved if r["entity_id"]}
            # Removal by absence is only sound against a COMPLETE target
            # set. One label CQ could not resolve and absence stops
            # meaning "did not speak", so that call adds and removes
            # nothing, and says which labels cost it.
            unresolved = [r["label"] for r in resolved if r["status"] == "unresolved"]

            rows = await conn.fetch(
                "SELECT entity_id, capacities FROM person_appearances "
                "WHERE user_id = $1 AND origin_id = $2",
                user_id, req.meeting_id,
            )
            plan = plan_speaker_map(
                [{"entity_id": str(r["entity_id"]), "capacities": r["capacities"]}
                 for r in rows],
                targets,
                allow_removal=not unresolved,
            )

            # ----------------------------------------------------
            # 3. Apply. Additions are dated by the meeting, never by
            #    NOW(), for the same reason the reassign path is.
            # ----------------------------------------------------
            if plan["add"]:
                anchor, project_id = (await _meeting_presence_anchors(
                    conn, user_id, subject_key, [req.meeting_id],
                )).get(req.meeting_id, (None, None))
                if anchor is not None:
                    for eid in plan["add"]:
                        await _upsert_speaker_appearance(
                            conn, user_id, eid, req.meeting_id, anchor,
                            project_id,
                            # No turn count to give: the transcript is
                            # gone and the mapping carries labels, not
                            # counts. NULL is unknown, never zero turns.
                            None,
                        )
                        appearances_recorded += 1

            if plan["strip"]:
                # The row survives on another capacity, so the person was
                # still in that meeting. What goes with the label is every
                # per-speaker MEASUREMENT: a turn count and a question
                # count are claims about what this person SAID, and the
                # mapping just said those words were somebody else's. They
                # are not recoverable, which is the price of a corrected
                # attribution; an honest unknown beats a confident
                # misattribution.
                stripped = await conn.execute(
                    f"""
                    UPDATE person_appearances
                    SET capacities = array_remove(capacities, 'speaker'),
                        {", ".join(f"{c} = NULL" for c in SPEAKER_METRICS)}
                    WHERE user_id = $1 AND origin_id = $2
                      AND entity_id = ANY($3::uuid[])
                    """,
                    user_id, req.meeting_id, plan["strip"],
                )
                capacities_reduced = int(stripped.rsplit(" ", 1)[-1])

            if plan["remove"]:
                # `speaker` was the only capacity: nothing else ever
                # claimed this person was in the room, so the row goes
                # rather than surviving with an empty capacity set, which
                # would keep reading as presence (empty means
                # pre-migration-31 unknown, and unknown counts as there).
                dropped = await conn.execute(
                    "DELETE FROM person_appearances WHERE user_id = $1 "
                    "AND origin_id = $2 AND entity_id = ANY($3::uuid[])",
                    user_id, req.meeting_id, plan["remove"],
                )
                appearances_removed = int(dropped.rsplit(" ", 1)[-1])

    if created_any:
        await _rebuild_entity_index(user_id)

    return {
        "meeting_id": req.meeting_id,
        "labels_received": len(req.labels),
        "appearances_recorded": appearances_recorded,
        "capacities_reduced": capacities_reduced,
        "appearances_removed": appearances_removed,
        # Echoed back so the caller can see what CQ acted on, the same
        # RECEIVED-plus-ignored shape the People list uses for its query
        # echo. Non-empty means the removal half was skipped entirely.
        "unresolved_labels": unresolved,
        # Every entry carries every sibling key, so a client decodes one
        # type: label, entity_id, name, patch_id, status. status is
        # exists | created | nobody | unresolved.
        "labels": resolved,
    }


# ============================================
# People — identity write-back
#
# The user is the authority on who is who. These four endpoints are where
# that authority lands: merge two entities the user says are one human,
# record the ones they say are NOT, confirm an inferred person, and create
# a person from a name in a report.
#
# See docs/architecture/16-people.md. Reads (the People list and detail)
# ship separately; nothing here changes the recall hot path.
#
# Merge is a forward pointer, never a delete: SS already holds entity_ids
# (POST /v1/quilt/{u}/reassign-speaker takes to_person_id), and
# relationships cascade on entity delete. A merged row stays readable and
# resolves forward, so a stale client id self-heals.
# ============================================

class PeopleMergeRequest(BaseModel):
    canonical_entity_id: str
    merge_entity_ids: List[str]
    source: Optional[str] = None
    # WHICH NAME THE SURVIVING PERSON KEEPS.
    #
    # Missing until 2026-08-18, and SS had shipped a picker for it. The
    # sheet said "Keep the name: Pallavi / Pallavi Kandanu", the user
    # chose the full name, and there was nowhere to send the answer, so
    # the survivor kept its own name and the choice looked ignored.
    #
    # It is a DISPLAY choice, deliberately separate from
    # canonical_entity_id, which decides identity. The 88-meeting row
    # must stay the surviving row: clients hold its id and insights
    # reference it. Folding 88 meetings into a 4-meeting row to acquire
    # a surname would be a bad trade for a rename.
    #
    # Must name one of the entities in the merge, by name or alias.
    # Anything else is refused rather than silently ignored.
    canonical_name: Optional[str] = None
    # Proceed even though the user previously answered "keep separate"
    # for one of these pairs. Set only after they have been TOLD, which
    # is what the 409 SEPARATION_CONFLICT exists to make possible.
    #
    # A durable no should be hard to overturn, not impossible: Scott
    # recorded one on 2026-08-07, changed his mind, and had no way to
    # say so. Overturning also DELETES the separation, because he has
    # just contradicted it and leaving it would refuse the next merge
    # for a reason he already answered.
    override_separation: Optional[bool] = None
    # Proceed even though a row being folded in is BIGGER than the
    # survivor. Almost always a client bug rather than an intent, so it
    # is refused by default; see the direction check below for why that
    # became safe to enforce only on 2026-08-18.
    allow_smaller_canonical: Optional[bool] = None


class KeepSeparateRequest(BaseModel):
    entity_ids: List[str]
    source: Optional[str] = None


class ConfirmPersonRequest(BaseModel):
    source: Optional[str] = None


class PersonCreate(BaseModel):
    name: str
    description: Optional[str] = None
    source: Optional[str] = None
    # Set after the caller has SEEN the candidates and chosen "someone
    # new". Without it a contested name is refused, which is the point;
    # with it the caller is asserting this is a different person and CQ
    # records what they say. It never hard-requires a distinguishing
    # surname: refusing to record a real colleague because we wanted a
    # tidier graph is the wrong trade, and sometimes you genuinely only
    # know "Mike".
    create_new: Optional[bool] = None


class PersonRenameRequest(BaseModel):
    name: str
    source: Optional[str] = None


def _identity_error(exc: IdentityRequestError) -> HTTPException:
    detail = {"code": exc.code, "message": exc.message}
    detail.update(exc.extra)
    return HTTPException(status_code=422, detail=detail)


async def _rebuild_entity_index(user_id: str) -> None:
    """Repoint the Redis entity index at current names + aliases.

    Same shape rename-speaker uses. Merged entities keep their name in the
    index on purpose: the merge records that name as an alias of the
    canonical, so recall must still match it. It resolves to the canonical
    through the alias leg of the entity lookup.
    """
    entity_index_key = f"entity_index:{user_id}"
    rows = await db_pool.fetch(ENTITY_INDEX_NAMES_SQL, user_id)
    if not rows:
        return
    await redis_client.delete(entity_index_key)
    await redis_client.sadd(entity_index_key, *[r["name"] for r in rows])
    await redis_client.expire(entity_index_key, 7200)


async def _load_active_person(
    conn, user_id: str, entity_id: str,
    person_entity_type: str = "person",
) -> Any:
    """Fetch a person entity, following merged_into forward.

    A client holding the id of an already-merged entity gets the canonical
    it became rather than a 404 — that is the whole point of keeping the
    row. The hop count is bounded because merge always points at an
    already-resolved canonical, but the loop is capped anyway: a cycle
    here would hang a request holding a transaction open.
    """
    try:
        current = str(uuid.UUID(str(entity_id)))
    except (ValueError, AttributeError, TypeError):
        raise IdentityRequestError(
            "MALFORMED_ENTITY_ID", f"'{entity_id}' is not a valid entity id",
            entity_id=str(entity_id),
        )

    seen: set[str] = set()
    for _ in range(8):
        row = await conn.fetchrow(
            """
            SELECT entity_id, name, entity_type, description, metadata,
                   first_seen_at, last_seen_at, mention_count,
                   confirmed_at, confirmation_source, merged_into,
                   suppressed_at
            FROM entities
            WHERE user_id = $1 AND entity_id = $2::uuid
            """,
            user_id, current,
        )
        if row is None:
            return None
        if row["entity_type"] != person_entity_type:
            raise IdentityRequestError(
                "NOT_A_PERSON",
                f"Entity {current} is a '{row['entity_type']}', not a person",
                entity_id=current, entity_type=row["entity_type"],
            )
        if row["suppressed_at"] is not None:
            # The user disowned this entity ("not a person"). Every
            # identity verb treats it as gone; the lift handler reads
            # the row directly rather than through this loader.
            raise IdentityRequestError(
                "SUPPRESSED",
                f"Entity {current} was marked not-a-person. Lift the "
                "suppression (DELETE .../not-a-person) to act on it.",
                entity_id=current,
            )
        if row["merged_into"] is None:
            return row
        nxt = str(row["merged_into"])
        if nxt in seen:
            logger.error("entity_merge_cycle", user_id=user_id, entity_id=nxt)
            return row
        seen.add(nxt)
        current = nxt
    logger.error("entity_merge_chain_too_deep", user_id=user_id, entity_id=current)
    return None


async def _read_separations(conn, user_id: str, entity_ids: List[str]) -> List[tuple]:
    rows = await conn.fetch(
        """
        SELECT entity_id_lo, entity_id_hi FROM entity_separations
        WHERE user_id = $1
          AND (entity_id_lo = ANY($2::uuid[]) OR entity_id_hi = ANY($2::uuid[]))
        """,
        user_id, entity_ids,
    )
    return [(str(r["entity_id_lo"]), str(r["entity_id_hi"])) for r in rows]


_recall_vocab_cache: dict = {}


async def _people_vocab_cached(app_id: str) -> "PeopleVocabulary":
    """The caller's People vocabulary for the recall hot path.

    Same 300s posture as the facet runtime: vocabulary changes only at
    manifest registration, so the hot path pays a manifest fetch at most
    once per TTL per app, not per call.
    """
    import time as _time
    now = _time.monotonic()
    hit = _recall_vocab_cache.get(app_id)
    if hit and now - hit[1] < 300:
        return hit[0]
    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
    _recall_vocab_cache[app_id] = (vocab, now)
    return vocab


async def _people_read_context(conn, app_id: str):
    """(vocabulary, owed_to_available, insights_available, person_rule).

    All three are properties of the app's registered manifest, not of
    CQ's code: the vocabulary says WHICH types and labels carry People
    semantics for this caller (SS-default floor when no `people` block
    is declared), and the two availabilities say whether a counterparty
    ledger and a lens stack can honestly be lists rather than null. The
    read logic can compute a you_owe ledger the moment it ships, but for
    an app whose extraction never emits the edge the answer would be an
    empty list for every person, which reads as "nothing outstanding"
    rather than "not tracked". Insights are the same shape of claim: an
    app declaring no person-clustered consolidation rule never runs the
    profile pass, so an empty stack there would read as "nothing to say
    about this person" rather than "this app does not do that". So the
    capability follows the schema that produces the data, not the code
    that reads it.

    The fourth member is the person-clustered rule itself, because the
    readiness surface has to report the THRESHOLDS the pass actually runs
    on. "Two more meetings" is only honest if it counts toward the number
    that gates the derivation, and that number lives in the manifest.

    Any failure to resolve the manifest, a legacy non-UUID app id, no
    registered schema, unparseable JSON, degrades to (default floor,
    False, False, None). False is the conservative direction: the caller
    gets null and a stated reason.
    """
    import uuid as _uuid
    empty = (DEFAULT_PEOPLE_VOCABULARY, False, False, None)
    try:
        app_uuid = _uuid.UUID(app_id)
    except (ValueError, AttributeError, TypeError):
        return empty
    row = await conn.fetchrow(
        "SELECT manifest FROM app_schemas WHERE app_id = $1 "
        "ORDER BY version DESC LIMIT 1",
        app_uuid,
    )
    if row is None:
        return empty
    manifest = row["manifest"]
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except (ValueError, TypeError):
            return empty
    return (
        people_vocabulary(manifest),
        manifest_declares_owed_to(manifest),
        manifest_declares_person_insights(manifest),
        person_insight_rule(manifest),
    )


async def _person_insight_readiness(
    user_id: str,
    person_patch_ids: Optional[Any],
    ownership_label: str,
    rule: dict,
    entity_id: Optional[str] = None,
) -> dict:
    """Per lens: where this person stands, and whether waiting helps.

    Detail route only, and one person at a time: this is two extra
    queries, which is nothing against one person and 300 round trips
    against a directory.

    A person with no person patch is not skipped, they are the whole
    point. They have no owned items and no lens stamps, so every lens
    reports zero observed against its real threshold, which is the
    honest and specific version of "not yet".

    Both queries are deliberately shaped like the passes they report on:
    the same ownership edge, the same source types, the same 180 day
    window, and stamps read status-blind because a suppressed lens is a
    stamp in a non-active status and is exactly what the client needs to
    stop asking the user to wait for it.
    """
    source_rows: list = []
    stamp_rows: list = []
    # One person, every surface form the extractor has used for them. A
    # lens stamp and an ownership edge both point at whichever patch was
    # current when they were written, so reading only the primary reports
    # "not yet" over a lens that exists and counts a fraction of the
    # items the person owns.
    if isinstance(person_patch_ids, (list, tuple, set)):
        patch_ids = [str(p) for p in person_patch_ids if p]
    else:
        patch_ids = [str(person_patch_ids)] if person_patch_ids else []
    if patch_ids:
        source_rows = await db_pool.fetch(
            f"""
            SELECT cp.patch_id, cp.origin_id, cp.completed_at,
                   COALESCE(cp.status, 'active') AS status,
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
            f"user:{user_id}", patch_ids, ownership_label,
            rule["from_types"],
        )
        stamp_rows = await db_pool.fetch(
            """
            SELECT cp.value->>'lens' AS lens,
                   COALESCE(cp.status, 'active') AS status,
                   cp.value->>'archive_cause' AS archive_cause
            FROM context_patches cp
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            WHERE ps.subject_key = $1
              AND cp.origin_mode = 'derived'
              AND cp.value->>'source_person' = ANY($2::text[])
            """,
            f"user:{user_id}", patch_ids,
        )
    # What the pass has already TRIED and failed on. Without this a
    # person whose card fails the same parse gate every cycle is served
    # `pending_pattern`, which the client renders as "this fills in as
    # more comes in", inviting an action that cannot work.
    attempt_rows: list = []
    try:
        attempt_rows = [dict(r) for r in await db_pool.fetch(
            """
            SELECT lens, attempts, evidence_at_attempt
              FROM person_lens_attempts
             WHERE user_id = $1 AND entity_id = $2::uuid
            """,
            user_id, entity_id,
        )] if entity_id else []
    except Exception:
        # Table may lag on the MCP deployment's own Postgres. Degrading
        # to today's behaviour is safe: every lens simply reads as
        # pending rather than stalled.
        attempt_rows = []

    return build_insight_readiness(
        [dict(r) for r in source_rows],
        [dict(r) for r in stamp_rows],
        today=datetime.utcnow().date(),
        min_patches=rule["min_patches"],
        min_meetings=rule["min_meetings"],
        attempt_rows=attempt_rows,
    )


async def _people_core(
    conn,
    user_id: str,
    entity_ids: Optional[List[str]] = None,
    owed_to_available: bool = False,
    include_completed: bool = False,
    vocab: PeopleVocabulary = DEFAULT_PEOPLE_VOCABULARY,
) -> dict:
    """Everything the list and the detail both need, in set-based queries.

    Deliberately not N+1: at 155 people a per-person round trip would be
    155 round trips. Each leg fetches for the whole population and the
    rows are stitched in Python.
    """
    subject_key = f"user:{user_id}"
    scope = " AND e.entity_id = ANY($3::uuid[])" if entity_ids else ""
    args = [user_id, vocab.person_entity_type] + ([entity_ids] if entity_ids else [])

    # One runtime snapshot for the whole assembly: the ledger's type set,
    # the decay bands' anchor membership, and the registry TTL loop all
    # read the SAME facts (manifest-declared, SS floor fallback).
    type_runtime = await facet_runtime.get_type_runtime(conn.fetch)
    completable_types = type_runtime.completable_types
    # Two sets, and the difference between them is load bearing.
    #
    # COMPLETABLE is what a person can OWE, and it is the only thing the
    # `commitments` block may ever contain. LEDGER TRACKED is what the
    # item ledger holds across meetings: every completable, plus any type
    # whose manifest declares `ledger_tracked` (a question nobody
    # answered, a decision that keeps being revisited). The ledger's
    # primitive is a thing that keeps coming back without resolving, and
    # a recurring question is emphatically NOT something the person owes
    # the user.
    #
    # Day one the two sets are equal, so every byte of the existing
    # People surface is unchanged. They stop being equal the moment a
    # manifest declares a non-completable type, which is exactly when
    # `they_owe` must not widen. The fetch below uses the superset and
    # every ledger array filters back down to completables explicitly,
    # so the widening cannot leak by omission.
    ledger_types = tuple(sorted(type_runtime.ledger_tracked_types))
    completable_set = frozenset(completable_types)
    # One UTC day for the whole assembly, so two people in one response
    # can never be classified against different todays, and the response
    # stays byte stable within a day (the upstream prompt cache rule).
    ledger_today = item_ledger.today_utc()

    people = await conn.fetch(
        f"""
        SELECT e.entity_id, e.name, e.description, e.mention_count,
               e.first_seen_at, e.last_seen_at,
               e.confirmed_at, e.confirmation_source,
               e.self_at IS NOT NULL AS _is_self_row
        FROM entities e
        WHERE e.user_id = $1 AND e.entity_type = $2
          AND e.merged_into IS NULL AND e.suppressed_at IS NULL{scope}
        ORDER BY e.last_seen_at DESC NULLS LAST, e.entity_id
        """,
        *args,
    )
    ids = [r["entity_id"] for r in people]
    if not ids:
        return {"people": [], "by_id": {}}

    alias_rows = await conn.fetch(
        "SELECT entity_id, alias, source FROM entity_aliases "
        "WHERE user_id = $1 AND entity_id = ANY($2::uuid[]) ORDER BY alias",
        user_id, ids,
    )
    # Keep-separate rulings, served per person (lost-phone recovery,
    # 2026-08-24). SS's merge-proposal veto was a device-only UserDefaults
    # cache of a decision CQ already holds; serving the pairs makes the
    # cache derived, so a fresh phone never re-proposes a pair the user
    # refused. Degrades to empty on a DB without the table.
    separated_by_id: dict = {}
    try:
        for lo, hi in await _read_separations(conn, user_id, [str(i) for i in ids]):
            separated_by_id.setdefault(lo, set()).add(hi)
            separated_by_id.setdefault(hi, set()).add(lo)
    except Exception as exc:
        logger.debug("separations_unavailable", error=str(exc)[:120])
    appearance_rows = await conn.fetch(
        """
        SELECT pa.entity_id, pa.origin_id, pa.origin_type, pa.project_id,
               pa.last_seen_at, pa.capacities, pa.turn_count,
               -- Question counts, captured at ingest with the turn count
               -- (migration 37). NULL is unknown, never zero asked.
               pa.questions_asked, pa.questions_received_explicit,
               pa.questions_received_inferred,
               pa.questions_from_user_explicit, pa.questions_from_user_inferred,
               pa.meeting_questions_by_user,
               pr.name AS project
        FROM person_appearances pa
        LEFT JOIN projects pr ON pr.project_id = pa.project_id
        WHERE pa.user_id = $1 AND pa.entity_id = ANY($2::uuid[])
        ORDER BY pa.last_seen_at DESC
        """,
        user_id, ids,
    )
    # Person patches are matched to entities by name, the same way the
    # rest of CQ does: there is no entity_id on context_patches.
    patch_rows = await conn.fetch(
        """
        SELECT cp.patch_id, LOWER(cp.value->>'text') AS text_key
        FROM context_patches cp
        JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
        WHERE ps.subject_key = $1 AND cp.patch_type = $2
          AND COALESCE(cp.status, 'active') = 'active'
        """,
        subject_key, vocab.person_type,
    )
    patch_by_name = {r["text_key"]: str(r["patch_id"]) for r in patch_rows if r["text_key"]}

    # Open completables, with whichever ownership signal they carry: a
    # free-text value.owner the extractor copied out of the transcript,
    # or an explicit `owns` edge from a person patch.
    open_items = await conn.fetch(
        """
        SELECT cp.patch_id, cp.patch_type, cp.value->>'text' AS text,
               cp.value->>'owner' AS owner,
               cp.value->>'deadline' AS deadline,
               cp.value->>'deadline_date' AS deadline_date,
               cp.value->>'overdue_since' AS overdue_since,
               cp.value->>'shelved_at' AS shelved_at,
               cp.value->>'shelved_source' AS shelved_source,
               cp.value->>'salience' AS salience,
               -- A meeting said something that looked like this being
               -- finished, but not well enough to close on. The item is
               -- still open and still owed; these four are what the app
               -- renders the "looks done, confirm?" state from.
               cp.value->>'believed_complete_at' AS believed_complete_at,
               cp.value->>'believed_complete_evidence' AS believed_complete_evidence,
               cp.value->'believed_complete_reasons' AS believed_complete_reasons,
               cp.value->>'believed_complete_origin_id' AS believed_complete_origin_id,
               -- The molt record: every later meeting that said this same
               -- item again, and the monotonic count that survives the
               -- array's cap. Empty on every item stored before the
               -- restatement write path shipped.
               cp.value->'restatements' AS restatements,
               cp.value->>'restatement_count' AS restatement_count,
               cp.value->'deadline_history' AS deadline_history,
               cp.updated_at, cp.created_at, cp.permanence_override,
               pum.last_accessed_at,
               cp.project_id, cp.project, cp.origin_id,
               -- An item can carry more than one `owns` edge when the
               -- extractor had no vocabulary for a person's actual role
               -- (a counterparty, someone supplying a precondition) and
               -- fell back to ownership. Bare LIMIT 1 over that set is
               -- non-deterministic: Postgres may return either row, so a
               -- person's ledger could gain or lose an item between two
               -- identical calls. Prefer the edge whose person matches the
               -- stated value.owner, then oldest, then id for a total
               -- order. enforce_owner_edge_agreement stops new multi-owner
               -- items being written; this keeps existing ones stable.
               (SELECT pc.from_patch_id FROM patch_connections pc
                  JOIN context_patches op ON op.patch_id = pc.from_patch_id
                 WHERE pc.to_patch_id = cp.patch_id
                   AND pc.connection_label = $3
                   AND COALESCE(pc.status, 'active') = 'active'
                 ORDER BY (lower(btrim(op.value->>'text'))
                           = lower(btrim(cp.value->>'owner'))) DESC NULLS LAST,
                          pc.created_at, pc.connection_id
                 LIMIT 1) AS owner_patch_id
        FROM context_patches cp
        JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
        LEFT JOIN patch_usage_metrics pum ON pum.patch_id = cp.patch_id
        WHERE ps.subject_key = $1
          AND cp.patch_type = ANY($2::text[])
          AND COALESCE(cp.status, 'active') = 'active'
        ORDER BY cp.value->>'deadline_date' NULLS LAST, cp.patch_id
        """,
        subject_key, list(ledger_types), vocab.ownership_label,
    )
    # Registry TTL overrides for the completable types, resolved the same
    # way the decay loop resolves them, so the decay_state served here and
    # the archival the worker performs derive from the SAME parameters.
    registry_ttls: dict = {}
    for _pt in ledger_types:
        try:
            _ttl_row = await conn.fetchrow(decay_model.TTL_REGISTRY_QUERY, _pt)
            if _ttl_row and _ttl_row["default_ttl_days"] is not None:
                registry_ttls[_pt] = _ttl_row["default_ttl_days"]
        except Exception:
            pass  # Registry table may not exist yet (matches the worker)

    # Completed history, fetched only for the detail route: the list
    # assembles every person and would pay for the full completed
    # population per request while rendering none of it. Everything with
    # completed_at survives forever, so this is the read surface for
    # "what did this person actually deliver". Decayed/archived-without-
    # completion rows are deliberately NOT here: done is a claim, expired
    # is not.
    completed_items: list = []
    completed_owed_by_person: dict = {}
    if include_completed:
        completed_rows = await conn.fetch(
            """
            SELECT cp.patch_id, cp.patch_type, cp.value->>'text' AS text,
                   cp.value->>'owner' AS owner,
                   cp.value->>'deadline' AS deadline,
                   cp.value->>'deadline_date' AS deadline_date,
                   cp.value->>'overdue_since' AS overdue_since,
                   cp.value->>'shelved_at' AS shelved_at,
                   cp.value->>'shelved_source' AS shelved_source,
                   cp.value->>'completion_source' AS completion_source,
                   cp.value->>'completion_evidence' AS completion_evidence,
                   cp.value->>'completion_origin_id' AS completion_origin_id,
                   cp.value->'restatements' AS restatements,
                   cp.value->>'restatement_count' AS restatement_count,
                   cp.value->'deadline_history' AS deadline_history,
                   cp.completed_at, cp.created_at,
                   cp.project_id, cp.project, cp.origin_id,
                   (SELECT pc.from_patch_id FROM patch_connections pc
                      JOIN context_patches op ON op.patch_id = pc.from_patch_id
                     WHERE pc.to_patch_id = cp.patch_id
                       AND pc.connection_label = $3
                       AND COALESCE(pc.status, 'active') = 'active'
                     ORDER BY (lower(btrim(op.value->>'text'))
                               = lower(btrim(cp.value->>'owner'))) DESC NULLS LAST,
                              pc.created_at, pc.connection_id
                     LIMIT 1) AS owner_patch_id
            FROM context_patches cp
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            WHERE ps.subject_key = $1
              AND cp.patch_type = ANY($2::text[])
              AND cp.completed_at IS NOT NULL
            ORDER BY cp.completed_at DESC
            """,
            subject_key, list(ledger_types), vocab.ownership_label,
        )
        # decay_state is None on a completed item: decay no longer applies,
        # and per the house rule null means "not tracked", not a band.
        completed_items = [dict(r) | {"decay_state": None} for r in completed_rows]
        if owed_to_available:
            completed_owed_rows = await conn.fetch(
                """
                SELECT pc.from_patch_id AS item_patch_id,
                       pc.to_patch_id AS person_patch_id
                FROM patch_connections pc
                JOIN context_patches item ON item.patch_id = pc.from_patch_id
                JOIN patch_subjects ps ON ps.patch_id = item.patch_id
                WHERE ps.subject_key = $1
                  AND pc.connection_label = $2
                  AND COALESCE(pc.status, 'active') = 'active'
                  AND item.completed_at IS NOT NULL
                """,
                subject_key, vocab.counterparty_label,
            )
            for r in completed_owed_rows:
                completed_owed_by_person.setdefault(
                    str(r["person_patch_id"]), set()
                ).add(str(r["item_patch_id"]))

    # asyncpg Records are read-only; re-shape each open item as a dict and
    # annotate the decay band once, so the ledger arrays, the counts and
    # `open_by_decay` all read the SAME value for the same row.
    open_items = [
        dict(r) | {
            "decay_state": decay_model.decay_state(
                r["patch_type"],
                updated_at=r["updated_at"],
                created_at=r["created_at"],
                deadline_date=r["deadline_date"],
                salience=r["salience"],
                permanence_override=r["permanence_override"],
                registry_ttl_days=registry_ttls.get(r["patch_type"]),
                last_accessed_at=r["last_accessed_at"],
                freshness_types=type_runtime.freshness_tracked_types,
                deadline_types=type_runtime.deadline_anchored_types,
            ),
        }
        for r in open_items
    ]
    # The other side of the ledger. `owed_to` runs item -> person, the
    # mirror of the `owns` edge above, and it is the only thing that can
    # say the user owes a named person something. Fetched only when the
    # caller's manifest declares the label: without it the list is
    # guaranteed empty and an empty list here would mean "none open".
    owed_rows = (
        await conn.fetch(
            """
            SELECT pc.from_patch_id AS item_patch_id,
                   pc.to_patch_id AS person_patch_id
            FROM patch_connections pc
            JOIN context_patches item ON item.patch_id = pc.from_patch_id
            JOIN patch_subjects ps ON ps.patch_id = item.patch_id
            WHERE ps.subject_key = $1
              AND pc.connection_label = $2
              AND COALESCE(pc.status, 'active') = 'active'
              AND COALESCE(item.status, 'active') = 'active'
            """,
            subject_key, vocab.counterparty_label,
        )
        if owed_to_available
        else []
    )
    owed_by_person: dict = {}
    for r in owed_rows:
        owed_by_person.setdefault(str(r["person_patch_id"]), set()).add(
            str(r["item_patch_id"])
        )

    # The user's own display name, which is how `is_self_owned` recognises
    # an action item the extractor labelled with the user's name instead
    # of leaving the owner null. Absent profile is fine: the predicate
    # falls back to empty and self-token owners only.
    profile = await conn.fetchrow(
        "SELECT display_name FROM profiles WHERE user_id = $1", user_id
    )
    user_label = profile["display_name"] if profile else None

    stated_rows = await conn.fetch(
        """
        SELECT src.patch_id AS person_patch_id,
               tgt.project_id, tgt.value->>'text' AS project
        FROM patch_connections pc
        JOIN context_patches src ON src.patch_id = pc.from_patch_id
        JOIN context_patches tgt ON tgt.patch_id = pc.to_patch_id
        JOIN patch_subjects ps ON ps.patch_id = src.patch_id
        WHERE ps.subject_key = $1 AND pc.connection_label = $2
          AND COALESCE(pc.status, 'active') = 'active'
          AND COALESCE(src.status, 'active') = 'active'
          AND COALESCE(tgt.status, 'active') = 'active'
        """,
        subject_key, vocab.works_on_label,
    )

    aliases_by_id: dict = {}
    alias_sources: dict = {}
    for r in alias_rows:
        aliases_by_id.setdefault(str(r["entity_id"]), []).append(r["alias"])
        src = alias_sources.setdefault(str(r["entity_id"]), {})
        src[r["source"]] = src.get(r["source"], 0) + 1

    appearances_by_id: dict = {}
    for r in appearance_rows:
        appearances_by_id.setdefault(str(r["entity_id"]), []).append(r)

    assembled = []
    # is_self (13b ratification answer 2): true on the ego-linked row,
    # false on every other row WHEN an ego link exists, null everywhere
    # when it does not. False and cannot-tell are different claims: the
    # graph excludes the ego, and excluding nobody because the link is
    # absent must be visible to the client, not silent.
    #
    # A USER-level fact, never derived from the fetched rows: the detail
    # route scopes this query to one entity, and the first live check
    # proved the scoped version serves null-for-false on every non-self
    # detail.
    has_self = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM entities "
        "WHERE user_id = $1 AND self_at IS NOT NULL)",
        user_id,
    )

    by_id = {}
    for p in people:
        eid = str(p["entity_id"])
        aliases = aliases_by_id.get(eid, [])
        keys = owner_keys(p["name"], aliases)
        # A person can hold SEVERAL person patches, one per surface form
        # the extractor has used for them ("Suresh", "Suresh Muchakurti").
        # `patch_id` is the primary, canonical name first then aliases,
        # and it stays the one thing that identifies this person for the
        # ledger and the ownership joins.
        #
        # `patch_ids` is every one of them, and exists because derived
        # insights stamp `value.source_person` with whichever patch was
        # current WHEN THEY WERE DERIVED. Suresh's three insights were
        # derived against "Suresh" and his page resolves "Suresh
        # Muchakurti", so a lookup on the primary alone found nothing and
        # rendered three not-yet cards over three finished insights
        # (2026-08-16). Which form wins is an accident of extraction
        # history, so the read has to accept any of them.
        name_keys = [p["name"].lower()] + [a.lower() for a in aliases]
        patch_ids = list(dict.fromkeys(
            patch_by_name[k] for k in name_keys if k in patch_by_name
        ))
        patch_id = patch_ids[0] if patch_ids else None

        appearances = appearances_by_id.get(eid, [])
        project_counts: dict = {}
        for a in appearances:
            # "Where they show up" is a presence claim, and the project
            # filter stands on it: someone merely NAMED in a Kore room
            # did not show up in Kore (the second 17a field pass, where
            # mention-grade rows gave bystanders project membership).
            # Same predicate as the signals block: empty capacities are
            # pre-migration-31 rows and count as presence.
            caps = set(a["capacities"] or [])
            if caps and not caps & {"speaker", "ownership"}:
                continue
            if a["project_id"] or a["project"]:
                k = (a["project_id"], a["project"])
                project_counts[k] = project_counts.get(k, 0) + 1
        observed = [
            {"project_id": pid, "project": pname, "meeting_count": n}
            for (pid, pname), n in project_counts.items()
        ]
        stated = [
            {"project_id": r["project_id"], "project": r["project"]}
            for r in stated_rows
            if patch_id and str(r["person_patch_id"]) == patch_id
        ]
        projects = merge_project_rollups(observed, stated)

        # Shelved items are excluded from BOTH the arrays and every count
        # derived from them ("Let it go" removes it from the ledger). The
        # counting condition SS holds us to: a served count agrees with the
        # rows it gates, so the exclusion happens HERE, once, before
        # anything counts anything.
        # Everything of this person's that the LEDGER holds: any
        # ledger-tracked type they own, shelved rows already gone. This
        # is the wider set, and it is the one the item ledger classifies,
        # because a recurring question belongs in the ledger.
        owned_open = [
            r for r in open_items
            if r["shelved_at"] is None
            and ((r["owner"] or "").strip().lower() in keys
                 or (patch_id and r["owner_patch_id"] and str(r["owner_patch_id"]) == patch_id))
        ]

        # What this person OWES, which is the narrower set and is
        # COMPLETABLE ONLY. Filtered explicitly rather than inherited
        # from the fetch, so the day a manifest declares a
        # non-completable ledger type, a question cannot arrive on
        # somebody's card as an outstanding obligation. Day one the two
        # sets are identical and this is a no-op.
        they_owe = [r for r in owned_open if r["patch_type"] in completable_set]

        # What the USER owes this person: an item pointing here with an
        # owed_to edge, that the user themselves holds. Both halves are
        # required. Without the ownership gate, "Lockridge owes Marcus the
        # shortlist" would render on Marcus's card as something the user
        # owes him. The edge alone says who is waiting, not who is late.
        #
        # Stays null for a person with no person PATCH even when the
        # capability is on. An owed_to edge targets a patch, so a
        # patch-less entity cannot be the target of one, and 0 there would
        # be structurally guaranteed rather than measured: an
        # unfalsifiable "you owe them nothing" for the largest group on
        # the list (on prod, 332 person entities against 175 person
        # patches). `they_owe` degrades gracefully here because it also
        # matches the free-text value.owner by name; `you_owe` has no such
        # leg, so it says so instead of guessing.
        you_owe = None
        if owed_to_available and patch_id:
            owed_ids = owed_by_person.get(patch_id) or ()
            owed = [
                r for r in open_items
                if r["shelved_at"] is None
                and r["patch_type"] in completable_set
                and str(r["patch_id"]) in owed_ids
                and is_self_owned(r["owner"], user_label)
            ]
            # AN EMPTY LIST ASSERTS "WE LOOKED AND FOUND NOTHING", and
            # that is only honest when the instrument that looks has
            # ever worked. It had not. `owed_to` edges could not be
            # produced before OWED_TO_OBSERVABLE_SINCE: the edge shape
            # lived only in a JSON schema the model never received, so
            # every edge it emitted was discarded on arrival. Two
            # survived in three months across 9,088 connections.
            #
            # Scott's Steven card read "You owe Steven: nothing open"
            # above two open items that named Steven in their text.
            # `owed_to_available` was true because the manifest DECLARES
            # the label, and declared is not the same as populated.
            #
            # So: a real edge is served whenever it exists. An empty
            # answer is served only if at least one of the user's open
            # commitments was captured by a working instrument, meaning
            # extracted (it has an origin) on or after the fix. A
            # hand-written item can never carry the edge, because the
            # client composer does not send one, so it is not evidence
            # either way. Otherwise the honest answer is null: not that
            # we heard. Transcripts are not retained, so the pre-fix
            # rows can never be re-read, and this gate is what stops a
            # permanent falsehood from wearing the shape of a fact.
            if owed:
                you_owe = owed
            elif people_signals.owed_to_instrument_has_looked(
                    open_items, completable_set,
                    lambda o: is_self_owned(o, user_label)):
                you_owe = []

        # History legs: same matching predicates as the open ledger (owner
        # name/alias or owns edge; owed_to edge + self ownership), applied
        # to the completed population. No shelved filter: done is done,
        # whatever state preceded it. completed_items arrives newest
        # completion first, so these stay in that order.
        owned_completed = [
            r for r in completed_items
            if (r["owner"] or "").strip().lower() in keys
            or (patch_id and r["owner_patch_id"] and str(r["owner_patch_id"]) == patch_id)
        ]
        completed_they_owe = [
            r for r in owned_completed if r["patch_type"] in completable_set
        ]
        completed_you_owe = None
        if owed_to_available and patch_id and include_completed:
            c_owed_ids = completed_owed_by_person.get(patch_id) or ()
            completed_you_owe = [
                r for r in completed_items
                if r["patch_type"] in completable_set
                and str(r["patch_id"]) in c_owed_ids
                and is_self_owned(r["owner"], user_label)
            ]

        # Closure mode per item (the molt ledger): what actually happened
        # to each thing this person owes, not merely whether it is open.
        # Computed from the SAME filtered rows the ledger arrays carry,
        # so every count here opens into items the caller already has.
        # The completed leg is present only on the detail route, which is
        # why the served block states its own scope rather than letting a
        # client assume the denominators match across the two surfaces.
        ledger_items = item_ledger.classify_items(
            owned_open + owned_completed,
            today=ledger_today,
            appearances=appearances,
            user_label=user_label,
        )

        row = {
            "entity_id": eid,
            "name": p["name"],
            "is_self": p["_is_self_row"] if has_self else None,
            "aliases": aliases,
            "patch_id": patch_id,
            # Every surface form's patch, for reads that must not care
            # which one was current when a thing was written. Not served:
            # internal to the assembly, like the other underscore keys.
            "_patch_ids": patch_ids,
            "description": p["description"] or None,
            "confirmed": p["confirmed_at"] is not None,
            "confirmation_source": p["confirmation_source"],
            "first_seen_at": p["first_seen_at"].isoformat() if p["first_seen_at"] else None,
            "last_seen_at": p["last_seen_at"].isoformat() if p["last_seen_at"] else None,
            "meeting_count": len(appearances),
            # The 17a list intelligence: recent-interaction weights, the
            # cadence behind DRIFTING, and the open-ledger summary that
            # writes the row sentence. Served inputs, client-owned
            # situation assignment; every count agrees with the ledger
            # arrays because it is computed from the SAME filtered rows.
            "signals": compute_person_signals(appearances, they_owe, you_owe),
            "project_count": len(projects),
            # The one project worth putting in a list-row subtitle, so
            # rendering "Atlas Migration" under a name does not cost a
            # detail fetch per row (SS ask 5, 2026-08-06).
            #
            # Deliberately `projects[0]` rather than a second selection
            # rule: merge_project_rollups already orders by meeting_count
            # descending then name, and a browse surface must not reshuffle
            # between polls. Reusing that ordering means "top" here and
            # "first" on the detail route can never disagree. A separate
            # max() would be a second source of truth for the same claim.
            #
            # Same object shape as an entry in `projects`, so a client
            # decodes one type in both places.
            "top_project": projects[0] if projects else None,
            # The badge for a disambiguation list: where this person was
            # last seen, in ANY capacity, capacity stated. top_project is
            # presence-grade and serves null for a mention-only person,
            # which is exactly who a "which Sam?" picker needs it for.
            "last_seen_in": last_seen_in(appearances),
            # Entity ids the user ruled are NOT this person. Sorted, so a
            # browse surface never reshuffles; [] when nothing was ruled.
            "separated_from": sorted(separated_by_id.get(eid, ())),
            "open_they_owe": len(they_owe),
            # Per-band counts over the SAME rows `they_owe` carries (shelved
            # already excluded), so the chip and the card read one source and
            # neither invents a number. All three keys always present; the
            # vocabulary is open per the additive rule, so a client must
            # tolerate keys it does not know.
            "open_by_decay": {
                band: sum(1 for r in they_owe if r["decay_state"] == band)
                for band in (
                    decay_model.DECAY_STATE_LIVE,
                    decay_model.DECAY_STATE_AGING,
                    decay_model.DECAY_STATE_STALE,
                )
            },
            # null, never 0, until the caller's manifest declares owed_to.
            # See READ_CAPABILITIES.you_owe: 0 would read as "you owe this
            # person nothing", which is a different claim from "CQ cannot
            # tell". Once the label exists, 0 is honest and this is a count.
            "open_you_owe": None if you_owe is None else len(you_owe),
            # Follow up pressure: how many questions this person asked,
            # was asked, and was asked BY THE USER, with the denominators
            # they were computed from. Counts only; CQ draws no
            # conclusion from them and names no pattern.
            "questions": compute_question_totals(appearances),
            "_appearances": appearances,
            "_ledger_items": ledger_items,
            # Which modes can occur for the object types in THIS
            # person's ledger, so a client never guesses. Dated types
            # come from the runtime's deadline-anchored set, which is
            # what makes `re_dated` impossible for an object that never
            # had a date.
            "_ledger_vocabulary": item_ledger.vocabulary(
                {i["object_type"] for i in ledger_items},
                dated_types=type_runtime.deadline_anchored_types,
            ),
            "_ledger_scope": "open_and_completed" if include_completed else "open_only",
            "_projects": projects,
            "_they_owe": they_owe,
            "_you_owe": you_owe,
            "_completed_they_owe": completed_they_owe,
            "_completed_you_owe": completed_you_owe,
            "_mention_count": p["mention_count"] or 0,
            "_alias_sources": alias_sources.get(eid, {}),
            # Newest of the two things that change a person row: another
            # mention (last_seen_at) or a human vouching (confirmed_at).
            # Drives the `since` delta.
            "_changed_at": max(
                [t for t in (p["last_seen_at"], p["confirmed_at"]) if t is not None],
                default=None,
            ),
        }
        assembled.append(row)
        by_id[eid] = row

    return {"people": assembled, "by_id": by_id}


def _public_person(row: dict) -> dict:
    return {k: v for k, v in row.items() if not k.startswith("_")}


@app.get("/v1/people/{user_id}/network", tags=["People"])
async def people_network(
    user_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    The orbit graph (design 13b, contract ratified 2026-08-11): person
    to person co-presence with the ego excluded, precomputed daily by
    the worker and served as stored bytes (no computation here; the
    zero-latency rule, and the design's own build note that nobody runs
    a force simulation at render time).

    Envelope: version, computed_at, caps (stated, never implied), nodes
    (list-identical name, meeting_count, nullable cluster_id), edges
    (a < b pinned, weight = distinct shared meetings), clusters
    (dominant_project_id for the client's local name mapping,
    member_count), positions (unit square; letterboxing is the
    client's). computed_at null = no snapshot yet (a new user before
    the first daily cycle); every array empty, which renders as an
    empty sky, not an error.
    """
    try:
        row = await db_pool.fetchrow(
            "SELECT payload FROM people_network_snapshots WHERE user_id = $1",
            user_id,
        )
    except Exception:
        row = None  # table not yet migrated (MCP lag): empty envelope
    if row:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return payload
    return {
        "version": NETWORK_SNAPSHOT_VERSION,
        "computed_at": None,
        "caps": {"nodes": NETWORK_NODE_CAP,
                 "min_shared_meetings": NETWORK_MIN_SHARED},
        "nodes": [], "edges": [], "clusters": [], "positions": [],
    }


@app.get("/v1/people/{user_id}", tags=["People"])
async def list_people(
    user_id: str,
    since: Optional[str] = Query(None, description="ISO 8601 — only people changed after this time, plus a `deleted` array of entity_ids folded away by a merge since then"),
    confirmed: str = Query("all", description="Filter on confirmation state: true, false, or all"),
    min_meetings: Optional[int] = Query(None, ge=0, description="Only people seen in at least this many meetings. Applied BEFORE limit, and `total` reflects it."),
    limit: Optional[int] = Query(None, ge=1, le=1000, description="Cap the people array (applied last, after every filter)"),
    app_id: str = Depends(verify_application_access),
):
    """
    The People list: every person CQ knows about this user, newest activity
    first.

    Includes people CQ only inferred from a transcript and nobody has
    vouched for. Those come back with `confirmed: false` and often a null
    `patch_id` (an entity with no person patch behind it yet). They are
    NOT filtered out by default: the design renders them explicitly as
    unconfirmed, and hiding them would mean the app can never offer the
    confirmation. Use `confirmed=` and `min_meetings=` to shape the list.

    **Every filter runs before `limit`, and `total` reflects the filters.**
    A floor applied after pagination is not a filter, it is a truncation:
    the caller gets an arbitrary subset with no way to know whether the
    next page holds more that would have passed.

    Three counts, three meanings:
      * `len(people)`      what came back, after `limit`
      * `total`            how many matched the filters, before `limit`
      * `total_unfiltered` every active person, ignoring all filters

    `total_unfiltered` counts ACTIVE entities only and excludes anything
    folded away by a merge, so tidying up a roster makes the number go
    down rather than quietly inflating it.

    `open_you_owe` is a count for callers whose registered manifest
    declares the `owed_to` connection label, and null for everyone else.
    See the `capabilities` block: without that label CQ has no counterparty
    on a commitment, and answering 0 would read as "you owe this person
    nothing", which is a different claim from "CQ cannot tell".

    `top_project` is the highest-signal project for that person, in the
    same object shape as an entry in the detail route's `projects` array,
    or null when CQ knows of none. It exists so a list-row subtitle can
    read "Atlas Migration" without a detail fetch per row. It is the FIRST
    element of that same ordering, not a separately computed maximum, so
    the list and the detail can never disagree about which project leads.

    `open_you_owe` stays null PER PERSON for anyone with a null `patch_id`, even when
    the capability is on, because an `owed_to` edge targets a person patch
    and an entity without one cannot be the target of a single edge. So
    the capability answers "can CQ answer this question at all" and the
    per-person null answers "can it answer it for this person". Both
    render the same way: not tracked, rather than nothing outstanding.

    **The `query` block echoes what CQ RECEIVED, not what it applied.**
    See the response contract in doc 16 section 8b: raw received values,
    plus an `ignored` array naming anything CQ could not use. That split
    is deliberate. A middlebox that strips or mangles a parameter is the
    thing this exists to catch, so the echo has to show the wire, and the
    `ignored` array separately shows CQ's behavior. `confirmed=maybe`
    therefore echoes `"maybe"` and lists `confirmed` in `ignored`.
    """
    server_time = datetime.utcnow()
    # Received values, echoed verbatim. Anything CQ cannot act on is named
    # in `ignored` rather than silently rewritten here.
    ignored: List[str] = []

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            # Postgres hands back tz-aware timestamps; a caller echoing our
            # naive `server_time` back would blow up the comparison.
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            # Lenient on purpose (rejecting would be a breaking change for
            # existing callers), but no longer silent: an unparseable
            # `since` degrades to a full sync, and this is what says so.
            ignored.append("since")

    if confirmed not in ("true", "false", "all"):
        ignored.append("confirmed")

    async with db_pool.acquire() as conn:
        vocab, owed_to_available, insights_available, person_rule = (
            await _people_read_context(conn, app_id)
        )
        core = await _people_core(
            conn, user_id, owed_to_available=owed_to_available, vocab=vocab
        )
        rows = core["people"]
        total_unfiltered = len(rows)

        if confirmed in ("true", "false"):
            want = confirmed == "true"
            rows = [r for r in rows if r["confirmed"] is want]

        if min_meetings:
            rows = [r for r in rows if r["meeting_count"] >= min_meetings]

        deleted: List[str] = []
        if since_dt:
            merged = await conn.fetch(
                """
                SELECT entity_id FROM entities
                WHERE user_id = $1 AND entity_type = $3
                  AND ((merged_into IS NOT NULL AND merged_at > $2)
                       OR suppressed_at > $2)
                """,
                user_id, since_dt, vocab.person_entity_type,
            )
            # A merge removes a person from the list, so clients holding
            # the folded id need a tombstone the same way patch deletes do.
            deleted = [str(r["entity_id"]) for r in merged]
            rows = [
                r for r in rows
                if r["_changed_at"] is not None and r["_changed_at"] > since_dt
            ]

    # `total` is taken after every filter and before `limit`, so a caller
    # can always tell "showing 50 of 137 matching, 272 known".
    total = len(rows)
    if limit:
        rows = rows[:limit]

    # The across-people read, and it is about the USER rather than about
    # any of them: which items get re-dated and re-stated instead of
    # closed, and which ones the user ends up holding themselves
    # (`absorbed_by_user`). CQ serves the counts, their denominators and
    # the patch ids behind them. It computes no ratio, ranks nobody, and
    # emits no string naming a pattern; the reading is the client's.
    #
    # Deliberately over the UNFILTERED population, so paging or a
    # `min_meetings` filter cannot change a user-level number. Open items
    # only, because the list route does not fetch the completed
    # population, which is why `scope` is stated rather than assumed: the
    # person detail's ledger runs the same classifier over a wider set.
    all_ledger_items = [i for r in core["people"] for i in r["_ledger_items"]]

    def _counts_only(items: list) -> dict:
        """A summary with the receipt keys stripped.

        Counts on the list, receipts on the detail. This is a browse
        surface polled for every person the user has, and a patch id
        list per mode per person grows that payload without earning it:
        the ids are only useful once the user has chosen somebody, and
        the detail route serves them there along with each item's own
        restatements. Stripped by FAMILY (`RECEIPT_KEYS`) rather than by
        name, so a receipt key added later cannot quietly land here.
        """
        return {
            k: v for k, v in item_ledger.summarize(items).items()
            if k not in item_ledger.RECEIPT_KEYS
        }

    item_ledger_rollup = {
        "scope": "open_only",
        "people_considered": total_unfiltered,
        "people_with_items": sum(1 for r in core["people"] if r["_ledger_items"]),
        "summary": _counts_only(all_ledger_items),
        # Per person, ordered by name so a browse surface never
        # reshuffles between polls.
        "by_person": [
            {
                "entity_id": r["entity_id"],
                "name": r["name"],
                **_counts_only(r["_ledger_items"]),
                # Question VOLUME, kept as its own count and never
                # folded into the chase numbers above. Measured by hand
                # against the transcripts, volume is nearly level across
                # people whose follow up is nothing alike, so a claim
                # built on it asserts something the data contradicts.
                # Two counts, two questions, both served.
                "questions": r["questions"],
            }
            for r in sorted(
                (r for r in core["people"] if r["_ledger_items"]),
                key=lambda r: ((r["name"] or "").lower(), r["entity_id"]),
            )
        ],
    }

    return {
        "people": [_public_person(r) for r in rows],
        "total": total,
        "total_unfiltered": total_unfiltered,
        "item_ledger_rollup": item_ledger_rollup,
        "deleted": deleted,
        "capabilities": capability_report(owed_to_available, insights_available),
        "query": {
            "since": since,
            "confirmed": confirmed,
            "min_meetings": min_meetings,
            "limit": limit,
            "ignored": ignored,
        },
        "server_time": server_time.isoformat(),
    }


@app.get("/v1/people/{user_id}/{entity_id}", tags=["People"])
async def get_person(
    user_id: str,
    entity_id: str,
    app_id: str = Depends(verify_application_access),
    accept_language: Optional[str] = Header(None, alias="Accept-Language"),
):
    """
    One person, with the meetings, projects and open items behind the
    counts on the list row.

    Resolves a folded entity_id forward, so a client that cached an id
    before a merge still lands on the right person.

    CQ deliberately returns no meeting titles or durations, only
    `origin_id`. Per the context-flow contract (doc 15 item 5) CQ wins on
    state and the app wins on content: ShoulderSurf joins origin_id to its
    own records for "Atlas cutover checkpoint, Jul 28, 47m".

    `commitments.they_owe` and `commitments.you_owe` are the two sides of
    the ledger and they are computed differently on purpose. `they_owe` is
    ownership: items this person owns, matched by name, alias, or `owns`
    edge. `you_owe` is a counterparty: items the USER owns that carry an
    `owed_to` edge pointing here. Both halves of that are required. The
    edge alone says who is waiting, not who is late, so without the
    ownership gate "Lockridge owes Marcus the shortlist" would appear on
    Marcus's card as something the user owes him.

    `you_owe` is null, not an empty list, for callers whose registered
    manifest does not declare `owed_to`. See capabilities.you_owe.

    `item_ledger` is the closure lens over those same items (doc 16
    section 5.10): per item, WHAT happened to it rather than whether it is
    open. `delivered` is the only mode that is delivery; `restated` is the
    molt (said again, with the date never moving), `re_dated` is an item
    being managed against a calendar, `not_raised_since` is one that has
    not come up across the last meetings with THAT PERSON (counted in
    meetings, never in elapsed days), and `absorbed_by_user` is one the
    user ended up holding themselves.

    `not_raised_since` is named for what was observed and must be
    rendered that way. It is the only mode where ABSENCE does the work,
    and a meeting cannot see an email: an item finished offline on the
    Tuesday and never mentioned again produces exactly this state. The
    safe sentence is the one built from `meetings_since_last_statement`,
    "has not come up in your last 3 meetings with her", which is true
    whatever happened away from the room.

    Each item carries one headline `mode` plus every
    other mode also true of it in `modes`, so `summary.by_mode` counts
    each item once and the counts sum to `summary.items`. Every count
    opens into `summary.patch_ids_by_mode`. No ratio is served at any
    denominator, and no field here describes a person: the surface ships
    the count, never the cause.

    `questions` (and `meetings[].questions`) is question VOLUME: how many
    this person asked, was asked, and was asked by the user, captured
    from the transcript at ingest because transcripts are not retained.
    The explicit and inferred grades are separate on purpose and must not
    be summed: one is a vocative CQ read, the other is a guess from who
    spoke next. Null is unknown, never "was asked nothing".

    Volume is NOT the follow up finding and must not be rendered as one.
    Measured against real transcripts it runs nearly level across people
    whose follow up is nothing alike, because a chase and a substantive
    probe both count as one question. The metric that carries it is
    `item_ledger.summary.raised_without_advance`: an item already
    in the ledger came up in a meeting where the user asked this person
    something, and it had not closed by their next meeting. The two are
    served as separate counts and stay separate.

    `insights` is the 16a lens stack: up to one derived card per lens in
    the CQ-side lens vocabulary, each with the claim, the do line, its
    own `decay_state`, the computed `facts` where the lens has any, and
    the receipts. It is null ONLY when the fetch failed; an empty list
    means the pass has produced nothing yet, which includes a person too
    thin to have a person patch. See capabilities.insights for whether
    this app can produce any.

    `insight_readiness` says, per lens, why it is absent and how close it
    is: `pending_evidence` with the counts still needed, `pending_pattern`
    when the gate is met and no claim has been found yet, `suppressed`
    when the user rejected that card (it is never coming back, so never
    invite them to wait for it), `retired` when the system archived it,
    `available` when it is in `insights`. `more_meetings_help` answers
    the client's actual rendering question directly.

    Each evidence row is one DISTINCT meeting behind the claim, carrying
    the source patch text and the patch ids so the client can join to
    patches it already holds. `ingested_on` (and its legacy alias
    `date`) is the day CQ STORED that source, not the day the meeting
    happened: CQ persists no meeting date and will not invent one. Join
    `origin_id` to the app's own meeting record for that. Archived
    sources are not served, so an insight can honestly show fewer
    receipts than the gate that created it required.
    """
    async with db_pool.acquire() as conn:
        vocab, owed_to_available, insights_available, person_rule = (
            await _people_read_context(conn, app_id)
        )
        try:
            resolved = await _load_active_person(
                conn, user_id, entity_id, vocab.person_entity_type
            )
        except IdentityRequestError as e:
            raise _identity_error(e)
        if resolved is None:
            raise HTTPException(
                status_code=404, detail=f"Person {entity_id} not found for this user"
            )
        eid = str(resolved["entity_id"])
        core = await _people_core(
            conn, user_id, [eid], owed_to_available=owed_to_available,
            include_completed=True, vocab=vocab,
        )

    row = core["by_id"].get(eid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Person {eid} not found for this user")

    def _item(r):
        """One open ledger item.

        `owner` is the RAW extracted surface form, exactly as it sits in
        `value.owner`. **Normalizing it to the canonical entity name is a
        regression, not a tidy-up.** This is one of the few places where
        the raw string is the payload and the resolved identity is the
        redundant part: the caller already knows the canonical identity,
        because it is the person whose endpoint they called. What they
        cannot get anywhere else is the string the extractor actually
        wrote, which is the only thing that will line up against the owner
        strings in the app's own action-item ledger (doc 16 section 8d).
        Resolve it here and the field looks helpful while doing nothing.
        """
        return {
            "patch_id": str(r["patch_id"]),
            "type": r["patch_type"],
            "text": r["text"],
            "owner": r["owner"],
            "deadline": r["deadline"],
            "deadline_date": r["deadline_date"],
            "overdue_since": r["overdue_since"],
            "project_id": r["project_id"],
            "project": r["project"],
            "origin_id": r["origin_id"],
            # live | aging | stale, derived from the LIVE decay parameters
            # (type TTL, salience, deadline anchor, access exemption) and
            # bucketed to the UTC day. Open vocabulary: pass unknown values
            # through. This is neglect, not age — a recall access can move
            # an item from stale back to live with no deliberate user act.
            "decay_state": r["decay_state"],
            # Null by construction here (the ledger excludes shelved rows);
            # the keys exist so this item shape and the quilt's are one
            # shape, and so a client that un-shelves via
            # DELETE .../patches/{id}/shelve decodes one type everywhere.
            "shelved_at": r["shelved_at"],
            "shelved_source": r["shelved_source"],
        }

    def _done_item(r):
        """A completed ledger item: the open shape plus the closure facts.

        `completion_source` is which lane closed it (app tap, extraction
        auto-close, user chat), and null on completions that predate
        source stamping. `decay_state` is null: decay no longer applies
        to a completed row, and null means not tracked, never a band.
        """
        return _item(r) | {
            "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            "completion_source": r["completion_source"],
            "completion_evidence": r["completion_evidence"],
        }

    def _history(rows):
        """{"total": N, "items": [...capped...]}: the cap self-describes.

        `total` counts the whole matched population; `items` carries the
        newest COMPLETED_HISTORY_CAP completions. Serving a bare capped
        array would read as "this is everything", the same silent
        truncation the quilt coverage line exists to prevent.
        """
        return {
            "total": len(rows),
            "items": [_done_item(r) for r in rows[:COMPLETED_HISTORY_CAP]],
        }

    # Profile insights (16a): derived patches from the person-keyed
    # consolidation pass, keyed by value.source_person = this person's
    # patch id. Up to one card per lens in the CQ-side lens vocabulary.
    # Active-only on the READ (a suppressed insight disappears from the
    # surface) while the pass's idempotency check ignores status PER
    # LENS, which together make hold-to-suppress a durable no for that
    # one card through the existing DELETE /patches route, and leave the
    # person's other lenses derivable.
    #
    # Null ONLY when the fetch failed. A patchless person is NOT a
    # cannot-tell: an entity accumulates a person patch as it is
    # observed, so "no patch yet" is the thinnest possible NOT YET, and
    # it is precisely the case the not-yet card exists for (a user two
    # meetings in, wondering why a person has no card). #239 served null
    # for it on the reasoning that CQ could "never" derive for a
    # patchless entity, which was wrong about the word never: clients
    # correctly render nothing for null, so the motivating case got a
    # blank screen. It is [] now, with `insight_readiness` saying how far
    # along the person is.
    insights: Optional[list] = []
    who_they_are_card: Optional[dict] = None
    trajectory_card: Optional[dict] = None
    # Per lens: where this person stands and whether waiting helps.
    # Null when the app cannot produce insights at all (capabilities
    # explains that) or when the fetch failed.
    readiness: Optional[dict] = None
    if insights_available and person_rule:
        try:
            readiness = await _person_insight_readiness(
                user_id,
                row.get("_patch_ids") or row.get("patch_id"),
                vocab.ownership_label,
                person_rule,
                entity_id=eid,
            )
        except Exception:
            readiness = None
    # An entity is enough. A card keyed on the entity does not need the
    # person to hold a `person` patch at all, and gating on one would
    # hide it from exactly the thin people the lens can still speak
    # about.
    if row.get("patch_id") or row.get("entity_id"):
        try:
            # db_pool, NOT the _people_core conn: that connection's
            # acquire block has already closed by this point in the
            # function, and the first live derivation proved it (insight
            # in DB, zero served, the guard swallowing the closed-conn
            # error).
            ins_rows = await db_pool.fetch(
                """
                SELECT cp.patch_id, cp.patch_type, cp.value, cp.created_at,
                       cp.updated_at, cp.last_observed_at,
                       cp.permanence_override, cp.source_patch_ids,
                       pum.last_accessed_at
                FROM context_patches cp
                JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                LEFT JOIN patch_usage_metrics pum ON pum.patch_id = cp.patch_id
                WHERE ps.subject_key = $1
                  AND cp.origin_mode = 'derived'
                  AND (cp.value->>'source_person' = ANY($2::text[])
                       OR ($3::text IS NOT NULL
                           AND cp.value->>'source_entity_id' = $3))
                  AND COALESCE(cp.status, 'active') = 'active'
                ORDER BY cp.created_at DESC
                """,
                f"user:{user_id}",
                # ANY over every surface form's patch, not just the
                # primary. An insight stamps the patch that was current
                # when it was derived, and which form that is changes as
                # the extractor rephrases someone.
                [str(pid) for pid in (row.get("_patch_ids")
                                      or ([row["patch_id"]] if row.get("patch_id") else []))],
                # And the ENTITY, because the identity a card is keyed on
                # depends on which pass wrote it. The two model lenses and
                # follow-through stamp `source_person` with a person PATCH
                # id; the contrastive pass keys on the entity, which is
                # the identity that does not move when the extractor
                # rephrases somebody. Matching only the patch ids meant
                # three live cards sat in the database unreachable by the
                # page they belong to (measured 2026-08-16).
                str(row["entity_id"]) if row.get("entity_id") else None,
            )
            insights = []
            who_they_are_card = None
            trajectory_card = None
            # Same runtime and registry TTLs the ledger's decay bands and
            # the worker's decay loop read, so an insight's band and the
            # archival CQ will actually perform come from one authority.
            ins_runtime = await facet_runtime.get_type_runtime(db_pool.fetch)
            ins_ttls: dict = {}
            for _pt in {ir["patch_type"] for ir in ins_rows}:
                try:
                    _ttl_row = await db_pool.fetchrow(
                        decay_model.TTL_REGISTRY_QUERY, _pt
                    )
                    if _ttl_row and _ttl_row["default_ttl_days"] is not None:
                        ins_ttls[_pt] = _ttl_row["default_ttl_days"]
                except Exception:
                    pass  # registry table may not exist yet (matches the worker)
            for ir in ins_rows:
                iv = ir["value"]
                if isinstance(iv, str):
                    iv = json.loads(iv)
                # The synthesis lens is a short paragraph with its own
                # receipts, not a one-line capsule; it leaves the card
                # stack here and is served as `who_they_are` below. The
                # newest wins if the worker's replace ever left two.
                if iv.get("lens") == who_they_are.LENS:
                    if who_they_are_card is None:
                        who_they_are_card = who_they_are.served(iv) | {
                            "patch_id": str(ir["patch_id"]),
                        }
                    continue
                # The hero lens leaves the capsule stack the same way:
                # it is a card with its own arithmetic and receipts,
                # served as `trajectory` (doc 16 5.15). Newest wins if
                # the worker's replace ever left two.
                if iv.get("lens") == trajectory_svc.LENS:
                    if trajectory_card is None:
                        served_traj = trajectory_svc.served(iv)
                        if served_traj:
                            trajectory_card = served_traj | {
                                "patch_id": str(ir["patch_id"]),
                            }
                    continue
                # Evidence is the source patches' meetings: the receipts
                # the 12a design demands, one row per DISTINCT meeting.
                #
                # Archived sources are excluded. A decayed or superseded
                # patch is not a live receipt, and counting it would let
                # the card claim support it no longer has. That can drop
                # the list below the rule's min_meetings gate; the list
                # is served honestly anyway and the count speaks. It is
                # never padded back up.
                #
                # `text` is the source patch's OWN text, which is CQ
                # state, not app content (doc 15 item 5 draws that line
                # at things like a meeting TITLE). One meeting can carry
                # several sources, so the representative is the oldest
                # by created_at with patch_id breaking ties: a total
                # order, so two identical calls render identically.
                evidence: list = []
                if ir["source_patch_ids"]:
                    ev_rows = await db_pool.fetch(
                        """
                        SELECT origin_id,
                               min(created_at)::date AS met_on,
                               array_agg(patch_id
                                   ORDER BY created_at, patch_id) AS patch_ids,
                               (array_agg(value->>'text'
                                   ORDER BY created_at, patch_id))[1] AS text
                        FROM context_patches
                        WHERE patch_id = ANY($1::uuid[])
                          AND origin_id IS NOT NULL
                          AND COALESCE(status, 'active') = 'active'
                        GROUP BY origin_id
                        ORDER BY met_on ASC, origin_id ASC
                        """,
                        ir["source_patch_ids"],
                    )
                    evidence = [
                        {"origin_id": e["origin_id"],
                         # INGEST date: when CQ stored the source patch,
                         # not when the meeting happened. CQ does not
                         # persist a meeting date, so it does not invent
                         # one. Join origin_id to the app's own meeting
                         # record for the real date. `date` is the
                         # original name of this same field, kept
                         # because the served surface is additive only.
                         "ingested_on": e["met_on"].isoformat(),
                         "date": e["met_on"].isoformat(),
                         "text": e["text"],
                         "patch_ids": [str(p) for p in (e["patch_ids"] or [])]}
                        for e in ev_rows
                    ]
                # The insight patch's OWN decay band, from the shared
                # decay model. Deliberately not a confidence float over
                # the sources: the source types decay at wildly
                # different rates (and one of them, `decision`, is
                # pinned to never decay at all), so a fraction would
                # report a threshold the decay loop never acts on, which
                # is the exact split brain decay_model.py exists to
                # prevent. Null when the type carries no TTL anywhere,
                # because "not tracked" is not a band. No float is
                # served here at all, so there is nothing that could
                # reach GP's allow_nan=False serializer as NaN.
                ins_state = None
                if decay_model.effective_ttl_days(
                    ir["patch_type"],
                    iv.get("salience"),
                    ir["permanence_override"],
                    ins_ttls.get(ir["patch_type"]),
                ) is not None:
                    ins_state = decay_model.decay_state(
                        ir["patch_type"],
                        updated_at=ir["updated_at"] or ir["created_at"],
                        created_at=ir["created_at"],
                        last_observed_at=ir["last_observed_at"],
                        salience=iv.get("salience"),
                        permanence_override=ir["permanence_override"],
                        registry_ttl_days=ins_ttls.get(ir["patch_type"]),
                        last_accessed_at=ir["last_accessed_at"],
                        freshness_types=ins_runtime.freshness_tracked_types,
                        deadline_types=ins_runtime.deadline_anchored_types,
                    )
                insights.append({
                    "patch_id": str(ir["patch_id"]),
                    "lens": iv.get("lens"),
                    "text": iv.get("text", ""),
                    "do": iv.get("do"),
                    # The arithmetic a COMPUTED lens was written from,
                    # ints only, so the client can show the numbers
                    # behind the sentence and anyone can audit the claim
                    # against them. Null for a lens a model reasoned its
                    # way to: it counted nothing, so it has no counts.
                    # The subject line under each count is CQ's own
                    # fixed string, rendered verbatim by the client
                    # inside a localized sentence; served in the
                    # caller's language (Accept-Language), English
                    # otherwise. See services/people_i18n.
                    "facts": people_i18n.localize_facts(
                        iv.get("facts"), people_i18n.resolve_locale(accept_language)),
                    # Lower sorts earlier; absent means after the
                    # ordered ones. Served because the client sorts by
                    # whether a lens is named, so an order that carries
                    # meaning cannot be inferred on their side.
                    "display_order": iv.get("display_order"),
                    "derived_at": ir["created_at"].isoformat() if ir["created_at"] else None,
                    # live | aging | stale, the SAME open vocabulary and
                    # UTC-day bucketing the ledger items carry.
                    "decay_state": ins_state,
                    "evidence": evidence,
                })
            insights = insight_cards.one_card_per_lens(insights)
        except Exception:
            # Serving must never fail the detail route. Null, not []:
            # a swallowed error is CQ not knowing, not CQ knowing there
            # are none. This is now the ONLY path that serves null.
            insights = None

    # How this person has been DESCRIBED over time, newest first. The
    # `changed_from` field is the indicator: non-null means the
    # perception moved and there is something to show under the name.
    #
    # Degrades to None, never raises: the table is migration 39 and the
    # MCP deployment's separate Postgres can lag migrations, and a
    # missing series must not take down a person page.
    described_as_series = None
    try:
        # db_pool, NOT conn: the acquire block closed above, and this
        # fetch on the released connection raised "connection has been
        # released back to the pool" into a guard written for a missing
        # table, which logged at debug and served null. Receipt
        # `dismissed_at IS NULL`: a perception the user said was wrong
        # leaves the series but stays in the table (migration 46). The
        # meeting did say it; what was wrong is treating it as true of
        # the person. Excluding rather than deleting also keeps "the
        # user dismissed this" distinguishable from "this was never
        # observed", which is the argument that shaped `shelve`.
        #
        # 2026-08-21: Suresh had four rows in entity_descriptions and
        # described_as: null on the wire, for every person, since #286.
        # The guard hid it because null is the honest answer to a lagging
        # DB and the same dishonest answer to a programming error.
        desc_rows = await db_pool.fetch(
            """
            SELECT description, first_origin_id, observation_count,
                   first_observed_at, last_observed_at
            FROM entity_descriptions
            WHERE user_id = $1 AND entity_id = $2::uuid
              AND dismissed_at IS NULL
            ORDER BY first_observed_at DESC
            """,
            user_id, entity_id,
        )
        described_as_series = described_as.series_payload([
            {
                "description": r["description"],
                "first_origin_id": r["first_origin_id"],
                "observation_count": r["observation_count"],
                "first_observed_at": r["first_observed_at"].isoformat()
                if r["first_observed_at"] else None,
                "last_observed_at": r["last_observed_at"].isoformat()
                if r["last_observed_at"] else None,
            }
            for r in desc_rows
        ])
    except Exception as exc:
        logger.warning("described_as_series_unavailable", error=str(exc)[:140])

    # IS THIS PERSON MID-RECONCILIATION, and say so rather than let the
    # client infer it from an absence.
    #
    # When the user marks a characterisation wrong, three things happen
    # at three different speeds: the readings are stamped immediately,
    # the syntheses built from them are archived immediately, and a new
    # summary is written by the worker's profile pass whenever it next
    # runs. In between, `who_they_are` is legitimately absent.
    #
    # Without this field that gap is indistinguishable from "this person
    # never had a summary", so a client either says nothing or says
    # something reassuring it cannot support. Scott asked for exactly
    # this on 2026-08-31: a way for the app to show that a correction is
    # settling and that there may be an inaccuracy until it does.
    #
    # Derived, never stored: a stored flag would need clearing, and the
    # thing that clears it is a worker pass that has no reason to know
    # this field exists.
    # WHY THERE IS NO TRAJECTORY CARD, which is a different question
    # from whether there is one.
    #
    # 2026-08-31: Scott asked why "how they're changing" had disappeared.
    # It had not broken. Suresh's card was archived that afternoon with
    # cause `lapsed` because his speaking turns went from 316 against
    # 201 (a real shift) to 271 against 258, a 5% move, under a card
    # that requires a 20 point gap AND a 40% relative change before it
    # will claim anything. The lens correctly withdrew a claim that had
    # stopped being true, and the screen showed a hole.
    #
    # An absence with no reason is indistinguishable from a bug, and the
    # reader resolves that ambiguity against us every time. This is the
    # same rule as `dropped` on the woven route, `project_known`'s three
    # states, and `reconciling` above.
    #
    # LAPSED AND NEVER-QUALIFIED MUST NOT COLLAPSE, which is
    # ShoulderSurf's condition and it is right. "This was a trend and it
    # flattened" earns a sentence, because steady is a finding and it is
    # the one a reader would not have guessed. "There has never been
    # enough here to measure" earns silence, and dressing it up as a
    # finding of steadiness would be inventing one.
    trajectory_status = None
    try:
        recent_card = await db_pool.fetchrow(
            """
            SELECT cp.status, cp.updated_at, cp.value
              FROM context_patches cp
              JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
             WHERE ps.subject_key = $1
               AND cp.patch_type = 'insight'
               AND cp.value->>'lens' = $2
               AND (cp.value->>'source_entity_id' = $3
                    OR cp.value->>'source_person' = ANY($4::text[]))
             ORDER BY cp.updated_at DESC
             LIMIT 1
            """,
            f"user:{user_id}", trajectory_svc.LENS, entity_id,
            [str(pid) for pid in (row.get("_patch_ids")
                                  or ([row["patch_id"]] if row.get("patch_id") else []))],
        )
        if recent_card is None:
            # Never qualified. Silence, per SS: a not-yet is not a
            # finding, and this state must never render as steadiness.
            trajectory_status = {"state": "never_qualified", "since": None,
                                 "measure": None, "withdrawn_claim": None}
        elif (recent_card["status"] or "active") == "active":
            trajectory_status = {"state": "active", "since": None,
                                 "measure": None, "withdrawn_claim": None}
        else:
            cv = recent_card["value"]
            if isinstance(cv, str):
                cv = json.loads(cv)
            cause = (cv or {}).get("archive_cause")
            facts = (cv or {}).get("facts") or {}
            trajectory_status = {
                # `lapsed` is the only one that earns a sentence. Any
                # other archive cause is reported as itself rather than
                # folded into lapsed, because a card removed by a
                # correction did not flatten.
                "state": "lapsed" if cause == "lapsed" else "withdrawn",
                "since": recent_card["updated_at"].isoformat()
                if recent_card["updated_at"] else None,
                "measure": facts.get("measure_key") or cv.get("measure_key"),
                # What it used to say, so the client can be specific
                # rather than generic: "turns were up sharply through
                # late August, the last 8 meetings are level" beats "no
                # significant change". Named for what it is so nobody
                # renders it as a live claim.
                "withdrawn_claim": cv.get("text"),
            }
    except Exception as exc:
        # Null means CQ cannot tell. Claiming "never qualified" because
        # a query failed would be inventing the quietest possible lie.
        logger.warning("trajectory_status_unavailable", error=str(exc)[:140])

    reconciling = None
    try:
        rec = await db_pool.fetchrow(
            """
            SELECT count(*) AS n,
                   max(dismissed_at) AS latest,
                   bool_or(dismissed_note IS NOT NULL) AS had_note
              FROM entity_descriptions
             WHERE user_id = $1 AND entity_id = $2::uuid
               AND dismissed_at IS NOT NULL
            """,
            user_id, entity_id,
        )
        if rec and rec["n"]:
            newest_card = await db_pool.fetchval(
                """
                SELECT max(cp.created_at)
                  FROM context_patches cp
                  JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                 WHERE ps.subject_key = $1
                   AND cp.patch_type = 'insight'
                   AND cp.value->>'lens' = 'who_they_are'
                   AND COALESCE(cp.status, 'active') = 'active'
                   AND (cp.value->>'source_entity_id' = $2
                        OR cp.value->>'source_person' IN (
                            SELECT cp2.patch_id::text FROM context_patches cp2
                              JOIN patch_subjects ps2 ON ps2.patch_id = cp2.patch_id
                             WHERE ps2.subject_key = $1 AND cp2.patch_type = $3))
                """,
                # The app's OWN person type, never the literal. The
                # People surface was born speaking ShoulderSurf's
                # dialect and a test guards against it drifting back.
                f"user:{user_id}", entity_id, vocab.person_type,
            )
            # A summary written BEFORE the rejection is a summary built
            # from rejected material, so it does not clear the state.
            if newest_card is None or (rec["latest"] and newest_card < rec["latest"]):
                reconciling = {
                    "since": rec["latest"].isoformat() if rec["latest"] else None,
                    "dismissed_readings": int(rec["n"]),
                    # Whether the user gave their own version, which is
                    # the difference between "we removed this" and "we
                    # are folding in what you told us".
                    "correction_recorded": bool(rec["had_note"]),
                }
    except Exception as exc:
        # Null means CQ cannot tell, which is honest. Claiming a person
        # is settled because a query failed is not.
        logger.warning("reconciling_state_unavailable", error=str(exc)[:140])

    # What this person has STATED they are, as opposed to what a meeting
    # showed them doing. A stated role ("Suresh is scrum master on ABM
    # project") is a `role` patch linked to its project, not to the
    # person, so nothing here ever saw it; the card served the last
    # meeting's inference instead. Matched the way every other people
    # read matches patches: by name and alias, here as the opening of
    # the text or via a `describes` edge to the person's patch. Null =
    # the app does not track stated roles, or the fetch failed; an empty
    # list = tracked, none stated.
    stated_roles = None
    if vocab.stated_role_type:
        try:
            subject_key = f"user:{user_id}"
            role_names = [row["name"]] + list(row.get("aliases") or [])
            keys = [n.strip().lower() for n in role_names if n and n.strip()]
            role_rows = await db_pool.fetch(
                """
                SELECT DISTINCT ON (cp.patch_id)
                       cp.patch_id, cp.value->>'text' AS text, cp.origin_id,
                       cp.created_at, cp.project_id,
                       COALESCE(pr.name, cp.project) AS project
                FROM context_patches cp
                JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                LEFT JOIN projects pr ON pr.project_id = cp.project_id
                LEFT JOIN patch_connections pc
                       ON pc.from_patch_id = cp.patch_id
                      AND pc.connection_label = 'describes'
                      AND COALESCE(pc.status, 'active') = 'active'
                LEFT JOIN context_patches person_p
                       ON person_p.patch_id = pc.to_patch_id
                WHERE ps.subject_key = $1
                  AND cp.patch_type = $2
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND (
                        LOWER(cp.value->>'text') LIKE ANY($3::text[])
                     OR LOWER(person_p.value->>'text') = ANY($4::text[])
                  )
                ORDER BY cp.patch_id, cp.created_at DESC
                """,
                subject_key, vocab.stated_role_type,
                [k + "%" for k in keys], keys,
            )
            role_rows = sorted(role_rows, key=lambda r: r["created_at"], reverse=True)
            stated_roles = stated_roles_payload([
                {
                    "patch_id": r["patch_id"], "text": r["text"],
                    "project": r["project"], "project_id": r["project_id"],
                    "origin_id": r["origin_id"],
                    "stated_at": r["created_at"].isoformat() if r["created_at"] else None,
                }
                for r in role_rows
            ], role_names)
        except Exception as exc:
            logger.warning("stated_roles_unavailable", error=str(exc)[:140])

    detail = _public_person(row)
    detail.update({
        # The series behind the description, so a client can show that a
        # perception changed and open the history. Null = cannot tell
        # (fetch failed); iterations 0 = we have never described them.
        "described_as": described_as_series,
        # Null when nothing is pending. An object means a
        # characterisation was rejected and the replacement has not been
        # written yet, so the client can say so out loud.
        "reconciling": reconciling,
        # PRECEDENCE RULE, on the wire: a role the person STATED beats a
        # description a meeting INFERRED. `title` is the newest stated
        # role with the person's own name and copula stripped; a client
        # shows it under the name and keeps `description` as "last seen
        # doing". `stated_roles.items` are the receipts.
        "stated_roles": stated_roles,
        "title": stated_roles["title"] if stated_roles else None,
        # The synthesis across stated roles and the description series,
        # written by the worker's who_they_are lens on its own model and
        # regenerated only when the inputs change. Null = not generated
        # yet (or the insights fetch failed); a client shows the series
        # and the title and waits. Receipts are the cited inputs.
        "who_they_are": who_they_are_card,
        # The 5.15 hero: how this person is changing against their own
        # past. Object or null; null is the common case, not the edge.
        "trajectory": trajectory_card,
        # WHY there is no card, which is a different question from
        # whether there is one. Null only when CQ could not tell.
        # `lapsed` earns a sentence, `never_qualified` earns silence.
        "trajectory_status": trajectory_status,
        # A list (possibly empty) unless the fetch failed, which is the
        # only cannot-tell. See capabilities.insights for whether this
        # app can ever produce them at all.
        "insights": insights,
        # Why a lens is missing and how close it is, per lens, so an
        # empty stack can be explained honestly instead of papered over.
        # Null when the app produces no insights at all, or the readiness
        # fetch failed. See doc 16 section 5.8.2.
        "insight_readiness": readiness,
        "projects": row["_projects"],
        "commitments": {
            "they_owe": [_item(r) for r in row["_they_owe"]],
            # A list once the caller's manifest declares `owed_to`, null
            # before that. An empty list means "none open"; null means CQ
            # cannot tell. See capabilities.you_owe.
            "you_owe": (
                None if row["_you_owe"] is None
                else [_item(r) for r in row["_you_owe"]]
            ),
            # The history legs: what this person actually delivered, and
            # what the user delivered to them. Completed-only by
            # construction (completed_at set); decayed items never appear
            # here, because "expired" is not a claim anyone completed
            # anything. Same null semantics as the open you_owe.
            "completed_they_owe": _history(row["_completed_they_owe"]),
            "completed_you_owe": (
                None if row["_completed_you_owe"] is None
                else _history(row["_completed_you_owe"])
            ),
        },
        # The closure ledger: what HAPPENED to each item this person
        # owes, over the same rows `commitments` carries. An action item
        # tracker can say an item is open; it cannot say that this one
        # object has come back three times as a fresh commitment with
        # its state unchanged, which is the failure mode that survives
        # every accountability system the user already has.
        #
        # `summary.by_mode` counts each item under ONE headline mode and
        # `item.modes` lists everything else that is also true of it, so
        # the totals add up to `summary.items` and nothing is hidden by
        # the choice of headline. `patch_ids_by_mode` is the trace: every
        # count opens into the exact patches behind it, which is the
        # difference between "three times, here they are" and a verdict.
        # No ratio is served at any denominator.
        "item_ledger": {
            # open_and_completed here; the person LIST serves the same
            # classifier over open items only and says so, because the
            # two denominators are not the same number.
            "scope": row["_ledger_scope"],
            "items": row["_ledger_items"],
            "summary": item_ledger.summarize(row["_ledger_items"]),
            # The mode contract for the object types actually present.
            # Open in both directions: a type that does not exist yet
            # appears the day it appears in the data, with no contract
            # change, and a client meeting a mode it does not know skips
            # that row rather than guessing.
            "vocabulary": row["_ledger_vocabulary"],
        },
        # When this person was actually PRESENT, from the same predicate
        # over the same rows the person LIST computes its `signals` from,
        # so the two screens cannot answer "when did we last meet" with
        # two different numbers. Null last_present_at means NOT PRESENT,
        # never "we do not know": a client must omit the line rather than
        # fall back to any other date. The entity-level `last_seen_at` in
        # `meetings[]` below is per-appearance and mention-inclusive; it is
        # not a met-date and must never be rendered as one.
        "presence": presence_anchor(row["_appearances"]),
        # `capacities` says HOW this person turned up in this meeting, not
        # merely that they did: `speaker` means a diarization label resolved
        # to them, `ownership` means they were named as the owner of an item
        # extracted from it, `mention` means the transcript said their name.
        #
        # Served because ShoulderSurf's duplicate veto needs it. Their client
        # cannot see who spoke, so their veto has to suppress a merge
        # proposal on ANY shared meeting, which fails safe but goes silent on
        # true duplicates shaped exactly like the Vijay set: five spellings of
        # one human, co-occurring in eight meetings, and in every one of them
        # only ONE spelling carried `speaker` while the other was ownership
        # only. That is the signature of label drift rather than two people,
        # and without this field it is invisible to them.
        #
        # An EMPTY list means unknown, not absent. Migration 31's rule: a row
        # carrying no capacity predates the column, and dropping unknowns
        # from a veto would turn "we do not know" into "they did not speak".
        "meetings": [
            {
                "origin_id": a["origin_id"],
                "origin_type": a["origin_type"],
                "project_id": a["project_id"],
                "last_seen_at": a["last_seen_at"].isoformat() if a["last_seen_at"] else None,
                "capacities": list(a["capacities"] or []),
                # Null = unknown (pre-metric rows, or not a speaker),
                # never "spoke zero turns". Enables the turn-count veto
                # refinement the Vijay set proved right: a 1-turn label
                # against a 41-turn label in one meeting is a diarization
                # artifact, not a second person.
                "turn_count": a["turn_count"],
                # Questions in THIS meeting, read off the transcript at
                # ingest (migration 37). The two attribution grades are
                # separate and must never be summed by a reader: one is a
                # vocative CQ read, the other is a guess from who spoke
                # next. Null is unknown (the meeting predates the metric,
                # or no speaker label could be identified as the user),
                # never "was asked nothing".
                "questions": {
                    "asked": a["questions_asked"],
                    "received_explicit": a["questions_received_explicit"],
                    "received_inferred": a["questions_received_inferred"],
                    "from_user_explicit": a["questions_from_user_explicit"],
                    "from_user_inferred": a["questions_from_user_inferred"],
                    # The denominator: every question the user asked in
                    # this meeting, whoever it landed on.
                    "user_asked_total": a["meeting_questions_by_user"],
                },
            }
            for a in row["_appearances"]
        ],
        "provenance": {
            "name_mentions": row["_mention_count"],
            "meetings_observed": row["meeting_count"],
            "confirmed": row["confirmed"],
            "confirmation_source": row["confirmation_source"],
            # How each surface form was resolved: 'heuristic' is CQ
            # guessing, 'user_confirmation' is the human saying so. The
            # design shows these differently and should keep being able to.
            "alias_sources": row["_alias_sources"],
            # Null, not 0. See capabilities.confirmed_mention_split.
            "confirmed_mentions": None,
            "assumed_mentions": None,
        },
        "capabilities": capability_report(owed_to_available, insights_available),
    })
    return detail


async def _fold_person_patches(
    conn, candidates: List[dict], canonical_name: str, source: str,
) -> List[str]:
    """Collapse one human's several `person` patches into one.

    The extractor mints a person patch per surface form a transcript
    used, so one colleague accumulates several and every join that keys
    on a person patch then sees a fraction of them. Measured on
    production 2026-08-16: Xhoi held five, Mike, Vijay and Sukumar four
    each, and Suresh five.

    Shared by MERGE (the user said two people are one) and CONFIRM (the
    user said this person is who we think). Those are the same write with
    different triggers, and a second implementation would drift from this
    one the first time either was touched.

    Returns (folded_patch_ids, items_moved). The second number is the
    one worth showing a human: "folded 1 name variant" undersells what
    happened, because the variant carried work. Measured on production
    2026-08-16, Vijay's two live forms held 98 and 37 ownership edges,
    so folding them brings 37 items under the same person as the other
    98 rather than leaving his record split in two.
    """
    survivor, patch_losers = choose_surviving_person_patch(
        candidates, canonical_name
    )
    if not (survivor and patch_losers):
        return ([], 0)
    survivor_id = survivor["patch_id"]
    loser_ids = [c["patch_id"] for c in patch_losers]
    # Counted BEFORE the repoint, because afterwards these edges hang
    # off the survivor and are indistinguishable from its own.
    items_moved = await conn.fetchval(
        """
        SELECT count(DISTINCT to_patch_id) FROM patch_connections
        WHERE from_patch_id = ANY($1::uuid[])
          AND COALESCE(status, 'active') = 'active'
        """,
        loser_ids,
    ) or 0

    # Connections follow the person. Same UNIQUE(from, to, role)
    # collision the relationship repoint has, so guard with NOT EXISTS
    # and sweep the true duplicates after.
    for column, other in (("from_patch_id", "to_patch_id"),
                          ("to_patch_id", "from_patch_id")):
        await conn.execute(
            f"""
            UPDATE patch_connections pc SET {column} = $1
            WHERE pc.{column} = ANY($2::uuid[])
              AND NOT EXISTS (
                  -- status-agnostic read, deliberately. This is a
                  -- unique-constraint collision check, not a semantic
                  -- one, and the index on (from, to, role) spans
                  -- archived rows. Filtering to active here would let
                  -- the repoint collide with an archived duplicate.
                  SELECT 1 FROM patch_connections pc2
                  WHERE pc2.{column} = $1
                    AND pc2.{other} = pc.{other}
                    AND pc2.connection_role = pc.connection_role
              )
            """,
            survivor_id, loser_ids,
        )
    # Hard delete, deliberately. Migration 32 archives connections so a
    # removal stays auditable, but these edges reference an identity that
    # no longer exists as a distinct thing. Archiving them would preserve
    # rows pointing at a patch this fold just retired.
    await conn.execute(
        "DELETE FROM patch_connections WHERE from_patch_id = ANY($1::uuid[]) "
        "OR to_patch_id = ANY($1::uuid[])",
        loser_ids,
    )
    await conn.execute(
        "DELETE FROM patch_connections WHERE from_patch_id = $1 AND to_patch_id = $1",
        survivor_id,
    )
    # The survivor speaks for the canonical identity now.
    await conn.execute(
        """
        UPDATE context_patches
        SET value = jsonb_set(value, '{text}', to_jsonb($1::text)),
            updated_at = NOW()
        WHERE patch_id = $2
        """,
        canonical_name, survivor_id,
    )
    # Archive, never delete: this is what puts the folded ids in the
    # delta-sync `deleted` array, which SS has decoded since delta sync
    # shipped.
    await conn.execute(
        """
        UPDATE context_patches
        SET status = 'archived', updated_at = NOW(),
            value = value
                || jsonb_build_object('merged_into_patch', $1::text)
                || jsonb_build_object('merge_source', $2::text)
                || jsonb_build_object('archive_cause', 'merge')
        WHERE patch_id = ANY($3::uuid[])
        """,
        str(survivor_id), source, loser_ids,
    )
    return ([str(p) for p in loser_ids], int(items_moved))


class DescriptionDismissal(BaseModel):
    """What the user said when they rejected a perception.

    `note` is the difference between the two affordances the card
    offers. "This is inaccurate" is a bare dismissal and carries none.
    "Correct this" carries the user's own words, and those words are
    kept verbatim on the row rather than only being fed to a model,
    because the correction is a thing the user SAID and the record of
    what was said is the asset.
    """
    note: Optional[str] = Field(
        default=None, max_length=2000,
        description="The user's own correction, when they gave one",
    )
    source: Optional[str] = Field(
        default="user_card",
        description="Which affordance said so: user_card, user_chat, correction",
    )


@app.post("/v1/people/{user_id}/{entity_id}/descriptions/dismiss", tags=["People"])
async def dismiss_descriptions(
    user_id: str,
    entity_id: str,
    body: DescriptionDismissal | None = Body(default=None),
    app_id: str = Depends(verify_application_access),
):
    """Mark every live perception of this person as wrong.

    A meeting's description of a person can simply be false, and until
    migration 46 there was no way to say so: `entity_descriptions` had
    no status column, no API write path, and chat corrections operate on
    `context_patches` and never arrive here. The card showed the STATED
    role correctly ("Stated, not inferred" beats an inference, and that
    precedence rule worked), while the inferred series underneath it
    and the `who_they_are` summary above both kept repeating the wrong
    one.

    THE ROW IS RETAINED. The meeting did say it, so the row is a true
    record of what was said; what is wrong is treating it as true of the
    person. Deleting would destroy an accurate observation and make "the
    user rejected this" indistinguishable from "this was never observed"
    -- the same argument that shaped `shelve`, where a tombstone would
    have been indistinguishable from decay.

    Whole-person rather than per-row on purpose. A user looking at a
    card is rejecting a CHARACTERISATION, not auditing rows, and every
    live row feeds the same summary. Per-row dismissal would ask them to
    understand a data model to fix a sentence.

    Reversible with DELETE. Returns the count so the caller can render
    what happened rather than assume it.
    """
    payload = body or DescriptionDismissal()
    try:
        rows = await db_pool.fetch(
            """
            UPDATE entity_descriptions
               SET dismissed_at = NOW(),
                   dismissed_source = $3,
                   dismissed_note = $4
             WHERE user_id = $1 AND entity_id = $2::uuid
               AND dismissed_at IS NULL
            RETURNING description_id
            """,
            user_id, entity_id, (payload.source or "user_card"), payload.note,
        )
    except Exception as exc:
        logger.warning("description_dismiss_failed", user_id=user_id,
                       entity_id=entity_id, error=str(exc)[:200])
        raise HTTPException(status_code=500, detail="dismiss failed")

    logger.info("descriptions_dismissed", user_id=user_id, entity_id=entity_id,
                count=len(rows), source=(payload.source or "user_card"),
                had_note=bool(payload.note))

    # ARCHIVE THE SYNTHESES BUILT FROM THOSE ROWS, NOW, not on the next
    # worker pass.
    #
    # Steven Williams, 2026-08-31: the user marked the "who they are"
    # paragraph inaccurate, all four readings were stamped, the route
    # answered that the summary was regenerating, and the paragraph was
    # STILL ACTIVE in the database and still being served when he looked
    # again. `who_they_are` regenerates only when its input fingerprint
    # changes, on a periodic pass, so "it will be here next time you
    # look" was a promise this endpoint could not keep.
    #
    # `trajectory` goes with it for the same reason: it is the other
    # synthesis OF the description series, and it kept narrating the
    # rejected arc ("first seen as a practitioner-buyer, then
    # reidentified as an advisor") under a paragraph the user had just
    # struck through.
    #
    # Archived rather than deleted, cause `corrected`, so the worker's
    # own replace machinery still sees a coherent history and an undo
    # has something to reason about. Guarded: losing a card must never
    # fail the dismissal the user actually asked for.
    syntheses_archived = 0
    try:
        # The app's OWN person type, resolved from its manifest, never
        # the literal: this surface was born speaking ShoulderSurf's
        # dialect and a test guards against it drifting back.
        dismiss_vocab = await _people_vocab_cached(app_id)
        person_patch_ids = await db_pool.fetch(
            """
            SELECT cp.patch_id::text AS patch_id
              FROM context_patches cp
              JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
             WHERE ps.subject_key = $1 AND cp.patch_type = $2
            """,
            f"user:{user_id}", dismiss_vocab.person_type,
        )
        archived = await db_pool.fetch(
            """
            UPDATE context_patches SET
                status = 'archived',
                updated_at = NOW(),
                value = jsonb_set(value, '{archive_cause}', '"corrected"')
             WHERE patch_type = 'insight'
               AND COALESCE(status, 'active') = 'active'
               AND value->>'lens' = ANY($1::text[])
               AND (value->>'source_entity_id' = $2
                    OR value->>'source_person' = ANY($3::text[]))
            RETURNING patch_id
            """,
            ["who_they_are", "trajectory"], entity_id,
            [r["patch_id"] for r in person_patch_ids],
        )
        syntheses_archived = len(archived)
        if syntheses_archived:
            logger.info("dismissal_archived_syntheses", user_id=user_id,
                        entity_id=entity_id, count=syntheses_archived)
    except Exception as exc:
        logger.warning("dismissal_synthesis_archive_failed", user_id=user_id,
                       entity_id=entity_id, error=str(exc)[:200])

    # "Correct this" rather than "this is inaccurate": the note is a
    # fact the user STATED, so it goes down the existing correction lane
    # rather than a parallel path of its own. handle_correction already
    # supersedes the contradicted patch by id, lands the new fact as
    # origin_mode='declared', and connects them with `replaces`. Building
    # a second writer would be a second source of truth about one person.
    #
    # Enqueued rather than awaited: the user is looking at a card, the
    # dismissal has already taken effect on this response, and the patch
    # rewrite is cold-path work. Failure here must not fail the
    # dismissal, which is why it is guarded and only logged.
    if payload.note:
        try:
            await redis_client.xadd("memory_updates", {"data": json.dumps({
                "user_id": user_id,
                "app_id": app_id,
                "task_type": "correction",
                "interaction_type": "correction",
                "content": payload.note,
                "metadata": {
                    "source": "person_card",
                    "subject_entity_id": entity_id,
                },
            })})
            logger.info("description_correction_enqueued",
                        user_id=user_id, entity_id=entity_id)
        except Exception as exc:
            logger.warning("description_correction_enqueue_failed",
                           user_id=user_id, entity_id=entity_id,
                           error=str(exc)[:200])
    # The summary is rebuilt by the worker's profile pass, not here: it
    # regenerates when its input fingerprint changes and the dismissed
    # rows have just left that input set. Saying so on the wire means a
    # client renders "updating" rather than expecting a fresh sentence.
    return {
        "dismissed": len(rows),
        "correction_enqueued": bool(payload.note),
        "who_they_are": "regenerating",
        # What actually happened, not what was intended. The count is
        # the difference between a summary that was withdrawn and one
        # that is still sitting on somebody's screen, and the previous
        # version of this response could not tell the caller which.
        "syntheses_archived": syntheses_archived,
    }


@app.delete("/v1/people/{user_id}/{entity_id}/descriptions/dismiss", tags=["People"])
async def undismiss_descriptions(
    user_id: str,
    entity_id: str,
    app_id: str = Depends(verify_application_access),
):
    """Undo a dismissal. Every stamp cleared, the series comes back.

    Nothing was destroyed, so there is nothing to reconstruct.
    """
    try:
        rows = await db_pool.fetch(
            """
            UPDATE entity_descriptions
               SET dismissed_at = NULL, dismissed_source = NULL,
                   dismissed_note = NULL
             WHERE user_id = $1 AND entity_id = $2::uuid
               AND dismissed_at IS NOT NULL
            RETURNING description_id
            """,
            user_id, entity_id,
        )
    except Exception as exc:
        logger.warning("description_undismiss_failed", user_id=user_id,
                       entity_id=entity_id, error=str(exc)[:200])
        raise HTTPException(status_code=500, detail="undismiss failed")
    logger.info("descriptions_undismissed", user_id=user_id,
                entity_id=entity_id, count=len(rows))
    return {"restored": len(rows), "who_they_are": "regenerating"}


# ---------------------------------------------------------------
# Woven (Memory tab redesign, 2026-08-31)
# ---------------------------------------------------------------

WOVEN_CANDIDATE_SQL = """
    SELECT cp.patch_id, cp.patch_type, cp.value, cp.origin_id,
           cp.created_at, cp.last_observed_at, cp.completed_at,
           cp.sensitivity, cp.project_id,
           (SELECT COUNT(*) FROM patch_connections pc
             WHERE (pc.from_patch_id = cp.patch_id
                 OR pc.to_patch_id = cp.patch_id)
               AND COALESCE(pc.status, 'active') = 'active') AS edge_count
      FROM context_patches cp
      JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
     WHERE ps.subject_key = $1
       AND COALESCE(cp.status, 'active') = 'active'
       AND cp.created_at >= (NOW() AT TIME ZONE 'utc') - ($2::int * INTERVAL '1 day')
       {PROJECT}
     ORDER BY cp.created_at DESC
     LIMIT 400
"""

# Labels for "stitched to", derived from the LINKED patch rather than
# invented: section 6.4 requires a label that reads as a thing and that
# every link resolve to a patch the user can open. Both directions of
# the edge count, because "informs" and "informed by" are the same
# thread from either end.
WOVEN_LINKS_SQL = """
    SELECT pc.from_patch_id, pc.to_patch_id, pc.connection_role,
           other.patch_id AS other_id,
           other.value->>'text' AS other_text,
           other.patch_type AS other_type
      FROM patch_connections pc
      JOIN context_patches other
        ON other.patch_id = CASE WHEN pc.from_patch_id = ANY($1::uuid[])
                                 THEN pc.to_patch_id ELSE pc.from_patch_id END
     WHERE (pc.from_patch_id = ANY($1::uuid[]) OR pc.to_patch_id = ANY($1::uuid[]))
       AND COALESCE(pc.status, 'active') = 'active'
       AND COALESCE(other.status, 'active') = 'active'
       AND other.patch_type <> 'person'
"""


def _woven_window_days(raw: Optional[str]) -> int:
    """`7d` / `30d` / a bare integer. Anything else is 7.

    Never 4xx on a malformed window: this is a browse surface and a
    typo in a query param should not cost the user their memory tab.
    """
    if not raw:
        return 7
    text = str(raw).strip().lower().rstrip("d")
    try:
        value = int(text)
    except (TypeError, ValueError):
        return 7
    return value if 1 <= value <= 3650 else 7


@app.get("/v1/quilt/{user_id}/woven", tags=["Quilt"])
async def woven_digest(
    user_id: str,
    window: Optional[str] = Query("7d", description="7d, 30d, or a day count"),
    limit: int = Query(6, ge=1, le=60, description="Tiles per page, 1-60"),
    offset: int = Query(0, ge=0, description="Tiles already shown, for paging"),
    project_id: Optional[str] = Query(None, description="Scope by project id"),
    project: Optional[str] = Query(None, description="Scope by project NAME"),
    app_id: str = Depends(verify_application_access),
):
    """The week's quilt: the few patches worth a tile, already ranked.

    The app makes no ranking decisions (Woven handoff section 5), so
    everything here is pruned and ordered before it leaves. Selection,
    scoring and the tile layout live in `services/woven_digest`, which
    is pure, so this route is a fetch and a call.

    TWO TIME BASES IN ONE RESPONSE, and that is deliberate rather than a
    bug to tidy. The header numbers (`total_memories`, `meetings_count`,
    `since`) are LIFETIME, because the screen opens with "2,770 things
    you'd have forgotten" and "SINCE MARCH". The `patches` array is the
    WINDOW, because the grid under it says "THIS WEEK'S PATCHES". Making
    them consistent is the obvious fix and it guts the opening claim.

    Titles, durations and minute marks are absent on purpose: doc 15
    item 5, CQ wins on state and the app wins on content, so
    ShoulderSurf joins `origin_id` to its own records.
    """
    subject_key = f"user:{user_id}"
    days = _woven_window_days(window)

    # BOTH SPELLINGS, and the reason is a failure mode rather than
    # politeness. The handoff's section 5 spells the param `project`
    # while this route grew up with `project_id`, and a project NAME
    # arriving in the id slot matches no row, so the tab renders empty
    # with a 200 and nothing anywhere errors. Recall already takes both
    # for the same reason, so this is the house pattern rather than a
    # new one.
    project_clause = ""
    args: list = [subject_key, days]
    if project_id:
        project_clause = "AND cp.project_id = $3"
        args.append(project_id)
    elif project:
        project_clause = "AND cp.project = $3"
        args.append(project)

    # AND THE REAL FIX IS NOT THE SPELLING. "no such project" and "this
    # project had a quiet week" are the same observable when a filter
    # returns nothing, and only one of them is a bug. So a supplied
    # filter that matches NO patch at all in this user's whole quilt is
    # reported as unknown rather than served as an empty week. Scoped to
    # the user, and deliberately checked against the WHOLE quilt rather
    # than the window: a real project with a quiet week must still read
    # as quiet.
    project_known = None
    if project_id or project:
        col = "project_id" if project_id else "project"
        try:
            project_known = bool(await db_pool.fetchval(
                f"""
                SELECT 1 FROM context_patches cp
                  JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                 WHERE ps.subject_key = $1 AND cp.{col} = $2
                 LIMIT 1
                """,
                subject_key, project_id or project,
            ))
        except Exception as exc:
            # Unknown rather than false: a failed check must not accuse
            # a real project of not existing.
            logger.warning("woven_project_check_failed", error=str(exc)[:200])
            project_known = None

    try:
        rows = await db_pool.fetch(
            WOVEN_CANDIDATE_SQL.replace("{PROJECT}", project_clause), *args)
    except Exception as exc:
        logger.warning("woven_candidates_failed", user_id=user_id,
                       error=str(exc)[:200])
        raise HTTPException(status_code=500, detail="woven fetch failed")

    candidates = [dict(r) for r in rows]
    edge_counts = {str(r["patch_id"]): int(r["edge_count"] or 0) for r in rows}
    # Conduct rows never tile on their own; the person card is where they
    # live. The reason lands in `dropped` so SS can see the count move.
    conduct_types = (await facet_runtime.get_type_runtime(db_pool.fetch)).conduct_types
    digest = woven_digest_svc.build_digest(
        candidates, limit=limit, edge_counts=edge_counts, offset=offset,
        conduct_types=conduct_types)

    await _attach_woven_links(digest["patches"])

    totals = await _woven_lifetime_totals(subject_key)
    # THE SCOPE IS LOGGED BECAUSE ITS ABSENCE COST A RECONSTRUCTION.
    #
    # 2026-08-31: Scott opened a project called "Immigration Interview
    # App" and saw another project's work under it. This line recorded
    # candidates, tiles and window and NOT the project it filtered on,
    # so answering "did a scope arrive, and which one" took running the
    # candidate SQL against every project id this user has until one
    # returned exactly the 29 the log showed. It was CBE's id: the
    # client had sent one project's id under another's heading, and CQ
    # had filtered correctly on what it was given.
    #
    # One line here would have answered it immediately. `project_known`
    # goes too, because "the filter matched nothing" and "this project
    # does not exist for this user" are different answers and only one
    # of them is a client bug.
    logger.info("woven_digest_served", user_id=user_id, window_days=days,
                candidates=len(candidates), tiles=len(digest["patches"]),
                offset=offset, total=digest["tiles_available"],
                project_id=project_id, project=project,
                project_known=project_known,
                dropped=digest["dropped"])
    return {
        **totals,
        "patches": digest["patches"],
        "rows": [list(r) for r in digest["row_pairs"]],
        "window_days": days,
        # Why a thin week is thin, so a client can say so rather than
        # rendering an unexplained empty state.
        "dropped": digest["dropped"],
        # null when no filter was passed, true for a project this user
        # actually has, false when the filter matched nothing anywhere.
        # False plus an empty `patches` means "wrong project", not
        # "quiet week", and the client should say so.
        "project_known": project_known,
        # PAGING. Scott raised the ceiling from 6 to 60 on 2026-08-31,
        # after it was measured that a real week holds 322 eligible
        # tiles for the heaviest user and 125 for the next, and that
        # serving more costs NO model spend: nothing on the read path
        # calls an LLM and headlines are written once at ingest. The
        # real cost is payload, about 713 bytes a tile, which is an
        # argument for paging rather than for a cap.
        #
        # `tiles_available` counts what EARNED a tile, after pruning, so
        # "showing 6 of 265" is honest. A raw candidate count would
        # include rows the quilt can never show and would promise a
        # scroll that ends early. Deliberately NOT named
        # `total_available`: the sibling /v1/quilt/{user_id} route
        # already uses that name for a count taken BEFORE its cap, and
        # GhostPour caught the collision from the middle hop.
        "tiles_available": digest["tiles_available"],
        "offset": digest["offset"],
        "has_more": digest["has_more"],
    }


@app.get("/v1/quilt/{user_id}/meetings/{origin_id}/woven", tags=["Quilt"])
async def woven_meeting_seam(
    user_id: str,
    origin_id: str,
    app_id: str = Depends(verify_application_access),
):
    """One meeting's patches, in CAPTURE ORDER, for the seam screen.

    Not ranked: the seam is a record of a conversation and reordering it
    would break the one thing that screen is for, which is reading the
    meeting back in the order it happened. Same pruning as the home
    quilt so a rejected or sensitive patch cannot appear here either.
    """
    subject_key = f"user:{user_id}"
    try:
        rows = await db_pool.fetch(
            """
            SELECT cp.patch_id, cp.patch_type, cp.value, cp.origin_id,
                   cp.created_at, cp.completed_at, cp.sensitivity
              FROM context_patches cp
              JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
             WHERE ps.subject_key = $1 AND cp.origin_id = $2
               AND COALESCE(cp.status, 'active') = 'active'
             ORDER BY cp.created_at ASC, cp.patch_id ASC
            """,
            subject_key, origin_id,
        )
    except Exception as exc:
        logger.warning("woven_seam_failed", user_id=user_id,
                       origin=origin_id, error=str(exc)[:200])
        raise HTTPException(status_code=500, detail="woven seam fetch failed")

    patches, dropped = [], {}
    for row in rows:
        patch = dict(row)
        reason = woven_digest_svc.why_not_a_tile(patch)
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        # Same JSONB-as-string trap the digest service hit: asyncpg
        # hands `value` back as a JSON STRING unless a codec is
        # registered, so `or {}` yields a str and `.get` explodes or
        # silently misses. One helper, both routes.
        value = woven_digest_svc._value(patch)
        patches.append({
            "patch_id": patch["patch_id"],
            "patch_type": patch["patch_type"],
            "fact": (value.get("text") or "").strip(),
            "headline": value.get("headline") or None,
            "source_meeting_id": patch.get("origin_id"),
            "occurred_at": patch.get("created_at"),
        })

    await _attach_woven_links(patches)
    return {"meeting_id": origin_id, "patches": patches, "dropped": dropped}


async def _attach_woven_links(patches: list) -> None:
    """Add `stitched_to` in place: up to 4 per patch, strongest first.

    `{patch_id, label}` rather than a bare string. The prototype renders
    flat labels in pills that do not navigate, but section 6.4 requires
    every link to resolve to a patch the user can open, so the id ships
    too. The prototype is authoritative about what the screen LOOKS
    like, not about what it does.
    """
    if not patches:
        return
    ids = [str(p["patch_id"]) for p in patches]
    try:
        rows = await db_pool.fetch(WOVEN_LINKS_SQL, ids)
    except Exception as exc:
        # A link is decoration on a fact. Losing it must never cost the
        # fact, so this degrades rather than failing.
        #
        # BUT IT DEGRADES TO NULL, NOT TO AN ABSENT KEY. Returning here
        # used to leave `stitched_to` off EVERY patch, so a failed link
        # query and a patch with no links were one observable and the
        # wire shape varied under a condition no client can see.
        # ShoulderSurf hit the consequence from the other side: a
        # decoder requiring the key threw, the fetch returned nil, and
        # the screen fell back to its local builder, which on a device
        # is INDISTINGUISHABLE FROM A 404. A first successful deploy
        # could have read as a route that was never shipped.
        #
        # Three states, the same ones doc 16 uses for `capabilities`: a
        # list means these are the links, `[]` means none, null means
        # CQ could not tell.
        logger.warning("woven_links_failed", error=str(exc)[:200])
        for patch in patches:
            patch["stitched_to"] = None
        return

    by_patch: dict = {}
    for r in rows:
        for side in ("from_patch_id", "to_patch_id"):
            pid = str(r[side])
            if pid in ids and str(r["other_id"]) != pid:
                by_patch.setdefault(pid, []).append(r)

    for patch in patches:
        pid = str(patch["patch_id"])
        seen, links = set(), []
        for r in by_patch.get(pid, []):
            other = str(r["other_id"])
            if other in seen:
                continue
            seen.add(other)
            links.append({
                "patch_id": other,
                "label": woven_digest_svc.stitch_label(r["other_text"]),
            })
            if len(links) >= 4:
                break
        patch["stitched_to"] = links


async def _woven_lifetime_totals(subject_key: str) -> dict:
    """The header's numbers, which are ALL-TIME and not the window."""
    try:
        row = await db_pool.fetchrow(
            """
            SELECT COUNT(*) AS memories,
                   COUNT(DISTINCT cp.origin_id) AS meetings,
                   MIN(cp.created_at) AS since
              FROM context_patches cp
              JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
             WHERE ps.subject_key = $1
               AND COALESCE(cp.status, 'active') = 'active'
            """,
            subject_key,
        )
    except Exception as exc:
        logger.warning("woven_totals_failed", error=str(exc)[:200])
        return {"total_memories": None, "meetings_count": None, "since": None}
    return {
        "total_memories": int(row["memories"] or 0),
        "meetings_count": int(row["meetings"] or 0),
        "since": row["since"].isoformat() if row["since"] else None,
    }


@app.post("/v1/people/{user_id}/merge", tags=["People"])
async def merge_people(
    user_id: str,
    req: PeopleMergeRequest,
    app_id: str = Depends(verify_application_access),
):
    """
    Record that two or more person entities are the same human.

    Each losing entity's name becomes an alias of the canonical, its
    aliases and relationships repoint, its mention counts fold in, and it
    is marked merged rather than deleted.

    Refuses (409) any pair the user has previously answered "keep
    separate" — the whole batch, not just the offending pair, because
    quietly merging part of a batch while reporting success is how you end
    up investigating why CQ thinks two people are one.
    """
    try:
        canonical_id, loser_ids = normalise_merge_request(
            req.canonical_entity_id, req.merge_entity_ids
        )
        source = resolve_identity_source(req.source)
    except IdentityRequestError as e:
        raise _identity_error(e)

    subject_key = f"user:{user_id}"
    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        async with conn.transaction():
            try:
                canonical = await _load_active_person(
                    conn, user_id, canonical_id, vocab.person_entity_type
                )
                if canonical is None:
                    raise HTTPException(
                        status_code=404,
                        detail=f"Person {canonical_id} not found for this user",
                    )
                canonical_uuid = str(canonical["entity_id"])

                losers = []
                for lid in loser_ids:
                    row = await _load_active_person(
                        conn, user_id, lid, vocab.person_entity_type
                    )
                    if row is None:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Person {lid} not found for this user",
                        )
                    # Already resolved to the canonical (a re-sent request,
                    # or a chain that already collapsed) — nothing to do.
                    if str(row["entity_id"]) == canonical_uuid:
                        continue
                    losers.append(row)
            except IdentityRequestError as e:
                raise _identity_error(e)

            if not losers:
                return {
                    "status": "noop",
                    "canonical_entity_id": canonical_uuid,
                    "merged": [],
                    "reason": "all requested entities already resolve to the canonical",
                }

            loser_uuids = [str(r["entity_id"]) for r in losers]
            # DIRECTION CHECK. Folding a bigger relationship into a
            # smaller one relocates identity: the surviving entity_id
            # changes, SS's navigation goes stale, and our own
            # `source_entity_id` insight references point at the row
            # that lost.
            #
            # Refusing by default became correct only once
            # `canonical_name` existed, earlier the same day. Before
            # that, sending the smaller row as canonical was the ONLY
            # way to end up with its name, so it was a legitimate if
            # lossy move. Now the name is independent, and picking the
            # smaller survivor buys nothing.
            #
            # SS hit this twice in an hour: once in the original
            # `commitMerge`, and again in the fix, because a property
            # they believed ranked by relationship size actually ranked
            # by token count of the NAME, so "Pallavi Kandanu" outranked
            # "Pallavi" while holding 4 meetings against 88. Audited on
            # prod first: 13 merges on record, none in the wrong
            # direction, so this guard rejects nothing that has ever
            # happened.
            sizes = {
                str(r["entity_id"]): r["meetings"]
                for r in await conn.fetch(
                    """
                    SELECT e.entity_id,
                           (SELECT count(*) FROM person_appearances pa
                             WHERE pa.entity_id = e.entity_id) AS meetings
                    FROM entities e WHERE e.entity_id = ANY($1::uuid[])
                    """,
                    [canonical_uuid] + loser_uuids,
                )
            }
            canonical_size = sizes.get(canonical_uuid, 0)
            bigger = [
                {"entity_id": lid, "meetings": sizes.get(lid, 0)}
                for lid in loser_uuids
                if sizes.get(lid, 0) > canonical_size
            ]
            if bigger and not req.allow_smaller_canonical:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "MERGE_DIRECTION",
                        "message": (
                            "The surviving person has fewer meetings than one "
                            "being folded into them, which relocates identity. "
                            "Send the larger row as canonical_entity_id, and "
                            "canonical_name to keep the other name."
                        ),
                        "canonical_entity_id": canonical_uuid,
                        "canonical_meetings": canonical_size,
                        "larger_entities": bigger,
                    },
                )

            separated = await _read_separations(
                conn, user_id, [canonical_uuid] + loser_uuids
            )
            conflicts = separation_conflicts(canonical_uuid, loser_uuids, separated)
            if conflicts and req.override_separation:
                # The user has been shown the 409 and said merge anyway.
                # Drop the record: it has been contradicted, and keeping
                # it would refuse the next merge on an answer they have
                # already changed.
                for a, b in conflicts:
                    lo, hi = canonical_pair(a, b)
                    await conn.execute(
                        """
                        DELETE FROM entity_separations
                        WHERE user_id = $1 AND entity_id_lo = $2::uuid
                          AND entity_id_hi = $3::uuid
                        """,
                        user_id, lo, hi,
                    )
                logger.info(
                    "separation_overturned", user_id=user_id,
                    pairs=[[a, b] for a, b in conflicts], source=source,
                )
                conflicts = []
            if conflicts:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "SEPARATION_CONFLICT",
                        "message": (
                            "These entities were previously kept separate by the user. "
                            "Merging would silently overturn that answer."
                        ),
                        "pairs": [
                            {"canonical_entity_id": a, "entity_id": b}
                            for a, b in conflicts
                        ],
                    },
                )

            for loser in losers:
                loser_uuid = str(loser["entity_id"])

                # 1. The loser's name becomes an alias of the canonical.
                #    DO UPDATE rather than DO NOTHING: if the surface form
                #    was pointing somewhere else, the user just told us
                #    where it actually belongs.
                await conn.execute(
                    """
                    INSERT INTO entity_aliases (user_id, entity_id, alias, source)
                    VALUES ($1, $2::uuid, $3, $4)
                    ON CONFLICT (user_id, LOWER(alias))
                    DO UPDATE SET entity_id = EXCLUDED.entity_id,
                                  source = EXCLUDED.source
                    """,
                    user_id, canonical_uuid, loser["name"], source,
                )

                # 2. The loser's own aliases follow it. The unique index is
                #    on (user_id, LOWER(alias)) so a repoint cannot collide.
                await conn.execute(
                    "UPDATE entity_aliases SET entity_id = $1::uuid "
                    "WHERE user_id = $2 AND entity_id = $3::uuid",
                    canonical_uuid, user_id, loser_uuid,
                )

                # 3. Relationships repoint, skipping edges the canonical
                #    already has (relationships is UNIQUE on
                #    user_id + from + to + type, so a blind UPDATE would
                #    throw on any overlapping neighborhood).
                for column in ("from_entity_id", "to_entity_id"):
                    other = "to_entity_id" if column == "from_entity_id" else "from_entity_id"
                    await conn.execute(
                        f"""
                        UPDATE relationships r SET {column} = $1::uuid
                        WHERE r.user_id = $2 AND r.{column} = $3::uuid
                          AND NOT EXISTS (
                              SELECT 1 FROM relationships r2
                              WHERE r2.user_id = r.user_id
                                AND r2.{column} = $1::uuid
                                AND r2.{other} = r.{other}
                                AND r2.relationship_type = r.relationship_type
                          )
                        """,
                        canonical_uuid, user_id, loser_uuid,
                    )

                # 4. Whatever is still attached to the loser is a true
                #    duplicate of an edge the canonical already had, plus
                #    any loser->canonical edge that just became a self
                #    loop. Both are dropped.
                await conn.execute(
                    """
                    DELETE FROM relationships
                    WHERE user_id = $1
                      AND (from_entity_id = $2::uuid OR to_entity_id = $2::uuid)
                    """,
                    user_id, loser_uuid,
                )
                await conn.execute(
                    "DELETE FROM relationships WHERE user_id = $1 "
                    "AND from_entity_id = $2::uuid AND to_entity_id = $2::uuid",
                    user_id, canonical_uuid,
                )

                # 4b. Meeting history follows the person. Without this a
                #     merge silently costs the canonical every meeting the
                #     folded identity was seen in, which is the opposite of
                #     what "these are the same human" means. Two identities
                #     seen in the SAME meeting collapse to one appearance,
                #     because that is still one meeting.
                #
                #     Unguarded on purpose: if person_appearances is
                #     missing, a 500 is the right answer. Swallowing it
                #     would mangle history quietly.
                await conn.execute(
                    """
                    INSERT INTO person_appearances
                        (user_id, entity_id, origin_id, origin_type,
                         project_id, first_seen_at, last_seen_at, capacities,
                         turn_count)
                    SELECT user_id, $1::uuid, origin_id, origin_type,
                           project_id, first_seen_at, last_seen_at, capacities,
                           turn_count
                    FROM person_appearances
                    WHERE user_id = $2 AND entity_id = $3::uuid
                    ON CONFLICT (user_id, entity_id, origin_id) DO UPDATE SET
                        first_seen_at = LEAST(person_appearances.first_seen_at,
                                              EXCLUDED.first_seen_at),
                        last_seen_at  = GREATEST(person_appearances.last_seen_at,
                                                 EXCLUDED.last_seen_at),
                        project_id    = COALESCE(person_appearances.project_id,
                                                 EXCLUDED.project_id),
                        -- Same-meeting fold keeps the MAX turn count (one
                        -- human, two labels: 41 turns + 1 turn is a 41-turn
                        -- human, not 42); NULL never clobbers a known value.
                        turn_count    = CASE
                            WHEN EXCLUDED.turn_count IS NULL THEN person_appearances.turn_count
                            ELSE GREATEST(COALESCE(person_appearances.turn_count, 0), EXCLUDED.turn_count)
                        END,
                        -- Union rather than replace. If the loser was a
                        -- speaker in a meeting where the canonical was only
                        -- mentioned, the merged person was demonstrably in
                        -- the room, and dropping that would weaken the very
                        -- signal the next merge decision reads.
                        capacities    = ARRAY(SELECT DISTINCT unnest(
                                            person_appearances.capacities
                                            || EXCLUDED.capacities))
                    """,
                    canonical_uuid, user_id, loser_uuid,
                )
                await conn.execute(
                    "DELETE FROM person_appearances WHERE user_id = $1 AND entity_id = $2::uuid",
                    user_id, loser_uuid,
                )

                # 5. Separations the loser was party to survive against the
                #    canonical. Without this, "A is not B" silently stops
                #    applying the moment B merges into C. Re-inserted rather
                #    than updated because the (lo, hi) ordering has to be
                #    recomputed against the new id.
                loser_seps = await conn.fetch(
                    """
                    SELECT entity_id_lo, entity_id_hi, source FROM entity_separations
                    WHERE user_id = $1
                      AND (entity_id_lo = $2::uuid OR entity_id_hi = $2::uuid)
                    """,
                    user_id, loser_uuid,
                )
                await conn.execute(
                    """
                    DELETE FROM entity_separations
                    WHERE user_id = $1
                      AND (entity_id_lo = $2::uuid OR entity_id_hi = $2::uuid)
                    """,
                    user_id, loser_uuid,
                )
                for sep in loser_seps:
                    lo, hi = str(sep["entity_id_lo"]), str(sep["entity_id_hi"])
                    other_id = hi if lo == loser_uuid else lo
                    if other_id == canonical_uuid:
                        continue  # would be a self-pair; the merge settled it
                    new_lo, new_hi = canonical_pair(canonical_uuid, other_id)
                    await conn.execute(
                        """
                        INSERT INTO entity_separations
                            (user_id, entity_id_lo, entity_id_hi, source)
                        VALUES ($1, $2::uuid, $3::uuid, $4)
                        ON CONFLICT DO NOTHING
                        """,
                        user_id, new_lo, new_hi, sep["source"],
                    )

                # 6. Fold the observation history forward. Canonical wins on
                #    metadata key collisions (its own keys applied last).
                await conn.execute(
                    """
                    UPDATE entities SET
                        mention_count = mention_count + $1,
                        first_seen_at = LEAST(first_seen_at, $2),
                        last_seen_at  = GREATEST(last_seen_at, $3),
                        description   = COALESCE(NULLIF(description, ''), $4),
                        metadata      = COALESCE($5::jsonb, '{}'::jsonb) || COALESCE(metadata, '{}'::jsonb)
                    WHERE entity_id = $6::uuid
                    """,
                    loser["mention_count"] or 0,
                    loser["first_seen_at"], loser["last_seen_at"],
                    loser["description"],
                    json.dumps(loser["metadata"]) if isinstance(loser["metadata"], dict) else (loser["metadata"] or "{}"),
                    canonical_uuid,
                )

                # 7. Forward pointer. The row survives so held ids resolve.
                await conn.execute(
                    """
                    UPDATE entities
                    SET merged_into = $1::uuid, merged_at = NOW()
                    WHERE entity_id = $2::uuid
                    """,
                    canonical_uuid, loser_uuid,
                )

            # 8. Fold duplicate person PATCHES (doc 16 section 6.5).
            #
            #    Merging entities alone left the quilt showing two Sarahs
            #    one segment over: `person` is a rendered patch type and
            #    GET /v1/quilt applies no type exclusion, so a user who
            #    merged in People still saw both in Memory. That is the
            #    same split brain this feature exists to prevent,
            #    reappearing inside the same screen.
            #
            #    Patches join to entities only by case-insensitive name,
            #    so the fold set is every person patch matching the
            #    canonical or any folded identity, including their
            #    aliases.
            names = {canonical["name"]}
            names.update(str(r["alias"]) for r in await conn.fetch(
                "SELECT alias FROM entity_aliases WHERE user_id = $1 AND entity_id = $2::uuid",
                user_id, canonical_uuid,
            ))
            for loser in losers:
                names.add(loser["name"])
            keys = [n.strip().lower() for n in names if n and n.strip()]

            candidates = [dict(r) for r in await conn.fetch(
                """
                SELECT cp.patch_id, cp.value->>'text' AS text, cp.created_at
                FROM context_patches cp
                JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                WHERE ps.subject_key = $1
                  AND cp.patch_type = $3
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND LOWER(cp.value->>'text') = ANY($2::text[])
                ORDER BY cp.created_at
                """,
                subject_key, keys, vocab.person_type,
            )]
            folded_patch_ids, _items_moved = await _fold_person_patches(
                conn, candidates, canonical["name"], source
            )

            # A user-asserted merge also vouches for the canonical.
            await conn.execute(
                """
                UPDATE entities
                SET confirmed_at = COALESCE(confirmed_at, NOW()),
                    confirmation_source = COALESCE(confirmation_source, $1)
                WHERE entity_id = $2::uuid
                """,
                source, canonical_uuid,
            )

            # WHICH NAME SURVIVES, if the caller chose one. Runs LAST,
            # inside the same transaction, so the losers' names are
            # already aliases and the chosen one may be any of them.
            #
            # Identity does not move: the canonical row stays canonical
            # and keeps its id. Only the display name changes, and the
            # name it had becomes an alias, so recall by either spelling
            # keeps working and nothing holding the id is disturbed.
            renamed_to = None
            if (req.canonical_name or "").strip():
                wanted = req.canonical_name.strip()
                current = await conn.fetchval(
                    "SELECT name FROM entities WHERE entity_id = $1::uuid",
                    canonical_uuid,
                )
                known = {(current or "").strip().lower()}
                known |= {
                    (r["alias"] or "").strip().lower()
                    for r in await conn.fetch(
                        "SELECT alias FROM entity_aliases "
                        "WHERE user_id = $1 AND entity_id = $2::uuid",
                        user_id, canonical_uuid,
                    )
                }
                if wanted.lower() not in known:
                    # Refused rather than ignored. Silently keeping the
                    # old name is the bug this field exists to fix, so
                    # an unrecognised choice must not do the same thing
                    # by a different route.
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "UNKNOWN_CANONICAL_NAME",
                            "message": (
                                "canonical_name must be a name or alias of "
                                "one of the people being merged."
                            ),
                            "canonical_name": wanted,
                            "known": sorted(k for k in known if k),
                        },
                    )
                if wanted.lower() != (current or "").strip().lower():
                    await conn.execute(
                        "DELETE FROM entity_aliases WHERE user_id = $1 "
                        "AND entity_id = $2::uuid AND LOWER(alias) = LOWER($3)",
                        user_id, canonical_uuid, wanted,
                    )
                    await conn.execute(
                        """
                        INSERT INTO entity_aliases (user_id, entity_id, alias, source)
                        VALUES ($1, $2::uuid, $3, $4)
                        ON CONFLICT (user_id, LOWER(alias))
                        DO UPDATE SET entity_id = EXCLUDED.entity_id,
                                      source = EXCLUDED.source
                        """,
                        user_id, canonical_uuid, current, source,
                    )
                    await conn.execute(
                        "UPDATE entities SET name = $1 WHERE entity_id = $2::uuid",
                        wanted, canonical_uuid,
                    )
                    # The person patches say what the entity says, the
                    # same match set the rename route uses: name plus
                    # every alias, because a patch whose text is an old
                    # spelling is still this person's patch.
                    keys = list(known)
                    await conn.execute(
                        """
                        UPDATE context_patches cp
                        SET value = jsonb_set(value, '{text}', to_jsonb($1::text)),
                            updated_at = NOW()
                        FROM patch_subjects ps
                        WHERE ps.patch_id = cp.patch_id
                          AND ps.subject_key = $2
                          AND cp.patch_type = $4
                          AND COALESCE(cp.status, 'active') = 'active'
                          AND LOWER(cp.value->>'text') = ANY($3::text[])
                        """,
                        wanted, subject_key, keys, vocab.person_type,
                    )
                    renamed_to = wanted

            alias_rows = await conn.fetch(
                "SELECT alias FROM entity_aliases WHERE user_id = $1 AND entity_id = $2::uuid ORDER BY alias",
                user_id, canonical_uuid,
            )

    await _rebuild_entity_index(user_id)
    logger.info(
        "people_merged", user_id=user_id, app_id=str(app_id),
        canonical_entity_id=canonical_uuid, merged=loser_uuids, source=source,
        renamed_to=renamed_to,
    )
    return {
        "status": "merged",
        "canonical_entity_id": canonical_uuid,
        # The name AFTER any rename, not the one captured before the
        # merge began. Echoing the stale one would tell a client the
        # choice was ignored, or that it landed when it did not, which
        # is the whole class of bug this field was added to fix.
        "canonical_name": renamed_to or canonical["name"],
        "renamed": renamed_to is not None,
        "merged": loser_uuids,
        "aliases": [r["alias"] for r in alias_rows],
        # Archived duplicate person patches. They ride the delta-sync
        # `deleted` array too; named here so a caller does not have to
        # diff a sync to find out what a merge did (doc 16 section 6.5).
        "folded_patch_ids": folded_patch_ids,
    }


@app.post("/v1/people/{user_id}/keep-separate", tags=["People"])
async def keep_people_separate(
    user_id: str,
    req: KeepSeparateRequest,
    app_id: str = Depends(verify_application_access),
):
    """
    Record that two person entities are NOT the same human.

    The negative answer has to be as durable as the positive one. Without
    it the merge endpoint, scripts/backfill_entity_aliases.py, and any
    future merge-proposal read will keep re-offering a merge the user has
    already refused.

    Idempotent. Refuses (409) a pair that is already merged: undoing a
    merge is a different operation and does not exist yet.
    """
    if not req.entity_ids or len(req.entity_ids) != 2:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_PAIR",
                "message": "entity_ids must contain exactly two entity ids",
            },
        )

    source = resolve_identity_source(req.source)

    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        async with conn.transaction():
            try:
                raw_a, raw_b = req.entity_ids
                # Resolve BEFORE pairing: separating two ids that both
                # already point at one canonical is the merged case, not a
                # legitimate pair.
                a_row = await _load_active_person(
                    conn, user_id, raw_a, vocab.person_entity_type
                )
                b_row = await _load_active_person(
                    conn, user_id, raw_b, vocab.person_entity_type
                )
                if a_row is None or b_row is None:
                    missing = raw_a if a_row is None else raw_b
                    raise HTTPException(
                        status_code=404,
                        detail=f"Person {missing} not found for this user",
                    )
                a_id, b_id = str(a_row["entity_id"]), str(b_row["entity_id"])
                if a_id == b_id:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "ALREADY_MERGED",
                            "message": (
                                "These ids already resolve to one entity. "
                                "Separating a merged pair is not supported."
                            ),
                            "entity_id": a_id,
                        },
                    )
                lo, hi = canonical_pair(a_id, b_id)
            except IdentityRequestError as e:
                raise _identity_error(e)

            await conn.execute(
                """
                INSERT INTO entity_separations
                    (user_id, entity_id_lo, entity_id_hi, source)
                VALUES ($1, $2::uuid, $3::uuid, $4)
                ON CONFLICT (user_id, entity_id_lo, entity_id_hi) DO NOTHING
                """,
                user_id, lo, hi, source,
            )

            # Saying "these are two different people" vouches for both.
            await conn.execute(
                """
                UPDATE entities
                SET confirmed_at = COALESCE(confirmed_at, NOW()),
                    confirmation_source = COALESCE(confirmation_source, $1)
                WHERE entity_id = ANY($2::uuid[])
                """,
                source, [lo, hi],
            )

    logger.info(
        "people_kept_separate", user_id=user_id, app_id=str(app_id),
        entity_id_lo=lo, entity_id_hi=hi, source=source,
    )
    return {
        "status": "separated",
        "entity_ids": [lo, hi],
        "source": source,
    }


@app.post("/v1/people/{user_id}/{entity_id}/confirm", tags=["People"])
async def confirm_person(
    user_id: str,
    entity_id: str,
    req: Optional[ConfirmPersonRequest] = None,
    app_id: str = Depends(verify_application_access),
):
    """
    Mark a person a human has vouched for.

    This is the unconfirmed-to-confirmed transition for someone CQ
    inferred from a transcript. Idempotent: the first confirmation's
    timestamp and source stick, so re-confirming does not rewrite history.
    """
    source = resolve_identity_source(req.source if req else None)

    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        async with conn.transaction():
            try:
                row = await _load_active_person(
                    conn, user_id, entity_id, vocab.person_entity_type
                )
            except IdentityRequestError as e:
                raise _identity_error(e)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Person {entity_id} not found for this user",
                )
            resolved = str(row["entity_id"])
            updated = await conn.fetchrow(
                """
                UPDATE entities
                SET confirmed_at = COALESCE(confirmed_at, NOW()),
                    confirmation_source = COALESCE(confirmation_source, $1)
                WHERE entity_id = $2::uuid
                RETURNING name, confirmed_at, confirmation_source
                """,
                source, resolved,
            )

            # A confirmation is an identity assertion, so it does the
            # identity write. Before this it stamped a timestamp and
            # nothing else, which is why four people could be confirmed
            # by a human and still hold four or five person patches
            # each (measured 2026-08-16). The user had answered the
            # question and the answer changed nothing.
            #
            # Same fold the merge does, because they are the same write:
            # merge says two people are one, confirm says this person is
            # who we think, and both mean one human owns every surface
            # form the extractor minted for them.
            alias_names = [r["alias"] for r in await conn.fetch(
                "SELECT alias FROM entity_aliases "
                "WHERE user_id = $1 AND entity_id = $2::uuid",
                user_id, resolved,
            )]
            keys = sorted(owner_keys(updated["name"], alias_names))
            candidates = [dict(r) for r in await conn.fetch(
                """
                SELECT cp.patch_id, cp.value->>'text' AS text, cp.created_at
                FROM context_patches cp
                JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
                WHERE ps.subject_key = $1
                  AND cp.patch_type = $3
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND LOWER(cp.value->>'text') = ANY($2::text[])
                ORDER BY cp.created_at
                """,
                f"user:{user_id}", keys, vocab.person_type,
            )]
            folded_patch_ids, items_moved = await _fold_person_patches(
                conn, candidates, updated["name"], source
            )

    await _rebuild_entity_index(user_id)
    logger.info(
        "person_confirmed", user_id=user_id, app_id=str(app_id),
        entity_id=resolved, source=source, folded=len(folded_patch_ids),
    )
    return {
        "status": "confirmed",
        "entity_id": resolved,
        "name": updated["name"],
        "confirmed_at": updated["confirmed_at"].isoformat() if updated["confirmed_at"] else None,
        "confirmation_source": updated["confirmation_source"],
        # What the tap actually did. A confirmation that reports nothing
        # is indistinguishable from one that did nothing, which is
        # exactly how this surface read before: the user answers a
        # question and the app has no way to say what changed.
        "folded_patch_ids": folded_patch_ids,
        "folded_count": len(folded_patch_ids),
        # The number worth showing a human. A name variant is CQ
        # bookkeeping; the items it carried are what the user recognises
        # as theirs, and "brought 37 more items under Vijay" is an answer
        # to "what did that buy me" where "folded 1 variant" is not.
        "items_moved": items_moved,
    }


@app.post("/v1/people/{user_id}/{entity_id}/rename", tags=["People"])
async def rename_person(
    user_id: str,
    entity_id: str,
    req: PersonRenameRequest,
    app_id: str = Depends(verify_application_access),
):
    """
    Change a person's display name. A display-name update, not an
    identity operation: the entity_id is untouched, and everything keyed
    on it (appearances, relationships, separations) is unaffected.

    Rename is bigger than a column update because person patches join to
    entities BY NAME (there is no entity_id on context_patches), so a
    name change that stopped there would orphan the person's patch from
    its entity. One transaction does all of it:

      1. The OLD name becomes an alias, so recall still matches it and a
         future transcript saying the old name resolves here instead of
         minting a duplicate person.
      2. The entity's name changes.
      3. Every active person patch matching the old name or any alias is
         rewritten to the new name, with an `updated_at` bump so the
         change rides the next delta (SS holds no person patch_ids and
         re-decodes, so a text change is just an update to them).
      4. A rename vouches for the person, same reasoning as merge and
         keep-separate: typing someone's actual name is asserting who
         they are.
      5. The Redis entity index rebuilds.

    Deliberately NOT touched: `value.owner` strings on ledger items stay
    the raw extracted surface form (doc 16 section 8b: raw, never
    canonicalised; the ledger keeps matching them through the alias).

    Refuses (409 NAME_TAKEN) a name that already belongs to another
    person, by name or by alias: making two people share a name is a
    merge question, not a rename, and answering it here would silently
    overturn any keep-separate the user recorded. Renaming to one of the
    person's OWN aliases promotes it: the alias becomes the name and the
    name becomes an alias (swapping display preference between known
    surface forms).

    422 for placeholder names ("Speaker 3") and for the user's own name
    (the quilt owner is the root of the graph, not a person node in it;
    every sanitizer fights self-person patches and rename must not mint
    one).
    """
    try:
        new_name = validate_person_name(req.name)
        source = resolve_identity_source(req.source)
    except IdentityRequestError as e:
        raise _identity_error(e)

    subject_key = f"user:{user_id}"
    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        async with conn.transaction():
            profile = await conn.fetchrow(
                "SELECT display_name FROM profiles WHERE user_id = $1", user_id
            )
            if is_user_reference(new_name, profile["display_name"] if profile else None):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "SELF_NAME",
                        "message": (
                            f"'{new_name}' refers to the quilt owner. The user is not "
                            "a person node; renaming someone into them would create "
                            "the self-person every write path filters out."
                        ),
                    },
                )

            try:
                row = await _load_active_person(
                    conn, user_id, entity_id, vocab.person_entity_type
                )
            except IdentityRequestError as e:
                raise _identity_error(e)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Person {entity_id} not found for this user",
                )
            resolved = str(row["entity_id"])
            old_name = row["name"]

            if new_name == old_name:
                return {
                    "status": "noop",
                    "entity_id": resolved,
                    "name": old_name,
                    "reason": "name unchanged",
                }

            # Collision: the new name must not already be another person,
            # by entity name or by alias. Aliases resolve forward first, so
            # an alias whose owner merged away compares against the
            # canonical it became, not the dead row.
            other_entity = await conn.fetchrow(
                """
                SELECT entity_id, name FROM entities
                WHERE user_id = $1 AND entity_type = $4
                  AND merged_into IS NULL
                  AND LOWER(name) = LOWER($2)
                  AND entity_id <> $3::uuid
                """,
                user_id, new_name, resolved, vocab.person_entity_type,
            )
            alias_owner = None
            alias_row = await conn.fetchrow(
                "SELECT entity_id FROM entity_aliases "
                "WHERE user_id = $1 AND LOWER(alias) = LOWER($2)",
                user_id, new_name,
            )
            if alias_row is not None:
                owner_row = await _load_active_person(
                    conn, user_id, str(alias_row["entity_id"]),
                    vocab.person_entity_type,
                )
                if owner_row is not None and str(owner_row["entity_id"]) != resolved:
                    alias_owner = owner_row
            taken_by = other_entity or alias_owner
            if taken_by is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "NAME_TAKEN",
                        "message": (
                            f"'{new_name}' already belongs to another person. If they "
                            "are the same human, that is a merge, not a rename."
                        ),
                        "entity_id": str(taken_by["entity_id"]),
                        "name": taken_by["name"],
                    },
                )

            case_only = new_name.lower() == old_name.lower()

            # Promotion: renaming to one of the person's own aliases makes
            # that alias the name, so the alias row retires (the unique
            # index on (user_id, LOWER(alias)) would otherwise collide
            # with the old-name insert below only in the case-only case,
            # but an alias identical to the display name is dead weight in
            # every case).
            await conn.execute(
                "DELETE FROM entity_aliases "
                "WHERE user_id = $1 AND entity_id = $2::uuid AND LOWER(alias) = LOWER($3)",
                user_id, resolved, new_name,
            )

            # The old name stays reachable as an alias, except on a
            # case-only rename, where old and new are the same surface
            # form and an alias would duplicate the name itself.
            if not case_only:
                await conn.execute(
                    """
                    INSERT INTO entity_aliases (user_id, entity_id, alias, source)
                    VALUES ($1, $2::uuid, $3, $4)
                    ON CONFLICT (user_id, LOWER(alias))
                    DO UPDATE SET entity_id = EXCLUDED.entity_id,
                                  source = EXCLUDED.source
                    """,
                    user_id, resolved, old_name, source,
                )

            # A merged-away row keeps its name as a forward pointer, so
            # the collision check above rightly ignores it, and then the
            # UPDATE below walks into it on the (user, name, type) unique
            # index. Receipt 2026-08-21: "Pallavi Kandanur" was merged
            # INTO "Pallavi" (the bare form won the merge), and renaming
            # the survivor back to her full name 500d against her own
            # ghost. Swap instead of fail: the ghost parks on a unique
            # placeholder, the survivor takes the name, then the ghost
            # takes the survivor's OLD name, so an exact-hit lookup on the
            # old form still forwards to the survivor through merged_into
            # and nothing a user can type stops resolving. Case-only
            # renames leave the ghost parked: old and new are the same
            # surface form and the alias insert above already covers it.
            ghosts = await conn.fetch(
                """
                SELECT entity_id FROM entities
                WHERE user_id = $1 AND entity_type = $3
                  AND merged_into IS NOT NULL
                  AND LOWER(name) = LOWER($2)
                """,
                user_id, new_name, vocab.person_entity_type,
            )
            for g in ghosts:
                await conn.execute(
                    "UPDATE entities SET name = $1 WHERE entity_id = $2::uuid",
                    f"{new_name} [merged {str(g['entity_id'])[:8]}]", g["entity_id"],
                )

            await conn.execute(
                "UPDATE entities SET name = $1 WHERE entity_id = $2::uuid",
                new_name, resolved,
            )

            if ghosts and not case_only:
                # Only the first ghost can take the old name (unique
                # index); any further ones stay parked, which is harmless
                # because they are dead rows whose forward pointer is
                # what matters.
                await conn.execute(
                    "UPDATE entities SET name = $1 WHERE entity_id = $2::uuid",
                    old_name, ghosts[0]["entity_id"],
                )

            # Rewrite the person patch(es). The match set is name plus
            # aliases, the same join _people_core and the merge fold use:
            # a patch whose text is an old alias is still this person's
            # patch, and after a rename it should say what the entity
            # says. updated_at bump = the change rides the next delta.
            alias_names = [
                r["alias"] for r in await conn.fetch(
                    "SELECT alias FROM entity_aliases WHERE user_id = $1 AND entity_id = $2::uuid",
                    user_id, resolved,
                )
            ]
            keys = list({n.strip().lower() for n in [old_name, *alias_names] if n and n.strip()})
            renamed = await conn.fetch(
                """
                UPDATE context_patches cp
                SET value = jsonb_set(value, '{text}', to_jsonb($1::text)),
                    updated_at = NOW()
                FROM patch_subjects ps
                WHERE ps.patch_id = cp.patch_id
                  AND ps.subject_key = $2
                  AND cp.patch_type = $4
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND LOWER(cp.value->>'text') = ANY($3::text[])
                RETURNING cp.patch_id
                """,
                new_name, subject_key, keys, vocab.person_type,
            )

            # A rename vouches, same as merge and keep-separate.
            await conn.execute(
                """
                UPDATE entities
                SET confirmed_at = COALESCE(confirmed_at, NOW()),
                    confirmation_source = COALESCE(confirmation_source, $1)
                WHERE entity_id = $2::uuid
                """,
                source, resolved,
            )

            alias_rows = await conn.fetch(
                "SELECT alias FROM entity_aliases WHERE user_id = $1 AND entity_id = $2::uuid ORDER BY alias",
                user_id, resolved,
            )

    await _rebuild_entity_index(user_id)
    logger.info(
        "person_renamed", user_id=user_id, app_id=str(app_id),
        entity_id=resolved, old_name=old_name, new_name=new_name, source=source,
        renamed_patch_count=len(renamed),
    )
    return {
        "status": "renamed",
        "entity_id": resolved,
        "name": new_name,
        "old_name": old_name,
        "aliases": [r["alias"] for r in alias_rows],
        "renamed_patch_ids": [str(r["patch_id"]) for r in renamed],
    }


@app.post("/v1/people/{user_id}/{entity_id}/not-a-person", tags=["People"])
async def suppress_person(
    user_id: str,
    entity_id: str,
    req: Optional[ConfirmPersonRequest] = None,
    app_id: str = Depends(verify_application_access),
):
    """
    "This was never a person": suppress an entity the extractor minted
    from ASR garbage ("Horm Hel"), the missing verb once Delete Memory
    left person rows (boundary piece 3, 2026-08-11).

    The SUPPRESSED ROW IS THE NEGATIVE RECORD: the entity survives,
    marked, so the next transcript emitting the same surface form
    exact-matches the suppressed row instead of minting the garbage
    again (the durable-no lesson keep-separate taught). What changes:

    - excluded from /v1/people (with a tombstone in the list delta's
      `deleted` array, same as a merge),
    - its names and aliases leave the recall entity index,
    - appearance recording stops (the row absorbs re-observations,
      nothing it absorbs is served),
    - its person patches archive with archive_cause 'not_a_person' and
      ride the quilt delta's `deleted` array,
    - every identity verb refuses it with code SUPPRESSED until lifted.

    Idempotent. Reversible via DELETE (ASR garbage and a real person
    with an unfortunate transcription can collide; an unfixable wrong
    answer is worse than a reversible one).
    """
    source = resolve_identity_source(req.source if req else None)
    subject_key = f"user:{user_id}"
    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        async with conn.transaction():
            try:
                row = await _load_active_person(
                    conn, user_id, entity_id, vocab.person_entity_type
                )
            except IdentityRequestError as e:
                if e.code == "SUPPRESSED":
                    return {"status": "suppressed", "entity_id": e.extra.get("entity_id"),
                            "already": True, "archived_patch_ids": []}
                raise _identity_error(e)
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Person {entity_id} not found for this user",
                )
            resolved = str(row["entity_id"])

            await conn.execute(
                "UPDATE entities SET suppressed_at = NOW(), suppressed_source = $1 "
                "WHERE entity_id = $2::uuid",
                source, resolved,
            )

            # Archive the person patches behind the entity (name and
            # alias match, the same fold set the merge uses), so the
            # Memory side converges through the normal delta.
            alias_names = [
                r["alias"] for r in await conn.fetch(
                    "SELECT alias FROM entity_aliases WHERE user_id = $1 AND entity_id = $2::uuid",
                    user_id, resolved,
                )
            ]
            keys = list({n.strip().lower() for n in [row["name"], *alias_names] if n and n.strip()})
            archived = await conn.fetch(
                """
                UPDATE context_patches cp
                SET status = 'archived', updated_at = NOW(),
                    value = jsonb_set(value, '{archive_cause}', '"not_a_person"')
                FROM patch_subjects ps
                WHERE ps.patch_id = cp.patch_id
                  AND ps.subject_key = $1
                  AND cp.patch_type = $2
                  AND COALESCE(cp.status, 'active') = 'active'
                  AND LOWER(cp.value->>'text') = ANY($3::text[])
                RETURNING cp.patch_id
                """,
                subject_key, vocab.person_type, keys,
            )

    await _rebuild_entity_index(user_id)
    logger.info(
        "person_suppressed", user_id=user_id, app_id=str(app_id),
        entity_id=resolved, name=row["name"], source=source,
        archived_patches=len(archived),
    )
    return {
        "status": "suppressed",
        "entity_id": resolved,
        "name": row["name"],
        "already": False,
        "archived_patch_ids": [str(r["patch_id"]) for r in archived],
    }


@app.delete("/v1/people/{user_id}/{entity_id}/not-a-person", tags=["People"])
async def unsuppress_person(
    user_id: str,
    entity_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    Lift a suppression: the entity was a real person after all. Restores
    the entity to every surface and un-archives exactly the person
    patches this suppression archived (guarded on archive_cause, so a
    patch archived by decay or merge is never resurrected by accident).
    409 when the entity is not suppressed.
    """
    subject_key = f"user:{user_id}"
    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        async with conn.transaction():
            # Direct read: the standard loader refuses suppressed rows.
            row = await conn.fetchrow(
                "SELECT entity_id, name, suppressed_at FROM entities "
                "WHERE user_id = $1 AND entity_id = $2::uuid AND entity_type = $3",
                user_id, entity_id, vocab.person_entity_type,
            )
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Person {entity_id} not found for this user",
                )
            if row["suppressed_at"] is None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "NOT_SUPPRESSED",
                            "message": "Entity is not marked not-a-person."},
                )
            resolved = str(row["entity_id"])
            await conn.execute(
                "UPDATE entities SET suppressed_at = NULL, suppressed_source = NULL "
                "WHERE entity_id = $1::uuid",
                resolved,
            )
            alias_names = [
                r["alias"] for r in await conn.fetch(
                    "SELECT alias FROM entity_aliases WHERE user_id = $1 AND entity_id = $2::uuid",
                    user_id, resolved,
                )
            ]
            keys = list({n.strip().lower() for n in [row["name"], *alias_names] if n and n.strip()})
            restored = await conn.fetch(
                """
                UPDATE context_patches cp
                SET status = 'active', updated_at = NOW(),
                    value = value - 'archive_cause'
                FROM patch_subjects ps
                WHERE ps.patch_id = cp.patch_id
                  AND ps.subject_key = $1
                  AND cp.patch_type = $2
                  AND cp.value->>'archive_cause' = 'not_a_person'
                  AND LOWER(cp.value->>'text') = ANY($3::text[])
                RETURNING cp.patch_id
                """,
                subject_key, vocab.person_type, keys,
            )

    await _rebuild_entity_index(user_id)
    logger.info(
        "person_unsuppressed", user_id=user_id, app_id=str(app_id),
        entity_id=resolved, restored_patches=len(restored),
    )
    return {
        "status": "unsuppressed",
        "entity_id": resolved,
        "name": row["name"],
        "restored_patch_ids": [str(r["patch_id"]) for r in restored],
    }


USER_AUTHORED_ALIAS_SOURCES = frozenset({"user_edit", "user_confirmation"})


async def _name_candidates(conn, user_id: str, name: str, person_type: str,
                           all_sharing_first_token: bool = False):
    """Every live person a typed name could denote, with ranking signals.

    Two sources, unioned, because they catch different things:

      STRUCTURAL  first-name and initial matching against the live
                  roster, so "Mike" finds every Mike and "Mike P" finds
                  only the P ones (services/entity_aliasing.py).
      ALIAS       every recorded alias row that matches, not the first.
                  This is what catches "VJ", which no amount of token
                  matching would, AND it is the exact lookup that caused
                  the damage: a bare `LIMIT 1` on it meant
                  'Mike' -> Mike DiTroia resolved forever.

    Returns [] for a name nobody holds, which is a NEW PERSON and must
    stay creatable. One candidate resolves. More than one is a question
    for a human.
    """
    rows = await conn.fetch(
        """
        SELECT e.entity_id, e.name,
               (SELECT count(*) FROM person_appearances pa
                 WHERE pa.entity_id = e.entity_id) AS meetings,
               (SELECT max(pa.last_seen_at)::date FROM person_appearances pa
                 WHERE pa.entity_id = e.entity_id) AS last_met,
               (SELECT array_agg(DISTINCT pa.project_id) FROM person_appearances pa
                 WHERE pa.entity_id = e.entity_id
                   AND pa.project_id IS NOT NULL) AS projects,
               -- The SAME presence predicate as people_signals.is_presence_grade,
               -- in SQL: speaker or ownership capacity, or the pre-31 empty
               -- array (unknown must not become "did not attend"). A mention
               -- is not a meeting. Two definitions of presence would let the
               -- picker rank someone as "been here" whom the cadence never
               -- counted.
               EXISTS (SELECT 1 FROM person_appearances pa
                        WHERE pa.entity_id = e.entity_id
                          AND (cardinality(pa.capacities) = 0
                               OR pa.capacities && ARRAY['speaker','ownership']::text[])
                      ) AS present
        FROM entities e
        WHERE e.user_id = $1 AND e.entity_type = $2
          AND e.merged_into IS NULL AND e.suppressed_at IS NULL
        """,
        user_id, person_type,
    )
    by_id = {str(r["entity_id"]): r for r in rows}
    roster = [(str(r["entity_id"]), r["name"]) for r in rows]

    if all_sharing_first_token:
        # A BARE FIRST NAME HAS NO DECISIVE MATCH, so the exact hit must
        # not hide the others. `person_candidates` short-circuits on an
        # exact token match and returns only the entity literally called
        # "John", which is the correct answer to "who is named exactly
        # this" and the wrong answer to "who could this mean". When the
        # caller is about to ASK, they need every John.
        first = tokenize_name(name)[:1]
        structural = {
            eid for eid, n in roster
            if tokenize_name(n or "")[:1] == first and first
        }
    else:
        structural = {eid for eid, _ in person_candidates(name, roster)}
    aliased: set = set()
    hits = set(structural)

    try:
        alias_rows = await conn.fetch(
            """
            SELECT e.entity_id, a.source FROM entity_aliases a
            JOIN entities e ON e.entity_id = a.entity_id
            WHERE a.user_id = $1 AND LOWER(a.alias) = LOWER($2)
              AND e.entity_type = $3
              AND e.merged_into IS NULL AND e.suppressed_at IS NULL
            """,
            user_id, name, person_type,
        )
        # ONLY A USER-AUTHORED ALIAS IS AN ANSWER. The single-candidate
        # rule below treats `matched_by == "alias"` as "a question the
        # user already answered", and on Scott's roster (2026-08-23) 128
        # of 145 alias rows were written by `merge_backfill` or
        # `heuristic`, not by him. "christina" resolved to Christina
        # McAlpin without a prompt on a script's say-so. A machine alias
        # is a guess with the same authority as a name match, so it is
        # a hit (it still finds the person) but it is reported as one.
        all_alias_hits = {str(r["entity_id"]) for r in alias_rows}
        aliased = {
            str(r["entity_id"]) for r in alias_rows
            if r["source"] in USER_AUTHORED_ALIAS_SOURCES
        }
        hits |= all_alias_hits
    except Exception as exc:
        # entity_aliases can lag on the MCP deployment's separate
        # Postgres. Degrading to structural matching only is safe: it
        # errs toward asking rather than toward guessing.
        logger.debug("alias_candidates_unavailable", error=str(exc)[:120])

    out = []
    for eid in hits:
        r = by_id.get(eid)
        if not r:
            continue
        out.append({
            "entity_id": eid,
            "name": r["name"],
            "meetings": r["meetings"] or 0,
            "last_met": r["last_met"].isoformat() if r["last_met"] else None,
            "projects": [p for p in (r["projects"] or []) if p],
            # Has this person ever been IN a meeting (presence-grade
            # appearance), or only been talked about? Ranks present
            # people first (Scott, 2026-08-26) and is served so the
            # picker can say why.
            "present": bool(r["present"]),
            # WHY this person is a candidate, because the caller's
            # decision depends on it. A recorded alias is a question the
            # user already answered; a structural first-name match is a
            # question nobody has been asked. Only the first is safe to
            # resolve without confirmation.
            "matched_by": "alias" if eid in aliased else "name",
        })
    return out


async def _resolve_or_create_person(
    conn, user_id: str, app_id: str, name: str, description: str,
    source: str, vocab, now, source_prompt: str,
    create_new: bool = False, scope_project_ids=(),
) -> dict:
    """Resolve a user-supplied person name, creating the person if new.

    The single identity-authoring path, shared by POST /v1/people and by
    reassign-speaker's `to_name`. Both are the same act (a human vouching
    for who someone is) and they must not produce different people: a
    name typed into the "+" sheet and the same name typed onto a speaker
    have to land on one row, whole in both cases. Writes the entity AND
    the declared person patch, so nothing arrives half created depending
    on which door the client used.

    Runs inside the CALLER's transaction. Returns entity_id, the name CQ
    actually stores (not the caller's casing), patch_id, and whether each
    half was created.
    """
    # Exact name, then recorded alias, the same resolution order the
    # worker's store_entities uses, so the API and the extraction path
    # agree on what counts as "already known".
    row = await conn.fetchrow(
        """
        SELECT entity_id FROM entities
        WHERE user_id = $1 AND entity_type = $3
          AND LOWER(name) = LOWER($2)
        LIMIT 1
        """,
        user_id, name, vocab.person_entity_type,
    )

    separated_from: List[str] = []

    # AN EXACT MATCH ON A BARE FIRST NAME IS NOT DECISIVE.
    #
    # This is the real two-Johns mechanism and it took three attempts to
    # find, because the two fixes before it were both one layer too low.
    # A CBE meeting created a person called "John". Scott labelled a
    # speaker "John" for a DIFFERENT man, the exact-name lookup above hit
    # immediately, and his friend was attached to the CBE John's eight
    # meetings before any candidate logic ran at all. He then renamed
    # that entity to "John Kirker", which carried the CBE history with it.
    #
    # An exact match IS decisive at two or more tokens: "John Kirker"
    # matching "John Kirker" is the same person, and asking there would
    # be noise. A single token identifies nobody. Half the world's Johns
    # match "John" exactly, and the whole reason a person has a surname
    # is that the first name does not do this job.
    #
    # The cost is real and Scott accepted it explicitly: labelling any
    # speaker with a one-word name that already exists now asks, INCLUDING
    # when it is genuinely the same person. That is the trade he asked
    # for, in his words: if we are not sure two Johns are the same person,
    # we must ask when we label them.
    #
    # `create_new` is the escape, and the 409 carries every John rather
    # than only the exact one, because "which John" is unanswerable from
    # a list of one when a second exists.
    if create_new and len(tokenize_name(name)) == 1:
        # "SOMEONE NEW" WITH A NAME THAT IS LITERALLY TAKEN CANNOT CREATE,
        # AND NEITHER CAN ONE THAT SHARES A FIRST NAME WITH ANYONE.
        #
        # The second half is Scott's ruling of 2026-08-26 ("strict
        # everywhere"): the live labelling prompt on the device already
        # refused a bare "Christina" while a "Christina Lee" was on the
        # roster, and Review > Rename, which obeys this server, let it
        # through. Two doors, two answers. A bare first name that
        # collides with any live person (mention-only included, the
        # roster is the roster) needs a surname or a nickname first,
        # because a bare-named person is a permanent question in every
        # picker after this one (#317: a bare first name always asks).
        #
        # entities is UNIQUE on (user_id, name, entity_type) and the
        # ingest worker upserts on that key, so two people named
        # exactly "John" cannot exist. The first cut of #315 nulled the
        # exact hit here and would have raised on the INSERT; a prod
        # smoke in a rolled-back transaction found it, the source
        # reading tests could not. Scott ruled (2026-08-23) B: ask for
        # more name rather than drop the uniqueness ingest depends on.
        # Same 409 family, same candidates, a different code so the
        # client knows this one needs TEXT, not a pick.
        candidates = await _name_candidates(
            conn, user_id, name, vocab.person_entity_type,
            all_sharing_first_token=True,
        )
        if row is not None or candidates:
            # Two messages, one code: the client already distinguishes
            # "already someone's exact name" from "others are also
            # called X" in its own live alert, so the server says the
            # same thing on the same input.
            if row is not None:
                message = (
                    f"{name!r} is already someone's exact name. Add a "
                    "last name or a nickname to record a different person."
                )
            else:
                message = (
                    f"Others are also called {name!r}. Add a last name "
                    "or a nickname so this person can be told apart."
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "NAME_TAKEN",
                    "message": message,
                    "name": name,
                    "reason": "bare_first_name",
                    # WHICH collision, as a machine field, because the
                    # client's rename alert branches on it and does not
                    # parse `message` (SS, 2026-08-26). The candidates
                    # list carries the same fact (an exact hit is a
                    # candidate whose name equals the typed one), so a
                    # client on an older server can derive it.
                    "collision": "exact_name" if row is not None else "first_name",
                    **candidate_payload(candidates, scope_project_ids),
                },
            )
    if row is not None and not create_new \
            and len(tokenize_name(name)) == 1:
        candidates = await _name_candidates(
            conn, user_id, name, vocab.person_entity_type,
            all_sharing_first_token=True,
        )
        if candidates:
            payload = candidate_payload(candidates, scope_project_ids)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CONTESTED_NAME",
                    "message": (
                        f"{name!r} is a first name, so it does not say which "
                        "person this is. Pick one, or send create_new to add "
                        "someone new."
                    ),
                    "name": name,
                    "reason": "bare_first_name",
                    **payload,
                },
            )

    if row is None:
        # A CONTESTED TYPED NAME IS A QUESTION, NOT A GUESS.
        #
        # This used to be a bare alias lookup with LIMIT 1, and that is
        # the line that put an interview candidate's description on a
        # Kore.ai colleague's page: 'Mike' was a recorded alias of Mike
        # DiTroia, so typing "Mike" onto any speaker resolved to him
        # forever. Ingest stopped guessing in #283; this is the other
        # door, the one where a HUMAN is typing and can simply be asked.
        #
        # 409 rather than 422: this is not a malformed request, it is a
        # fork the caller must resolve, and it carries the ranked
        # candidates to resolve it with.
        candidates = await _name_candidates(
            conn, user_id, name, vocab.person_entity_type
        )
        if len(candidates) > 1 and not create_new:
            payload = candidate_payload(candidates, scope_project_ids)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CONTESTED_NAME",
                    "message": (
                        f"{name!r} could be more than one person. "
                        "Pick one, or send create_new to add someone new."
                    ),
                    "name": name,
                    **payload,
                },
            )
        # ONE CANDIDATE IS STILL A QUESTION WHEN THE MATCH IS STRUCTURAL.
        #
        # This used to resolve silently, and that is the whole of the
        # two-Johns bug (2026-08-23). A CBE meeting created "John". Weeks
        # later a human typed a name for a DIFFERENT John, exactly one
        # candidate came back, and his friend was attached to the CBE
        # John's eight meetings and four projects without anyone being
        # asked. The card then read "Primary CBE admin for AI for Work"
        # over a description of somebody else entirely.
        #
        # The contested guard above is backwards for the FIRST collision,
        # and the first collision is the only one that matters: by the
        # time two Johns exist on the roster the damage is already done,
        # because the second was silently absorbed into the first and so
        # there is only ever one. A rule that fires on the second
        # occurrence can never fire.
        #
        # `matched_by` decides it. An ALIAS is a question the user
        # already answered, so resolving to it is honouring their
        # decision. A NAME match is `person_candidates` guessing that a
        # first name means the one person who currently has it, and that
        # guess is exactly what nobody was asked about. Ask.
        #
        # Same 409 shape as the multi-candidate case, so a client that
        # already handles CONTESTED_NAME handles this with no change; the
        # payload just carries one candidate instead of several.
        if len(candidates) == 1 and not create_new:
            only = candidates[0]
            # NARROWED, within the hour, because the first version broke
            # the common case. Asking on every structural single match
            # means labelling a speaker "Suresh" against a roster holding
            # "Suresh Muchakurti" now 409s, and that is the normal,
            # correct operation this endpoint exists to perform. A fix
            # that refuses the thing users do all day is worse than the
            # bug it closes.
            #
            # The distinguishing signal is DIRECTION OF INFORMATION.
            #
            #   typed SHORTER than the match   "Suresh" -> "Suresh Muchakurti"
            #     The user is using a shorthand for somebody the system
            #     already knows more about than they typed. Resolve.
            #
            #   typed LONGER than the match    "John Kirker" -> "John"
            #     The user is asserting information CQ does not have. The
            #     match rests entirely on the token they share, and the
            #     token they do not share is the whole question. This is
            #     Scott's two-Johns case exactly: a bare "John" from a CBE
            #     meeting, and a friend named John Kirker, joined on
            #     "John" alone. Ask.
            #
            # Equal length with different tokens cannot reach here: it
            # would not have matched structurally in the first place.
            typed_tokens = len(tokenize_name(name))
            match_tokens = len(tokenize_name(only.get("name") or ""))
            # A BARE FIRST NAME ALWAYS ASKS (Scott, 2026-08-23, after
            # "christina" landed on Christina McAlpin with no prompt).
            # The shorthand exemption above was written when SS had no
            # 409 handler and every ask surfaced as a failure; with the
            # picker live an ask is one tap on "Christina McAlpin, 13
            # meetings", and a first name alone is never sure. The one
            # thing that still resolves a bare name silently is a
            # USER-authored alias, because that is his prior answer.
            # Full-name shorthand ("Suresh M" onto "Suresh Muchakurti")
            # keeps resolving; the direction rule still governs it.
            bare = typed_tokens == 1
            if only.get("matched_by") == "name" \
                    and (bare or typed_tokens > match_tokens):
                payload = candidate_payload(candidates, scope_project_ids)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "CONTESTED_NAME",
                        "message": (
                            f"{name!r} matches {only['name']!r} on the name "
                            "alone. Confirm it is the same person, or send "
                            "create_new to add someone new."
                        ),
                        "name": name,
                        **payload,
                    },
                )
            row = {"entity_id": only["entity_id"]}

    created = False
    if row is not None:
        resolved = await _load_active_person(
            conn, user_id, str(row["entity_id"]), vocab.person_entity_type
        )
        entity_uuid = str(resolved["entity_id"])
        # Answer with the name CQ actually stores, not the caller's
        # casing: "kinsley raman" resolving to "Kinsley Raman" is
        # information the client needs to render the row it just got.
        name = resolved["name"]
        await conn.execute(
            """
            UPDATE entities SET
                description = COALESCE(NULLIF(description, ''), $1),
                confirmed_at = COALESCE(confirmed_at, NOW()),
                confirmation_source = COALESCE(confirmation_source, $2)
            WHERE entity_id = $3::uuid
            """,
            description, source, entity_uuid,
        )
    else:
        created = True
        if create_new:
            # The caller SAW a candidate list and said none of these.
            # Every person sharing the first token is that list (it is
            # what both 409s carry), so the new person is kept separate
            # from each of them, server side here and client side via
            # the `separated_from` echo. A surname retry after
            # NAME_TAKEN ("John Smith" after "John") lands here too.
            separated_from = [
                str(c["entity_id"]) for c in await _name_candidates(
                    conn, user_id, name, vocab.person_entity_type,
                    all_sharing_first_token=True,
                )
            ]
        entity_uuid = str(await conn.fetchval(
            """
            INSERT INTO entities
                (user_id, name, entity_type, description,
                 confirmed_at, confirmation_source)
            VALUES ($1, $2, $5, $3, NOW(), $4)
            RETURNING entity_id
            """,
            user_id, name, description, source, vocab.person_entity_type,
        ))
        for other in separated_from:
            lo, hi = canonical_pair(entity_uuid, other)
            await conn.execute(
                """
                INSERT INTO entity_separations
                    (user_id, entity_id_lo, entity_id_hi, source)
                VALUES ($1, $2::uuid, $3::uuid, $4)
                ON CONFLICT (user_id, entity_id_lo, entity_id_hi) DO NOTHING
                """,
                user_id, lo, hi, source,
            )

    # The person patch. Looked up in both branches so the caller
    # always gets an id it can wire connections to; only created
    # when missing, because a second person patch for the same
    # human is exactly the duplicate this path exists to avoid.
    subject_key = f"user:{user_id}"
    existing_patch = await conn.fetchrow(
        """
        SELECT cp.patch_id FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
        WHERE ps.subject_key = $1
          AND cp.patch_type = $3
          AND COALESCE(cp.status, 'active') = 'active'
          AND LOWER(cp.value->>'text') = LOWER($2)
        LIMIT 1
        """,
        subject_key, name, vocab.person_type,
    )
    patch_id = str(existing_patch["patch_id"]) if existing_patch else None
    patch_created = patch_id is None
    if patch_id is None:
        patch_id = str(uuid.uuid4())
        await conn.execute(
            """
            INSERT INTO context_patches (
                patch_id, patch_name, patch_type, value,
                origin_mode, source_prompt, confidence, persistence,
                status, created_at, updated_at, last_observed_at
            ) VALUES ($1, $2, $6, $3, 'declared', $7,
                      1.0, $4, 'active', $5, $5, $5)
            """,
            patch_id, f"declared_{patch_id[:8]}",
            json.dumps({"text": name}),
            PATCH_PERSISTENCE.get(vocab.person_type, "decaying"), now,
            vocab.person_type, source_prompt,
        )
        await conn.execute(
            "INSERT INTO patch_subjects (patch_id, subject_key) VALUES ($1, $2)",
            patch_id, subject_key,
        )
        try:
            await conn.execute(
                """
                INSERT INTO context_patch_acl
                    (patch_id, app_id, can_read, can_write, can_delete)
                VALUES ($1, $2, TRUE, TRUE, TRUE)
                """,
                patch_id, uuid.UUID(str(app_id)),
            )
        except (ValueError, AttributeError):
            pass  # legacy X-App-ID, no ACL row

    return {
        "entity_id": entity_uuid,
        "name": name,
        "patch_id": patch_id,
        "created": created,
        "patch_created": patch_created,
        "separated_from": separated_from,
    }


@app.post("/v1/people/{user_id}", tags=["People"])
async def create_person(
    user_id: str,
    req: PersonCreate,
    app_id: str = Depends(verify_application_access),
):
    """
    Create a person the user named directly.

    Backs the People list "+" button and "Create X as a new person" in the
    link-or-create sheet. Writes both halves of CQ's person model so the
    new person is whole: the entity (so recall can match the name and the
    graph can hang relationships off it) and a declared person patch (so
    it appears in the quilt like any other stated fact).

    Returns the existing person if the name is already known, rather than
    creating a duplicate the user would then have to merge.
    """
    try:
        name = validate_person_name(req.name)
        source = resolve_identity_source(req.source)
    except IdentityRequestError as e:
        raise _identity_error(e)

    description = (req.description or "").strip()
    now = datetime.utcnow()

    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        async with conn.transaction():
            resolved = await _resolve_or_create_person(
                conn, user_id, app_id, name, description, source, vocab,
                now, "people_create",
                create_new=bool(req.create_new),
            )
    entity_uuid = resolved["entity_id"]
    name = resolved["name"]
    patch_id = resolved["patch_id"]
    created = resolved["created"]
    patch_created = resolved["patch_created"]

    await _rebuild_entity_index(user_id)
    # An entity that already existed can still gain its first person patch
    # here (the entity-only "inferred but never stated" case), and that is
    # a quilt change readers need to see.
    if created or patch_created:
        await redis_client.xadd("memory_updates", {"data": json.dumps(
            {"type": "hydrate", "user_id": user_id, "timestamp": now.isoformat()}
        )})

    logger.info(
        "person_created" if created else "person_create_resolved_existing",
        user_id=user_id, app_id=str(app_id), entity_id=entity_uuid,
        name=name, source=source,
    )
    return {
        "status": "created" if created else "exists",
        "entity_id": entity_uuid,
        "patch_id": patch_id,
        "name": name,
        "separated_from": resolved["separated_from"],
    }


# ============================================
# Alignment Layer (design e6ee7ae8, phase 1: the record)
# ============================================
#
# THE PRIVACY BOUNDARY IS IN THE SELECT. Every read below names its
# columns and none of them is private_instruction; a shared surface can
# never receive it because no shared query fetches it. Events with
# shippable = FALSE (no evidence in the transcript, or a guard hit that
# the regeneration did not clear) stay private candidates and are not
# served here either (requirements 4: evidence is mandatory).

ALIGNMENT_SHARED_COLUMNS = """
    event_id::text, project_id, origin_id, origin_type, topic, statement, rationale,
    decision_owner, implementation_owner, status, confidence,
    ARRAY(SELECT x::text FROM unnest(supersedes) x) AS supersedes,
    superseded_by::text AS superseded_by,
    ARRAY(SELECT x::text FROM unnest(source_patch_ids) x) AS source_patch_ids,
    ARRAY(SELECT x::text FROM unnest(superseded_patch_ids) x) AS superseded_patch_ids,
    impact, evidence, proposed_at, expires_at, confirmed_at, confirmed_by,
    confirmation_on_behalf, correction_reason, corrected_by
"""


def _alignment_row(r) -> dict:
    d = dict(r)
    for k in ("impact", "evidence"):
        v = d.get(k)
        if isinstance(v, str):
            try:
                d[k] = json.loads(v)
            except Exception:
                d[k] = []
    for k in ("proposed_at", "expires_at", "confirmed_at"):
        if d.get(k) is not None and hasattr(d[k], "isoformat"):
            d[k] = d[k].isoformat()
    d["active_until_superseded"] = d["status"] == "confirmed" and not d.get("superseded_by")
    return d


class AlignmentConfirmRequest(BaseModel):
    # Who confirmed, as the app names them. CQ authenticates apps, not
    # end users; the app vouches (doc 10). Required so a confirmation is
    # always attributed; on_behalf marks the admin override.
    confirmed_by: str
    on_behalf: bool = False


class AlignmentCorrectRequest(BaseModel):
    statement: str
    reason: str
    corrected_by: str
    rationale: Optional[str] = None


@app.get("/v1/alignment/{user_id}/meetings/{origin_id}", tags=["Alignment"])
async def alignment_for_meeting(
    user_id: str, origin_id: str,
    app_id: str = Depends(verify_application_access),
):
    """The meeting view's card: every shippable alignment event this
    meeting produced, capture order. Empty `events` means no card; the
    meeting view is byte-identical to today (requirements 6)."""
    async with db_pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                f"SELECT {ALIGNMENT_SHARED_COLUMNS} FROM alignment_events "
                "WHERE user_id = $1 AND origin_id = $2 AND shippable "
                "ORDER BY proposed_at, created_at",
                user_id, origin_id,
            )
        except Exception as exc:
            logger.warning("alignment_read_unavailable", error=str(exc)[:120])
            rows = []
    return {"origin_id": origin_id, "events": [_alignment_row(r) for r in rows]}


@app.get("/v1/alignment/{user_id}/projects/{project_id}", tags=["Alignment"])
async def alignment_record(
    user_id: str, project_id: str,
    app_id: str = Depends(verify_application_access),
):
    """The project's alignment record: current direction per topic,
    decision history (the sequence, never annotated), awaiting
    confirmation, the direction-change count and cumulative impact."""
    async with db_pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                f"SELECT {ALIGNMENT_SHARED_COLUMNS} FROM alignment_events "
                "WHERE user_id = $1 AND project_id = $2 AND shippable "
                "ORDER BY proposed_at",
                user_id, project_id,
            )
        except Exception as exc:
            logger.warning("alignment_read_unavailable", error=str(exc)[:120])
            rows = []
    events = [_alignment_row(r) for r in rows]
    rec = alignment_svc.project_record(events)
    rec.pop("by_id", None)
    rec["project_id"] = project_id
    rec["definitions"] = {
        "direction_change_count": "Confirmed events on this project that superseded a prior item. A count of the record, never of a person.",
        "cumulative_impact": "Union of the derived impact lines of every confirmed superseding event, deduplicated by the item they derive from.",
    }
    return rec


@app.post("/v1/alignment/{user_id}/events/{event_id}/confirm", tags=["Alignment"])
async def alignment_confirm(
    user_id: str, event_id: str, req: AlignmentConfirmRequest,
    app_id: str = Depends(verify_application_access),
):
    """Two-step confirmation (requirements 4): nothing is confirmed by
    inference. Confirming makes the statement the active direction and
    displaces what it superseded. 409 on anything but an open proposal."""
    who = (req.confirmed_by or "").strip()
    if not who:
        raise HTTPException(status_code=422, detail={"code": "CONFIRMED_BY_REQUIRED"})
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT event_id, status, confirmed_at, superseded_by, supersedes, topic, project_id "
                "FROM alignment_events WHERE user_id = $1 AND event_id = $2::uuid FOR UPDATE",
                user_id, event_id,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Alignment event not found")
            if row["status"] not in ("proposed", "corrected") or row["confirmed_at"] or row["superseded_by"]:
                raise HTTPException(status_code=409, detail={
                    "code": "NOT_CONFIRMABLE", "status": row["status"],
                    "superseded_by": str(row["superseded_by"]) if row["superseded_by"] else None,
                })
            await conn.execute(
                "UPDATE alignment_events SET status = 'confirmed', confirmed_at = NOW(), "
                "confirmed_by = $3, confirmation_on_behalf = $4, expires_at = NULL, updated_at = NOW() "
                "WHERE user_id = $1 AND event_id = $2::uuid",
                user_id, event_id, who, bool(req.on_behalf),
            )
            # A confirmed direction displaces what it superseded (and any
            # other open proposal on the same topic, which the record now
            # answers). Corrections already displaced their proposal.
            await conn.execute(
                "UPDATE alignment_events SET superseded_by = $2::uuid, updated_at = NOW() "
                "WHERE user_id = $1 AND event_id = ANY($3::uuid[]) AND superseded_by IS NULL",
                user_id, event_id, list(row["supersedes"] or []),
            )
            await conn.execute(
                "UPDATE alignment_events SET superseded_by = $2::uuid, updated_at = NOW() "
                "WHERE user_id = $1 AND project_id = $3 AND topic = $4 AND event_id <> $2::uuid "
                "AND status = 'confirmed' AND superseded_by IS NULL AND confirmed_at < NOW()",
                user_id, event_id, row["project_id"], row["topic"],
            )
            fresh = await conn.fetchrow(
                f"SELECT {ALIGNMENT_SHARED_COLUMNS} FROM alignment_events WHERE user_id = $1 AND event_id = $2::uuid",
                user_id, event_id,
            )
    logger.info("alignment_confirmed", user_id=user_id, event_id=event_id, on_behalf=bool(req.on_behalf))
    return {"status": "confirmed", "event": _alignment_row(fresh)}


@app.post("/v1/alignment/{user_id}/events/{event_id}/correct", tags=["Alignment"])
async def alignment_correct(
    user_id: str, event_id: str, req: AlignmentCorrectRequest,
    app_id: str = Depends(verify_application_access),
):
    """Corrections are events, not edits (requirements 4). A new
    `corrected` event supersedes the proposal; history is append-only.
    The corrected text passes the same guard as generated text. Two
    corrections on one proposal escalate (409 CORRECTION_CONFLICT with
    both), never auto-merge."""
    statement = (req.statement or "").strip()
    who = (req.corrected_by or "").strip()
    if not statement or not who or not (req.reason or "").strip():
        raise HTTPException(status_code=422, detail={"code": "STATEMENT_REASON_AND_AUTHOR_REQUIRED"})
    hit = alignment_svc.guard_shared_text(statement) or alignment_svc.guard_shared_text(req.rationale)
    if hit:
        raise HTTPException(status_code=422, detail={
            "code": "SHARED_TEXT_REJECTED", "term": hit,
            "message": "Shared text states what the project believes; it never describes a person.",
        })
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM alignment_events WHERE user_id = $1 AND event_id = $2::uuid FOR UPDATE",
                user_id, event_id,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Alignment event not found")
            if row["status"] == "expired":
                raise HTTPException(status_code=409, detail={"code": "NOT_CORRECTABLE", "status": "expired"})
            prior = await conn.fetch(
                f"SELECT {ALIGNMENT_SHARED_COLUMNS} FROM alignment_events "
                "WHERE user_id = $1 AND status = 'corrected' AND $2::uuid = ANY(supersedes) "
                "AND confirmed_at IS NULL AND superseded_by IS NULL",
                user_id, event_id,
            )
            if prior:
                raise HTTPException(status_code=409, detail={
                    "code": "CORRECTION_CONFLICT",
                    "message": "A correction is already awaiting confirmation on this event. Escalate; CQ never merges two corrections.",
                    "existing": [_alignment_row(p) for p in prior],
                    "proposed": {"statement": statement, "reason": req.reason, "corrected_by": who},
                })
            new_id = str(uuid.uuid4())
            await conn.execute(
                """
                INSERT INTO alignment_events (
                    event_id, user_id, app_id, project_id, origin_id, origin_type,
                    topic, statement, rationale, decision_owner, implementation_owner,
                    status, confidence, supersedes, source_patch_ids, superseded_patch_ids,
                    impact, evidence, shippable, proposed_at, expires_at,
                    correction_reason, corrected_by, private_instruction)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        'corrected', $12, ARRAY[$13::uuid], $14, $15,
                        $16, $17, TRUE, NOW(), NOW() + ($18 || ' hours')::interval,
                        $19, $20, $21)
                """,
                new_id, user_id, row["app_id"], row["project_id"], row["origin_id"], row["origin_type"],
                row["topic"], statement, (req.rationale or None), row["decision_owner"], row["implementation_owner"],
                row["confidence"], event_id, row["source_patch_ids"], row["superseded_patch_ids"],
                row["impact"], row["evidence"], str(alignment_svc.PROPOSAL_TTL_HOURS),
                req.reason.strip(), who, row["private_instruction"],
            )
            # The proposal is displaced immediately: the owner now confirms
            # the corrected wording, not the original (design: "nothing was
            # overwritten", the original stays in history, superseded).
            await conn.execute(
                "UPDATE alignment_events SET superseded_by = $2::uuid, updated_at = NOW() "
                "WHERE user_id = $1 AND event_id = $3::uuid",
                user_id, new_id, event_id,
            )
            fresh = await conn.fetchrow(
                f"SELECT {ALIGNMENT_SHARED_COLUMNS} FROM alignment_events WHERE user_id = $1 AND event_id = $2::uuid",
                user_id, new_id,
            )
    logger.info("alignment_corrected", user_id=user_id, original=event_id, event_id=new_id)
    # One carrier for one fact: the superseded id is event.supersedes.
    # (GP, reading the live bodies: a top-level string copy was a second
    # shape for the same fact, and two shapes drift.)
    return {"status": "corrected", "event": _alignment_row(fresh)}


# ============================================
# Projects
# ============================================

class ProjectCreate(BaseModel):
    project_id: str
    name: str

class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None  # "active" or "archived"

class ProjectResponse(BaseModel):
    project_id: str
    name: str
    status: str
    patch_count: int = 0
    created_at: Optional[str] = None

@app.get("/v1/projects/{user_id}", tags=["Projects"])
async def get_user_projects(
    user_id: str,
    app_id: str = Depends(verify_application_access),
):
    """Get all projects for a user."""
    rows = await db_pool.fetch(
        """
        SELECT p.project_id, p.name, p.status, p.created_at,
               COUNT(cp.patch_id) as patch_count
        FROM projects p
        LEFT JOIN context_patches cp ON cp.project_id = p.project_id
            AND COALESCE(cp.status, 'active') = 'active'
        WHERE p.user_id = $1
        GROUP BY p.project_id, p.name, p.status, p.created_at
        ORDER BY p.updated_at DESC
        """,
        user_id
    )
    return [ProjectResponse(
        project_id=r["project_id"], name=r["name"], status=r["status"],
        patch_count=r["patch_count"],
        created_at=r["created_at"].isoformat() if r["created_at"] else None
    ) for r in rows]

class ProjectPersonAdd(BaseModel):
    """Who to add. A name OR an entity_id, never both required."""
    name: Optional[str] = None
    entity_id: Optional[str] = None
    source: Optional[str] = "user_project_card"
    create_new: Optional[bool] = False


@app.post("/v1/projects/{user_id}/{project_id}/people", tags=["Projects"])
async def add_project_person(
    user_id: str,
    project_id: str,
    body: ProjectPersonAdd,
    app_id: str = Depends(verify_application_access),
):
    """Say that a person is on this project.

    Scott retired ShoulderSurf's "Contact" project note on 2026-08-31 and
    this replaces it. That note stored a person as PROSE inside a
    project: no entity, no aliases, no appearances, no insights, no
    ledger, no `owed_to`, and no way to correct, merge or rename them. It
    was a dead-end string that looked like a person. It also never
    reached CQ at all; it lived in a CloudKit blob and was flattened into
    project-chat prompts as an untyped bullet, so a model saw it many
    times and CQ never did.

    DECLARED BEATS INFERRED, which is the rule this endpoint exists to
    honour. Until now CQ only ever inferred membership, from speaker
    labels and ownership inside meetings, so a person who matters and has
    never been in a recorded meeting was invisible. A user SAYING someone
    is on a project is a fact, and it outranks an inference here exactly
    as a stated role outranks an inferred description.

    The person is resolved or created through `_resolve_or_create_person`,
    the SAME path as `POST /v1/people` and reassign-speaker's `to_name`.
    A name typed here and the same name typed onto a speaker must land on
    one row: a second identity-authoring path would be a second source of
    truth about one person, which is the defect this whole surface has
    spent months removing.

    Idempotent. Adding someone already on the project returns the
    membership unchanged and says so, rather than erroring, because the
    client cannot always know and a 409 would make a no-op look like a
    failure.
    """
    name = (body.name or "").strip()
    entity_id = (body.entity_id or "").strip()
    if not name and not entity_id:
        raise HTTPException(status_code=400,
                            detail="name or entity_id is required")

    # The project must be one CQ holds. An unknown project id is the
    # state that produced tonight's incident, and silently accepting a
    # membership against it would store a fact nothing can ever read.
    known = await db_pool.fetchrow(
        "SELECT name FROM projects WHERE user_id = $1 AND project_id = $2",
        user_id, project_id,
    )
    if known is None:
        raise HTTPException(
            status_code=404,
            detail=("no such project for this user; resolve it first with "
                    "GET /v1/projects/{user_id}/resolve"))

    created_person = False
    async with db_pool.acquire() as conn:
        vocab, _, _, _ = await _people_read_context(conn, app_id)
        if entity_id:
            row = await conn.fetchrow(
                """SELECT entity_id::text AS entity_id, name FROM entities
                    WHERE user_id = $1 AND entity_id = $2::uuid
                      AND merged_into IS NULL""",
                user_id, entity_id,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="no such person")
            resolved = {"entity_id": row["entity_id"], "name": row["name"],
                        "created": False}
        else:
            async with conn.transaction():
                resolved = await _resolve_or_create_person(
                    conn, user_id, app_id, name, "", body.source or "user_project_card",
                    vocab, datetime.utcnow(), "project_membership",
                    create_new=bool(body.create_new),
                )
            created_person = bool(resolved.get("created"))

    # Retained, never deleted: re-adding someone previously removed
    # clears the removal rather than writing a second row, so the history
    # of the statement survives without duplicating it.
    row = await db_pool.fetchrow(
        """
        INSERT INTO project_people
            (user_id, project_id, entity_id, added_source)
        VALUES ($1, $2, $3::uuid, $4)
        ON CONFLICT (user_id, project_id, entity_id) DO UPDATE
           SET removed_at = NULL, removed_source = NULL,
               added_at = COALESCE(project_people.added_at, NOW())
        RETURNING added_at, (xmax = 0) AS inserted
        """,
        user_id, project_id, str(resolved["entity_id"]),
        body.source or "user_project_card",
    )
    logger.info("project_person_added", user_id=user_id, project_id=project_id,
                entity_id=str(resolved["entity_id"]),
                person_created=created_person, membership_new=row["inserted"])
    # The echo, so the caller compares what it meant against what
    # happened rather than reading a 200 as agreement.
    return {
        "project_id": project_id,
        "project": known["name"],
        "entity_id": str(resolved["entity_id"]),
        # CQ's stored spelling, which may differ from what was typed.
        "name": resolved["name"],
        "person_created": created_person,
        "membership_created": bool(row["inserted"]),
        "added_at": row["added_at"].isoformat() if row["added_at"] else None,
    }


@app.delete("/v1/projects/{user_id}/{project_id}/people/{entity_id}",
            tags=["Projects"])
async def remove_project_person(
    user_id: str,
    project_id: str,
    entity_id: str,
    source: Optional[str] = Query("user_project_card"),
    app_id: str = Depends(verify_application_access),
):
    """Take a person off a project, keeping the fact that they were on it.

    Stamped rather than deleted. "They are not on this after all" is a
    statement, and it has to stay distinguishable from never having been
    added, which is the same argument that shaped `shelve` and
    description dismissal. Re-adding clears the stamp.
    """
    row = await db_pool.fetchrow(
        """
        UPDATE project_people
           SET removed_at = NOW(), removed_source = $4
         WHERE user_id = $1 AND project_id = $2 AND entity_id = $3::uuid
           AND removed_at IS NULL
        RETURNING entity_id
        """,
        user_id, project_id, entity_id, source,
    )
    logger.info("project_person_removed", user_id=user_id,
                project_id=project_id, entity_id=entity_id,
                was_member=row is not None)
    # Not an error when they were not on it: the caller wanted them off
    # and they are off. `removed` says which actually happened.
    return {"project_id": project_id, "entity_id": entity_id,
            "removed": row is not None}


@app.get("/v1/projects/{user_id}/{project_id}/people", tags=["Projects"])
async def list_project_people(
    user_id: str,
    project_id: str,
    app_id: str = Depends(verify_application_access),
):
    """Who is on this project, and HOW CQ knows.

    Two sources, never merged into one undifferentiated list:

      declared  the user said so. Survives having no meetings at all.
      observed  CQ saw them in a meeting assigned to this project.

    `source` is `declared`, `observed`, or `both`. A client that flattens
    these loses the only thing that distinguishes a fact from an
    inference, and this whole surface runs on that distinction.

    Declared members are returned even when CQ has never seen them in a
    meeting, which is the entire point: that person was invisible before.
    """
    declared = await db_pool.fetch(
        """
        SELECT pp.entity_id::text AS entity_id, e.name, pp.added_at
          FROM project_people pp
          JOIN entities e ON e.entity_id = pp.entity_id
         WHERE pp.user_id = $1 AND pp.project_id = $2
           AND pp.removed_at IS NULL
           AND e.merged_into IS NULL
        """,
        user_id, project_id,
    )
    observed = await db_pool.fetch(
        f"""
        SELECT DISTINCT pa.entity_id::text AS entity_id, e.name,
               count(DISTINCT pa.origin_id) OVER (PARTITION BY pa.entity_id)
                 AS meetings
          FROM person_appearances pa
          JOIN entities e ON e.entity_id = pa.entity_id
          JOIN ({project_meetings.meetings_for_project_sql('$2')}) m
            ON m.origin_id = pa.origin_id
         WHERE pa.user_id = $1
           AND e.merged_into IS NULL
        """,
        user_id, project_id,
    )
    # Merged in the service so a test can EXECUTE it. A sabotage that
    # deleted the promotion to `both` left every source-reading test
    # green here, because they check that the strings appear in this
    # file and "both" also appears in the docstring above.
    roster = project_roster.merge_roster(
        [dict(r) for r in declared], [dict(r) for r in observed])
    return {"project_id": project_id, **roster}



@app.get("/v1/projects/{user_id}/resolve", tags=["Projects"])
async def resolve_project(
    user_id: str,
    name: Optional[str] = Query(None, description="Project name to resolve to CQ's id"),
    project_id: Optional[str] = Query(None, description="Project id to validate against CQ's record"),
    app_id: str = Depends(verify_application_access),
):
    """Resolve a project reference to CQ's own record, or refuse to.

    Built 2026-08-31 after a client-side repair matched projects BY NAME
    and pointed one project's record at another's id. Scott opened
    "Immigration  Interview App" and saw CBE's work: the app asked CQ
    for CBE's project id under the Immigration heading, and CQ filtered
    exactly as asked. His ruling was that repairs match on ID, never on
    name, and this is where a repair holding only a name comes to ask.

    IT WILL NOT GUESS. An endpoint that returned a best match would move
    the guess from the client into CQ without removing it, and guessing
    is what caused the incident. Three answers:

      resolved    one project. `project_id` and CQ's exact stored `name`
      ambiguous   several. `candidates`, and NO project_id
      unknown     none, or an id CQ does not hold for this user

    `project_id=` IS THE ONE A REPAIR SHOULD PREFER, because it answers
    the question that actually detects drift: is the id I am holding
    real for this user. An `unknown` there means the client is scoped to
    something that does not exist in CQ, which is exactly the state that
    produced the incident and is invisible from the device.

    The candidate counts exist so a HUMAN can choose between two real
    projects. A client that ranks them and takes the largest has
    reinvented the bug this endpoint was written to end.
    """
    rows = await db_pool.fetch(
        f"""
        SELECT p.project_id, p.name, p.status,
               (SELECT count(*) FROM context_patches cp
                 WHERE cp.project_id = p.project_id
                   AND COALESCE(cp.status, 'active') = 'active') AS patch_count,
               {project_meetings.meeting_count_sql('p.project_id')}
                 AS meeting_count
          FROM projects p
         WHERE p.user_id = $1
        """,
        user_id,
    )
    answer = project_resolve.resolve(
        [dict(r) for r in rows], name=name, project_id=project_id)
    logger.info("project_resolve", user_id=user_id, asked_name=name,
                asked_project_id=project_id, status=answer["status"],
                match=answer["match"], candidates=len(answer["candidates"]))
    # The query is echoed so a caller comparing what it asked against
    # what it got can see a dropped parameter rather than infer one from
    # an empty answer. A middlebox has eaten a query param on this
    # system before.
    return {"query": {"name": name, "project_id": project_id}, **answer}


@app.post("/v1/projects/{user_id}", tags=["Projects"])
async def create_project(
    user_id: str,
    project: ProjectCreate,
    app_id: str = Depends(verify_application_access),
):
    """Register a project. Called by ShoulderSurf when a new project is created."""
    await db_pool.execute(
        """
        INSERT INTO projects (project_id, user_id, name)
        VALUES ($1, $2, $3)
        ON CONFLICT (project_id) DO UPDATE SET name = $3, updated_at = NOW()
        """,
        project.project_id, user_id, project.name
    )
    return {"status": "created", "project_id": project.project_id}

@app.patch("/v1/projects/{user_id}/{project_id}", tags=["Projects"])
async def update_project(
    user_id: str,
    project_id: str,
    update: ProjectUpdate,
    app_id: str = Depends(verify_application_access),
):
    """
    Rename or archive a project.
    Renaming updates the display name — the project_id never changes.
    Archiving cascades: all patches with this project_id get archived.
    """
    row = await db_pool.fetchrow(
        "SELECT project_id FROM projects WHERE project_id = $1 AND user_id = $2",
        project_id, user_id
    )
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    renamed = None
    archived = None
    if update.name is not None:
        await db_pool.execute(
            "UPDATE projects SET name = $1, updated_at = NOW() WHERE project_id = $2",
            update.name, project_id
        )
        # Also update the text project column on patches for backward compat
        renamed = await db_pool.execute(
            "UPDATE context_patches SET project = $1, updated_at = NOW() WHERE project_id = $2",
            update.name, project_id
        )
        # And the project patch's own text. The people surface resolves
        # OBSERVED projects through `projects.name`, but a person who is
        # only STATED on a project (a works-on edge, no appearance) takes
        # the name from this patch's value.text, which nothing else
        # rewrites. Found 2026-08-21 while tracing a rename that never
        # arrived (SS sent it, GP had no route, seven swallowed since
        # 07-31); this is the one CQ-side copy the rename missed.
        await db_pool.execute(
            """
            UPDATE context_patches
               SET value = jsonb_set(value, '{text}', to_jsonb($1::text)),
                   updated_at = NOW()
             WHERE project_id = $2 AND patch_type = 'project'
               AND COALESCE(status, 'active') = 'active'
            """,
            update.name, project_id
        )

    if update.status == "archived":
        await db_pool.execute(
            "UPDATE projects SET status = 'archived', updated_at = NOW() WHERE project_id = $1",
            project_id
        )
        # Cascade: archive all patches belonging to this project
        archived = await db_pool.execute(
            "UPDATE context_patches SET status = 'archived', updated_at = NOW(), "
            "value = jsonb_set(value, '{archive_cause}', '\"project_archived\"') "
            "WHERE project_id = $1 AND COALESCE(status, 'active') = 'active'",
            project_id
        )

    # The echo, READ BACK FROM THE ROW after the writes rather than copied
    # from the request. This route returned `status` and `project_id` and
    # nothing else until 2026-09-04, when SS's client compared the name it
    # sent against the body and found no name to compare (rule 4: a 200
    # says the request was processed, never that the value the caller
    # sent is the value that landed). Copying `update.name` here would
    # agree with the caller by construction, which is the same claim as
    # the 200; the stored row is the only thing that can disagree.
    stored = await db_pool.fetchrow(
        "SELECT name, status FROM projects WHERE project_id = $1 AND user_id = $2",
        project_id, user_id
    )
    return {
        "status": "updated",
        "project_id": project_id,
        "name": stored["name"] if stored else None,
        "project_status": (stored["status"] if stored else None) or "active",
        # Same convention as unscope: numeric when the half ran, null when
        # the request did not ask for it, so 0 stays distinguishable from
        # "this build does not count".
        "patches_renamed": int(renamed.split()[-1]) if renamed else None,
        "patches_archived": int(archived.split()[-1]) if archived else None,
    }


class OriginProjectAssignment(BaseModel):
    project_id: str
    project_name: str

@app.post("/v1/origins/{user_id}/{origin_type}/{origin_id}/assign-project", tags=["Projects"])
async def assign_origin_to_project(
    user_id: str,
    origin_type: str,
    origin_id: str,
    assignment: OriginProjectAssignment,
    app_id: str = Depends(verify_application_access),
):
    """
    Retroactively assign an origin's patches to a project.
    Use when a user records a meeting / practice session / note without
    selecting a project, then assigns it to a project later. Bulk-updates
    all patches with the given (origin_type, origin_id) to the specified
    project_id.
    """
    project_id = assignment.project_id
    project_name = assignment.project_name

    # Ensure project exists (upsert)
    await db_pool.execute(
        """
        INSERT INTO projects (project_id, user_id, name)
        VALUES ($1, $2, $3)
        ON CONFLICT (project_id) DO UPDATE SET updated_at = NOW()
        """,
        project_id, user_id, project_name
    )

    # RECORD THE DECISION ITSELF, before moving anything, so an ingest
    # phase that finishes AFTER this call can still honour it. The
    # rescope below can only move rows that already exist; this row is
    # what makes the answer to "did it all follow" a yes rather than a
    # matter of timing. See migration 43.
    try:
        await db_pool.execute(
            """
            INSERT INTO origin_project_assignments
                (user_id, origin_id, origin_type, project_id, project)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id, origin_id, origin_type) DO UPDATE
               SET project_id = EXCLUDED.project_id,
                   project    = EXCLUDED.project,
                   assigned_at = NOW()
            """,
            user_id, origin_id, origin_type, project_id, project_name,
        )
    except Exception as exc:
        # Never fail the user's assignment because the durable note could
        # not be written; the rescope below is still the visible effect.
        logger.warning("origin_project_intent_not_recorded",
                       origin_id=origin_id, error=str(exc)[:140])

    # Find all patches for this origin that are currently unscoped
    subject_key = f"user:{user_id}"
    updated = await db_pool.execute(
        """
        UPDATE context_patches SET
            project_id = $1,
            project = $2,
            updated_at = NOW()
        WHERE patch_id IN (
            SELECT cp.patch_id FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $3
              AND cp.origin_type = $4
              AND cp.origin_id = $5
              AND cp.patch_type NOT IN ('trait', 'preference')
        )
        """,
        project_id, project_name, subject_key, origin_type, origin_id
    )

    # Extract count from "UPDATE N"
    patches_updated = int(updated.split()[-1]) if updated else 0

    # THE PEOPLE MOVE WITH THE MEETING (Scott, 2026-08-28: "make sure
    # that people and meetings are associated with that project after it
    # changed").
    #
    # Until now this route rescoped `context_patches` and nothing else,
    # so a meeting assigned after the fact kept presence rows that still
    # said "no project". Found on his own data: all three of Steven
    # Williams's meetings had every patch correctly scoped and every
    # appearance unscoped. A person is IN a project because they were in
    # its meetings, so a rescope that moves the facts and leaves the
    # attendance behind has only done half the job.
    appearances = await db_pool.execute(
        """
        UPDATE person_appearances SET project_id = $1
        WHERE user_id = $2 AND origin_id = $3 AND origin_type = $4
          AND project_id IS DISTINCT FROM $1
        """,
        project_id, user_id, origin_id, origin_type,
    )
    appearances_updated = int(appearances.split()[-1]) if appearances else 0

    # Trigger cache refresh
    stream_key = "memory_updates"
    payload = {"type": "hydrate", "user_id": user_id, "timestamp": datetime.utcnow().isoformat()}
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})

    return {
        "status": "assigned",
        "origin_type": origin_type,
        "origin_id": origin_id,
        "project_id": project_id,
        "patches_updated": patches_updated,
        # Served so the caller can SEE both halves happened rather than
        # infer it from a 200 (rule 4: check the echo, not the status).
        "appearances_updated": appearances_updated,
    }


class OriginProjectUnassignment(BaseModel):
    # Optional guard: only clear patches currently scoped to this project.
    # Protects a meeting that was since reassigned elsewhere — the removal
    # of an OLD association must not strip the NEW one.
    project_id: Optional[str] = None


@app.post("/v1/origins/{user_id}/{origin_type}/{origin_id}/unassign-project", tags=["Projects"])
async def unassign_origin_from_project(
    user_id: str,
    origin_type: str,
    origin_id: str,
    unassignment: OriginProjectUnassignment = OriginProjectUnassignment(),
    app_id: str = Depends(verify_application_access),
):
    """
    Clear project scope from one origin's patches (context-flow contract
    item 2). The mirror of assign-project: when the app removes a meeting
    from a project, its patches must not stay scoped to a project they no
    longer belong to — wrong scope poisons project recall and the rundown
    view.

    Pass body {"project_id": ...} to clear only patches currently scoped
    to that project (recommended); an empty body clears unconditionally.
    """
    subject_key = f"user:{user_id}"
    # An explicit unassignment is a DECISION, not the absence of one: it
    # must stop a later ingest phase adopting the project still sitting
    # on this origin's earlier patches. project_id NULL in the row means
    # exactly that (migration 43).
    try:
        await db_pool.execute(
            """
            INSERT INTO origin_project_assignments
                (user_id, origin_id, origin_type, project_id, project)
            VALUES ($1, $2, $3, NULL, NULL)
            ON CONFLICT (user_id, origin_id, origin_type) DO UPDATE
               SET project_id = NULL, project = NULL, assigned_at = NOW()
            """,
            user_id, origin_id, origin_type,
        )
    except Exception as exc:
        logger.warning("origin_project_intent_not_recorded",
                       origin_id=origin_id, error=str(exc)[:140])

    guard_sql = ""
    params = [subject_key, origin_type, origin_id]
    if unassignment.project_id:
        guard_sql = "AND cp.project_id = $4"
        params.append(unassignment.project_id)

    updated = await db_pool.execute(
        f"""
        UPDATE context_patches SET
            project_id = NULL,
            project = NULL,
            updated_at = NOW()
        WHERE patch_id IN (
            SELECT cp.patch_id FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND cp.origin_type = $2
              AND cp.origin_id = $3
              {guard_sql}
        )
        """,
        *params,
    )
    patches_updated = int(updated.split()[-1]) if updated else 0

    # The mirror of the rescope: presence leaves with the facts, under
    # the SAME guard. Without the guard here a stale "remove it from the
    # old project" would strip a meeting that has since been reassigned,
    # which is the exact case the guard exists for on the patches half.
    appearance_guard = "AND project_id = $4" if unassignment.project_id else ""
    appearance_params = [user_id, origin_id, origin_type]
    if unassignment.project_id:
        appearance_params.append(unassignment.project_id)
    appearances = await db_pool.execute(
        f"""
        UPDATE person_appearances SET project_id = NULL
        WHERE user_id = $1 AND origin_id = $2 AND origin_type = $3
          AND project_id IS NOT NULL
          {appearance_guard}
        """,
        *appearance_params,
    )
    appearances_updated = int(appearances.split()[-1]) if appearances else 0

    stream_key = "memory_updates"
    payload = {"type": "hydrate", "user_id": user_id, "timestamp": datetime.utcnow().isoformat()}
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})

    return {
        "status": "unassigned",
        "origin_type": origin_type,
        "origin_id": origin_id,
        "project_id_guard": unassignment.project_id,
        "patches_updated": patches_updated,
        "appearances_updated": appearances_updated,
    }


#: What "affects" means, ON THE WIRE rather than in a docstring.
#: Doc 16 section 5.13: where inference is unavoidable, publish the
#: definition where the client can read it, because the client is the one
#: rendering a sentence to a human and it cannot render a caveat it was
#: never told about.
AFFECTED_PEOPLE_DEFINITION = (
    "A person is listed when EVERY meeting CQ has recorded them in belongs "
    "to this project. That is a statement about what was observed, not about "
    "who they are: someone can be central to your work and still appear in "
    "only one project. Nothing here is a recommendation to remove anybody."
)

#: The three states, and why a bare boolean was refused. Measured on real
#: data 2026-09-03: of 305 people matching the naive rule, 51 provably
#: owned memory outside the project (including the account owner, three
#: times over, at 157 patches each) and another 145 were flagged only by
#: a substring test that matches "Ian" inside "Brian". Collapsing that
#: into safe/unsafe would hide the middle category, and the middle
#: category is most of it.
AFFECTED_PEOPLE_CONFIDENCE = {
    "appears_only_here": (
        "Every signal CQ has agrees: no memory owned outside this project, "
        "no graph edges, no name match elsewhere."
    ),
    "uncertain": (
        "A weak signal says they exist elsewhere. The name match is a "
        "SUBSTRING test, so it fires on 'Ian' inside 'Brian'. Treat as "
        "unresolved, never as clean."
    ),
    "appears_elsewhere": (
        "They own active memory outside this project, matched exactly on "
        "owner. Listing them here is CQ telling you the rule is wrong "
        "about this person."
    ),
}

#: Hard cap. `total_affected` is computed WITHOUT it, so a client never
#: renders a count that is quietly the page size.
AFFECTED_PEOPLE_LIMIT = 200


@app.get("/v1/projects/{user_id}/{project_id}/affected-people", tags=["Projects"])
async def project_affected_people(
    user_id: str,
    project_id: str,
    app_id: str = Depends(verify_application_access),
):
    """READ ONLY. Who appears in this project and nowhere else.

    Built for the project-delete confirmation. It answers a question and
    removes nothing, and it is deliberately not the read half of a write
    endpoint: if the list turns out to be wrong, the correct outcome is
    that nothing is ever deleted, not that a confirm button is added.

    Every row carries the SIGNALS rather than a verdict. A bare
    `safe_to_remove` would force the client to infer the reason from an
    absence, which is the failure this group keeps paying for, and here
    the inference would be wrong most of the time: measured on prod, the
    naive rule was clean for 96 of 305 people and its most confident
    answer was the account owner.
    """
    # The person type comes from the CALLER'S manifest, never a literal.
    # The first draft of this query hardcoded the ShoulderSurf type name
    # in its patch_type predicate and `test_people_surface_speaks_no_
    # literals` caught it, which is the overfit failure the People
    # surface was cleaned of once already.
    #
    # That guard greps the file, so it cannot tell a predicate from a
    # comment quoting one. This comment is therefore written WITHOUT the
    # offending string rather than with it, because a test that has to be
    # weakened to accommodate prose stops guarding the code.
    vocab = await _people_vocab_cached(app_id)

    rows = await db_pool.fetch(
        """
        WITH per_entity AS (
            SELECT pa.entity_id,
                   count(*)                                       AS all_appearances,
                   count(*) FILTER (WHERE pa.project_id IS NULL)  AS unscoped_appearances,
                   count(DISTINCT pa.project_id)                  AS distinct_projects,
                   count(*) FILTER (WHERE pa.project_id = $2)     AS here
            FROM person_appearances pa
            WHERE pa.user_id = $1
            GROUP BY pa.entity_id
        ),
        candidates AS (
            SELECT pe.entity_id, pe.here, e.name
            FROM per_entity pe
            JOIN entities e ON e.entity_id = pe.entity_id
            WHERE pe.distinct_projects = 1
              AND pe.unscoped_appearances = 0
              AND pe.here > 0
        )
        SELECT ca.entity_id,
               ca.name,
               ca.here AS appearances_in_project,
               (SELECT count(*) FROM context_patches cp
                 WHERE cp.status = 'active'
                   AND cp.project_id IS DISTINCT FROM $2
                   AND cp.value->>'owner' = ca.name)          AS owns_elsewhere,
               (SELECT count(*) FROM context_patches cp
                 WHERE cp.status = 'active'
                   AND cp.project_id IS DISTINCT FROM $2
                   AND cp.patch_type = $3
                   AND cp.value->>'text' ILIKE '%' || ca.name || '%')
                                                              AS name_hits_elsewhere,
               (SELECT count(*) FROM relationships r
                 WHERE r.user_id = $1
                   AND (r.from_entity_id = ca.entity_id
                        OR r.to_entity_id = ca.entity_id))    AS graph_edges,
               (SELECT count(*) FROM entities e2
                 WHERE e2.user_id = $1 AND e2.name = ca.name
                   AND e2.entity_type = $4)                   AS entities_with_this_name
        FROM candidates ca
        ORDER BY ca.here DESC, ca.name ASC
        """,
        user_id, project_id, vocab.person_type, vocab.person_entity_type,
    )

    project_name = await db_pool.fetchval(
        "SELECT name FROM projects WHERE project_id = $1", project_id
    )

    people = []
    for r in rows:
        if r["owns_elsewhere"] > 0:
            confidence = "appears_elsewhere"
        elif r["name_hits_elsewhere"] > 0 or r["graph_edges"] > 0:
            confidence = "uncertain"
        else:
            confidence = "appears_only_here"
        people.append({
            "entity_id": str(r["entity_id"]),
            "name": r["name"],
            "appearances_in_project": r["appearances_in_project"],
            "confidence": confidence,
            "signals": {
                "owns_patches_elsewhere": r["owns_elsewhere"],
                "name_hits_elsewhere_uncertain": r["name_hits_elsewhere"],
                "graph_edges": r["graph_edges"],
            },
            # A user reading a list of nine names cannot tell that two of
            # them are the same person twice.
            #
            # CORRECTED 2026-09-04, and the correction is the useful part.
            # This shipped counting entities of ANY type, so a person who
            # shares a name with an artifact or a company was reported as
            # a duplicate identity. "Mac" is a person AND an artifact on
            # prod, and the GL Unlimited smoke duly reported one duplicate
            # that was not one.
            #
            # The claim that motivated the field was also wrong. I said
            # Alex resolved to 4 entities and the account owner to 3.
            # That came from grouping candidates by name ACROSS USERS, so
            # three different people each having a colleague named Scott
            # read as one Scott existing three times. Measured properly:
            # person entities are UNIQUE BY NAME per user, across the
            # whole population, zero exceptions.
            #
            # The field stays because the shape it guards against is real
            # and cheap to report, and because a client that has already
            # built for it should not have the key vanish. It now counts
            # only entities of the caller's person type, so it reports 1
            # for every row until a genuine duplicate identity appears.
            "entities_with_this_name": r["entities_with_this_name"],
        })

    counts = {k: 0 for k in AFFECTED_PEOPLE_CONFIDENCE}
    for p in people:
        counts[p["confidence"]] += 1

    total = len(people)
    return {
        "project_id": project_id,
        "project_name": project_name,
        # Computed before the cap, so a client never renders a number
        # that is quietly the page size.
        "total_affected": total,
        "returned": min(total, AFFECTED_PEOPLE_LIMIT),
        "truncated": total > AFFECTED_PEOPLE_LIMIT,
        "counts_by_confidence": counts,
        "duplicate_names": sum(1 for p in people if p["entities_with_this_name"] > 1),
        "definition": AFFECTED_PEOPLE_DEFINITION,
        "vocabulary": {"confidence": AFFECTED_PEOPLE_CONFIDENCE},
        "people": people[:AFFECTED_PEOPLE_LIMIT],
    }


@app.post("/v1/projects/{user_id}/{project_id}/unscope", tags=["Projects"])
async def unscope_project(
    user_id: str,
    project_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    Clear project scope from ALL of a user's patches carrying this
    project_id (context-flow contract item 2, the project-deletion form).
    Patches survive as unscoped memory — deleting a project container
    must never delete what was learned in its meetings. The projects
    registry row is left in place (harmless, and its name remains useful
    for display-name fallback matching on historical data).
    """
    subject_key = f"user:{user_id}"
    updated = await db_pool.execute(
        """
        UPDATE context_patches SET
            project_id = NULL,
            project = NULL,
            updated_at = NOW()
        WHERE patch_id IN (
            SELECT cp.patch_id FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND cp.project_id = $2
        )
        """,
        subject_key, project_id,
    )
    patches_updated = int(updated.split()[-1]) if updated else 0

    # PRESENCE LEAVES WITH THE FACTS, the same way it does on the
    # single-meeting mirror `unassign-project`.
    #
    # This route did only the patches half until 2026-09-03, and its
    # mirror did both, which meant deleting a project left every
    # `person_appearances` row still stamped with it. 32 such rows were
    # sitting on prod when this was found. Nothing read them, so nothing
    # looked broken, and that is exactly why it survived: the two routes
    # are documented as mirrors and only one of them was.
    #
    # It stops being invisible the moment anything groups presence BY
    # PROJECT, which is what the affected-people screen does, so a list
    # computed off these rows would name people as belonging to a project
    # that no longer exists.
    #
    # No guard clause here, unlike the mirror. There, the guard exists
    # because a stale request could strip a meeting that has since been
    # REASSIGNED. Here the caller is deleting the project itself, so
    # there is no later state to protect: every row carrying this
    # project_id is meant to lose it.
    appearances = await db_pool.execute(
        """
        UPDATE person_appearances SET project_id = NULL
        WHERE user_id = $1 AND project_id = $2
        """,
        user_id, project_id,
    )
    appearances_updated = int(appearances.split()[-1]) if appearances else 0

    stream_key = "memory_updates"
    payload = {"type": "hydrate", "user_id": user_id, "timestamp": datetime.utcnow().isoformat()}
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})

    return {
        "status": "unscoped",
        "project_id": project_id,
        "patches_updated": patches_updated,
        # Served back rather than inferred. A caller comparing the two
        # numbers is the only way to notice this half regressing again,
        # and a 200 has already told this group that a write was
        # processed while a field it sent was not stored.
        "appearances_updated": appearances_updated,
    }


class AppUpdate(BaseModel):
    enforce_auth: Optional[bool] = None
    llm_api_key: Optional[str] = None  # Update/rotate LLM API key
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None

@app.patch("/v1/auth/apps/{app_id}", tags=["Authentication"])
async def update_application(app_id: str, update: AppUpdate):
    from contextquilt.services.key_encryption import encrypt_key, mask_key

    try:
        if update.enforce_auth is not None:
            await db_pool.execute(
                "UPDATE applications SET enforce_auth = $1 WHERE app_id = $2",
                update.enforce_auth, app_id
            )
        if update.llm_api_key is not None:
            encrypted = encrypt_key(update.llm_api_key)
            await db_pool.execute(
                "UPDATE applications SET llm_api_key_encrypted = $1 WHERE app_id = $2",
                encrypted, app_id
            )
        if update.llm_base_url is not None:
            await db_pool.execute(
                "UPDATE applications SET llm_base_url = $1 WHERE app_id = $2",
                update.llm_base_url, app_id
            )
        if update.llm_model is not None:
            await db_pool.execute(
                "UPDATE applications SET llm_model = $1 WHERE app_id = $2",
                update.llm_model, app_id
            )
        return {"status": "updated", "llm_key_masked": mask_key(update.llm_api_key) if update.llm_api_key else None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
