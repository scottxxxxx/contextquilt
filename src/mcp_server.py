"""
Context Quilt MCP Server

Exposes ContextQuilt as a Model Context Protocol server so any MCP-compatible
client (Claude Desktop, Claude Code, Cursor, etc.) can use CQ for persistent
cognitive memory.

Run standalone:
    python src/mcp_server.py                    # stdio (local dev)
    python src/mcp_server.py --transport sse    # SSE (remote, port 8001)
    python src/mcp_server.py --transport http   # Streamable HTTP (remote)

Or mount in the existing FastAPI app via mcp_app().
"""

import os
import sys
import json
import asyncio
import asyncpg
import redis.asyncio as aioredis
from typing import Optional
from mcp.server.fastmcp import FastMCP

# ============================================
# Configuration
# ============================================

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
MCP_PORT = int(os.getenv("MCP_PORT", "8001"))
MCP_API_KEY = os.getenv("MCP_API_KEY", "")  # Required for remote transport. Set to a strong random string.

# ============================================
# MCP Server Definition
# ============================================

mcp = FastMCP(
    "ContextQuilt",
    instructions="""ContextQuilt is a persistent cognitive memory layer for AI applications.
It remembers user preferences, decisions, commitments, and relationships across sessions.
Use these tools to store and recall context for your users.""",
)

# ============================================
# Database connections (lazy init)
# ============================================

_db_pool: Optional[asyncpg.Pool] = None
_redis: Optional[aioredis.Redis] = None


async def get_db():
    global _db_pool
    if _db_pool is None:
        _db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _db_pool


async def get_redis():
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis


# ============================================
# MCP Tools
# ============================================

@mcp.tool()
async def recall_context(
    user_id: str,
    text: str,
    project_id: Optional[str] = None,
    project: Optional[str] = None,
    max_hops: int = 2,
) -> str:
    """Recall relevant context for a user based on query text.

    Searches the user's memory graph for matching entities, traverses
    relationships, and returns a formatted context block ready to inject
    into an LLM prompt. No LLM call — pure graph traversal, typically <50ms.

    Args:
        user_id: The user identifier
        text: Query or transcript text to match entities against
        project_id: Optional project UUID to scope results
        project: Optional project display name (fallback if no project_id)
        max_hops: Graph traversal depth (default 2)

    Returns:
        Formatted context block with matched entities, relationships, and patches
    """
    db = await get_db()
    redis_client = await get_redis()

    text_lower = text.lower()

    # Step 1: Entity matching from Redis index
    entity_index_key = f"entity_index:{user_id}"
    known_entities = await redis_client.smembers(entity_index_key)

    matched_names = []
    if known_entities:
        for name in known_entities:
            if name.lower() in text_lower:
                matched_names.append(name)

    if not matched_names:
        return "No matching context found for this query."

    # Step 2: Resolve entity IDs
    entity_rows = await db.fetch(
        "SELECT entity_id, name, entity_type, description FROM entities WHERE user_id = $1 AND name = ANY($2)",
        user_id, matched_names
    )
    if not entity_rows:
        return f"Entities mentioned ({', '.join(matched_names)}) but no details found."

    entity_ids = [row["entity_id"] for row in entity_rows]

    # Step 3: Graph traversal
    rel_rows = await db.fetch(
        """
        WITH RECURSIVE graph AS (
            SELECT r.from_entity_id, r.to_entity_id, r.relationship_type, r.context, 1 as depth
            FROM relationships r
            WHERE r.user_id = $1 AND (r.from_entity_id = ANY($2) OR r.to_entity_id = ANY($2))
            UNION
            SELECT r.from_entity_id, r.to_entity_id, r.relationship_type, r.context, g.depth + 1
            FROM relationships r
            JOIN graph g ON (r.from_entity_id = g.to_entity_id OR r.from_entity_id = g.from_entity_id
                          OR r.to_entity_id = g.from_entity_id OR r.to_entity_id = g.to_entity_id)
            WHERE r.user_id = $1 AND g.depth < $3
        )
        SELECT DISTINCT g.from_entity_id, g.to_entity_id, g.relationship_type, g.context,
               e1.name as from_name, e2.name as to_name
        FROM graph g
        JOIN entities e1 ON g.from_entity_id = e1.entity_id
        JOIN entities e2 ON g.to_entity_id = e2.entity_id
        """,
        user_id, entity_ids, max_hops
    )

    # Step 4: Get patches
    subject_key = f"user:{user_id}"
    if project_id:
        fact_rows = await db.fetch(
            """
            SELECT cp.value, cp.patch_type FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND (cp.project_id = $2 OR cp.project_id IS NULL OR cp.patch_type IN ('trait', 'preference'))
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY cp.created_at DESC LIMIT 20
            """,
            subject_key, project_id
        )
    elif project:
        fact_rows = await db.fetch(
            """
            SELECT cp.value, cp.patch_type FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND (cp.project = $2 OR cp.project IS NULL OR cp.patch_type IN ('trait', 'preference'))
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY cp.created_at DESC LIMIT 20
            """,
            subject_key, project
        )
    else:
        # No project — traits and preferences only
        fact_rows = await db.fetch(
            """
            SELECT cp.value, cp.patch_type FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND cp.patch_type IN ('trait', 'preference')
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY cp.created_at DESC LIMIT 20
            """,
            subject_key
        )

    # Step 5: Format context block
    sections = []

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
    if rel_rows:
        sections.append("Connections:\n" + "\n".join(
            f"- {r['from_name']} {r['relationship_type']} {r['to_name']}"
            for r in rel_rows
        ))

    def parse_value(row):
        v = row["value"]
        return json.loads(v) if isinstance(v, str) else v

    # Group patches by type
    for label, types in [
        ("About you", ("trait", "preference")),
        ("Decisions", ("decision",)),
        ("Open commitments", ("commitment",)),
        ("Blockers", ("blocker",)),
        ("Key facts", ("experience", "identity", "takeaway", "person", "role")),
    ]:
        patches = [parse_value(r) for r in fact_rows if r["patch_type"] in types]
        if patches:
            lines = []
            for v in patches[:10]:
                text_val = v.get("text", "")
                owner = v.get("owner", "")
                if owner:
                    lines.append(f"- {owner}: {text_val}")
                else:
                    lines.append(f"- {text_val}")
            sections.append(f"{label}:\n" + "\n".join(lines))

    if not sections:
        return "No relevant context found."

    return "\n\n".join(sections)


