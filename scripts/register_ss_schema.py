#!/usr/bin/env python3
"""
Register the ShoulderSurf manifest against a running CQ instance.

Usage:
    # Required env vars:
    #   CQ_BASE_URL       e.g. http://localhost:8000  (default)
    #   CQ_ADMIN_KEY      the admin key for schema registration
    #   SS_APP_ID         the UUID of the ShoulderSurf application
    #                     (created separately via /v1/auth/register)

    python scripts/register_ss_schema.py

Prerequisites:
    - PR 1 deployed (registration endpoints live)
    - ShoulderSurf registered as an application (GET /v1/auth/apps
      returns its UUID)
    - init-db/11_shouldersurf_schema.json present in the repo

Prints the response from the registration endpoint. Exits non-zero
on validation failure or network error.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


def main() -> int:
    base_url = os.environ.get("CQ_BASE_URL", "http://localhost:8000").rstrip("/")
    admin_key = os.environ.get("CQ_ADMIN_KEY", "")
    app_id = os.environ.get("SS_APP_ID", "").strip()

    if not admin_key:
        print("ERROR: CQ_ADMIN_KEY env var is required.", file=sys.stderr)
        return 2
    if not app_id:
        print("ERROR: SS_APP_ID env var is required (the UUID of the "
              "registered ShoulderSurf application).", file=sys.stderr)
        return 2

    manifest_path = Path(__file__).resolve().parent.parent / "init-db" / "11_shouldersurf_schema.json"
    if not manifest_path.exists():
        print(f"ERROR: manifest fixture not found at {manifest_path}", file=sys.stderr)
        return 2

    with open(manifest_path) as f:
        manifest = json.load(f)

    # Ensure the manifest's app_id matches the URL path app_id
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
        with urlopen(request, timeout=15) as response:
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


if __name__ == "__main__":
    sys.exit(main())
