"""Alert delivery over email (Gmail SMTP) and ntfy.sh push. Both are free."""

from html import escape
import logging
import os
import smtplib
from email.message import EmailMessage

import requests

from poller.net import USER_AGENT

log = logging.getLogger(__name__)

NTFY_BASE = "https://ntfy.sh"


def _sector_line(posting):
    from shared.db import unpack_list
    from shared.sectors import sector_labels

    labels = sector_labels()
    tags = unpack_list(posting.sector_tags)
    return ", ".join(labels.get(t, t) for t in tags) or "Uncategorized"


def send_email(postings, *, subject=None, groups=None) -> bool:
    """Send one digest email covering all newly matched postings."""
    sender = os.environ.get("ALERT_EMAIL_FROM")
    recipient = os.environ.get("ALERT_EMAIL_TO") or sender
    password = os.environ.get("GMAIL_APP_PASSWORD")

    if not (sender and recipient and password):
        log.info("email: not configured, skipping")
        return False
    if not postings:
        return False

    count = len(postings)
    subject = subject or f"{count} new internship{'s' if count != 1 else ''}"

    text_lines, html_rows = [], []
    if groups:
        for name, rows in groups.items():
            text_lines.append(
                f"{name}: " + "; ".join(f"{p.company_name} — {p.title}" for p in rows)
            )
            html_rows.append(
                f"<tr><td><h3>{escape(name)}</h3><p>"
                + "<br>".join(escape(f"{p.company_name} — {p.title}") for p in rows)
                + "</p></td></tr>"
            )
    for posting in postings:
        sectors = _sector_line(posting)
        location = posting.location or "Location not specified"
        text_lines.append(
            f"{posting.company_name} - {posting.title}\n"
            f"  {location} | {sectors}\n  {posting.url}\n"
        )
        html_rows.append(
            f'<tr style="border-bottom:1px solid #eee">'
            f'<td style="padding:12px 8px">'
            f'<div style="font-weight:600;font-size:15px">{escape(posting.title)}</div>'
            f'<div style="color:#444;margin:2px 0">{escape(posting.company_name)}</div>'
            f'<div style="color:#777;font-size:13px">{escape(location)} &middot; {escape(sectors)}</div>'
            f'<a href="{escape(posting.url)}" style="color:#0b57d0;font-size:13px">View posting &rarr;</a>'
            f"</td></tr>"
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content("\n".join(text_lines))
    message.add_alternative(
        f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif">
        <h2 style="font-size:18px">{escape(subject)}</h2>
        <table style="border-collapse:collapse;width:100%;max-width:640px">
        {"".join(html_rows)}
        </table>
        <p style="color:#888;font-size:12px">Sent by your internship alert system.</p>
        </body></html>""",
        subtype="html",
    )

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender, password)
            smtp.send_message(message)
        log.info("email: sent %s postings to %s", count, recipient)
        return True
    except Exception as exc:
        log.error("email: send failed: %s", exc)
        return False


def publish_ntfy(payload):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    headers = {"User-Agent": USER_AGENT}
    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.post(
            (os.environ.get("NTFY_BASE") or NTFY_BASE),
            json={"topic": topic, **payload},
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        return True
    except requests.RequestException:
        log.warning("ntfy: delivery failed")
        return False


def send_ntfy_deliveries(postings):
    """Return ONLY successfully delivered posting IDs, including acknowledged digest."""
    delivered = set()
    for p in postings[:20]:
        if publish_ntfy(
            {
                "title": p.title[:200],
                "message": f"{p.company_name} - {p.location or 'Location N/A'}",
                "click": p.url,
                "tags": ["briefcase"],
            }
        ):
            delivered.add(p.id)
    if len(postings) > 20:
        # Include actual job links so acknowledgement means the jobs were delivered.
        rest = postings[20:40]
        message = "\n".join(f"{p.company_name}: {p.title}\n{p.url}" for p in rest)
        if len(message.encode("utf-8")) <= 3500 and publish_ntfy(
            {"title": "More internship matches", "message": message}
        ):
            delivered.update(p.id for p in rest)
        # Oversized digests and remaining rows stay pending for later runs.
    return delivered


def send_ntfy(postings):
    """Legacy boolean interface for callers without database IDs."""
    if not postings or not os.environ.get("NTFY_TOPIC"):
        return False
    return all(
        [
            publish_ntfy(
                {
                    "title": p.title[:200],
                    "message": f"{p.company_name} - {p.location or 'Location N/A'}",
                    "click": p.url,
                }
            )
            for p in postings[:20]
        ]
    )
