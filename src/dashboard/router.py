from fastapi import APIRouter, Depends, HTTPException, Header, Request
from typing import List, Dict, Any, Optional
import asyncpg
from pydantic import BaseModel
from datetime import datetime, timedelta
import uuid
import json
import logging
import aiohttp
from contextquilt.config import get_settings
from contextquilt.gateway.extraction import classify_fact, extract_facts_from_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])


# Module-level config constants. Kept as constants (not inline
# `get_settings().x` at every call site) to minimize churn against the
# ~20 existing `asyncpg.connect(DATABASE_URL)` call sites.
_settings = get_settings()
DATABASE_URL = _settings.database_url
OLLAMA_URL = _settings.ollama_url


async def verify_admin_key(x_admin_key: str = Header(default="")):
    """Verify the admin key. If CQ_ADMIN_KEY is not set, access is open (dev mode)."""
    admin_key = get_settings().cq_admin_key
    if admin_key and x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")

@router.get("/verify-key")
async def verify_key(x_admin_key: str = Header(default="")):
    """Endpoint for the dashboard login to verify the admin key."""
    admin_key = get_settings().cq_admin_key
    if admin_key and x_admin_key != admin_key:
        raise HTTPException(status_code=403, detail="Invalid admin key")
    return {"status": "ok"}

# Dependency to get DB connection
async def get_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()

class DashboardStats(BaseModel):
    total_users: int
    total_facts: int

class PatchItem(BaseModel):
    patch_id: str
    user_id: str
    patch_name: str
    value: Any
    patch_type: str
    origin: str
    created_at: datetime

class TypeItem(BaseModel):
    label: str
    count: int

@router.get("/stats", response_model=DashboardStats, dependencies=[Depends(verify_admin_key)])
async def get_stats(days: Optional[int] = None, start_date: Optional[str] = None, end_date: Optional[str] = None):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        total_users = await conn.fetchval("SELECT COUNT(*) FROM profiles")
        
        if start_date and end_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
            total_patches = await conn.fetchval(
                "SELECT COUNT(*) FROM context_patches WHERE created_at::date >= $1::date AND created_at::date <= $2::date",
                start_dt, end_dt
            )
        elif days:
            total_patches = await conn.fetchval(
                "SELECT COUNT(*) FROM context_patches WHERE created_at >= NOW() - INTERVAL '1 day' * $1",
                days
            )
        else:
            total_patches = await conn.fetchval("SELECT COUNT(*) FROM context_patches")
        return {"total_users": total_users or 0, "total_facts": total_patches or 0}
    finally:
        await conn.close()

@router.get("/patches/recent", response_model=List[PatchItem], dependencies=[Depends(verify_admin_key)])
async def get_recent_patches(
    limit: int = 100, 
    patch_type: Optional[str] = None,
    origin: Optional[str] = None
):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Dynamic filtering
        filters = []
        params = []
        param_idx = 1
        
        if patch_type:
            filters.append(f"patch_type = ${param_idx}")
            params.append(patch_type)
            param_idx += 1
            
        if origin:
            filters.append(f"origin_mode = ${param_idx}")
            params.append(origin)
            param_idx += 1
            
        where_clause = "WHERE " + " AND ".join(filters) if filters else ""
        
        # Add limit as last param
        params.append(limit)
        
        query = f"""
            SELECT cp.patch_id, ps.subject_key, cp.patch_name, cp.value, cp.patch_type, cp.origin_mode, cp.created_at 
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            {where_clause}
            ORDER BY cp.created_at DESC 
            LIMIT ${param_idx}
        """
        rows = await conn.fetch(query, *params)
        
        results = []
        for row in rows:
            # Extract user_id from subject_key (e.g. "user:123" -> "123")
            subject = row['subject_key']
            user_id = subject.split(':')[1] if ':' in subject else subject
            
            results.append(PatchItem(
                patch_id=str(row['patch_id']),
                user_id=user_id,
                patch_name=row['patch_name'],
                value=row['value'],
                patch_type=row['patch_type'],
                origin=row['origin_mode'] or 'unknown',
                created_at=row['created_at']
            ))
            
        return results
    finally:
        await conn.close()

class HistoryItem(BaseModel):
    date: str
    counts: Dict[str, int]

@router.get("/patches/history", response_model=List[HistoryItem], dependencies=[Depends(verify_admin_key)])
async def get_patches_history(days: Optional[int] = 7, start_date: Optional[str] = None, end_date: Optional[str] = None):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        if days == 1 and not start_date:
            query = """
                WITH date_series AS (
                    SELECT generate_series(
                        date_trunc('hour', NOW()) - INTERVAL '23 hours',
                        date_trunc('hour', NOW()),
                        '1 hour'::interval
                    ) AS hour
                )
                SELECT 
                    ds.hour, 
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'trait') as trait,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'preference') as preference,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'goal') as goal,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'constraint') as "constraint",
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'person') as person,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'org') as org,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'project') as project,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'role') as role,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'decision') as "decision",
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'commitment') as commitment,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'blocker') as blocker,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'takeaway') as takeaway,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'event') as event
                FROM date_series ds
                LEFT JOIN context_patches p ON date_trunc('hour', p.created_at) = ds.hour
                GROUP BY ds.hour
                ORDER BY ds.hour ASC
            """
            rows = await conn.fetch(query)
            return [{
                "date": row['hour'].strftime("%H:%M"), 
                "counts": {
                    "trait": row['trait'] or 0,
                    "preference": row['preference'] or 0,
                    "goal": row['goal'] or 0,
                    "constraint": row['constraint'] or 0,
                    "person": row['person'] or 0,
                    "org": row['org'] or 0,
                    "project": row['project'] or 0,
                    "role": row['role'] or 0,
                    "decision": row['decision'] or 0,
                    "commitment": row['commitment'] or 0,
                    "blocker": row['blocker'] or 0,
                    "takeaway": row['takeaway'] or 0,
                    "event": row['event'] or 0,
                }
            } for row in rows]

        elif start_date and end_date:
            # Custom Date Range (Daily granularity)
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
            
            query = """
                WITH date_series AS (
                    SELECT generate_series(
                        $1::date,
                        $2::date,
                        '1 day'::interval
                    )::date AS day
                )
                SELECT 
                    ds.day, 
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'trait') as trait,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'preference') as preference,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'goal') as goal,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'constraint') as "constraint",
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'person') as person,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'org') as org,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'project') as project,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'role') as role,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'decision') as "decision",
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'commitment') as commitment,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'blocker') as blocker,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'takeaway') as takeaway,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'event') as event
                FROM date_series ds
                LEFT JOIN context_patches p ON date_trunc('day', p.created_at)::date = ds.day
                GROUP BY ds.day
                ORDER BY ds.day ASC
            """
            rows = await conn.fetch(query, start_dt, end_dt)
            return [{
                "date": str(row['day']), 
                "counts": {
                    "trait": row['trait'] or 0,
                    "preference": row['preference'] or 0,
                    "goal": row['goal'] or 0,
                    "constraint": row['constraint'] or 0,
                    "person": row['person'] or 0,
                    "org": row['org'] or 0,
                    "project": row['project'] or 0,
                    "role": row['role'] or 0,
                    "decision": row['decision'] or 0,
                    "commitment": row['commitment'] or 0,
                    "blocker": row['blocker'] or 0,
                    "takeaway": row['takeaway'] or 0,
                    "event": row['event'] or 0,
                }
            } for row in rows]

        else:

            query = """
                WITH date_series AS (
                    SELECT generate_series(
                        date_trunc('day', NOW()) - (INTERVAL '1 day' * ($1 - 1)),
                        date_trunc('day', NOW()),
                        '1 day'::interval
                    )::date AS day
                )
                SELECT 
                    ds.day, 
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'trait') as trait,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'preference') as preference,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'goal') as goal,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'constraint') as "constraint",
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'person') as person,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'org') as org,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'project') as project,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'role') as role,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'decision') as "decision",
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'commitment') as commitment,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'blocker') as blocker,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'takeaway') as takeaway,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'event') as event
                FROM date_series ds
                LEFT JOIN context_patches p ON date_trunc('day', p.created_at)::date = ds.day
                GROUP BY ds.day
                ORDER BY ds.day ASC
            """
            rows = await conn.fetch(query, days)
            return [{
                "date": str(row['day']), 
                "counts": {
                    "trait": row['trait'] or 0,
                    "preference": row['preference'] or 0,
                    "goal": row['goal'] or 0,
                    "constraint": row['constraint'] or 0,
                    "person": row['person'] or 0,
                    "org": row['org'] or 0,
                    "project": row['project'] or 0,
                    "role": row['role'] or 0,
                    "decision": row['decision'] or 0,
                    "commitment": row['commitment'] or 0,
                    "blocker": row['blocker'] or 0,
                    "takeaway": row['takeaway'] or 0,
                    "event": row['event'] or 0,
                }
            } for row in rows]
    finally:
        await conn.close()

