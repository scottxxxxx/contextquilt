"""
Access Control Helpers for Context Patches.
"""

from typing import List, Dict, Any, Optional
import asyncpg # type: ignore

async def get_readable_patches(
    conn: asyncpg.Connection,
    app_id: str,
    subject_key: str
) -> List[Dict[str, Any]]:
    """
    Fetch all patches for a subject that the app is allowed to read.
    
    Implements the ACL check:
    1. Match subject_key (e.g., "user:123")
    2. Join on context_patch_acl
    3. Filter where app_id matches AND can_read is TRUE
    """
    query = """
    SELECT p.*
    FROM context_patches p
    JOIN context_patch_acl acl ON p.patch_id = acl.patch_id
    WHERE p.subject_key = $1
      AND acl.app_id = $2
      AND acl.can_read = TRUE
    """
    rows = await conn.fetch(query, subject_key, app_id)
    return [dict(row) for row in rows]

async def check_write_access(
    conn: asyncpg.Connection,
    patch_id: str,
    app_id: str
) -> bool:
    """Check if app has write access to a specific patch."""
    query = """
    SELECT can_write
    FROM context_patch_acl
    WHERE patch_id = $1 AND app_id = $2
    """
    val = await conn.fetchval(query, patch_id, app_id)
    return bool(val)

async def check_delete_access(
    conn: asyncpg.Connection,
    patch_id: str,
    app_id: str
) -> bool:
    """Check if app has delete access to a specific patch."""
    query = """
    SELECT can_delete
    FROM context_patch_acl
    WHERE patch_id = $1 AND app_id = $2
    """
    val = await conn.fetchval(query, patch_id, app_id)
    return bool(val)
