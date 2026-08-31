#!/usr/bin/env python3
"""Report patch types that no registered manifest declares.

THE POINT IS THE DIRECTION OF THE COMPARISON. Three teams each holding
a generated list of the types they handle cannot catch a name that
every one of those lists was generated without, because each side
checks its own work against its own source. So this compares two
inventories against a THIRD that is neither: the registered manifest,
which is what the app actually declared and the only thing entitled to
be called expected.

  A. DECLARED   app_schemas, latest manifest version per app
  B. REGISTERED patch_type_registry, which drives the facet runtime
  C. WRITTEN    distinct patch_type actually present in context_patches

B and C are each checked against A. A type in C but not in A is the
finding that matters: it was captured, it is stored, and nothing knows
what it is.

Found on first run, 2026-08-30, on prod: `artifact` (5 rows, still
active, most recent 08-25) is an ENTITY type that the extraction model
borrowed as a patch type. Nothing on the write path rejected it, so it
takes facet defaults in the runtime, renders bare in the context block,
and is dropped by a client switching on a known-type enum.

RULED 2026-08-30, AND `artifact` IS NOT TO BE ACTED ON. Scott's call is
that the memory layer should not be concerned with artifacts at all, so
the five rows stay as they are and the write path is unchanged. This
script still reports it, deliberately, because suppressing a known case
is how a tool loses the ability to tell you about an unknown one. Read
a lone `artifact` line as the expected output of a clean run.

If anyone ever does revisit it, the place is the EXTRACTION PROMPT and
not the query path: the model is picking a name out of `ENTITY_TYPES`
when it should be picking a patch type. These rows are documents
MENTIONED IN A MEETING, minted by CQ's own extraction
(`source_prompt=meeting_summary`, `origin_mode=inferred`, written at
worker.py:404), not references a user attached to a query.

READ ONLY BY CONSTRUCTION. There is no --apply and no write anywhere in
this file, because what to DO about an undeclared type (reject at the
write path, or coerce to the manifest fallback the way an unmatched
correction lands as correction_fallback_type) is an open decision, not
this script's to make.

Usage:
    DATABASE_URL=postgres://... python scripts/audit_patch_type_inventory.py
    DATABASE_URL=postgres://... python scripts/audit_patch_type_inventory.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import asyncpg

# Latest manifest per app. DISTINCT ON rather than a window function so
# the intent reads off the query: one row per app, the newest version.
LATEST_MANIFESTS_SQL = """
    SELECT DISTINCT ON (s.app_id)
           s.app_id, s.version, s.manifest, a.app_name
      FROM app_schemas s
      LEFT JOIN applications a ON a.app_id = s.app_id
     ORDER BY s.app_id, s.version DESC
"""

# app_id IS NULL marks a built-in, available to every app, so a built-in
# absent from one app's manifest is not drift.
REGISTRY_SQL = "SELECT type_key, app_id FROM patch_type_registry"

WRITTEN_SQL = """
    SELECT patch_type,
           COUNT(*) AS total,
           COUNT(*) FILTER (WHERE COALESCE(status, 'active') = 'active') AS active,
           MIN(created_at)::date AS first_seen,
           MAX(created_at)::date AS last_seen
      FROM context_patches
     GROUP BY patch_type
     ORDER BY COUNT(*) DESC
"""

SAMPLES_SQL = """
    SELECT left(value->>'text', 72) AS text, source_prompt, origin_mode,
           created_at::date AS created
      FROM context_patches
     WHERE patch_type = $1
     ORDER BY created_at DESC
     LIMIT $2