class IngestionLogEntry(BaseModel):
    metric_id: str
    created_at: str
    user_id: Optional[str]
    app_id: Optional[str]
    origin_id: Optional[str]
    origin_type: Optional[str]
    model: Optional[str]
    interaction_type: Optional[str]
    owner_speaker_label: Optional[str]
    owner_marker_present: Optional[bool]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cost_usd: Optional[float]
    latency_ms: Optional[float]
    patches_before_filters: Optional[int]
    patches_after_filters: Optional[int]
    owner_gate_filtered: Optional[int]
    connection_dropped: Optional[int]
    reasoning_chars: Optional[int]
    transcript_chars: Optional[int]
    patches_extracted: Optional[int]
    entities_extracted: Optional[int]


@router.get("/ingestion-log", response_model=List[IngestionLogEntry], dependencies=[Depends(verify_admin_key)])
async def get_ingestion_log(limit: int = 50, user_id: Optional[str] = None, app_id: Optional[str] = None):
    """Return recent ingestion events with full pipeline trace."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        conditions = []
        params = []
        idx = 1
        if user_id:
            conditions.append(f"user_id = ${idx}")
            params.append(user_id)
            idx += 1
        if app_id:
            conditions.append(f"app_id = ${idx}")
            params.append(app_id)
            idx += 1
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(min(limit, 200))
        query = f"""
            SELECT metric_id, created_at, user_id, app_id, origin_id, origin_type,
                   model, interaction_type, owner_speaker_label, owner_marker_present,
                   input_tokens, output_tokens, cost_usd, latency_ms,
                   patches_before_filters, patches_after_filters,
                   owner_gate_filtered, connection_dropped,
                   reasoning_chars, transcript_chars,
                   patches_extracted, entities_extracted
            FROM extraction_metrics
            {where}
            ORDER BY created_at DESC
            LIMIT ${idx}
        """
        rows = await conn.fetch(query, *params)
        return [
            IngestionLogEntry(
                metric_id=str(r["metric_id"]),
                created_at=r["created_at"].isoformat() if r["created_at"] else "",
                user_id=r["user_id"],
                app_id=r["app_id"],
                origin_id=r["origin_id"],
                origin_type=r["origin_type"],
                model=r["model"],
                interaction_type=r["interaction_type"],
                owner_speaker_label=r["owner_speaker_label"],
                owner_marker_present=r["owner_marker_present"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                cost_usd=r["cost_usd"],
                latency_ms=r["latency_ms"],
                patches_before_filters=r["patches_before_filters"],
                patches_after_filters=r["patches_after_filters"],
                owner_gate_filtered=r["owner_gate_filtered"],
                connection_dropped=r["connection_dropped"],
                reasoning_chars=r["reasoning_chars"],
                transcript_chars=r["transcript_chars"],
                patches_extracted=r["patches_extracted"],
                entities_extracted=r["entities_extracted"],
            )
            for r in rows
        ]
    finally:
        await conn.close()


@router.get("/apps", response_model=List[str], dependencies=[Depends(verify_admin_key)])
async def get_apps():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Note: context_patches doesn't natively store creator app_id yet (only in ACL or metadata)
        # For now, we return empty or check 'source' if we map it later. 
        # Returning explicit empty list to avoid errors in UI relying on this
        # or we could fetch from 'applications' table directly?
        # The UI uses this for filtering. Let's fetch registered apps instead.
        rows = await conn.fetch("SELECT app_name FROM applications")
        return [row['app_name'] for row in rows]
    finally:
        await conn.close()


class SystemPrompt(BaseModel):
    prompt_key: str
    prompt_name: str
    description: Optional[str]
    prompt_text: str
    version_num: int
    updated_at: datetime

@router.get("/prompts", response_model=List[SystemPrompt], dependencies=[Depends(verify_admin_key)])
async def get_prompts():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Join Registry + Active Version
        query = """
            SELECT pr.prompt_key, pr.prompt_name, pr.description, 
                   pv.prompt_text, pv.version_num, pv.created_at as updated_at
            FROM prompt_registry pr
            JOIN prompt_versions pv ON pr.prompt_key = pv.prompt_key
            WHERE pv.is_active = TRUE
            ORDER BY pr.prompt_name
        """
        rows = await conn.fetch(query)
        return [SystemPrompt(**dict(row)) for row in rows]
    finally:
        await conn.close()

class UpdatePromptRequest(BaseModel):
    prompt_text: str
    change_reason: Optional[str] = "Manual update via dashboard"

@router.put("/prompts/{prompt_key}", dependencies=[Depends(verify_admin_key)])
async def update_prompt(prompt_key: str, request: UpdatePromptRequest):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        async with conn.transaction():
            # 1. Get current max version
            current_ver = await conn.fetchval(
                "SELECT COALESCE(MAX(version_num), 0) FROM prompt_versions WHERE prompt_key = $1", 
                prompt_key
            )
            
            if current_ver is None:
                 raise HTTPException(status_code=404, detail="Prompt not found")

            # 2. Deactivate old versions
            await conn.execute(
                "UPDATE prompt_versions SET is_active = FALSE WHERE prompt_key = $1", 
                prompt_key
            )
            
            # 3. Insert new version
            new_ver = current_ver + 1
            version_id = str(uuid.uuid4())
            
            await conn.execute(
                """
                INSERT INTO prompt_versions (version_id, prompt_key, version_num, prompt_text, is_active, change_reason)
                VALUES ($1, $2, $3, $4, TRUE, $5)
                """,
                version_id, prompt_key, new_ver, request.prompt_text, request.change_reason
            )
            
        return {"status": "success", "new_version": new_ver}
    finally:
        await conn.close()

class DistributionItem(BaseModel):
    label: str
    count: int

@router.get("/patches/distribution", response_model=List[DistributionItem], dependencies=[Depends(verify_admin_key)])
async def get_patches_distribution(
    group_by: str = "patch_type", 
    days: Optional[int] = None, 
    start_date: Optional[str] = None, 
    end_date: Optional[str] = None
):
    """Get distribution of patches grouped by patch_type or origin_mode."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Map frontend group keys to DB columns
        column_map = {
            "patch_type": "patch_type",
            "origin": "origin_mode",
            "source": "origin_mode", # Legacy compat
            "source_prompt": "source_prompt"
        }
        
        db_column = column_map.get(group_by, "patch_type")
        
        # Build query with optional time filter
        if days:
            query = f"""
                SELECT COALESCE({db_column}, 'Unknown') as label, COUNT(*) as count
                FROM context_patches
                WHERE created_at >= NOW() - INTERVAL '1 day' * $1
                GROUP BY {db_column}
                ORDER BY count DESC
                LIMIT 10
            """
            rows = await conn.fetch(query, days)
        elif start_date and end_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
            query = f"""
                SELECT COALESCE({db_column}, 'Unknown') as label, COUNT(*) as count
                FROM context_patches
                WHERE created_at::date >= $1::date AND created_at::date <= $2::date
                GROUP BY {db_column}
                ORDER BY count DESC
                LIMIT 10
            """
            rows = await conn.fetch(query, start_dt, end_dt)
        else:
            query = f"""
                SELECT COALESCE({db_column}, 'Unknown') as label, COUNT(*) as count
                FROM context_patches
                GROUP BY {db_column}
                ORDER BY count DESC
                LIMIT 10
            """
            rows = await conn.fetch(query)
        
        return [{"label": row['label'], "count": row['count']} for row in rows]
    finally:
        await conn.close()
class UserSummaryItem(BaseModel):
    user_id: str
    display_name: Optional[str]
    email: Optional[str]
    patch_count: int
    last_updated: Optional[datetime]
    last_provided: Optional[str] # Mocked for now (e.g. "2 hours ago")
    first_name: Optional[str]
    last_name: Optional[str]

