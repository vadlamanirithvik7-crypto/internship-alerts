"""Alert delivery over email (Gmail SMTP) and ntfy.sh push. Both are free."""

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


def send_email(postings, *, subject=None) -> bool:
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
            f'<div style="font-weight:600;font-size:15px">{posting.title}</div>'
            f'<div style="color:#444;margin:2px 0">{posting.company_name}</div>'
            f'<div style="color:#777;font-size:13px">{location} &middot; {sectors}</div>'
            f'<a href="{posting.url}" style="color:#0b57d0;font-size:13px">View posting &rarr;</a>'
            f"</td></tr>"
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    message.set_content("\n".join(text_lines))
    message.add_alternative(
        f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif">
        <h2 style="font-size:18px">{subject}</h2>
        <table style="border-collapse:collapse;width:100%;max-width:640px">
        {''.join(html_rows)}
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


def send_ntfy(postings) -> bool:
    """Push one notification per posting so each is individually tappable."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        log.info("ntfy: no topic configured, skipping")
        return False
    if not postings:
        return False

    sent = 0
    # Publish via the JSON API rather than ntfy's header-based API: HTTP headers
    # must be latin-1, and job titles routinely contain en-dashes, curly quotes
    # and accented names, which raise UnicodeEncodeError before the request is
    # even sent. The JSON body carries UTF-8 natively.
    def publish(payload):
        try:
            response = requests.post(
                NTFY_BASE,
                json={"topic": topic, **payload},
                headers={"User-Agent": USER_AGENT},
                timeout=15,
            )
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.warning("ntfy: push failed: %s", exc)
            return False

    # Cap the burst so a large first run doesn't flood the phone.
    for posting in postings[:20]:
        if publish(
            {
                "title": posting.title[:200],
                "message": f"{posting.company_name} - {posting.location or 'Location N/A'}",
                "click": posting.url,
                "tags": ["briefcase"],
            }
        ):
            sent += 1

    if len(postings) > 20:
        publish(
            {
                "title": "More new internships",
                "message": f"...and {len(postings) - 20} more. Open the dashboard to see them all.",
            }
        )

    log.info("ntfy: pushed %s notifications", sent)
    return sent > 0
