"""
App schema registration endpoints.

Admin-authenticated API for registering, retrieving, and updating
per-app manifests. See docs/design/app-schema-registration.md for the
manifest shape and design rationale.

Endpoints:
    POST   /v1/apps/{app_id}/schema           register a new manifest
    GET    /v1/apps/{app_id}/schema           fetch current manifest
    PATCH  /v1/apps/{app_id}/schema           update an existing manifest
    GET    /v1/apps/{app_id}/schema/history   list manifest versions

All endpoints require the X-Admin-Key header (per CQ_ADMIN_KEY env).
"""

import json
import os
from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from src.contextquilt.services.schema_validator import validate_manifest


router = APIRouter(prefix="/v1/apps", tags=["App Schemas"])

CQ_ADMIN_KEY = os.getenv("CQ_ADMIN_KEY", "")


async def verify_admin_key(x_admin_key: str = Header(default="")) -> None:
    """Admin-key gate. Matches the dashboard's verification pattern."""
    if CQ_ADMIN_KEY and x_admin_key != CQ_ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")


DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/context_quilt")


async def _get_conn():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()


# ============================================================
# Response models
# ============================================================


class RegistrationResponse(BaseModel):
    status: str
    app_id: str
    version: int
    patch_types_registered: int
    connection_labels_registered: int
    entity_types_registered: int


class ManifestHistoryEntry(BaseModel):
    version: int
    registered_at: str
    registered_by: Optional[str]


# ============================================================
# Endpoints
# ============================================================


@router.post(
    "/{app_id}/schema",
    response_model=RegistrationResponse,
    dependencies=[Depends(verify_admin_key)],
)
async def register_schema(
    app_id: str,
    manifest: Dict[str, Any],
    x_admin_key: str = Header(default=""),
    registered_by: Optional[str] = Header(default=None, alias="X-Registered-By"),
):
    """
    Register a new manifest for an app.

    Bumps the version of any existing manifest for this app. The registered
    version becomes current; older versions remain queryable via the
    history endpoint.
    """
    is_valid, errors = validate_manifest(manifest, app_id)
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail={"message": "Manifest validation failed", "errors": errors},
        )

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Confirm the app exists
        app_row = await conn.fetchrow(
            "SELECT app_id FROM applications WHERE app_id = $1::uuid", app_id
        )
        if app_row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Application {app_id!r} not found. Register it via /v1/auth/register first.",
            )

        # Determine next version
        latest = await conn.fetchval(
            "SELECT COALESCE(MAX(version), 0) FROM app_schemas WHERE app_id = $1::uuid",
            app_id,
        )
        new_version = (latest or 0) + 1

        async with conn.transaction():
            # Save the manifest snapshot
            await conn.execute(
                """
                INSERT INTO app_schemas (app_id, version, manifest, registered_by)
                VALUES ($1::uuid, $2, $3::jsonb, $4)
                """,
                app_id,
                new_version,
                json.dumps(manifest),
                registered_by,
            )

            # Clear old app-scoped rows for this app before re-writing
            await conn.execute(
                "DELETE FROM patch_type_registry WHERE app_id = $1::uuid", app_id
            )
            await conn.execute(
                "DELETE FROM connection_vocabulary WHERE app_id = $1::uuid", app_id
            )
            await conn.execute(
                "DELETE FROM entity_type_registry WHERE app_id = $1::uuid", app_id
            )

            # Write patch types
            patch_count = 0
            for pt in manifest["patch_types"]:
                await conn.execute(
                    """
                    INSERT INTO patch_type_registry
                        (type_key, app_id, display_name, schema, persistence,
                         default_ttl_days, is_completable, project_scoped,
                         facet, permanence)
                    VALUES
                        ($1, $2::uuid, $3, $4::jsonb, $5, $6, $7, $8, $9, $10)
                    """,
                    pt["domain_type"],
                    app_id,
                    pt["display_name"],
                    json.dumps(pt["value_shape"]),
                    _permanence_to_persistence(pt["permanence"]),
                    _permanence_to_default_ttl_days(pt["permanence"]),
                    bool(pt.get("completable", False)),
                    bool(pt.get("project_scoped", False)),
                    pt["facet"],
                    pt["permanence"],
                )
                patch_count += 1

            # Write connection labels
            label_count = 0
            for lb in manifest["connection_labels"]:
                await conn.execute(
                    """
                    INSERT INTO connection_vocabulary
                        (label, app_id, role, from_types, to_types, description)
                    VALUES
                        ($1, $2::uuid, $3, $4::text[], $5::text[], $6)
                    """,
                    lb["label"],
                    app_id,
                    lb["role"],
                    lb["from_types"],
                    lb["to_types"],
                    lb["description"],
                )
                label_count += 1

            # Write entity types (if provided)
            entity_count = 0
            for et in manifest.get("entity_types", []):
                await conn.execute(
                    """
                    INSERT INTO entity_type_registry
                        (entity_type, app_id, display_name, description,
                         indexed, extraction_rules)
                    VALUES
                        ($1, $2::uuid, $3, $4, $5, $6::jsonb)
                    """,
                    et["entity_type"],
                    app_id,
                    et["display_name"],
                    et.get("description"),
                    bool(et.get("indexed", True)),
                    json.dumps(et.get("extraction_rules", {})),
                )
                entity_count += 1

        return RegistrationResponse(
            status="registered",
            app_id=app_id,
            version=new_version,
            patch_types_registered=patch_count,
            connection_labels_registered=label_count,
            entity_types_registered=entity_count,
        )
    finally:
        await conn.close()