@router.get("/users", response_model=List[UserSummaryItem], dependencies=[Depends(verify_admin_key)])
async def get_users():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        query = """
            SELECT
                p.user_id,
                p.display_name,
                p.email,
                p.variables,
                COUNT(ps.patch_id) as patch_count,
                MAX(cp.created_at) as last_updated
            FROM profiles p
            LEFT JOIN patch_subjects ps ON ps.subject_key = 'user:' || p.user_id
            LEFT JOIN context_patches cp ON cp.patch_id = ps.patch_id
            GROUP BY p.user_id, p.display_name, p.email, p.variables
            ORDER BY last_updated DESC NULLS LAST
        """
        rows = await conn.fetch(query)

        results = []
        import json
        for row in rows:
            mock_last_provided = "1 hour ago"

            variables = row['variables']
            if isinstance(variables, str):
                try:
                    variables = json.loads(variables)
                except:
                    variables = {}

            first_name = variables.get('first_name')
            last_name = variables.get('last_name')

            results.append(UserSummaryItem(
                user_id=row['user_id'] or "Unknown",
                display_name=row['display_name'],
                email=row['email'],
                patch_count=row['patch_count'],
                last_updated=row['last_updated'],
                last_provided=mock_last_provided,
                first_name=first_name,
                last_name=last_name,
            ))

        return results
    finally:
        await conn.close()

class PatchConnection(BaseModel):
    to_patch_id: str
    to_patch_type: Optional[str] = None
    to_text: Optional[str] = None
    role: str
    label: Optional[str] = None
    context: Optional[str] = None


class QuiltPatch(BaseModel):
    patch_id: str
    patch_name: str
    value: Any
    patch_type: str
    origin_mode: str
    source_prompt: str
    confidence: float
    sensitivity: str
    created_at: datetime
    user_id: str
    origin_id: Optional[str] = None
    origin_type: Optional[str] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    status: Optional[str] = None
    connections: List[PatchConnection] = []

class TimelineEvent(BaseModel):
    date: str
    description: str
    sentiment: Optional[str] = "neutral"

class UserQuilt(BaseModel):
    user_id: str
    display_name: Optional[str] = None
    email: Optional[str] = None
    patches: List[QuiltPatch]
    timeline: List[TimelineEvent]
    communication_profile: Optional[Dict[str, Any]] = None

@router.get("/users/{user_id}/quilt", response_model=UserQuilt, dependencies=[Depends(verify_admin_key)])
async def get_user_quilt(
    user_id: str,
    origin_id: Optional[str] = None,
    origin_type: Optional[str] = None,
):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        subject_key = f"user:{user_id}"

        profile_row = await conn.fetchrow(
            "SELECT display_name, email, variables->'communication_profile' as comm_profile FROM profiles WHERE user_id = $1", user_id
        )

        # Base query with optional origin filter
        origin_filter = ""
        params = [subject_key]
        if origin_id and origin_type:
            origin_filter = f"AND cp.origin_type = ${len(params) + 1} AND cp.origin_id = ${len(params) + 2}"
            params.append(origin_type)
            params.append(origin_id)

        patch_rows = await conn.fetch(f"""
            SELECT cp.patch_id, cp.patch_name, cp.value, cp.patch_type, cp.origin_mode, cp.created_at,
                   cp.source_prompt, cp.confidence, cp.sensitivity, cp.origin_id, cp.origin_type,
                   cp.project_id, cp.status, pr.name as project_name
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            LEFT JOIN projects pr ON cp.project_id = pr.project_id
            WHERE ps.subject_key = $1 {origin_filter}
            ORDER BY cp.created_at DESC
        """, *params)

        # Fetch all connections for this user's patches in one query
        conn_rows = await conn.fetch("""
            SELECT pc.from_patch_id, pc.to_patch_id, pc.connection_role, pc.connection_label, pc.context,
                   cp2.patch_type as to_patch_type, cp2.value as to_value
            FROM patch_connections pc
            JOIN patch_subjects ps ON pc.from_patch_id = ps.patch_id
            LEFT JOIN context_patches cp2 ON pc.to_patch_id = cp2.patch_id
            WHERE ps.subject_key = $1
        """, subject_key)

        # Build connection lookup: from_patch_id -> [connections]
        conn_map: dict = {}
        for cr in conn_rows:
            fid = str(cr['from_patch_id'])
            to_value = cr['to_value']
            to_text = None
            if to_value:
                if isinstance(to_value, str):
                    try:
                        parsed = json.loads(to_value)
                        to_text = parsed.get('text', str(to_value)[:80])
                    except Exception:
                        to_text = str(to_value)[:80]
                elif isinstance(to_value, dict):
                    to_text = to_value.get('text', str(to_value)[:80])
                else:
                    to_text = str(to_value)[:80]
            conn_map.setdefault(fid, []).append(PatchConnection(
                to_patch_id=str(cr['to_patch_id']),
                to_patch_type=cr['to_patch_type'],
                to_text=to_text,
                role=cr['connection_role'] or '',
                label=cr['connection_label'],
                context=cr['context'],
            ))

        patches = []
        timeline = []

        for row in patch_rows:
            pid = str(row['patch_id'])
            patches.append(QuiltPatch(
                patch_id=pid,
                patch_name=row['patch_name'],
                value=str(row['value']),
                patch_type=row['patch_type'],
                origin_mode=row['origin_mode'] or 'system',
                source_prompt=row['source_prompt'] or 'none',
                confidence=float(row['confidence']) if row['confidence'] is not None else 1.0,
                sensitivity=row['sensitivity'] or 'normal',
                created_at=row['created_at'],
                user_id=user_id,
                origin_id=row['origin_id'],
                origin_type=row['origin_type'],
                project_id=row['project_id'],
                project_name=row['project_name'],
                status=row['status'],
                connections=conn_map.get(pid, []),
            ))

            desc = str(row['value'])
            timeline.append(TimelineEvent(
                date=row['created_at'].strftime("%b %d"),
                description=desc[:100] + "..." if len(desc) > 100 else desc,
                sentiment="neutral"
            ))

        comm_profile = None
        if profile_row and profile_row['comm_profile']:
            cp = profile_row['comm_profile']
            comm_profile = json.loads(cp) if isinstance(cp, str) else cp

        return UserQuilt(
            user_id=user_id,
            display_name=profile_row['display_name'] if profile_row else None,
            email=profile_row['email'] if profile_row else None,
            patches=patches,
            timeline=timeline[:20],
            communication_profile=comm_profile,
        )

    finally:
        await conn.close()

class PatchUpdateRequest(BaseModel):
    fact: Optional[str] = None
    category: Optional[str] = None

@router.patch("/patches/{patch_id}", dependencies=[Depends(verify_admin_key)])
async def update_patch(patch_id: str, request: PatchUpdateRequest):
    """Update a patch's fact text and/or category from the admin dashboard."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Verify patch exists
        row = await conn.fetchrow(
            "SELECT patch_id, value, patch_type FROM context_patches WHERE patch_id = $1",
            uuid.UUID(patch_id)
        )
        if not row:
            raise HTTPException(status_code=404, detail="Patch not found")

        async with conn.transaction():
            if request.fact is not None:
                current_value = row['value']
                if isinstance(current_value, dict):
                    current_value['text'] = request.fact
                else:
                    current_value = {'text': request.fact}
                await conn.execute(
                    "UPDATE context_patches SET value = $1, updated_at = NOW(), origin_mode = 'declared' WHERE patch_id = $2",
                    json.dumps(current_value), uuid.UUID(patch_id)
                )
            if request.category is not None:
                await conn.execute(
                    "UPDATE context_patches SET patch_type = $1, updated_at = NOW() WHERE patch_id = $2",
                    request.category, uuid.UUID(patch_id)
                )

        return {"status": "updated", "patch_id": patch_id}
    finally:
        await conn.close()

@router.delete("/patches/{patch_id}", dependencies=[Depends(verify_admin_key)])
async def delete_patch(patch_id: str):
    """Delete a patch from the admin dashboard."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        pid = uuid.UUID(patch_id)
        # Verify patch exists
        row = await conn.fetchrow(
            "SELECT patch_id FROM context_patches WHERE patch_id = $1", pid
        )
        if not row:
            raise HTTPException(status_code=404, detail="Patch not found")

        async with conn.transaction():
            await conn.execute("DELETE FROM patch_usage_metrics WHERE patch_id = $1", pid)
            await conn.execute("DELETE FROM context_patch_acl WHERE patch_id = $1", pid)
            await conn.execute("DELETE FROM patch_subjects WHERE patch_id = $1", pid)
            await conn.execute("DELETE FROM context_patches WHERE patch_id = $1", pid)

        return {"status": "deleted", "patch_id": patch_id}
    finally:
        await conn.close()

# ============================================================
# Patch Type Manager (replaces mock schema endpoints)
# ============================================================