@mcp.tool()
async def store_memory(
    user_id: str,
    content: str,
    interaction_type: str = "meeting_transcript",
    project_id: Optional[str] = None,
    project: Optional[str] = None,
    meeting_id: Optional[str] = None,
) -> str:
    """Store a conversation, transcript, or interaction for async extraction.

    The content is queued for background processing. A worker extracts
    structured patches (facts, commitments, decisions, etc.), entities,
    and relationships from the content asynchronously.

    Args:
        user_id: The user identifier
        content: The transcript, conversation, or interaction text
        interaction_type: Type of content — 'meeting_transcript', 'chat_log', 'query', or 'tool_call'
        project_id: Optional project UUID for grouping
        project: Optional project display name
        meeting_id: Optional session/meeting UUID

    Returns:
        Confirmation that the memory update was queued
    """
    redis_client = await get_redis()

    metadata = {}
    if project_id:
        metadata["project_id"] = project_id
    if project:
        metadata["project"] = project
    if meeting_id:
        metadata["meeting_id"] = meeting_id

    payload = {
        "user_id": user_id,
        "interaction_type": interaction_type,
        "content": content,
        "metadata": metadata,
        "timestamp": __import__("datetime").datetime.utcnow().isoformat(),
    }

    if interaction_type == "meeting_transcript":
        payload["summary"] = content

    await redis_client.xadd("memory_updates", {"data": json.dumps(payload)})

    return f"Memory queued for processing. Content will be extracted asynchronously."


