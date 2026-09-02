#!/usr/bin/env python3
"""
Register the ShoulderSurf manifest against a running CQ instance.

Usage:
    # Preview the manifest that would be POSTed (no network call)
    python scripts/register_ss_schema.py --dry-run

    # Check connectivity and app_id validity without registering
    python scripts/register_ss_schema.py --check

    # Register for real
    python scripts/register_ss_schema.py

Required env vars (all required for --check and real runs;
--dry-run only needs the manifest file):
    CQ_BASE_URL       e.g. https://api.contextquilt.com
                      (default: http://localhost:8000)
    CQ_ADMIN_KEY      the admin key for schema registration
    SS_APP_ID         the UUID of the ShoulderSurf application
                      (from GET /v1/auth/apps on the target server)

Prerequisites:
    - PR 1 (#45) deployed: registration endpoints live
    - ShoulderSurf registered as an application
    - init-db/11_shouldersurf_schema.json present in the repo

Exits:
    0 = success (or dry-run / check OK)
    1 = network or server error during registration
    2 = missing config / validation failure before any network call
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def load_manifest() -> dict:
    manifest_path = Path(__file__).resolve().parent.parent / "init-db" / "11_shouldersurf_schema.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest fixture not found at {manifest_path}", file=sys.stderr)
        sys.exit(2)
    with open(manifest_path) as f:
        return json.load(f)


def require_env() -> tuple[str, str, str]:
    base_url = os.environ.get("CQ_BASE_URL", "http://localhost:8000").rstrip("/")
    admin_key = os.environ.get("CQ_ADMIN_KEY", "")
    app_id = os.environ.get("SS_APP_ID", "").strip()

    missing = []
    if not admin_key:
        missing.append("CQ_ADMIN_KEY")
    if not app_id:
        missing.append("SS_APP_ID")
    if missing:
        print(f"ERROR: missing required env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(2)

    return base_url, admin_key, app_id


def do_dry_run() -> int:
    """Print what we would POST. No network call, no auth required."""
    manifest = load_manifest()
    app_id = os.environ.get("SS_APP_ID", "<SS_APP_ID-unset>").strip() or "<SS_APP_ID-unset>"
    manifest["app_id"] = app_id
    base_url = os.environ.get("CQ_BASE_URL", "http://localhost:8000").rstrip("/")
    url = f"{base_url}/v1/apps/{app_id}/schema"

    print("DRY RUN: no network call will be made")
    print("---")
    print(f"Target URL:       POST {url}")
    print(f"X-Admin-Key:      {'<set>' if os.environ.get('CQ_ADMIN_KEY') else '<NOT SET: real run will fail>'}")
    print(f"X-Registered-By:  {os.environ.get('USER', 'ops')}@bootstrap")
    print("---")
    print("Manifest summary:")
    print(f"  app_id:              {manifest.get('app_id')}")
    print(f"  version:             {manifest.get('version')}")
    print(f"  facet_enum_version:  {manifest.get('facet_enum_version')}")
    print(f"  patch_types:         {len(manifest.get('patch_types', []))}")
    print(f"    ({', '.join(pt['domain_type'] for pt in manifest.get('patch_types', []))})")
    print(f"  connection_labels:   {len(manifest.get('connection_labels', []))}")
    print(f"    ({', '.join(lb['label'] for lb in manifest.get('connection_labels', []))})")
    print(f"  entity_types:        {len(manifest.get('entity_types', []))}")
    print(f"  origin_types:        {manifest.get('origin_types', [])}")
    print("---")
    print("To register for real, rerun without --dry-run.")
    return 0


def do_check() -> int:
    """Verify connectivity and that the app exists on the target."""
    base_url, admin_key, app_id = require_env()

    # 1. Health check
    try:
        with urlopen(f"{base_url}/health", timeout=10) as resp:
            if resp.status != 200:
                print(f"ERROR: {base_url}/health returned HTTP {resp.status}", file=sys.stderr)
                return 1
    except URLError as e:
        print(f"ERROR: cannot reach {base_url}/health: {e}", file=sys.stderr)
        return 1

    # 2. Is this app a writer? Reported here, enforced on the real run.
    writer_guard(app_id, force=False, report_only=True)

    # 3. Try fetching the current schema for this app_id (may be 404)
    url = f"{base_url}/v1/apps/{app_id}/schema"
    req = Request(url=url, method="GET", headers={"X-Admin-Key": admin_key})
    try:
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            print("CHECK: app already has a schema registered:")
            print(f"  current version:     {data.get('version')}")
            print(f"  registered_at:       {data.get('registered_at')}")
            print(f"  registered_by:       {data.get('registered_by')}")
            print("Real run will register a NEW version (bumping the revision counter).")
            return 0
    except HTTPError as e:
        if e.code == 404:
            print("CHECK: app has no registered schema yet.")
            print("Real run will register the FIRST version (revision 1).")
            return 0
        if e.code == 403:
            print("ERROR: CQ_ADMIN_KEY was rejected by the target.", file=sys.stderr)
            return 1
        print(f"ERROR: HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}", file=sys.stderr)
        return 1
    except URLError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# The writing-app guard.
#
# 2026-09-01: manifest v12, v13 and v14 were registered on the app id that
# ShoulderSurf used until the 08-07 app-isolation split. The registration
# answered 200, the registry read that "verified" it read that same app's
# rows, and the worker builds its extraction prompt from the INGESTING
# app's latest row, so every SS meeting since 08-16 was extracted under
# the v11 wording. The env var carried a stale id and nothing in the path
# could tell. Doc 19.6: check what your instrument resolves through.
#
# So the real run asks the database which apps have actually ingested a
# meeting recently and refuses any other id. Derived lanes (profile pass,
# consolidation, headlines) stamp the app of the source patches and keep
# a dead app looking alive, so only origin-bound rows count. A brand new
# app has never written and is refused too; that is what --force is for,
# and it says so out loud.
# ---------------------------------------------------------------------------

# How far back to look for ingest at all, and how far behind the newest
# ingest anywhere an app may lag and still count as a writer. Relative,
# not calendar: on 2026-09-01 the pre-split id had ingested 149 patches
# "in the last 30 days" (its last on 08-12, during the switchover) and a
# fixed window passed it. Twenty days behind the app that actually
# writes is not a writer.
WRITER_LOOKBACK_DAYS = 90
WRITER_LAG_DAYS = 14

WRITERS_SQL = """
    SELECT acl.app_id::text AS app_id,
           max(cp.created_at) AS last_ingest,
           count(*) AS ingested
      FROM context_patches cp
      JOIN context_patch_acl acl ON acl.patch_id = cp.patch_id
     WHERE acl.can_write
       AND cp.origin_id IS NOT NULL
       AND cp.created_at >= now() - ($1::int * interval '1 day')
     GROUP BY 1
     ORDER BY 2 DESC