class PatchTypeItem(BaseModel):
    type_key: str
    app_id: Optional[str]
    display_name: str
    schema_def: Any
    persistence: str
    default_ttl_days: Optional[int]
    is_completable: bool
    project_scoped: bool

class PatchTypeCreate(BaseModel):
    type_key: str
    display_name: str
    persistence: str = "sticky"
    default_ttl_days: Optional[int] = None
    is_completable: bool = False
    project_scoped: bool = False

class PatchTypeUpdate(BaseModel):
    persistence: Optional[str] = None
    default_ttl_days: Optional[int] = None
    is_completable: Optional[bool] = None
    project_scoped: Optional[bool] = None

@router.get("/patch-types", response_model=List[PatchTypeItem], dependencies=[Depends(verify_admin_key)])
async def get_patch_types():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT * FROM patch_type_registry ORDER BY type_key")
        return [PatchTypeItem(
            type_key=r["type_key"], app_id=str(r["app_id"]) if r["app_id"] else None,
            display_name=r["display_name"], schema_def=r["schema"],
            persistence=r["persistence"], default_ttl_days=r["default_ttl_days"],
            is_completable=r["is_completable"], project_scoped=r["project_scoped"]
        ) for r in rows]
    finally:
        await conn.close()

@router.post("/patch-types", dependencies=[Depends(verify_admin_key)])
async def create_patch_type(req: PatchTypeCreate):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            """INSERT INTO patch_type_registry (type_key, display_name, schema, persistence,
                default_ttl_days, is_completable, project_scoped)
            VALUES ($1, $2, '{"text": "string"}'::jsonb, $3, $4, $5, $6)""",
            req.type_key, req.display_name, req.persistence,
            req.default_ttl_days, req.is_completable, req.project_scoped
        )
        return {"status": "created", "type_key": req.type_key}
    finally:
        await conn.close()

@router.put("/patch-types/{type_key}", dependencies=[Depends(verify_admin_key)])
async def update_patch_type(type_key: str, req: PatchTypeUpdate):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        sets, params, idx = [], [], 1
        if req.persistence is not None:
            sets.append(f"persistence = ${idx}"); params.append(req.persistence); idx += 1
        if req.default_ttl_days is not None:
            sets.append(f"default_ttl_days = ${idx}"); params.append(req.default_ttl_days); idx += 1
        if req.is_completable is not None:
            sets.append(f"is_completable = ${idx}"); params.append(req.is_completable); idx += 1
        if req.project_scoped is not None:
            sets.append(f"project_scoped = ${idx}"); params.append(req.project_scoped); idx += 1
        if not sets:
            return {"status": "no_changes"}
        params.append(type_key)
        await conn.execute(
            f"UPDATE patch_type_registry SET {', '.join(sets)} WHERE type_key = ${idx}", *params
        )
        return {"status": "updated", "type_key": type_key}
    finally:
        await conn.close()

class ConnectionItem(BaseModel):
    label: str
    app_id: Optional[str]
    role: str
    from_types: List[str]
    to_types: List[str]
    description: Optional[str]

class ConnectionCreate(BaseModel):
    label: str
    role: str
    from_types: List[str] = []
    to_types: List[str] = []
    description: Optional[str] = None

class ConnectionUpdate(BaseModel):
    role: Optional[str] = None
    from_types: Optional[List[str]] = None
    to_types: Optional[List[str]] = None

@router.get("/connections", response_model=List[ConnectionItem], dependencies=[Depends(verify_admin_key)])
async def get_connections():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch("SELECT * FROM connection_vocabulary ORDER BY label")
        return [ConnectionItem(
            label=r["label"], app_id=str(r["app_id"]) if r["app_id"] else None,
            role=r["role"], from_types=list(r["from_types"] or []),
            to_types=list(r["to_types"] or []), description=r.get("description")
        ) for r in rows]
    finally:
        await conn.close()

@router.post("/connections", dependencies=[Depends(verify_admin_key)])
async def create_connection(req: ConnectionCreate):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            """INSERT INTO connection_vocabulary (label, app_id, role, from_types, to_types, description)
            VALUES ($1, NULL, $2, $3, $4, $5)""",
            req.label, req.role, req.from_types, req.to_types, req.description
        )
        return {"status": "created", "label": req.label}
    finally:
        await conn.close()

@router.put("/connections/{label}", dependencies=[Depends(verify_admin_key)])
async def update_connection(label: str, req: ConnectionUpdate):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        sets, params, idx = [], [], 1
        if req.role is not None:
            sets.append(f"role = ${idx}"); params.append(req.role); idx += 1
        if req.from_types is not None:
            sets.append(f"from_types = ${idx}"); params.append(req.from_types); idx += 1
        if req.to_types is not None:
            sets.append(f"to_types = ${idx}"); params.append(req.to_types); idx += 1
        if not sets:
            return {"status": "no_changes"}
        params.append(label)
        await conn.execute(
            f"UPDATE connection_vocabulary SET {', '.join(sets)} WHERE label = ${idx}", *params
        )
        return {"status": "updated", "label": label}
    finally:
        await conn.close()

# ============================================================
# Extraction Cost Tracking (replaces mock ROI)
# ============================================================

class MetricsSummary(BaseModel):
    total_cost: float
    total_extractions: int
    avg_cost: float
    avg_latency: float

class CostDataPoint(BaseModel):
    date: str
    cost: float
    count: int

class ModelCost(BaseModel):
    model: str
    cost: float
    count: int

class TierCost(BaseModel):
    tier: str
    cost: float
    count: int
    avg_patches: float

class ExtractionMetricItem(BaseModel):
    user_id: Optional[str]
    model: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cost_usd: Optional[float]
    latency_ms: Optional[float]
    patches_extracted: Optional[int]
    entities_extracted: Optional[int]
    created_at: datetime

@router.get("/metrics/summary", response_model=MetricsSummary, dependencies=[Depends(verify_admin_key)])
async def get_metrics_summary(days: Optional[int] = 30):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            """SELECT COALESCE(SUM(cost_usd), 0) as total_cost,
                      COUNT(*) as total_extractions,
                      COALESCE(AVG(cost_usd), 0) as avg_cost,
                      COALESCE(AVG(latency_ms), 0) as avg_latency
            FROM extraction_metrics
            WHERE created_at >= NOW() - INTERVAL '1 day' * $1""", days
        )
        return MetricsSummary(
            total_cost=float(row["total_cost"]),
            total_extractions=row["total_extractions"],
            avg_cost=float(row["avg_cost"]),
            avg_latency=float(row["avg_latency"])
        )
    finally:
        await conn.close()

@router.get("/metrics/cost", response_model=List[CostDataPoint], dependencies=[Depends(verify_admin_key)])
async def get_metrics_cost(days: Optional[int] = 30, start_date: Optional[str] = None, end_date: Optional[str] = None):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        if start_date and end_date:
            rows = await conn.fetch(
                """SELECT created_at::date as day, SUM(cost_usd) as cost, COUNT(*) as count
                FROM extraction_metrics
                WHERE created_at::date >= $1::date AND created_at::date <= $2::date
                GROUP BY day ORDER BY day""",
                datetime.strptime(start_date, "%Y-%m-%d").date(),
                datetime.strptime(end_date, "%Y-%m-%d").date()
            )
        else:
            rows = await conn.fetch(
                """SELECT created_at::date as day, SUM(cost_usd) as cost, COUNT(*) as count
                FROM extraction_metrics
                WHERE created_at >= NOW() - INTERVAL '1 day' * $1
                GROUP BY day ORDER BY day""", days
            )
        return [CostDataPoint(date=str(r["day"]), cost=float(r["cost"]), count=r["count"]) for r in rows]
    finally:
        await conn.close()

@router.get("/metrics/models", response_model=List[ModelCost], dependencies=[Depends(verify_admin_key)])
async def get_metrics_models(days: Optional[int] = 30):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """SELECT model, SUM(cost_usd) as cost, COUNT(*) as count
            FROM extraction_metrics
            WHERE created_at >= NOW() - INTERVAL '1 day' * $1
            GROUP BY model ORDER BY cost DESC""", days
        )
        return [ModelCost(model=r["model"] or "unknown", cost=float(r["cost"]), count=r["count"]) for r in rows]
    finally:
        await conn.close()


