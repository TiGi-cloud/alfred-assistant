"""Alfred Gmail integration — read and send emails.

Supports two modes:
1. Google API with OAuth2 (full-featured: labels, threads, search)
2. SMTP/IMAP with App Password (simpler setup, basic send/read)

Config via env vars:
  GMAIL_ADDRESS      — your Gmail address
  GMAIL_APP_PASSWORD — Gmail App Password (for SMTP/IMAP mode)
  GMAIL_OAUTH_CREDS  — path to OAuth2 credentials.json (for API mode)
"""
from __future__ import annotations

import os
import re
import json
import email
import imaplib
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import decode_header
from datetime import datetime

logger = logging.getLogger("alfred")

# Check which mode is available
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_OAUTH_CREDS = os.environ.get("GMAIL_OAUTH_CREDS", "")

HAS_GMAIL = bool(GMAIL_ADDRESS and (GMAIL_APP_PASSWORD or GMAIL_OAUTH_CREDS))


def _decode_header_value(val: str) -> str:
    """Decode MIME-encoded header."""
    if not val:
        return ""
    parts = decode_header(val)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_body(msg: email.message.Message) -> str:
    """Extract text body from email message."""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback to HTML
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    text = payload.decode(charset, errors="replace")
                    # Strip HTML tags for Telegram
                    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
                    text = re.sub(r'<[^>]+>', '', text)
                    return text.strip()
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return "(no body)"


def read_emails(count: int = 5, folder: str = "INBOX", search: str | None = None) -> list[dict]:
    """Read recent emails via IMAP. Returns list of {subject, from, date, body, uid}."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return [{"error": "Gmail not configured. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD env vars."}]

    results = []
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select(folder, readonly=True)

        if search:
            # IMAP search query
            criteria = f'(OR SUBJECT "{search}" FROM "{search}" BODY "{search}")'
            _, data = imap.search(None, criteria)
        else:
            _, data = imap.search(None, "ALL")

        msg_ids = data[0].split()
        if not msg_ids:
            imap.close()
            imap.logout()
            return []

        # Get last N
        for uid in msg_ids[-count:]:
            _, msg_data = imap.fetch(uid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)

            body = _extract_body(msg)
            if len(body) > 500:
                body = body[:500] + "..."

            results.append({
                "uid": uid.decode(),
                "subject": _decode_header_value(msg.get("Subject", "(no subject)")),
                "from": _decode_header_value(msg.get("From", "")),
                "date": msg.get("Date", ""),
                "body": body,
            })

        imap.close()
        imap.logout()
    except Exception as e:
        logger.error("Gmail read error: %s", e)
        return [{"error": str(e)}]

    results.reverse()  # newest first
    return results


def send_email(to: str, subject: str, body: str, html: bool = False) -> str:
    """Send an email via SMTP. Returns 'sent' or error string."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "Gmail not configured. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD env vars."

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = to
        msg["Subject"] = subject

        if html:
            msg.attach(MIMEText(body, "html"))
        else:
            msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            smtp.send_message(msg)

        logger.info("Email sent to %s: %s", to, subject)
        return "sent"
    except Exception as e:
        logger.error("Gmail send error: %s", e)
        return str(e)


def list_labels() -> list[str]:
    """List available Gmail labels/folders."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return ["(not configured)"]
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        _, data = imap.list()
        labels = []
        for item in data:
            if isinstance(item, bytes):
                # Parse IMAP list response
                match = re.search(r'"([^"]+)"$', item.decode())
                if match:
                    labels.append(match.group(1))
        imap.logout()
        return labels
    except Exception as e:
        return [f"Error: {e}"]