@mcp.tool()
async def get_quilt(
    user_id: str,
    category: Optional[str] = None,
) -> str:
    """Get all patches (facts, commitments, decisions, etc.) for a user.

    Returns the user's complete quilt — all active patches organized by type.

    Args:
        user_id: The user identifier
        category: Optional filter by patch type (e.g., 'commitment', 'trait', 'decision')

    Returns:
        Formatted list of all active patches
    """
    db = await get_db()
    subject_key = f"user:{user_id}"

    if category:
        rows = await db.fetch(
            """
            SELECT cp.patch_id, cp.value, cp.patch_type, cp.project, cp.created_at
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1 AND cp.patch_type = $2
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY cp.created_at DESC
            """,
            subject_key, category
        )
    else:
        rows = await db.fetch(
            """
            SELECT cp.patch_id, cp.value, cp.patch_type, cp.project, cp.created_at
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE ps.subject_key = $1
              AND COALESCE(cp.status, 'active') = 'active'
            ORDER BY cp.created_at DESC
            """,
            subject_key
        )

    if not rows:
        return f"No patches found for user {user_id}."

    def parse_value(row):
        v = row["value"]
        return json.loads(v) if isinstance(v, str) else v

    lines = []
    for row in rows:
        v = parse_value(row)
        text = v.get("text", str(v))
        owner = v.get("owner", "")
        project = row["project"] or ""
        ptype = row["patch_type"]
        prefix = f"[{ptype}]"
        if project:
            prefix += f" ({project})"
        if owner:
            lines.append(f"{prefix} {owner}: {text}")
        else:
            lines.append(f"{prefix} {text}")

    return f"{len(rows)} patches:\n\n" + "\n".join(lines)


@mcp.tool()
async def delete_patch(
    user_id: str,
    patch_id: str,
) -> str:
    """Delete a specific patch from a user's quilt.

    Args:
        user_id: The user identifier
        patch_id: The UUID of the patch to delete

    Returns:
        Confirmation of deletion
    """
    db = await get_db()
    subject_key = f"user:{user_id}"

    # Verify patch belongs to user
    row = await db.fetchrow(
        """
        SELECT cp.patch_id FROM context_patches cp
        JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
        WHERE ps.subject_key = $1 AND cp.patch_id = $2
        """,
        subject_key, patch_id
    )
    if not row:
        return f"Patch {patch_id} not found for user {user_id}."

    await db.execute(
        "UPDATE context_patches SET status = 'archived', updated_at = NOW() WHERE patch_id = $1",
        patch_id
    )

    return f"Patch {patch_id} deleted."


# ============================================
# MCP Resources
# ============================================

@mcp.resource("quilt://{user_id}/profile")
async def get_user_profile(user_id: str) -> str:
    """Get the user's profile including communication style."""
    redis_client = await get_redis()

    data = await redis_client.get(f"active_context:{user_id}")
    if not data:
        # Try loading from Postgres
        db = await get_db()
        row = await db.fetchrow(
            "SELECT variables, display_name, email FROM profiles WHERE user_id = $1",
            user_id
        )
        if not row:
            return json.dumps({"error": f"No profile found for user {user_id}"})

        variables = row["variables"]
        if isinstance(variables, str):
            variables = json.loads(variables)

        return json.dumps({
            "user_id": user_id,
            "display_name": row["display_name"],
            "email": row["email"],
            "variables": variables,
        }, indent=2)

    return data


# ============================================
# Auth Middleware (for remote transports)
# ============================================

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Validates Authorization: Bearer <token> against MCP_API_KEY."""

    async def dispatch(self, request, call_next):
        # Skip auth for health check
        if request.url.path == "/health":
            return await call_next(request)

        if not MCP_API_KEY:
            # No key configured — allow (local dev)
            return await call_next(request)

        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"error": "Authorization: Bearer <token> required"}, status_code=401)

        token = auth[7:]
        if token != MCP_API_KEY:
            return JSONResponse({"error": "Invalid API key"}, status_code=403)

        return await call_next(request)


# ============================================
# Entry point
# ============================================

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="ContextQuilt MCP Server")
    parser.add_argument("--transport", choices=["stdio", "sse", "http"], default="sse",
                       help="Transport mode (default: sse)")
    parser.add_argument("--port", type=int, default=MCP_PORT,
                       help=f"Port for SSE/HTTP transport (default: {MCP_PORT})")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        # Get the Starlette app and add auth middleware
        if args.transport == "sse":
            app = mcp.sse_app()
        else:
            app = mcp.streamable_http_app()

        app.add_middleware(BearerAuthMiddleware)

        # Add a simple health check
        from starlette.routing import Route
        from starlette.responses import PlainTextResponse

        async def health(request):
            return PlainTextResponse("ok")

        app.routes.append(Route("/health", health))

        uvicorn.run(app, host="0.0.0.0", port=args.port)