@router.get("/metrics/tiers", response_model=List[TierCost], dependencies=[Depends(verify_admin_key)])
async def get_metrics_tiers(days: Optional[int] = 30):
    """
    Extraction cost grouped by subscription_tier.

    Answers the operator's "how much are we paying for Free user
    extractions?" question — the line item we're concerned about given
    that Free users can't read what they captured (recall is disabled
    for them on the GP side).

    Tier label "(missing)" covers any rows ingested before GP started
    forwarding subscription_tier on /v1/memory.
    """
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """SELECT COALESCE(subscription_tier, '(missing)') AS tier,
                      SUM(cost_usd) AS cost,
                      COUNT(*) AS count,
                      COALESCE(AVG(patches_extracted), 0) AS avg_patches
            FROM extraction_metrics
            WHERE created_at >= NOW() - INTERVAL '1 day' * $1
            GROUP BY COALESCE(subscription_tier, '(missing)')
            ORDER BY cost DESC""", days
        )
        return [
            TierCost(
                tier=r["tier"],
                cost=float(r["cost"] or 0),
                count=r["count"],
                avg_patches=float(r["avg_patches"] or 0),
            )
            for r in rows
        ]
    finally:
        await conn.close()

@router.get("/metrics/recent", response_model=List[ExtractionMetricItem], dependencies=[Depends(verify_admin_key)])
async def get_metrics_recent(limit: int = 50):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            "SELECT * FROM extraction_metrics ORDER BY created_at DESC LIMIT $1", limit
        )
        return [ExtractionMetricItem(
            user_id=r["user_id"], model=r["model"],
            input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
            cost_usd=r["cost_usd"], latency_ms=r["latency_ms"],
            patches_extracted=r["patches_extracted"], entities_extracted=r["entities_extracted"],
            created_at=r["created_at"]
        ) for r in rows]
    finally:
        await conn.close()

# ============================================================
# System Health
# ============================================================

import time
import redis.asyncio as aioredis

# Composed once via Settings (REDIS_URL wins; else compose from
# REDIS_HOST/PORT/PASSWORD). Same logic as `src/worker.py`.
REDIS_URL = _settings.redis_url

@router.get("/health-check", dependencies=[Depends(verify_admin_key)])
async def health_check():
    result = {"postgres": {}, "redis": {}, "worker": {}, "llm": {}, "config": {}}

    # Postgres
    try:
        start = time.monotonic()
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.fetchval("SELECT 1")
        latency = (time.monotonic() - start) * 1000
        patch_count = await conn.fetchval("SELECT COUNT(*) FROM context_patches")
        user_count = await conn.fetchval("SELECT COUNT(*) FROM profiles")
        await conn.close()
        result["postgres"] = {"status": "connected", "latency_ms": round(latency, 1),
                             "patches": patch_count, "users": user_count}
    except Exception as e:
        result["postgres"] = {"status": "disconnected", "error": str(e)}

    # Redis
    try:
        r = aioredis.from_url(REDIS_URL)
        start = time.monotonic()
        await r.ping()
        latency = (time.monotonic() - start) * 1000
        queue_keys = len(await r.keys("meeting_queue:*"))
        entity_keys = len(await r.keys("entity_index:*"))
        # Worker queue depth
        pending = 0
        try:
            groups = await r.xinfo_groups("memory_updates")
            pending = sum(g.get("pending", 0) for g in groups)
        except Exception:
            pass
        await r.close()
        result["redis"] = {"status": "connected", "latency_ms": round(latency, 1),
                          "queue_keys": queue_keys, "entity_keys": entity_keys}
        result["worker"] = {"pending_events": pending}
    except Exception as e:
        result["redis"] = {"status": "disconnected", "error": str(e)}
        result["worker"] = {"pending_events": -1}

    # LLM config (effective Settings values; pre-refactor showed
    # "not configured" only when env was unset, but Settings' defaults
    # are the actual production fallbacks, so showing them is more
    # accurate than masking).
    s = get_settings()
    result["llm"] = {
        "model": s.cq_llm_model,
        "base_url": s.cq_llm_base_url,
    }

    # Runtime config
    result["config"] = {
        "max_patches_per_meeting": s.cq_max_patches,
        "max_entities_per_meeting": s.cq_max_entities,
        "max_relationships_per_meeting": s.cq_max_relationships,
        "queue_max_wait_minutes": s.cq_queue_max_wait_minutes,
        "queue_budget_threshold": s.cq_queue_budget_threshold,
    }

    return result


# ============================================================
# Providers tab: managed LLM key surface
# ============================================================

# Maps the wire-level provider identifier to the (env_var, Settings field,
# SM secret name) triple. Keeps the API stable even if we rename the
# underlying env vars or add a third managed provider later.
_PROVIDER_KEY_MAPPING: Dict[str, Dict[str, str]] = {
    "anthropic": {
        "env_var": "CQ_ANTHROPIC_API_KEY",
        "settings_field": "cq_anthropic_api_key",
        "sm_secret_name": "anthropic-api-key",
    },
    "openrouter": {
        "env_var": "CQ_LLM_API_KEY",
        "settings_field": "cq_llm_api_key",
        "sm_secret_name": "openrouter-api-key",
    },
}


class UpdateKeyBody(BaseModel):
    provider: str   # "anthropic" | "openrouter"
    new_key: str    # the rotated value, sent in cleartext over TLS


class TestProviderBody(BaseModel):
    provider: str   # "anthropic" | "openrouter"


def _mask_key(key: str) -> str:
    """Display-safe masking. Six leading chars + last four — same shape
    GhostPour's admin dashboard uses, mirrors `mask_key` in
    `services/key_encryption.py`."""
    if not key or len(key) < 8:
        return "***"
    return key[:6] + "..." + key[-4:]


@router.get("/providers", dependencies=[Depends(verify_admin_key)])
async def list_providers():
    """Return current managed-key state per provider, masked.

    Pairs with `/api/dashboard/provider-health` (latest probe outcome
    per provider). The UI combines both: this endpoint gives "what's
    configured", that one gives "is it actually working right now".
    """
    from contextquilt.services.llm_client_anthropic import (
        DEFAULT_ANTHROPIC_MODEL,
    )

    s = get_settings()
    return {
        "anthropic": {
            "key_masked": _mask_key(s.cq_anthropic_api_key),
            "key_configured": bool(s.cq_anthropic_api_key),
            "model": s.cq_anthropic_model or DEFAULT_ANTHROPIC_MODEL,
        },
        "openrouter": {
            "key_masked": _mask_key(s.cq_llm_api_key),
            "key_configured": bool(s.cq_llm_api_key),
            "base_url": s.cq_llm_base_url,
            "model": s.cq_llm_model,
        },
    }


@router.post("/update-key", dependencies=[Depends(verify_admin_key)])
async def update_key(body: UpdateKeyBody):
    """Rotate a managed provider key. Live-mutates Settings + writes
    to Secret Manager so the next request uses the new key WITHOUT a
    container restart.

    Mirrors GhostPour's `app/routers/webhooks.py:update_key` flow
    exactly. Three steps, in order:

      1. `object.__setattr__` on the frozen Settings — pydantic's
         standard setattr is blocked, so this is the escape hatch the
         dashboard depends on. PR #121 deliberately verified this
         still works.
      2. `os.environ[env_var]` — keeps any future subprocess Python
         (e.g. management scripts run via `docker exec`) consistent
         with the in-process value.
      3. `persist_to_secret_manager(sm_name, new_key)` — durable copy
         so the rotation survives a container restart (next boot
         loads from SM via `ensure_secrets_in_env`).

    Then clear the `get_secret` lru_cache so the next caller resolves
    the fresh value rather than the previous one.

    SM failure is logged but does NOT roll back the in-memory mutation
    — the running process IS using the new key, the operator just
    needs to fix the SM persist failure (or re-paste the key after the
    next restart). Same trust boundary GP documents.
    """
    import os
    from contextquilt.secrets import get_secret, persist_to_secret_manager

    provider = body.provider.strip().lower()
    if provider not in _PROVIDER_KEY_MAPPING:
        raise HTTPException(
            status_code=400,
            detail=f"unknown provider '{body.provider}'. "
                   f"valid: {sorted(_PROVIDER_KEY_MAPPING.keys())}",
        )
    new_key = body.new_key.strip()
    if not new_key:
        raise HTTPException(status_code=400, detail="new_key is empty")

    mapping = _PROVIDER_KEY_MAPPING[provider]
    settings = get_settings()

    # Step 1: live-mutate Settings (frozen, but object.__setattr__
    # bypasses pydantic's guard — see test_object_setattr_live_mutation_works).
    object.__setattr__(settings, mapping["settings_field"], new_key)

    # Step 2: keep os.environ aligned with the in-process value so any
    # subprocess Python sees the same key.
    os.environ[mapping["env_var"]] = new_key

    # Step 3: durable copy in SM (auto-creates on first write). Failure
    # here is logged but doesn't fail the request — the running process
    # is already using the new value.
    sm_persisted = persist_to_secret_manager(mapping["sm_secret_name"], new_key)

    # Bust the secrets cache so the next get_secret() call returns the
    # fresh value (otherwise consumers that already pulled the old
    # value see staleness until process restart).
    get_secret.cache_clear()

    logger.info(
        f"provider_key_rotated provider={provider} sm_persisted={sm_persisted}"
    )
    return {
        "provider": provider,
        "key_masked": _mask_key(new_key),
        "sm_persisted": sm_persisted,
    }


