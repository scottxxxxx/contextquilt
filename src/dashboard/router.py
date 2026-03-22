from fastapi import APIRouter, Depends, HTTPException, Header, Request
from typing import List, Dict, Any, Optional
import asyncpg
import os
from pydantic import BaseModel
from datetime import datetime, timedelta
import uuid
import json
import logging
import aiohttp
from src.contextquilt.gateway.extraction import classify_fact, extract_facts_from_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["Dashboard"])

# Admin key authentication (same pattern as CloudZap)
CQ_ADMIN_KEY = os.getenv("CQ_ADMIN_KEY", "")

async def verify_admin_key(x_admin_key: str = Header(default="")):
    """Verify the admin key. If CQ_ADMIN_KEY is not set, access is open (dev mode)."""
    if CQ_ADMIN_KEY and x_admin_key != CQ_ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")

@router.get("/verify-key")
async def verify_key(x_admin_key: str = Header(default="")):
    """Endpoint for the dashboard login to verify the admin key."""
    if CQ_ADMIN_KEY and x_admin_key != CQ_ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")
    return {"status": "ok"}

# Database Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")

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
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'identity') as identity,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'preference') as preference,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'trait') as trait,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'experience') as experience
                FROM date_series ds
                LEFT JOIN context_patches p ON date_trunc('hour', p.created_at) = ds.hour
                GROUP BY ds.hour
                ORDER BY ds.hour ASC
            """
            rows = await conn.fetch(query)
            return [{
                "date": row['hour'].strftime("%H:%M"), 
                "counts": {
                    "identity": row['identity'] or 0,
                    "preference": row['preference'] or 0,
                    "trait": row['trait'] or 0,
                    "experience": row['experience'] or 0
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
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'identity') as identity,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'preference') as preference,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'trait') as trait,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'experience') as experience
                FROM date_series ds
                LEFT JOIN context_patches p ON date_trunc('day', p.created_at)::date = ds.day
                GROUP BY ds.day
                ORDER BY ds.day ASC
            """
            rows = await conn.fetch(query, start_dt, end_dt)
            return [{
                "date": str(row['day']), 
                "counts": {
                    "identity": row['identity'] or 0,
                    "preference": row['preference'] or 0,
                    "trait": row['trait'] or 0,
                    "experience": row['experience'] or 0
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
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'identity') as identity,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'preference') as preference,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'trait') as trait,
                    COUNT(p.patch_id) FILTER (WHERE p.patch_type = 'experience') as experience
                FROM date_series ds
                LEFT JOIN context_patches p ON date_trunc('day', p.created_at)::date = ds.day
                GROUP BY ds.day
                ORDER BY ds.day ASC
            """
            rows = await conn.fetch(query, days)
            return [{
                "date": str(row['day']), 
                "counts": {
                    "identity": row['identity'] or 0,
                    "preference": row['preference'] or 0,
                    "trait": row['trait'] or 0,
                    "experience": row['experience'] or 0
                }
            } for row in rows]
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
    patch_count: int
    last_updated: Optional[datetime]
    last_provided: Optional[str] # Mocked for now (e.g. "2 hours ago")
    first_name: Optional[str]
    last_name: Optional[str]

@router.get("/users", response_model=List[UserSummaryItem], dependencies=[Depends(verify_admin_key)])
async def get_users():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Fetch users from profiles table (Source of Truth) and join with facts for aggregation
        query = """
            SELECT 
                p.user_id,
                p.variables,
                COUNT(ps.patch_id) as patch_count,
                MAX(cp.created_at) as last_updated
            FROM profiles p
            LEFT JOIN patch_subjects ps ON ps.subject_key = 'user:' || p.user_id
            LEFT JOIN context_patches cp ON cp.patch_id = ps.patch_id
            GROUP BY p.user_id, p.variables
            ORDER BY last_updated DESC NULLS LAST
        """
        rows = await conn.fetch(query)
        
        results = []
        import json
        for row in rows:
            # Mock "Last Provided" logic
            mock_last_provided = "1 hour ago"
            
            # Extract names from variables
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
                patch_count=row['patch_count'],
                last_updated=row['last_updated'],
                last_provided=mock_last_provided,
                first_name=first_name,
                last_name=last_name
            ))
            
        return results
    finally:
        await conn.close()

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

class TimelineEvent(BaseModel):
    date: str
    description: str
    sentiment: Optional[str] = "neutral"

class UserQuilt(BaseModel):
    user_id: str
    patches: List[QuiltPatch]
    timeline: List[TimelineEvent]

@router.get("/users/{user_id}/quilt", response_model=UserQuilt, dependencies=[Depends(verify_admin_key)])
async def get_user_quilt(user_id: str):
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Construct subject_key (assuming user:ID format)
        subject_key = f"user:{user_id}"
        
        # Get Patches from context_patches (via patch_subjects)
        patch_rows = await conn.fetch("""
            SELECT cp.patch_id, cp.patch_name, cp.value, cp.patch_type, cp.origin_mode, cp.created_at,
                   cp.source_prompt, cp.confidence, cp.sensitivity
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
            ORDER BY cp.created_at DESC
        """, subject_key)

        patches = []
        timeline = []

        for row in patch_rows:
            patches.append(QuiltPatch(
                patch_id=str(row['patch_id']),
                patch_name=row['patch_name'],
                value=str(row['value']), # Convert JSON/Any to string for display if needed
                patch_type=row['patch_type'],
                origin_mode=row['origin_mode'] or 'system',
                source_prompt=row['source_prompt'] or 'none',
                confidence=float(row['confidence']) if row['confidence'] is not None else 1.0,
                sensitivity=row['sensitivity'] or 'normal',
                created_at=row['created_at'],
                user_id=user_id
            ))

            # Add to timeline
            # Use value as description
            desc = str(row['value'])
            timeline.append(TimelineEvent(
                date=row['created_at'].strftime("%b %d"),
                description=desc[:100] + "..." if len(desc) > 100 else desc,
                sentiment="neutral"
            ))

        return UserQuilt(
            user_id=user_id,
            patches=patches,
            timeline=timeline[:20] 
        )

    finally:
        await conn.close()

class SchemaItem(BaseModel):
    name: str
    type: str
    description: str
    status: str # active

class CandidateVariable(BaseModel):
    id: str
    name: str # e.g. "shoe_size"
    frequency: str # "15% of users"
    sample: str # "User mentions 'size 10'"

@router.get("/schema", response_model=List[SchemaItem], dependencies=[Depends(verify_admin_key)])
async def get_schema():
    # Mock data from memory_schema.yaml
    # in real app, parse the yaml or DB
    return [
        SchemaItem(name="food_allergies", type="preference", description="Critical food restrictions.", status="active"),
        SchemaItem(name="communication_style", type="trait", description="User's preferred tone and verbosity.", status="active"),
        SchemaItem(name="last_project_context", type="experience", description="Context from most recent project.", status="active"),
        SchemaItem(name="job_title", type="identity", description="User's professional role.", status="active")
    ]

@router.get("/schema/candidates", response_model=List[CandidateVariable], dependencies=[Depends(verify_admin_key)])
async def get_candidates():
    # Mock discovery inbox
    return [
        CandidateVariable(id="1", name="shoe_size", frequency="15% of users", sample="I wear a size 10 nike"),
        CandidateVariable(id="2", name="favorite_editor", frequency="8% of users", sample="I prefer VS Code over Vim"),
        CandidateVariable(id="3", name="timezone", frequency="35% of users", sample="I'm in PST")
    ]

@router.post("/schema/candidates/{id}/{action}", dependencies=[Depends(verify_admin_key)])
async def handle_candidate(id: str, action: str):
    # Mock action
    return {"status": "success", "message": f"Candidate {id} {action}d"}

class ROIMetrics(BaseModel):
    efficiency_gain_percent: int
    token_savings_count: int # Millions
    sentiment_lift_percent: int
    cost_saved_usd: float

@router.get("/roi", response_model=ROIMetrics, dependencies=[Depends(verify_admin_key)])
async def get_roi_metrics():
    # Mock ROI data
    # In reality, this would query conversations/tokens tables
    return ROIMetrics(
        efficiency_gain_percent=22,
        token_savings_count=4, # 4.5M -> represented as int or float? using int for simplicity in model if count is small unit
        sentiment_lift_percent=15,
        cost_saved_usd=1250.00
    )


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

