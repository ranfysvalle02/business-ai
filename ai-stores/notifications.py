"""Lead notifications — best-effort email to the store owner on a new inquiry.

Uses Resend's HTTP API via the already-pinned ``httpx`` (no new dependency).
Everything here is fire-and-forget: it is scheduled through FastAPI
``BackgroundTasks`` after the visitor's ``201``, and every failure path is
logged and swallowed so a mail outage can never affect lead capture.

Configuration (all optional — absence simply disables sending):
  * ``RESEND_API_KEY``  — platform Resend key. Unset → notifications disabled.
  * ``RESEND_FROM``     — verified sender, e.g. ``"AI Stores <leads@yourdomain>"``.
  * ``NOTIFY_ENABLED``  — global kill switch (default ``true``).

Per-store controls (on the store document, edited from the admin dashboard):
  * ``notify_enabled``  — per-store on/off (default on when the field is absent).
  * ``notify_email``    — override recipient; falls back to ``store.email``.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("ai-stores.notifications")

RESEND_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT = httpx.Timeout(6.0, connect=3.0)


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def _recipient(store: dict[str, Any]) -> str | None:
    to = str(store.get("notify_email") or store.get("email") or "").strip()
    return to or None


def _build_email(store: dict[str, Any], inquiry: dict[str, Any]) -> dict[str, str]:
    store_name = str(store.get("name") or "Your store")
    name = html.escape(str(inquiry.get("customer_name") or "Someone"))
    contact = html.escape(str(inquiry.get("customer_contact") or "—"))
    item = html.escape(str(inquiry.get("item_name") or "General inquiry"))
    issue = html.escape(str(inquiry.get("issue") or ""))
    message = html.escape(str(inquiry.get("message") or "")).replace("\n", "<br>")

    subject = f"New lead for {store_name}: {inquiry.get('customer_name') or 'inquiry'}"
    rows = [
        ("Name", name),
        ("Contact", contact),
        ("About", item),
    ]
    if issue:
        rows.append(("Topic", issue))
    if message:
        rows.append(("Message", message))
    body_rows = "".join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#64748b;font-weight:600;'
        f'vertical-align:top">{label}</td><td style="padding:4px 0;color:#0f172a">{value}</td></tr>'
        for label, value in rows
    )
    html_body = (
        f'<div style="font-family:system-ui,-apple-system,sans-serif;max-width:560px">'
        f'<h2 style="margin:0 0 4px;color:#0f172a">New inquiry for {html.escape(store_name)}</h2>'
        f'<p style="margin:0 0 16px;color:#64748b">Someone just reached out through your storefront.</p>'
        f'<table style="border-collapse:collapse;font-size:15px">{body_rows}</table>'
        f'<p style="margin:20px 0 0;color:#94a3b8;font-size:12px">'
        f"Reply directly to {contact} to follow up.</p></div>"
    )
    return {"subject": subject, "html": html_body}


async def notify_new_inquiry(store: dict[str, Any], inquiry: dict[str, Any]) -> bool:
    """Send a new-lead email. Returns ``True`` if a send was attempted OK.

    Never raises: any misconfiguration or transport error is logged and
    swallowed so inquiry capture is unaffected.
    """
    try:
        api_key = os.getenv("RESEND_API_KEY", "").strip()
        if not api_key:
            return False
        if not _truthy(os.getenv("NOTIFY_ENABLED"), default=True):
            return False
        if store.get("notify_enabled") is False:
            return False

        recipient = _recipient(store)
        if not recipient:
            logger.info("Lead notification skipped: no recipient (store.notify_email/email unset)")
            return False

        sender = os.getenv("RESEND_FROM", "").strip()
        if not sender:
            logger.warning("Lead notification skipped: RESEND_FROM is not set")
            return False

        email = _build_email(store, inquiry)
        payload = {
            "from": sender,
            "to": [recipient],
            "subject": email["subject"],
            "html": email["html"],
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                RESEND_ENDPOINT,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if resp.status_code >= 400:
            logger.warning(f"Lead notification failed: Resend returned {resp.status_code} {resp.text[:200]}")
            return False
        logger.info(f"Lead notification sent to {recipient}")
        return True
    except Exception as exc:  # noqa: BLE001 — notifications must never break the request path
        logger.warning(f"Lead notification error (swallowed): {exc}")
        return False