@router.post("/test-provider", dependencies=[Depends(verify_admin_key)])
async def test_provider(body: TestProviderBody):
    """Probe one provider on demand. Same probe function the daemon
    runs every 15 minutes, but synchronous so the operator can see
    the result of a rotation immediately rather than waiting for the
    next scheduled probe.

    Result is persisted to `provider_health_probes` the same way the
    daemon does — keeps the audit trail complete and the dashboard
    status indicator updates without a refresh delay.
    """
    from contextquilt.services.provider_health import (
        probe_anthropic,
        probe_openrouter,
    )

    provider = body.provider.strip().lower()
    if provider == "anthropic":
        result = await probe_anthropic()
    elif provider == "openrouter":
        result = await probe_openrouter()
    else:
        raise HTTPException(
            status_code=400,
            detail=f"unknown provider '{body.provider}'",
        )

    # Persist so the dashboard status badge reflects the operator's
    # on-demand check, not just the daemon's 15-min cycle.
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute(
            """
            INSERT INTO provider_health_probes (
                provider, status, latency_ms,
                balance_usd, limit_usd, is_free_tier, error_message
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
    finally:
        await conn.close()

    return {
        "provider": result.provider,
        "status": result.status,
        "latency_ms": result.latency_ms,
        "balance_usd": result.balance_usd,
        "limit_usd": result.limit_usd,
        "is_free_tier": result.is_free_tier,
        "error": result.error,
    }


@router.get("/provider-health", dependencies=[Depends(verify_admin_key)])
async def provider_health():
    """Return the latest health probe per provider.

    Backs the future "Providers" dashboard tab (Gap 3); also useful as
    a generic operator probe today. Shape mirrors what GP's Providers
    tab returns: a per-provider dict carrying status + latency, plus
    balance/limit/is_free_tier for OpenRouter (Anthropic doesn't
    expose a usable public balance API).

    Returns an empty dict per provider if the daemon hasn't probed
    yet (worker still warming up, or has been down).
    """
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (provider)
                provider, probed_at, status, latency_ms,
                balance_usd, limit_usd, is_free_tier, error_message
              FROM provider_health_probes
             ORDER BY provider, probed_at DESC
            """
        )
    finally:
        await conn.close()

    out: Dict[str, Any] = {}
    for row in rows:
        entry: Dict[str, Any] = {
            "status": row["status"],
            "latency_ms": row["latency_ms"],
            "last_probed_at": row["probed_at"].isoformat() if row["probed_at"] else None,
            "error": row["error_message"],
        }
        # Balance fields are OpenRouter-only; omit on Anthropic rows so
        # the response doesn't carry confusing nulls there.
        if row["provider"] == "openrouter":
            entry["balance_usd"] = (
                float(row["balance_usd"]) if row["balance_usd"] is not None else None
            )
            entry["limit_usd"] = (
                float(row["limit_usd"]) if row["limit_usd"] is not None else None
            )
            entry["is_free_tier"] = row["is_free_tier"]
        out[row["provider"]] = entry
    return out


# ============================================================
# Configuration
# ============================================================

@router.get("/config", dependencies=[Depends(verify_admin_key)])
async def get_config():
    s = get_settings()
    return {
        "extraction": {
            "max_patches_per_meeting": s.cq_max_patches,
            "max_entities_per_meeting": s.cq_max_entities,
            "max_relationships_per_meeting": s.cq_max_relationships,
        },
        "queue": {
            "max_wait_minutes": s.cq_queue_max_wait_minutes,
            "budget_threshold": s.cq_queue_budget_threshold,
        },
        "llm": {
            "model": s.cq_llm_model,
            "base_url": s.cq_llm_base_url,
        }
    }


class TestPipelineRequest(BaseModel):
    messages: List[Dict[str, Any]]

class TestPipelineResponse(BaseModel):
    patches: List[QuiltPatch]
    raw_response: str
    prompt_used: str
    execution_time_ms: float
    tokens_generated: int

from fastapi.responses import StreamingResponse
import time