"""


def judge_writer(app_id: str, writers: list) -> tuple[bool, str]:
    """Pure verdict: (ok, message). `writers` is rows of
    (app_id, last_ingest, ingested) for the lookback, newest first. An
    app is a writer when its latest ingest is within WRITER_LAG_DAYS of
    the newest ingest by ANY app."""
    from datetime import timedelta

    if not writers:
        return False, ("REFUSED: no app has ingested an origin-bound patch in the "
                       f"last {WRITER_LOOKBACK_DAYS} days.\n"
                       "Pass --force only for a brand new app that has never written.")
    newest = max(w[1] for w in writers)
    cutoff = newest - timedelta(days=WRITER_LAG_DAYS)
    mine = [w for w in writers if str(w[0]) == app_id]
    if mine and mine[0][1] >= cutoff:
        _, last, n = mine[0]
        return True, (f"app {app_id} ingested {n} origin-bound patches in the last "
                      f"{WRITER_LOOKBACK_DAYS} days, latest {last}, within "
                      f"{WRITER_LAG_DAYS} days of the newest ingest anywhere; it is a writer.")
    lines = []
    if mine:
        _, last, n = mine[0]
        lines.append(f"REFUSED: app {app_id} last ingested {last} ({n} patches), "
                     f"more than {WRITER_LAG_DAYS} days behind the newest ingest "
                     f"({newest}). A manifest registered on it never reaches an "
                     "extraction prompt.")
    else:
        lines.append(f"REFUSED: app {app_id} has ingested NOTHING in the last "
                     f"{WRITER_LOOKBACK_DAYS} days, so a manifest registered on it "
                     "never reaches an extraction prompt.")
    lines.append("Apps that DID ingest, newest first (writers marked *):")
    for a, last, n in writers:
        mark = "*" if last >= cutoff else " "
        lines.append(f" {mark} {a}  latest {last}  ({n} patches)")
    lines.append("Pass --force only for a brand new app that has never written.")
    return False, "\n".join(lines)


def writer_guard(app_id: str, force: bool, report_only: bool = False) -> int:
    """0 to proceed, 1 to stop. Reads DATABASE_URL; refuses when absent.
    report_only (--check) prints the verdict and never stops."""
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        if force:
            print("WARNING: no DATABASE_URL, writer guard skipped under --force.")
            return 0
        print("REFUSED: DATABASE_URL is not set, so the writer guard cannot run. "
              "Run from the compose container (it has it) or pass --force.",
              file=sys.stderr)
        return 1
    try:
        import asyncio

        import asyncpg

        async def _fetch():
            conn = await asyncpg.connect(dsn)
            try:
                rows = await conn.fetch(WRITERS_SQL, WRITER_LOOKBACK_DAYS)
                return [(r["app_id"], r["last_ingest"], r["ingested"]) for r in rows]
            finally:
                await conn.close()

        writers = asyncio.run(_fetch())
    except Exception as exc:  # the guard failing is not a reason to write
        if force:
            print(f"WARNING: writer guard could not run ({str(exc)[:120]}); "
                  "proceeding under --force.")
            return 0
        print(f"REFUSED: writer guard could not run: {str(exc)[:200]}", file=sys.stderr)
        return 1
    ok, msg = judge_writer(app_id, writers)
    print(msg)
    if ok:
        return 0
    if report_only:
        print("(--check only reports; the real run stops here unless --force.)")
        return 0
    if force:
        print("Proceeding anyway under --force.")
        return 0
    return 1


def do_register(force: bool = False) -> int:
    """POST the manifest. Real writes."""
    base_url, admin_key, app_id = require_env()
    if writer_guard(app_id, force):
        return 1
    manifest = load_manifest()
    manifest["app_id"] = app_id

    url = f"{base_url}/v1/apps/{app_id}/schema"
    body = json.dumps(manifest).encode("utf-8")
    request = Request(
        url=url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Admin-Key": admin_key,
            "X-Registered-By": os.environ.get("USER", "ops") + "@bootstrap",
        },
    )

    print(f"POST {url}")
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            print(f"HTTP {response.status}")
            print(body)
    except HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {detail}", file=sys.stderr)
        return 1
    except URLError as e:
        print(f"Network error: {e}", file=sys.stderr)
        return 1

    print("\nShoulderSurf schema registered successfully.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the manifest that would be POSTed. No network call, no auth needed.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify connectivity and whether the app already has a registered schema. No write.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Register even when the writer guard refuses (a brand new app that has never ingested).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.dry_run and args.check:
        print("ERROR: pass only one of --dry-run / --check.", file=sys.stderr)
        return 2
    if args.dry_run:
        return do_dry_run()
    if args.check:
        return do_check()
    return do_register(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