@router.get(
    "/{app_id}/schema",
    dependencies=[Depends(verify_admin_key)],
)
async def get_current_schema(app_id: str):
    """Fetch the current manifest for an app."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            """
            SELECT version, manifest, registered_at, registered_by
            FROM app_schemas
            WHERE app_id = $1::uuid
            ORDER BY version DESC
            LIMIT 1
            """,
            app_id,
        )
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"No schema registered for app_id {app_id!r}.",
            )
        return {
            "app_id": app_id,
            "version": row["version"],
            "registered_at": row["registered_at"].isoformat(),
            "registered_by": row["registered_by"],
            "manifest": row["manifest"]
            if isinstance(row["manifest"], dict)
            else json.loads(row["manifest"]),
        }
    finally:
        await conn.close()


@router.patch(
    "/{app_id}/schema",
    response_model=RegistrationResponse,
    dependencies=[Depends(verify_admin_key)],
)
async def update_schema(
    app_id: str,
    manifest: Dict[str, Any],
    registered_by: Optional[str] = Header(default=None, alias="X-Registered-By"),
):
    """
    Replace an app's current manifest with a new one, bumping the version.

    Logically this is the same as POST — we require a full manifest and
    re-register cleanly. Partial diff-based updates are deferred; apps
    should fetch GET, modify, and PATCH with the full body.
    """
    return await register_schema(
        app_id=app_id,
        manifest=manifest,
        x_admin_key="",  # already validated via dependency
        registered_by=registered_by,
    )


@router.get(
    "/{app_id}/schema/history",
    response_model=List[ManifestHistoryEntry],
    dependencies=[Depends(verify_admin_key)],
)
async def get_schema_history(app_id: str):
    """List all registered versions of an app's manifest (newest first)."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT version, registered_at, registered_by
            FROM app_schemas
            WHERE app_id = $1::uuid
            ORDER BY version DESC
            """,
            app_id,
        )
        return [
            ManifestHistoryEntry(
                version=r["version"],
                registered_at=r["registered_at"].isoformat(),
                registered_by=r["registered_by"],
            )
            for r in rows
        ]
    finally:
        await conn.close()


# ============================================================
# Permanence → persistence / TTL mapping
# ============================================================
# The patch_type_registry has existing columns for persistence ("sticky",
# "decaying", "completable") and default_ttl_days (int or NULL). We
# derive both from the manifest's permanence class so existing code
# that reads those columns continues to behave correctly.


def _permanence_to_persistence(permanence: str) -> str:
    """Map permanence class → existing persistence enum."""
    if permanence in ("permanent", "decade"):
        return "sticky"
    return "decaying"


def _permanence_to_default_ttl_days(permanence: str) -> Optional[int]:
    """Map permanence class → approximate default TTL in days."""
    mapping = {
        "permanent": None,   # never expire
        "decade": None,      # functionally never for current users
        "year": 365,
        "quarter": 90,
        "month": 30,
        "week": 14,
        "day": 1,
    }
    return mapping.get(permanence)