@router.post("/test-pipeline", dependencies=[Depends(verify_admin_key)])
async def test_learning_pipeline(request: TestPipelineRequest):
    """
    Dry-run the learning pipeline with streaming progress updates (SSE).
    """
    import asyncio  # Ensure asyncio is available
    logger.info(f"Test Pipeline requested for {len(request.messages)} messages")
    
    async def event_generator():
        # Helper to yield SSE
        async def send_event(event_data):
            yield f"data: {json.dumps(event_data)}\n\n"
            await asyncio.sleep(0.05) # Force flush

        # 1. PICKER (LLM Call)
        logger.info("Starting Picker Step")
        async for chunk in send_event({"type": "step_start", "step": "picker"}): yield chunk
        
        prompt_text = """<s>[INST] You are the Data Detective for the Context Quilt system.
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
    
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            row = await conn.fetchrow("""
                SELECT prompt_text 
                FROM prompt_versions 
                WHERE prompt_key = 'detective' AND is_active = TRUE
                ORDER BY version_num DESC 
                LIMIT 1
            """)
            if row:
                prompt_text = row["prompt_text"]
        finally:
            await conn.close()

        chat_text = json.dumps(request.messages, indent=2)
        final_prompt = prompt_text.replace("{chat_text}", chat_text)
        
        raw_response = ""
        picker_time = 0
        picker_tokens = 0
        
        try:
            start_time = time.perf_counter()
            logger.info(f"Calling Ollama with model qwen2.5-coder:7b-instruct. Prompt len: {len(final_prompt)}")
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    OLLAMA_URL,
                    json={
                        "model": "qwen2.5-coder:7b-instruct",
                        "prompt": final_prompt,
                        "stream": False
                    },
                    timeout=aiohttp.ClientTimeout(total=300) # Increased to 300s
                ) as response:
                    response.raise_for_status()
                    result = await response.json()
                    
                    raw_response = result.get("response", "")
                    server_time_ns = result.get("total_duration", 0)
                    picker_tokens = result.get("eval_count", 0)
            
            # Should match user experience (Handling cached responses properly)
            picker_time = (time.perf_counter() - start_time) * 1000
            
            logger.info(f"Ollama returned. Wall: {picker_time:.2f}ms. Server: {server_time_ns/1e6:.2f}ms")

            async for chunk in send_event({
                "type": "step_complete", 
                "step": "picker", 
                "time_ms": round(picker_time, 2), 
                "tokens": picker_tokens
            }): yield chunk

        except Exception as e:
            logger.error(f"Picker failed details: {e}")
            async for chunk in send_event({"type": "error", "message": f"Picker Failed: {str(e)}"}): yield chunk
            return

        # 2. STITCHER (Extraction)
        async for chunk in send_event({"type": "step_start", "step": "stitcher"}): yield chunk
        start_stitch = time.perf_counter()
        
        facts = extract_facts_from_response(raw_response)
        
        # Artificial delay for visual pacing if too fast
        if (time.perf_counter() - start_stitch) < 0.2:
            await asyncio.sleep(0.5)

        stitch_time = (time.perf_counter() - start_stitch) * 1000
        async for chunk in send_event({
            "type": "step_complete", 
            "step": "stitcher", 
            "time_ms": round(stitch_time, 2), 
            "tokens": 0
        }): yield chunk

        # 3. DESIGNER (Classification)
        async for chunk in send_event({"type": "step_start", "step": "designer"}): yield chunk
        start_design = time.perf_counter()
        
        simulated_patches = []
        for fact in facts:
            if isinstance(fact, str):
                fact_text = fact
            elif isinstance(fact, dict):
                fact_text = fact.get("fact", str(fact))
            else:
                fact_text = str(fact)
                
            category = classify_fact(fact_text)
            
            simulated_patches.append({
                "patch_id": str(uuid.uuid4()),
                "patch_name": "simulated_patch",
                "value": fact_text,
                "patch_type": category,
                "origin_mode": "inferred",
                "source_prompt": "detective",
                "confidence": 0.8,
                "sensitivity": "normal", # String for JSON
                "created_at": datetime.utcnow().isoformat(),
                "user_id": "simulated_user" 
            })

        # Artificial delay
        if (time.perf_counter() - start_design) < 0.2:
            await asyncio.sleep(0.4)

        design_time = (time.perf_counter() - start_design) * 1000
        async for chunk in send_event({
            "type": "step_complete", 
            "step": "designer", 
            "time_ms": round(design_time, 2), 
            "tokens": 0
        }): yield chunk

        # 4. CATALOGER (Archiving - Mock)
        async for chunk in send_event({"type": "step_start", "step": "cataloger"}): yield chunk
        await asyncio.sleep(0.4) 
        async for chunk in send_event({
            "type": "step_complete", 
            "step": "cataloger", 
            "time_ms": 400, 
            "tokens": 0
        }): yield chunk

        # FINAL RESULT
        async for chunk in send_event({
            "type": "result",
            "patches": simulated_patches,
            "raw_response": raw_response,
            "prompt_used": final_prompt
        }): yield chunk

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ============================================================
# Backup & DR
# ============================================================

class BackupStatus(BaseModel):
    health: str  # "healthy" | "stale" | "critical" | "unknown"
    health_reason: str
    last_success_at: Optional[datetime]
    last_success_age_hours: Optional[float]
    last_success_size_bytes: Optional[int]
    last_success_object: Optional[str]
    last_run_status: Optional[str]
    last_run_at: Optional[datetime]
    last_run_error: Optional[str]
    total_successes_30d: int
    total_failures_30d: int

class BackupRunItem(BaseModel):
    id: int
    started_at: datetime
    completed_at: Optional[datetime]
    status: str
    gcs_object: Optional[str]
    size_bytes: Optional[int]
    duration_seconds: Optional[float]
    error_message: Optional[str]


@router.get("/backup/status", response_model=BackupStatus, dependencies=[Depends(verify_admin_key)])
async def get_backup_status():
    """
    Surface DR posture for the dashboard. Health is derived from the
    age of the most recent successful backup:
      healthy   — newest success ≤ 26h old
      stale     — newest success 26–48h old
      critical  — newest success > 48h old, or no success ever, or last run failed
      unknown   — backup_runs table is empty (sidecar may not have started yet)
    """
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Last successful run
        last_success = await conn.fetchrow(
            """SELECT completed_at, size_bytes, gcs_object,
                      EXTRACT(EPOCH FROM (now() - completed_at))/3600.0 AS age_hours
               FROM backup_runs
               WHERE status = 'success'
               ORDER BY completed_at DESC
               LIMIT 1"""
        )

        # Most recent run regardless of status
        last_run = await conn.fetchrow(
            """SELECT status, started_at, completed_at, error_message
               FROM backup_runs
               ORDER BY started_at DESC
               LIMIT 1"""
        )

        # 30-day rollups
        rollup = await conn.fetchrow(
            """SELECT
                  COUNT(*) FILTER (WHERE status = 'success') AS successes,
                  COUNT(*) FILTER (WHERE status = 'failure') AS failures
               FROM backup_runs
               WHERE started_at >= now() - INTERVAL '30 days'"""
        )

        if last_run is None:
            health = "unknown"
            health_reason = "No backup runs recorded yet — sidecar may not have started or first run is pending."
        elif last_run["status"] == "failure":
            health = "critical"
            health_reason = f"Most recent backup attempt FAILED: {last_run['error_message'] or 'unknown error'}"
        elif last_success is None:
            health = "critical"
            health_reason = "No successful backup has ever completed."
        else:
            age_h = float(last_success["age_hours"])
            if age_h <= 26:
                health = "healthy"
                health_reason = f"Last successful backup {age_h:.1f}h ago."
            elif age_h <= 48:
                health = "stale"
                health_reason = f"Last successful backup {age_h:.1f}h ago — exceeds 26h freshness target."
            else:
                health = "critical"
                health_reason = f"Last successful backup {age_h:.1f}h ago — exceeds 48h critical threshold."

        return BackupStatus(
            health=health,
            health_reason=health_reason,
            last_success_at=last_success["completed_at"] if last_success else None,
            last_success_age_hours=round(float(last_success["age_hours"]), 2) if last_success else None,
            last_success_size_bytes=last_success["size_bytes"] if last_success else None,
            last_success_object=last_success["gcs_object"] if last_success else None,
            last_run_status=last_run["status"] if last_run else None,
            last_run_at=last_run["started_at"] if last_run else None,
            last_run_error=last_run["error_message"] if last_run else None,
            total_successes_30d=rollup["successes"] if rollup else 0,
            total_failures_30d=rollup["failures"] if rollup else 0,
        )
    finally:
        await conn.close()


@router.get("/backup/history", response_model=List[BackupRunItem], dependencies=[Depends(verify_admin_key)])
async def get_backup_history(limit: int = 30):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """SELECT id, started_at, completed_at, status, gcs_object,
                      size_bytes, duration_seconds, error_message
               FROM backup_runs
               ORDER BY started_at DESC
               LIMIT $1""",
            min(limit, 200),
        )
        return [
            BackupRunItem(
                id=r["id"],
                started_at=r["started_at"],
                completed_at=r["completed_at"],
                status=r["status"],
                gcs_object=r["gcs_object"],
                size_bytes=r["size_bytes"],
                duration_seconds=float(r["duration_seconds"]) if r["duration_seconds"] is not None else None,
                error_message=r["error_message"],
            )
            for r in rows
        ]
    finally:
        await conn.close()


# ============================================================================
# Critical-failure alerting (mirrors GhostPour cloudzap PR #194).
# ============================================================================

class AlertRecipientRequest(BaseModel):
    email: Optional[str] = None
    display_name: Optional[str] = None
    active: Optional[bool] = None
    categories: Optional[List[str]] = None


class AlertTestSendRequest(BaseModel):
    category: str
    subject: str = "synthetic"
    note: Optional[str] = None


@router.get("/alerts/categories", dependencies=[Depends(verify_admin_key)])
async def list_alert_categories():
    """Stable category catalog. The dashboard renders the subscription
    picker and the test-send dropdown against this, so a new category
    only needs to be added in alerting.py to surface everywhere."""
    from contextquilt.services.alerting import KNOWN_CATEGORIES
    return {
        "categories": [
            {"id": k, "label": v["label"], "description": v["description"]}
            for k, v in KNOWN_CATEGORIES.items()
        ],
    }


@router.get("/alerts/recipients", dependencies=[Depends(verify_admin_key)])
async def list_alert_recipients():
    """All recipients, active and inactive. UI distinguishes by the
    `active` column."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """SELECT id::text AS id, email, display_name, active,
                      categories, created_at, updated_at
                 FROM alert_recipients
                 ORDER BY email"""
        )
        return {
            "recipients": [
                {
                    "id": r["id"],
                    "email": r["email"],
                    "display_name": r["display_name"],
                    "active": r["active"],
                    "categories": r["categories"] or [],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                }
                for r in rows
            ]
        }
    finally:
        await conn.close()


@router.post("/alerts/recipients", dependencies=[Depends(verify_admin_key)])
async def create_alert_recipient(body: AlertRecipientRequest):
    """Add a recipient. Email is required and must be unique. Categories
    list is optional, empty or missing means subscribed to everything."""
    if not body.email or "@" not in body.email:
        raise HTTPException(status_code=400, detail="valid email required")

    from contextquilt.services.alerting import KNOWN_CATEGORIES
    if body.categories:
        bad = [c for c in body.categories if c not in KNOWN_CATEGORIES]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"unknown categories: {bad} (known: {list(KNOWN_CATEGORIES)})",
            )

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        try:
            row = await conn.fetchrow(
                """INSERT INTO alert_recipients (email, display_name, active, categories)
                   VALUES ($1, $2, $3, $4::jsonb)
                   RETURNING id::text AS id, email""",
                body.email.lower().strip(),
                body.display_name,
                True if body.active is None else body.active,
                json.dumps(body.categories) if body.categories else None,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="email already registered")
        return {"id": row["id"], "email": row["email"]}
    finally:
        await conn.close()


@router.patch("/alerts/recipients/{recipient_id}", dependencies=[Depends(verify_admin_key)])
async def update_alert_recipient(recipient_id: str, body: AlertRecipientRequest):
    """Patch a recipient. Only fields present in the body are updated.
    Toggling `active=false` keeps the row but stops alerts."""
    from contextquilt.services.alerting import KNOWN_CATEGORIES
    if body.categories:
        bad = [c for c in body.categories if c not in KNOWN_CATEGORIES]
        if bad:
            raise HTTPException(status_code=400, detail=f"unknown categories: {bad}")

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        exists = await conn.fetchrow(
            "SELECT id FROM alert_recipients WHERE id = $1::uuid", recipient_id,
        )
        if exists is None:
            raise HTTPException(status_code=404, detail="recipient not found")

        sets: list[str] = []
        args: list = []
        idx = 1
        if body.email is not None:
            sets.append(f"email = ${idx}")
            args.append(body.email.lower().strip())
            idx += 1
        if body.display_name is not None:
            sets.append(f"display_name = ${idx}")
            args.append(body.display_name)
            idx += 1
        if body.active is not None:
            sets.append(f"active = ${idx}")
            args.append(body.active)
            idx += 1
        if body.categories is not None:
            sets.append(f"categories = ${idx}::jsonb")
            args.append(json.dumps(body.categories) if body.categories else None)
            idx += 1

        if not sets:
            return {"id": recipient_id, "updated": False}

        sets.append("updated_at = NOW()")
        args.append(recipient_id)

        try:
            await conn.execute(
                f"UPDATE alert_recipients SET {', '.join(sets)} WHERE id = ${idx}::uuid",
                *args,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(status_code=409, detail="email already in use")
        return {"id": recipient_id, "updated": True}
    finally:
        await conn.close()


@router.delete("/alerts/recipients/{recipient_id}", dependencies=[Depends(verify_admin_key)])
async def delete_alert_recipient(recipient_id: str):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        result = await conn.execute(
            "DELETE FROM alert_recipients WHERE id = $1::uuid", recipient_id,
        )
        # asyncpg returns "DELETE n" — parse the count.
        try:
            deleted = int(result.split()[-1])
        except (ValueError, IndexError):
            deleted = 0
        if deleted == 0:
            raise HTTPException(status_code=404, detail="recipient not found")
        return {"id": recipient_id, "deleted": True}
    finally:
        await conn.close()


@router.get("/alerts/incidents", dependencies=[Depends(verify_admin_key)])
async def list_alert_incidents(limit: int = 100):
    """Recent incidents (open + resolved). Newest first. Used by the
    dashboard's history panel."""
    from contextquilt.services.alerting import list_incidents
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await list_incidents(conn, limit=min(limit, 500))
        return {"incidents": rows}
    finally:
        await conn.close()


