"""Critical-failure alerting for ContextQuilt.

Operator-facing email alerts for system-level outages that have
nothing to do with a single user's query: managed LLM provider key
revoked, managed budget exhausted, backup pipeline failed, etc.
Targets the "silent failure for 2 weeks" footgun that prompted the
feature after the May 2026 OpenRouter key-cap incident.

Mirrors GhostPour's alerting contract (cloudzap PR #194) verbatim
where it can. The schema and `report_incident` signature are the same
shape; SQL dialect is translated from SQLite to Postgres and the DB
handle is asyncpg's `Pool` or `Connection` instead of aiosqlite's
connection.

## Contract

Callers report an incident with three fields:

  * category: a stable token from `KNOWN_CATEGORIES`. Defines the
    semantic class (provider auth vs budget vs backup).
  * subject: short identifier of WHAT is broken inside that category.
    For provider failures, the provider id ("openrouter", "openai").
    For backup failures, the backup_run id. Becomes part of the
    dedup fingerprint.
  * details: arbitrary dict, stored as JSONB. Surfaces in the
    dashboard incident detail. **Must not contain secrets** —
    callers redact at the wire site.

The service:

  1. Computes `fingerprint = category + ":" + subject`.
  2. Auto-resolves any open incident whose `last_seen_at` is older
     than `INCIDENT_AUTO_RESOLVE_MINUTES`. A re-fire after that
     quiet window starts a fresh row and re-emails.
  3. Looks for an OPEN incident (resolved_at IS NULL) with this
     fingerprint. If found, bumps trigger_count + last_seen_at; does
     NOT re-email. This is the "once per incident" suppression.
  4. If no open incident, INSERTs a row, looks up recipients
     subscribed to this category, emails them, records who got it.

The INSERT uses `ON CONFLICT ... DO NOTHING` against the partial
unique index `idx_alert_incidents_open` so two concurrent callers
that race past the SELECT in step 3 don't both create open rows.
The losing caller treats it as "incident already open" via a refetch.

## Failure handling

`report_incident` swallows every downstream failure (DB hiccup,
Resend transport error, anything). It logs loudly but never propagates
out of band. The reason is the alerting use case itself: if the
alert path can break the request that triggered it, an outage
compounds rather than surfaces.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .email_send import send_email

logger = logging.getLogger(__name__)


# Auto-resolve any open incident whose last_seen_at is older than this.
# A real outage that keeps firing rolls last_seen_at forward and stays
# open; once it quiets for the window, the next failure with the same
# fingerprint re-fires email. Chosen to match GP for cross-stack parity.
INCIDENT_AUTO_RESOLVE_MINUTES = 30


# Single source of truth for the categories the dashboard renders,
# the recipient picker filters against, and the wire-site callers
# reference. Adding a category here makes it appear in the dashboard
# automatically.
#
# `cq_unreachable` is defined for cross-stack schema parity with GP
# but is NEVER fired from CQ itself (CQ can't email when CQ is down).
# GP fires it; this constant lives here so the dashboard picker
# matches across stacks.
KNOWN_CATEGORIES: dict[str, dict[str, str]] = {
    "cq_unreachable": {
        "label": "Context Quilt unreachable",
        "description": (
            "GhostPour's connection to the Context Quilt API is "
            "timing out or refused. Affects recall and capture for "
            "all users. Fired from the GP side, included here for "
            "schema parity."
        ),
    },
    "provider_auth_failed": {
        "label": "Managed LLM key rejected",
        "description": (
            "A 401 or 403 from one of our managed LLM provider keys. "
            "Likely the key was rotated, revoked, or billing on that "
            "provider account lapsed. This is the exact category "
            "that would have caught the May 2026 OpenRouter outage."
        ),
    },
    "provider_budget_exhausted": {
        "label": "Managed LLM budget exhausted",
        "description": (
            "A managed provider returned a quota or budget exceeded "
            "error (HTTP 402, or 429 with insufficient_quota or "
            "credit_balance_too_low). Time to top up that provider "
            "or raise the per-key spending cap."
        ),
    },
    "backup_failed": {
        "label": "Postgres backup failed",
        "description": (
            "The cq-backup sidecar recorded a failed run in the "
            "backup_runs table. Inspect cq-backup logs and the "
            "backup_runs.error_message column for the cause."
        ),
    },
    "account_purge_failed": {
        "label": "Account deletion could not be completed",
        "description": (
            "A queued account_deleted signal failed to purge across "
            "repeated retries, so a user asked to be deleted and CQ "
            "still holds their data. The consumer keeps retrying every "
            "60s, so this will not self-heal quietly, but it will also "
            "not stop on its own. Inspect the signal row in "
            "tier_signals (processed_at IS NULL) and the "
            "tier_signals_loop_error log lines. Subject is the "
            "signal_id, or 'tier_signals_loop' when the whole consumer "
            "is failing rather than one signal."
        ),
    },
    "account_purge_inconsistent": {
        "label": "Deletion request refused as malformed",
        "description": (
            "A signal arrived claiming account deletion but with an "
            "inconsistent shape (event_type and new_tier disagree), so "
            "CQ recorded it and deliberately did NOT purge. This is "
            "one-shot: the signal is stamped skipped_inconsistent and "
            "never retried, so nothing will fix it without a human. "
            "Either the request was malformed upstream and needs "
            "re-sending, or a real deletion is silently not happening."
        ),
    },
    "anthropic_fallback_to_or": {
        "label": "Anthropic extraction fell back to OpenRouter",
        "description": (
            "A CQ extraction call against Anthropic's native API "
            "failed (auth, rate limit, 5xx, timeout, or network) and "
            "the wrapper transparently retried through OpenRouter. "
            "The extraction succeeded but the Anthropic-side issue "
            "needs investigation before our managed key path silently "
            "rots."
        ),
    },
    "provider_health_failed": {
        "label": "LLM provider health probe failing",
        "description": (
            "The provider_health_loop has seen 3 consecutive failed "
            "probes (45 minutes of continuous failure) against this "
            "provider. Likely an auth issue (revoked key, lapsed "
            "billing) since transient outages would alert as "
            "'degraded' first. Subject is the provider name "
            "(anthropic | openrouter)."
        ),
    },
}


@dataclass
class IncidentReport:
    """Result of `report_incident`. Mostly for tests and dashboard
    feedback; production callers can call-and-forget."""
    incident_id: str
    is_new: bool
    emailed_to: list[str]
    suppressed_reason: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(category: str, subject: str) -> str:
    return f"{category}:{subject}"


async def sweep_stale_incidents(conn: Any) -> int:
    """Mark any open incident as resolved when its last_seen is older
    than INCIDENT_AUTO_RESOLVE_MINUTES. Returns count resolved.

    Two callers:
      1. report_incident, at the top, so a re-fire after a quiet
         window starts a fresh row (and re-emails).
      2. The worker's periodic resolver (backup_failure_watch_loop),
         so a HEALTHY system still closes its last open incident.
         report_incident only runs on failure, so without the periodic
         call the most recent incident never resolves once the system
         goes quiet — it just sits open forever, misrepresenting health.

    Cheap because the partial unique index covers WHERE resolved_at IS
    NULL."""
    cutoff = _utcnow() - timedelta(minutes=INCIDENT_AUTO_RESOLVE_MINUTES)
    result = await conn.execute(
        """
        UPDATE alert_incidents
           SET resolved_at = $1
         WHERE resolved_at IS NULL
           AND last_seen_at < $2
        """,
        _utcnow(),
        cutoff,
    )
    # asyncpg returns a status string like "UPDATE 3" — parse the count.
    try:
        return int(result.split()[-1]) if isinstance(result, str) else 0
    except (ValueError, IndexError):
        return 0


async def _active_recipients_for(
    conn: Any, category: str,
) -> list[tuple[str, str | None]]:
    """Return (email, display_name) tuples for active recipients
    subscribed to `category`. Recipients with no category filter
    (NULL or empty JSONB array) receive every category."""
    rows = await conn.fetch(
        """
        SELECT email, display_name, categories
          FROM alert_recipients
         WHERE active = TRUE
         ORDER BY email
        """
    )
    out: list[tuple[str, str | None]] = []
    for row in rows:
        cats_raw = row["categories"]
        # asyncpg returns JSONB as already-parsed Python (list/dict/None).
        if cats_raw is None:
            out.append((row["email"], row["display_name"]))
            continue
        # Defensive: handle either decoded list or raw JSON string.
        if isinstance(cats_raw, str):
            try:
                cats = json.loads(cats_raw)
            except (json.JSONDecodeError, TypeError):
                cats = None
        else:
            cats = cats_raw
        if not cats:
            out.append((row["email"], row["display_name"]))
        elif category in cats:
            out.append((row["email"], row["display_name"]))
    return out


def _render_email_html(
    category: str,
    subject: str,
    details: dict[str, Any],
    incident_id: str,
    first_seen_at: datetime,
) -> tuple[str, str]:
    """Return (subject_line, html_body) for the outgoing alert email."""
    cat_label = KNOWN_CATEGORIES.get(category, {}).get("label", category)
    cat_desc = KNOWN_CATEGORIES.get(category, {}).get("description", "")
    subject_line = f"[ContextQuilt] {cat_label} — {subject}"

    detail_lines = ""
    for k, v in (details or {}).items():
        val = str(v)
        if len(val) > 500:
            val = val[:500] + "…"
        detail_lines += (
            f"<tr><td style='padding:4px 12px;color:#666;vertical-align:top'>"
            f"{k}</td><td style='padding:4px 12px;font-family:monospace;"
            f"word-break:break-all'>{val}</td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#222">
<div style="max-width:600px;margin:0 auto;padding:24px">
  <div style="background:#fff4e5;border-left:4px solid #f59e0b;padding:16px 20px;margin-bottom:24px">
    <div style="font-size:14px;color:#92400e;text-transform:uppercase;letter-spacing:0.05em;font-weight:600">
      Critical failure detected
    </div>
    <div style="font-size:18px;font-weight:600;margin-top:6px">{cat_label}</div>
    <div style="font-size:14px;color:#555;margin-top:4px">Subject: {subject}</div>
  </div>
  <p style="color:#444;line-height:1.5">{cat_desc}</p>
  <div style="border:1px solid #e5e5e5;border-radius:4px;margin-top:20px">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <tr><td style="padding:4px 12px;color:#666">First seen</td><td style="padding:4px 12px">{first_seen_at.isoformat()}</td></tr>
      <tr><td style="padding:4px 12px;color:#666">Incident ID</td><td style="padding:4px 12px;font-family:monospace">{incident_id}</td></tr>
      <tr><td style="padding:4px 12px;color:#666">Category</td><td style="padding:4px 12px;font-family:monospace">{category}</td></tr>
      {detail_lines}
    </table>
  </div>
  <p style="color:#888;font-size:12px;margin-top:32px">
    Sent because you're subscribed to ContextQuilt critical-failure alerts.
    The next email for this same fingerprint won't fire until at least
    {INCIDENT_AUTO_RESOLVE_MINUTES} minutes of quiet pass.
  </p>
</div>
</body></html>
"""
    return subject_line, html


