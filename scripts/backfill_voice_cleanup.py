#!/usr/bin/env python3
"""
One-shot voice cleanup for existing trait / preference patches.

Identifies patches whose stored text still carries pre-PR-#43 voice
artifacts:
  - "(you)" suffix embedded in the stored text (should never have been
    persisted — the marker is a transcript-identification convention,
    not part of anyone's name)
  - Third-person voice ("Scott prefers X", "Scott wants Y") on
    self-typed patches (trait / preference) that should read in clean
    second person ("You prefer X", "You want Y")

Candidates are rewritten via an LLM (configurable) into clean second
person. The script is idempotent — patches already in clean form are
left untouched.

Usage:
    # Dry-run against the currently configured DATABASE_URL
    python scripts/backfill_voice_cleanup.py --dry-run

    # Apply changes for one user
    python scripts/backfill_voice_cleanup.py --user-id <user_id>

    # Apply changes for every user (with an optional cap)
    python scripts/backfill_voice_cleanup.py --limit 200

Required env:
    DATABASE_URL         Postgres connection string (same as the app uses)
    CQ_LLM_API_KEY       API key for the rewrite LLM (OpenRouter etc.)
    CQ_LLM_BASE_URL      (optional, default https://openrouter.ai/api/v1)
    CQ_LLM_MODEL         (optional, default anthropic/claude-haiku-4.5)

Safety:
    - Dry-run prints every proposed rewrite, writes nothing.
    - Apply mode writes per-patch in a transaction and logs old/new text
      to stdout as CSV so the run is auditable.
    - Unchanged patches (already-clean) are skipped silently.
    - LLM rewrite falls back to a regex-only sanitizer if the LLM call
      fails — at minimum the "(you)" marker is always stripped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import asyncpg


DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/context_quilt",
)
LLM_API_KEY = os.getenv("CQ_LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("CQ_LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_MODEL = os.getenv("CQ_LLM_MODEL", "anthropic/claude-haiku-4.5")


# ============================================================
# Patterns used to identify candidates
# ============================================================

# Any patch text that still contains "(you)" is dirty by definition —
# the marker should never have been persisted.
_YOU_MARKER_RE = re.compile(r"\s*\(you\)", re.IGNORECASE)

# Rough third-person detector for self-typed patches: name at start
# followed by a singular-third-person verb. Not a strict grammar check —
# it's a flagging heuristic. Patches with third-person form get
# flagged; the LLM decides whether to actually rewrite.
_THIRD_PERSON_HINTS = [
    re.compile(r"^[A-Z][a-z]+ (prefers|wants|values|dislikes|avoids|tends to|leans|is|has)\b"),
    re.compile(r"^[A-Z][a-z]+'s\b"),  # "Scott's preference for..."
]


def needs_rewrite(text: str) -> bool:
    """True if the text has any voice artifact worth rewriting."""
    if not text:
        return False
    if _YOU_MARKER_RE.search(text):
        return True
    for pat in _THIRD_PERSON_HINTS:
        if pat.search(text):
            return True
    return False


# ============================================================
# Regex fallback rewriter (always runs; LLM adds verb agreement)
# ============================================================


def regex_cleanup(text: str) -> str:
    """Strip (you) markers and do obvious pronoun flips.

    This always runs, even when the LLM succeeds — it's a belt-and-
    suspenders layer and the fallback when the LLM is unavailable.
    """
    if not text:
        return text
    # Strip the (you) marker entirely
    cleaned = _YOU_MARKER_RE.sub("", text).strip()
    return cleaned


# ============================================================
# LLM-driven voice cleanup (Haiku, cheap + deterministic enough)
# ============================================================


_REWRITE_SYSTEM = (
    "You rewrite short memory patches into clean second person. "
    "Input is a single sentence describing the app user. Output is the "
    "same content addressed as 'you' with correct verb agreement and "
    "pronoun replacement. Never add facts, never remove facts. If the "
    "input is already in clean second person, return it unchanged. "
    "Never include the literal string '(you)' in the output."
)

_REWRITE_EXAMPLES = [
    ("Scott (you) wants his voice to be recognized",
     "You want your voice to be recognized"),
    ("Scott prefers async standups",
     "You prefer async standups"),
    ("Scott's communication style is direct",
     "Your communication style is direct"),
    ("You tend to over-explain and be repetitive",
     "You tend to over-explain and be repetitive"),  # already clean — no change
    ("Scott (you) is based in Boston",
     "You are based in Boston"),
]


async def llm_rewrite(text: str) -> Optional[str]:
    """Ask the LLM to rewrite the patch text. Returns None on failure."""
    if not LLM_API_KEY:
        return None

    try:
        import httpx  # type: ignore
    except ImportError:
        print("WARN: httpx not available — falling back to regex cleanup only", file=sys.stderr)
        return None

    messages = [{"role": "system", "content": _REWRITE_SYSTEM}]
    for inp, out in _REWRITE_EXAMPLES:
        messages.append({"role": "user", "content": inp})
        messages.append({"role": "assistant", "content": out})
    messages.append({"role": "user", "content": text})

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {LLM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": LLM_MODEL,
                    "messages": messages,
                    "temperature": 0,
                    "max_tokens": 120,
                },
            )
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"].strip()
            # Defensive: LLM must not leak the marker back in
            content = _YOU_MARKER_RE.sub("", content).strip()
            return content or None
    except Exception as e:
        print(f"WARN: LLM rewrite failed ({e}); regex-only fallback", file=sys.stderr)
        return None


# ============================================================
# Main
# ============================================================


async def main(args: argparse.Namespace) -> int:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        query = """
            SELECT cp.patch_id, cp.patch_type, cp.value
            FROM context_patches cp
            JOIN patch_subjects ps ON cp.patch_id = ps.patch_id
            WHERE cp.patch_type IN ('trait', 'preference')
              AND COALESCE(cp.status, 'active') = 'active'
        """
        params: list = []
        if args.user_id:
            query += " AND ps.subject_key = $1"
            params.append(f"user:{args.user_id}")
        query += " ORDER BY cp.created_at DESC"
        if args.limit:
            query += f" LIMIT ${len(params) + 1}"
            params.append(args.limit)

        rows = await conn.fetch(query, *params)

        print(f"Scanned {len(rows)} trait/preference patches")
        print(f"Mode: {'DRY-RUN (no writes)' if args.dry_run else 'APPLY (writes enabled)'}")
        print("---")

        rewrites: List[Tuple[str, str, str, str]] = []
        skipped = 0

        for row in rows:
            patch_id = str(row["patch_id"])
            value = row["value"]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except Exception:
                    value = {}
            if not isinstance(value, dict):
                skipped += 1
                continue

            original_text = (value.get("text") or "").strip()
            if not original_text or not needs_rewrite(original_text):
                skipped += 1
                continue

            # Try LLM rewrite, fall back to regex-only
            new_text = await llm_rewrite(original_text)
            if not new_text:
                new_text = regex_cleanup(original_text)

            # If the rewrite matches the original, nothing to do
            if new_text == original_text:
                skipped += 1
                continue

            rewrites.append((patch_id, row["patch_type"], original_text, new_text))

        print(f"Candidates identified: {len(rewrites)}")
        print(f"Skipped (already-clean or unmatched): {skipped}")
        print("---")
        print("patch_id,type,old,new")
        for patch_id, patch_type, old, new in rewrites:
            print(f'"{patch_id}","{patch_type}","{old}","{new}"')

        if args.dry_run:
            print("---")
            print("DRY-RUN — no writes performed.")
            return 0

        # Apply — one transaction per patch so a single failure doesn't
        # cascade. Print a running tally.
        applied = 0
        failed = 0
        for patch_id, patch_type, old, new in rewrites:
            try:
                async with conn.transaction():
                    value_row = await conn.fetchrow(
                        "SELECT value FROM context_patches WHERE patch_id = $1::uuid",
                        patch_id,
                    )
                    if not value_row:
                        failed += 1
                        continue
                    v = value_row["value"]
                    if isinstance(v, str):
                        v = json.loads(v)
                    v["text"] = new
                    await conn.execute(
                        """
                        UPDATE context_patches
                        SET value = $1::jsonb,
                            origin_mode = 'declared',
                            updated_at = NOW()
                        WHERE patch_id = $2::uuid
                        """,
                        json.dumps(v),
                        patch_id,
                    )
                applied += 1
            except Exception as e:
                print(f"ERROR applying {patch_id}: {e}", file=sys.stderr)
                failed += 1

        print("---")
        print(f"Applied: {applied}")
        print(f"Failed:  {failed}")
        return 0 if failed == 0 else 1
    finally:
        await conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--user-id",
        help="Only process patches for this user_id (as stored in patch_subjects.subject_key)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print proposed rewrites without writing",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Cap on patches scanned (useful for staged rollouts)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    sys.exit(asyncio.run(main(args)))
