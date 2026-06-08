"""Thin Resend wrapper for transactional email.

Scope is deliberately small: one `send_email` async function that posts
to Resend's /emails endpoint and returns a result object. No retries,
no batching, no template rendering. Used today by the alerting service
for critical-failure notifications; if other surfaces ever need email
they can call this same function.

Configuration via env vars in `/opt/contextquilt/.env.prod` (same
pattern as every other CQ secret, no external service dependency):

  * `RESEND_API_KEY` — Resend API key for the account that owns the
    verified sender domain. CQ holds its own copy of the key rather
    than reaching into another stack's Secret Manager — keeps the
    trust boundary clean and makes rotation a normal .env.prod edit
    plus worker bounce (same flow as the OpenRouter key rotation).
  * `CQ_ALERT_EMAIL_FROM` — default from-address. Must be on a Resend
    verified sender domain or the send is refused. Today CQ sends
    from `cq-alerts@mail.shouldersurf.com` (mail.shouldersurf.com is
    verified on the shared Resend account; CQ can verify
    mail.contextquilt.com later with no code change beyond the env
    var).
  * Tags — every CQ-side send includes `purpose=critical-alert`,
    `category=<category>`, and `stack=cq` so the shared Resend
    account's analytics can cleanly partition CQ traffic from GP/SS
    marketing traffic. GP adds `stack=gp` on their side.

Errors are never raised. A failing send returns `SendResult(sent=False,
error=...)` and the caller decides how loud to be about it. This is a
property of the alerting use case: alerting must not break the request
that triggered it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from contextquilt.config import get_settings

logger = logging.getLogger(__name__)


RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_TIMEOUT_S = 10.0


@dataclass
class SendResult:
    sent: bool
    provider_id: str | None = None
    error: str | None = None
    tags: list[dict[str, str]] = field(default_factory=list)


async def send_email(
    *,
    to: str,
    subject: str,
    html: str,
    from_addr: str | None = None,
    tags: list[dict[str, str]] | None = None,
    api_key: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SendResult:
    """Send one HTML email via Resend.

    All arguments are keyword-only to keep call sites readable. Returns
    a `SendResult`. Never raises out of band, even on network errors,
    Resend 5xx, or misconfiguration. Callers should check `.sent`.
    """
    settings = get_settings()
    key = api_key or settings.resend_api_key
    if not key:
        logger.warning("email_send.no_api_key — set RESEND_API_KEY in .env.prod")
        return SendResult(sent=False, error="no_api_key")

    sender = from_addr or settings.cq_alert_email_from
    if not sender:
        logger.warning("email_send.no_from_addr — set CQ_ALERT_EMAIL_FROM")
        return SendResult(sent=False, error="no_from_addr")

    payload: dict[str, Any] = {
        "from": sender,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if tags:
        payload["tags"] = tags

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                RESEND_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        logger.warning("email_send.transport_failed reason=%s", exc)
        return SendResult(sent=False, error=f"transport: {exc}", tags=tags or [])

    if resp.status_code >= 400:
        body_preview = resp.text[:300] if resp.text else ""
        logger.warning(
            "email_send.rejected status=%d body=%s", resp.status_code, body_preview
        )
        return SendResult(
            sent=False,
            error=f"http {resp.status_code}: {body_preview}",
            tags=tags or [],
        )

    try:
        provider_id = resp.json().get("id")
    except Exception:
        provider_id = None

    return SendResult(sent=True, provider_id=provider_id, tags=tags or [])
