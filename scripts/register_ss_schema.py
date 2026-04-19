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

    print("DRY RUN — no network call will be made")
    print("---")
    print(f"Target URL:       POST {url}")
    print(f"X-Admin-Key:      {'<set>' if os.environ.get('CQ_ADMIN_KEY') else '<NOT SET — real run will fail>'}")
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
        print(f"ERROR: cannot reach {base_url}/health — {e}", file=sys.stderr)
        return 1

    # 2. Try fetching the current schema for this app_id (may be 404)
    url = f"{base_url}/v1/apps/{app_id}/schema"
    req = Request(url=url, method="GET", headers={"X-Admin-Key": admin_key})
    try:
        with urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            print("CHECK — app already has a schema registered:")
            print(f"  current version:     {data.get('version')}")
            print(f"  registered_at:       {data.get('registered_at')}")
            print(f"  registered_by:       {data.get('registered_by')}")
            print("Real run will register a NEW version (bumping the revision counter).")
            return 0
    except HTTPError as e:
        if e.code == 404:
            print("CHECK — app has no registered schema yet.")
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


def do_register() -> int:
    """POST the manifest. Real writes."""
    base_url, admin_key, app_id = require_env()
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
    return do_register()


if __name__ == "__main__":
    sys.exit(main())