"""


def declared_types(manifest) -> set:
    """The type names one manifest declares.

    The key is `domain_type`, not `name`. Worth stating: reading
    01_init.sql for a shape a later migration had changed is how the
    first version of this audit's own fixture broke.
    """
    if isinstance(manifest, str):
        manifest = json.loads(manifest)
    return {
        t["domain_type"]
        for t in (manifest.get("patch_types") or [])
        if t.get("domain_type")
    }


async def audit(conn, samples: int = 3) -> dict:
    manifests = await conn.fetch(LATEST_MANIFESTS_SQL)
    registry = await conn.fetch(REGISTRY_SQL)
    written = await conn.fetch(WRITTEN_SQL)

    builtin = {r["type_key"] for r in registry if r["app_id"] is None}
    per_app_registry: dict = {}
    for r in registry:
        if r["app_id"] is not None:
            per_app_registry.setdefault(r["app_id"], set()).add(r["type_key"])

    apps = []
    all_declared = set()
    for m in manifests:
        declared = declared_types(m["manifest"])
        all_declared |= declared
        known = builtin | per_app_registry.get(m["app_id"], set())
        apps.append({
            "app_id": str(m["app_id"]),
            "app_name": m["app_name"],
            "version": m["version"],
            "declared": sorted(declared),
            # Declared but the runtime has no registry row for it: the
            # facet runtime would fall back to the floor for a type the
            # app believes it registered.
            "declared_not_registered": sorted(declared - known),
        })

    expected = all_declared | builtin
    undeclared = []
    for r in written:
        if r["patch_type"] in expected:
            continue
        rows = await conn.fetch(SAMPLES_SQL, r["patch_type"], samples)
        undeclared.append({
            "patch_type": r["patch_type"],
            "total": r["total"],
            "active": r["active"],
            "first_seen": str(r["first_seen"]),
            "last_seen": str(r["last_seen"]),
            "samples": [
                {"text": s["text"], "source_prompt": s["source_prompt"],
                 "origin_mode": s["origin_mode"], "created": str(s["created"])}
                for s in rows
            ],
        })

    return {
        "apps": apps,
        "builtin": sorted(builtin),
        "undeclared_written": undeclared,
        # Not a defect on its own: a declared type that has never landed
        # may simply be an app that has not ingested yet. It is reported
        # because it is also what a type the extraction never emits
        # looks like, and only a human can tell those apart.
        "declared_never_written": sorted(
            all_declared - {r["patch_type"] for r in written}
        ),
    }


def render(report: dict) -> int:
    for app in report["apps"]:
        print(f"{app['app_name']} v{app['version']} ({app['app_id'][:8]}): "
              f"{len(app['declared'])} types declared")
        if app["declared_not_registered"]:
            print(f"  DECLARED BUT NOT REGISTERED: "
                  f"{', '.join(app['declared_not_registered'])}")

    never = report["declared_never_written"]
    if never:
        print(f"\ndeclared but never written: {', '.join(never)}")
        print("  (an app that has not ingested looks the same as a type the "
              "extraction never emits; only a human can tell those apart)")

    undeclared = report["undeclared_written"]
    if not undeclared:
        print("\nNo undeclared patch types written. Inventory is clean.")
        return 0

    print(f"\n{len(undeclared)} PATCH TYPE(S) WRITTEN THAT NO MANIFEST DECLARES:")
    for u in undeclared:
        print(f"\n  {u['patch_type']}  total={u['total']} active={u['active']}  "
              f"{u['first_seen']} to {u['last_seen']}")
        for s in u["samples"]:
            print(f"    {s['created']} src={s['source_prompt']} "
                  f"mode={s['origin_mode']}")
            print(f"      {s['text']}")
    print("\nThese rows are stored and mostly active. An undeclared type takes "
          "facet defaults,\nrenders bare, and is dropped by any client "
          "switching on a known-type enum.")
    return 1


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--samples", type=int, default=3,
                    help="example rows per undeclared type (default 3)")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    conn = await asyncpg.connect(dsn)
    try:
        report = await audit(conn, samples=args.samples)
    finally:
        await conn.close()

    if args.json:
        print(json.dumps(report, indent=2))
        # Same exit contract either way, so CI or an ops loop can gate on
        # it without parsing.
        return 1 if report["undeclared_written"] else 0
    return render(report)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