async def report_incident(
    conn: Any,
    *,
    category: str,
    subject: str,
    details: dict[str, Any] | None = None,
    from_addr: str | None = None,
) -> IncidentReport:
    """Report a critical failure. Idempotent per (category, subject)
    fingerprint while the incident is open.

    `conn` is an asyncpg `Pool` or `Connection` (both expose
    `.execute`, `.fetch`, `.fetchrow`).

    Returns an `IncidentReport` describing what happened. Production
    callers can call-and-forget; this function never raises out of
    band for downstream failures (Resend transport errors are
    logged-but-swallowed because alerting must not break the
    request that triggered it)."""
    if category not in KNOWN_CATEGORIES:
        logger.warning(
            "alerting.report_incident: unknown category=%r — recording anyway",
            category,
        )

    details = details or {}
    fingerprint = _fingerprint(category, subject)
    now = _utcnow()

    # 1) Auto-resolve stale opens so a re-fire after quiet starts fresh.
    try:
        await sweep_stale_incidents(conn)
    except Exception as exc:
        logger.warning("alerting.sweep_failed reason=%s", exc)

    # 2) Look up open incident by fingerprint.
    try:
        row = await conn.fetchrow(
            """
            SELECT id::text AS id, trigger_count, first_seen_at
              FROM alert_incidents
             WHERE fingerprint = $1
               AND resolved_at IS NULL
            """,
            fingerprint,
        )
    except Exception as exc:
        logger.exception("alerting.fetch_failed reason=%s", exc)
        return IncidentReport(
            incident_id="", is_new=False, emailed_to=[],
            suppressed_reason=f"db_error: {exc}",
        )

    if row is not None:
        # Existing open incident — bump counter, no re-email.
        try:
            await conn.execute(
                """
                UPDATE alert_incidents
                   SET last_seen_at = $1,
                       trigger_count = trigger_count + 1,
                       details_json = $2::jsonb
                 WHERE id = $3::uuid
                """,
                now, json.dumps(details), row["id"],
            )
        except Exception as exc:
            logger.warning("alerting.update_failed reason=%s", exc)
        return IncidentReport(
            incident_id=row["id"],
            is_new=False,
            emailed_to=[],
            suppressed_reason="incident_already_open",
        )

    # 3) New incident — INSERT with ON CONFLICT against the partial
    # unique index. If a concurrent caller raced past the SELECT,
    # the second INSERT becomes a no-op (returning_id is None),
    # and we refetch the now-open row and treat it as already open.
    incident_id = str(uuid.uuid4())
    try:
        inserted = await conn.fetchrow(
            """
            INSERT INTO alert_incidents
                (id, category, subject, fingerprint,
                 first_seen_at, last_seen_at, trigger_count, details_json)
            VALUES ($1::uuid, $2, $3, $4, $5, $5, 1, $6::jsonb)
            ON CONFLICT (fingerprint) WHERE resolved_at IS NULL DO NOTHING
            RETURNING id::text AS id
            """,
            incident_id, category, subject, fingerprint, now, json.dumps(details),
        )
    except Exception as exc:
        logger.exception("alerting.insert_failed reason=%s", exc)
        return IncidentReport(
            incident_id="", is_new=False, emailed_to=[],
            suppressed_reason=f"db_error: {exc}",
        )

    if inserted is None:
        # Lost the race. Refetch the open row that the other caller
        # created and report it as already open.
        existing = await conn.fetchrow(
            """
            SELECT id::text AS id FROM alert_incidents
             WHERE fingerprint = $1 AND resolved_at IS NULL
            """,
            fingerprint,
        )
        return IncidentReport(
            incident_id=existing["id"] if existing else "",
            is_new=False,
            emailed_to=[],
            suppressed_reason="incident_already_open",
        )

    new_id = inserted["id"]

    # 4) Resolve recipients + send.
    recipients = await _active_recipients_for(conn, category)
    if not recipients:
        return IncidentReport(
            incident_id=new_id, is_new=True, emailed_to=[],
            suppressed_reason="no_recipients",
        )

    subject_line, html = _render_email_html(
        category, subject, details, new_id, now,
    )

    emailed: list[str] = []
    for email, _name in recipients:
        try:
            result = await send_email(
                to=email,
                subject=subject_line,
                html=html,
                from_addr=from_addr,
                tags=[
                    {"name": "purpose", "value": "critical-alert"},
                    {"name": "category", "value": category},
                    # Lets the shared Resend account's analytics
                    # partition CQ traffic from GP/SS streams. GP adds
                    # stack=gp on their side.
                    {"name": "stack", "value": "cq"},
                ],
            )
            if result.sent:
                emailed.append(email)
        except Exception as exc:
            # Defensive: send_email already swallows everything and
            # returns SendResult(sent=False). This catch is belt-and-
            # suspenders so a bug in send_email can't bring down the
            # request that triggered the alert.
            logger.exception(
                "alerting.send_failed category=%s subject=%s recipient=%s reason=%s",
                category, subject, email, exc,
            )

    if emailed:
        try:
            await conn.execute(
                """
                UPDATE alert_incidents
                   SET email_sent_at = $1,
                       emailed_recipients = $2::jsonb
                 WHERE id = $3::uuid
                """,
                now, json.dumps(emailed), new_id,
            )
        except Exception as exc:
            logger.warning("alerting.email_record_failed reason=%s", exc)
        logger.info(
            "alerting.incident_opened category=%s subject=%s incident_id=%s emailed=%d",
            category, subject, new_id, len(emailed),
        )

    return IncidentReport(
        incident_id=new_id,
        is_new=True,
        emailed_to=emailed,
    )


async def list_incidents(conn: Any, *, limit: int = 100) -> list[dict[str, Any]]:
    """For dashboard history view. Includes open and resolved, newest first."""
    rows = await conn.fetch(
        """
        SELECT id::text AS id, category, subject, fingerprint,
               first_seen_at, last_seen_at, trigger_count,
               details_json, email_sent_at, emailed_recipients, resolved_at
          FROM alert_incidents
         ORDER BY first_seen_at DESC
         LIMIT $1
        """,
        limit,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "category": r["category"],
            "subject": r["subject"],
            "fingerprint": r["fingerprint"],
            "first_seen_at": r["first_seen_at"].isoformat() if r["first_seen_at"] else None,
            "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
            "trigger_count": r["trigger_count"],
            "details": r["details_json"],
            "email_sent_at": r["email_sent_at"].isoformat() if r["email_sent_at"] else None,
            "emailed_recipients": r["emailed_recipients"] or [],
            "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
            "status": "open" if r["resolved_at"] is None else "resolved",
        })
    return out