@router.post("/alerts/test-send", dependencies=[Depends(verify_admin_key)])
async def test_send_alert(body: AlertTestSendRequest):
    """Fire a synthetic incident under the chosen category so all
    active subscribed recipients receive an email. Useful right after
    adding a new address to confirm deliverability + Resend DKIM
    setup. The synthetic incident lands in the history list and
    auto-resolves like any other."""
    from contextquilt.services.alerting import report_incident, KNOWN_CATEGORIES
    if body.category not in KNOWN_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"unknown category: {body.category}")

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        result = await report_incident(
            conn,
            category=body.category,
            subject=f"test-send/{body.subject}",
            details={
                "test_send": True,
                "note": body.note or "Manual deliverability check from admin dashboard.",
                "triggered_at": datetime.utcnow().isoformat(),
            },
            from_addr=get_settings().cq_alert_email_from or None,
        )
        return {
            "incident_id": result.incident_id,
            "is_new": result.is_new,
            "emailed_to": result.emailed_to,
            "suppressed_reason": result.suppressed_reason,
        }
    finally:
        await conn.close()



# ============================================================
# Memory Health — lifecycle / recall-usage / overdue observability
# ============================================================
# The data all exists as of the June 2026 sprint: patch_usage_metrics is
# written by recall (#129), overdue_since is stamped by the deadline
# sweep (#139), completed_at distinguishes resolved from decayed (#130),
# and entity_aliases tracks canonical merges (#135). This endpoint just
# aggregates — no new tables, no new writes.

@router.get("/memory-health", dependencies=[Depends(verify_admin_key)])
async def get_memory_health(days: int = 30):
    """Aggregate memory-lifecycle observability for the dashboard tab."""
    days = max(1, min(365, days))
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        totals_row = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE COALESCE(status,'active')='active') AS active,
                   count(*) FILTER (WHERE status='archived' AND completed_at IS NOT NULL) AS resolved_all_time,
                   count(*) FILTER (WHERE status='archived' AND completed_at IS NULL) AS decayed_all_time
            FROM context_patches
            """
        )

        lifecycle_rows = await conn.fetch(
            """
            SELECT updated_at::date AS day,
                   count(*) FILTER (WHERE completed_at IS NOT NULL) AS resolved,
                   count(*) FILTER (WHERE completed_at IS NULL) AS decayed
            FROM context_patches
            WHERE status = 'archived'
              AND updated_at >= NOW() - INTERVAL '1 day' * $1
            GROUP BY 1 ORDER BY 1 DESC
            """,
            days,
        )

        archived_by_type = await conn.fetch(
            """
            SELECT patch_type,
                   count(*) FILTER (WHERE completed_at IS NOT NULL) AS resolved,
                   count(*) FILTER (WHERE completed_at IS NULL) AS decayed
            FROM context_patches
            WHERE status = 'archived'
              AND updated_at >= NOW() - INTERVAL '1 day' * $1
            GROUP BY 1 ORDER BY (count(*)) DESC
            """,
            days,
        )

        usage_row = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE pum.access_count > 1) AS reaccessed,
                   count(*) FILTER (WHERE pum.access_count <= 1) AS never_reaccessed
            FROM patch_usage_metrics pum
            JOIN context_patches cp ON cp.patch_id = pum.patch_id
            WHERE COALESCE(cp.status,'active') = 'active'
            """
        )

        top_recalled = await conn.fetch(
            """
            SELECT pum.access_count,
                   pum.last_accessed_at,
                   cp.patch_type,
                   cp.value->>'text' AS text,
                   COALESCE(cp.project, '') AS project,
                   replace(ps.subject_key, 'user:', '') AS user_id
            FROM patch_usage_metrics pum
            JOIN context_patches cp ON cp.patch_id = pum.patch_id
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            WHERE COALESCE(cp.status,'active') = 'active'
            ORDER BY pum.access_count DESC, pum.last_accessed_at DESC
            LIMIT 10
            """
        )

        overdue_rows = await conn.fetch(
            r"""
            SELECT cp.patch_type,
                   cp.value->>'text' AS text,
                   cp.value->>'deadline_date' AS deadline_date,
                   cp.value->>'overdue_since' AS overdue_since,
                   COALESCE(cp.value->>'owner', '') AS owner,
                   COALESCE(cp.project, '') AS project,
                   replace(ps.subject_key, 'user:', '') AS user_id
            FROM context_patches cp
            JOIN patch_subjects ps ON ps.patch_id = cp.patch_id
            WHERE cp.patch_type IN ('commitment', 'blocker')
              AND COALESCE(cp.status,'active') = 'active'
              AND cp.completed_at IS NULL
              AND cp.value->>'deadline_date' ~ '^\d{4}-\d{2}-\d{2}$'
              AND (cp.value->>'deadline_date')::date < (NOW() AT TIME ZONE 'utc')::date
            ORDER BY cp.value->>'deadline_date' ASC
            LIMIT 25
            """
        )

        alias_count = 0
        try:
            alias_count = await conn.fetchval("SELECT count(*) FROM entity_aliases") or 0
        except Exception:
            pass  # table may not exist on lagging deployments

        reobserved = await conn.fetchval(
            """
            SELECT count(*) FROM context_patches
            WHERE COALESCE(status,'active') = 'active'
              AND last_observed_at > created_at
            """
        )

        return {
            "window_days": days,
            "totals": {
                "active": totals_row["active"] or 0,
                "resolved_all_time": totals_row["resolved_all_time"] or 0,
                "decayed_all_time": totals_row["decayed_all_time"] or 0,
                "overdue_now": len(overdue_rows),
                "entity_aliases": alias_count,
                "reobserved_active": reobserved or 0,
            },
            "usage": {
                "reaccessed": usage_row["reaccessed"] or 0,
                "never_reaccessed": usage_row["never_reaccessed"] or 0,
            },
            "lifecycle_by_day": [dict(r) for r in lifecycle_rows],
            "archived_by_type": [dict(r) for r in archived_by_type],
            "top_recalled": [dict(r) for r in top_recalled],
            "overdue": [dict(r) for r in overdue_rows],
        }
    finally:
        await conn.close()
