"""
Context Quilt - Hot Path API (FastAPI)
Implements 'Zero-Latency' Context Enrichment & MCP Endpoints
"""

from fastapi import FastAPI, HTTPException, Depends, Header, Request, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any, Union
import os
import redis.asyncio as redis
import json
import re
import re
from datetime import datetime
import sys
import os

# Add src to path to allow imports if running from root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.dashboard.router import router as dashboard_router

import asyncpg
import uuid
import secrets
from fastapi.security import OAuth2PasswordRequestForm
from src import auth

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
    db_pool = await asyncpg.create_pool(os.getenv("DATABASE_URL"))

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

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/dashboard/")

# Redis Connection (Working Memory)
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    password=os.getenv("REDIS_PASSWORD", None),
    decode_responses=True
)

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
    interaction_type: str = Field(..., description="'chat_log', 'tool_call', or 'trace'")
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

    # Optional timestamp for backdating (e.g. historical import)
    timestamp: Optional[str] = None

    # Generic metadata — app-defined key-value pairs (e.g., meeting_id, project)
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
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Optional hints (e.g., project name)")
    max_hops: Optional[int] = Field(default=2, description="Graph traversal depth")

class RecallResponse(BaseModel):
    """Context recalled from the graph"""
    context: str
    matched_entities: List[str]
    patch_count: int

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
             raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # 2. Fallback to Legacy Mode (X-App-ID)
    app_id_to_check = x_app_id
    
    if not app_id_to_check:
        # If no token and no header, allow for now (legacy) but return None
        # Or should we require at least one?
        # Existing verify_app_id required X-App-ID.
        # Let's require it if no token.
        raise HTTPException(status_code=400, detail="X-App-ID header or Bearer Token required")

    # Check enforcement in DB
    try:
        # Ensure db_pool is initialized
        if not db_pool:
            return app_id_to_check
            
        row = await db_pool.fetchrow(
            "SELECT enforce_auth FROM applications WHERE app_id = $1",
            app_id_to_check
        )
        if row and row['enforce_auth']:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required for this application",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return app_id_to_check
    except Exception:
        # Fail open for legacy compatibility
        return app_id_to_check

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

    # Step 1: Find matching entities from Redis index (fast)
    entity_index_key = f"entity_index:{user_id}"
    known_entities = await redis_client.smembers(entity_index_key)

    matched_names = []
    if known_entities:
        for name in known_entities:
            if name.lower() in text_lower:
                matched_names.append(name)

    # Also check metadata hints
    if request.metadata:
        for val in request.metadata.values():
            if isinstance(val, str) and val not in matched_names:
                # Check if this metadata value is a known entity
                if known_entities and val in known_entities:
                    matched_names.append(val)

    if not matched_names:
        return RecallResponse(context="", matched_entities=[], patch_count=0)

    # Step 2: Traverse graph from matched entities (Postgres)
    # Find entity IDs for matched names
    entity_rows = await db_pool.fetch(
        """
        SELECT entity_id, name, entity_type, description
        FROM entities
        WHERE user_id = $1 AND name = ANY($2)
        """,
        user_id, matched_names
    )

    if not entity_rows:
        return RecallResponse(context="", matched_entities=matched_names, patch_count=0)

    entity_ids = [row["entity_id"] for row in entity_rows]

    # Step 3: Get relationships within N hops via recursive CTE
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
        SELECT DISTINCT g.from_entity_id, g.to_entity_id, g.relationship_type, g.context,
               e1.name as from_name, e1.entity_type as from_type,
               e2.name as to_name, e2.entity_type as to_type
        FROM graph g
        JOIN entities e1 ON g.from_entity_id = e1.entity_id
        JOIN entities e2 ON g.to_entity_id = e2.entity_id
        """,
        user_id, entity_ids, max_hops
    )

    # Step 4: Get patches for this user
    recall_project = request.metadata.get("project") if request.metadata else None
    subject_key = f"user:{user_id}"

    # Step 4a: Flat patch query (works for both V1 and V2 patches)
    if recall_project:
        fact_rows = await db_pool.fetch(
            """
            SELECT cp.value, cp.patch_type, cp.source_prompt
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND (cp.project = $2 OR cp.project IS NULL OR cp.patch_type IN ('trait', 'preference'))
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY cp.created_at DESC
            LIMIT 20
            """,
            subject_key, recall_project
        )
    else:
        fact_rows = await db_pool.fetch(
            """
            SELECT cp.value, cp.patch_type, cp.source_prompt
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY cp.created_at DESC
            LIMIT 20
            """,
            subject_key
        )

    # Step 4b: Traverse patch connections from project patches (Connected Quilt V2)
    connected_rows = []
    if recall_project:
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
                SELECT cp.value, cp.patch_type, cp.source_prompt
                FROM patch_connections pc
                JOIN context_patches cp ON pc.from_patch_id = cp.patch_id
                WHERE pc.to_patch_id = $1 AND pc.connection_role = 'parent'
                  AND COALESCE(cp.status, 'active') = 'active'
                """,
                project_patch["patch_id"]
            )

    # Merge flat rows + connected rows, deduplicate by value text
    all_patches = list(fact_rows)
    seen_texts = {(row["value"] if isinstance(row["value"], str) else json.dumps(row["value"])) for row in fact_rows}
    for row in connected_rows:
        key = row["value"] if isinstance(row["value"], str) else json.dumps(row["value"])
        if key not in seen_texts:
            all_patches.append(row)
            seen_texts.add(key)

    # Step 5: Build context block — grouped by patch type
    sections = []

    # Entities summary
    entity_map = {row["entity_id"]: row for row in entity_rows}
    people = [r for r in entity_rows if r["entity_type"] == "person"]
    projects = [r for r in entity_rows if r["entity_type"] == "project"]

    if projects:
        for p in projects:
            sections.append(f"Project: {p['name']} — {p['description'] or ''}")
    if people:
        sections.append("People: " + ", ".join(
            f"{p['name']} ({p['description']})" if p['description'] else p['name']
            for p in people
        ))

    # Relationships
    if rel_rows:
        rel_lines = []
        for r in rel_rows:
            rel_lines.append(f"{r['from_name']} {r['relationship_type']} {r['to_name']}"
                           + (f" ({r['context']})" if r['context'] else ""))
        sections.append("Connections:\n" + "\n".join(f"- {l}" for l in rel_lines))

    # Parse all patches by type
    def parse_value(row):
        v = row["value"]
        return json.loads(v) if isinstance(v, str) else v

    # About you (traits, preferences)
    universals = [parse_value(r) for r in all_patches if r["patch_type"] in ("trait", "preference")]
    if universals:
        sections.append("About you:\n" + "\n".join(f"- {v.get('text', '')}" for v in universals))

    # Decisions
    decisions = [parse_value(r) for r in all_patches if r["patch_type"] == "decision"]
    if decisions:
        sections.append("Decisions:\n" + "\n".join(f"- {v.get('text', '')}" for v in decisions))

    # Open commitments
    commitments = [parse_value(r) for r in all_patches if r["patch_type"] == "commitment"]
    if commitments:
        lines = []
        for v in commitments:
            owner = v.get("owner", "")
            deadline = v.get("deadline", "")
            dl = f" (by {deadline})" if deadline else ""
            prefix = f"{owner}: " if owner else ""
            lines.append(f"- {prefix}{v.get('text', '')}{dl}")
        sections.append("Open commitments:\n" + "\n".join(lines))

    # Blockers
    blockers = [parse_value(r) for r in all_patches if r["patch_type"] == "blocker"]
    if blockers:
        sections.append("Blockers:\n" + "\n".join(f"- {v.get('text', '')}" for v in blockers))

    # Roles
    roles = [parse_value(r) for r in all_patches if r["patch_type"] == "role"]
    if roles:
        sections.append("Roles:\n" + "\n".join(f"- {v.get('text', '')}" for v in roles))

    # Takeaways and remaining facts (experience, identity, person, etc.)
    other_types = ("experience", "identity", "takeaway", "person")
    others = [parse_value(r) for r in all_patches if r["patch_type"] in other_types]
    # Also include V1 action_items
    for r in all_patches:
        v = parse_value(r)
        if v.get("type") == "action_item":
            owner = v.get("owner", "")
            deadline = v.get("deadline", "")
            dl = f" (by {deadline})" if deadline else ""
            others.append({"text": f"{owner}: {v.get('text', '')}{dl}"})
    if others:
        sections.append("Key facts:\n" + "\n".join(f"- {v.get('text', '')}" for v in others[:10]))

    context = "\n\n".join(sections)

    return RecallResponse(
        context=context,
        matched_entities=matched_names,
        patch_count=len(fact_rows) + len(rel_rows),
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
    Trigger Cache Hydration.
    Moves data from Cold Storage (Postgres) to Hot Cache (Redis).
    """
    # Push 'hydrate' command to worker stream
    stream_key = "memory_updates"
    payload = {
        "type": "hydrate",
        "user_id": user_id,
        "app_id": app_id,
        "timestamp": datetime.utcnow().isoformat()
    }
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})
    
    return {"status": "queued", "message": "Hydration requested"}

@app.get("/health", tags=["Ops"])
async def health():
    return {"status": "healthy", "version": "3.10.0"}

@app.post("/v1/auth/register", response_model=auth.ApplicationResponse, tags=["Authentication"])
async def register_application(app_data: auth.ApplicationCreate):
    client_secret = secrets.token_urlsafe(32)
    secret_hash = auth.get_password_hash(client_secret)
    
    try:
        row = await db_pool.fetchrow(
            """
            INSERT INTO applications (app_name, client_secret_hash)
            VALUES ($1, $2)
            RETURNING app_id, created_at
            """,
            app_data.app_name, secret_hash
        )
        return {
            "app_id": str(row['app_id']),
            "app_name": app_data.app_name,
            "client_secret": client_secret,
            "created_at": row['created_at']
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
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect client_id or client_secret",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        access_token = auth.create_access_token(
            data={"sub": str(row['app_id'])}
        )
        return {"access_token": access_token, "token_type": "bearer", "expires_in": auth.ACCESS_TOKEN_EXPIRE_MINUTES * 60}
    except Exception as e:
        # Handle UUID conversion error or other DB errors
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
    patch_type: str = ""
    source: str = ""
    created_at: Optional[str] = None
    project: Optional[str] = None
    project_id: Optional[str] = None
    meeting_id: Optional[str] = None
    connections: List[PatchConnectionResponse] = []

class QuiltResponse(BaseModel):
    user_id: str
    facts: List[QuiltPatchResponse]
    action_items: List[QuiltPatchResponse]
    deleted: List[str] = []        # patch_ids removed since `since` timestamp
    server_time: Optional[str] = None  # use as `since` on next request

class PatchUpdate(BaseModel):
    fact: Optional[str] = None
    category: Optional[str] = None

@app.get("/v1/quilt/{user_id}", response_model=QuiltResponse, tags=["Quilt"])
async def get_user_quilt(
    user_id: str,
    category: Optional[str] = Query(None, description="Filter by patch_type"),
    since: Optional[str] = Query(None, description="ISO 8601 timestamp — return only patches created/updated after this time, plus IDs of patches deleted since then"),
    app_id: str = Depends(verify_application_access),
):
    """
    Get patches CQ knows about a user.

    Without `since`: returns all active patches (full sync).
    With `since`: returns only patches created or updated after that timestamp,
    plus a `deleted` array of patch_ids that were removed or archived since then.
    Pass the returned `server_time` as `since` on the next request.
    """
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
               cp.origin_mode, cp.source_prompt, cp.created_at, cp.project,
               cp.project_id, cp.meeting_id, cp.status
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

    query += " ORDER BY cp.created_at DESC"

    rows = await db_pool.fetch(query, *params)

    # Find patches deleted or archived since the timestamp
    deleted_ids = []
    if since_dt:
        deleted_rows = await db_pool.fetch(
            """
            SELECT cp.patch_id FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND cp.status IN ('archived', 'completed')
              AND cp.updated_at > $2
            """,
            subject_key, since_dt
        )
        deleted_ids = [str(r["patch_id"]) for r in deleted_rows]

    # Collect all patch IDs to fetch connections in one query
    patch_ids = [row["patch_id"] for row in rows]

    # Fetch all outgoing connections for these patches
    connections_by_patch: dict[str, list] = {}
    if patch_ids:
        conn_rows = await db_pool.fetch(
            """SELECT pc.from_patch_id, pc.to_patch_id,
                      pc.connection_role, pc.connection_label, pc.context
               FROM patch_connections pc
               WHERE pc.from_patch_id = ANY($1::uuid[])""",
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
            patch_type=row["patch_type"] or "",
            source=row["source_prompt"] or "",
            created_at=row["created_at"].isoformat() if row["created_at"] else None,
            project=row["project"],
            project_id=row.get("project_id"),
            meeting_id=row.get("meeting_id"),
            connections=connections_by_patch.get(pid, []),
        )

        if value.get("type") == "action_item":
            action_items.append(patch)
        else:
            facts.append(patch)

    return QuiltResponse(
        user_id=user_id,
        facts=facts,
        action_items=action_items,
        deleted=deleted_ids,
        server_time=server_time.isoformat() + "Z",
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

    if update.fact is not None:
        value["text"] = update.fact

    new_type = update.category if update.category else row["patch_type"]

    await db_pool.execute(
        """
        UPDATE context_patches
        SET value = $1, patch_type = $2, origin_mode = 'declared', updated_at = $3
        WHERE patch_id = $4
        """,
        json.dumps(value), new_type, datetime.utcnow(), patch_id
    )

    # Trigger cache refresh
    stream_key = "memory_updates"
    payload = {"type": "hydrate", "user_id": user_id, "timestamp": datetime.utcnow().isoformat()}
    await redis_client.xadd(stream_key, {"data": json.dumps(payload)})

    return {"status": "updated", "patch_id": patch_id}


@app.delete("/v1/quilt/{user_id}/patches/{patch_id}", tags=["Quilt"])
async def delete_patch(
    user_id: str,
    patch_id: str,
    app_id: str = Depends(verify_application_access),
):
    """
    Delete a fact or action item. User removes something CQ got wrong.
    Requires delete access via ACL (or no ACL entry = open access).
    """
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

    # Delete patch and related records
    await db_pool.execute("DELETE FROM patch_usage_metrics WHERE patch_id = $1", patch_id)
    await db_pool.execute("DELETE FROM context_patch_acl WHERE patch_id = $1", patch_id)
    await db_pool.execute("DELETE FROM patch_subjects WHERE patch_id = $1", patch_id)
    await db_pool.execute("DELETE FROM context_patches WHERE patch_id = $1", patch_id)

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
    Called by ShoulderSurf when a user renames "Speaker 4" to "SriDev".

    The app is responsible for updating patch text separately via PATCH /v1/quilt.
    This endpoint handles the graph layer (entities + relationships).
    """
    old = rename.old_name
    new = rename.new_name

    # Update entity name
    result = await db_pool.execute(
        "UPDATE entities SET name = $1, last_seen_at = NOW() WHERE user_id = $2 AND name = $3",
        new, user_id, old
    )

    # Update relationship context that mentions the old name
    await db_pool.execute(
        "UPDATE relationships SET context = REPLACE(context, $1, $2) WHERE user_id = $3 AND context LIKE '%' || $1 || '%'",
        old, new, user_id
    )

    # Rebuild Redis entity index so recall matches the new name
    entity_index_key = f"entity_index:{user_id}"
    all_names = await db_pool.fetch(
        "SELECT name FROM entities WHERE user_id = $1", user_id
    )
    if all_names:
        await redis_client.delete(entity_index_key)
        await redis_client.sadd(entity_index_key, *[r["name"] for r in all_names])
        await redis_client.expire(entity_index_key, 7200)

    return {"status": "renamed", "old_name": old, "new_name": new}


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

    if update.name is not None:
        await db_pool.execute(
            "UPDATE projects SET name = $1, updated_at = NOW() WHERE project_id = $2",
            update.name, project_id
        )
        # Also update the text project column on patches for backward compat
        await db_pool.execute(
            "UPDATE context_patches SET project = $1, updated_at = NOW() WHERE project_id = $2",
            update.name, project_id
        )

    if update.status == "archived":
        await db_pool.execute(
            "UPDATE projects SET status = 'archived', updated_at = NOW() WHERE project_id = $1",
            project_id
        )
        # Cascade: archive all patches belonging to this project
        await db_pool.execute(
            "UPDATE context_patches SET status = 'archived', updated_at = NOW() WHERE project_id = $1 AND COALESCE(status, 'active') = 'active'",
            project_id
        )

    return {"status": "updated", "project_id": project_id}


class AppUpdate(BaseModel):
    enforce_auth: bool

@app.patch("/v1/auth/apps/{app_id}", tags=["Authentication"])
async def update_application(app_id: str, update: AppUpdate):
    try:
        await db_pool.execute(
            "UPDATE applications SET enforce_auth = $1 WHERE app_id = $2",
            update.enforce_auth, app_id
        )
        return {"status": "updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
