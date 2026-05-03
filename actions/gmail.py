"""
/gmail — read recent inbox via macOS Mail.app or IMAP.

Two backends, in order of preference:

1. **Mail.app** (macOS) — runs an AppleScript against the user's Mail.app
   to read recent unread messages. Zero auth setup; works as long as Mail
   is signed into the account.
2. **IMAP** — if `IMAP_HOST`/`IMAP_USER`/`IMAP_PASS` are in `.env`, uses
   them to fetch recent messages. Useful for headless setups.

Sending mail isn't implemented in this v1 — that path needs proper SMTP
or OAuth and we're keeping things low-friction. Use Mail.app directly,
or open a draft from chat: /gmail draft to:foo@bar subject:hi body:hello
opens a new compose window in Mail.app via AppleScript.
"""
from __future__ import annotations

import asyncio
import imaplib
import logging
import os
import re
import sys
from email import message_from_bytes
from email.header import decode_header

from kernel.runner import Context

logger = logging.getLogger("alfred.actions.gmail")


def _decode(s: str | bytes | None) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        try:
            return s.decode(errors="replace")
        except Exception:
            return ""
    parts = decode_header(s)
    out = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(charset or "utf-8", errors="replace"))
            except Exception:
                out.append(chunk.decode(errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


# ---------------------------------------------------------------------------
# Mail.app backend (macOS)
# ---------------------------------------------------------------------------
_MAIL_LIST_SCRIPT = """
tell application "Mail"
    set acc to first account whose enabled is true
    set inboxRef to mailbox "INBOX" of acc
    set msgs to (messages of inboxRef whose read status is false)
    set output to ""
    set n to count of msgs
    if n > {limit} then set n to {limit}
    repeat with i from 1 to n
        set m to item i of msgs
        try
            set theSender to sender of m
            set theSubject to subject of m
            set theDate to date received of m
            set theSnippet to ""
            try
                set theSnippet to (get content of m)
                if (length of theSnippet) > 200 then set theSnippet to (text 1 thru 200 of theSnippet) & "…"
            end try
            set output to output & "FROM:" & theSender & linefeed & "SUBJ:" & theSubject & linefeed & "DATE:" & (theDate as string) & linefeed & "BODY:" & theSnippet & linefeed & "---" & linefeed
        end try
    end repeat
    return output
end tell
"""


async def _read_via_mail_app(limit: int = 5) -> list[dict]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", _MAIL_LIST_SCRIPT.replace("{limit}", str(limit)),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return []
    if proc.returncode != 0:
        logger.warning("Mail.app read failed: %s", err.decode(errors="replace"))
        return []
    text = out.decode(errors="replace")
    messages: list[dict] = []
    cur: dict = {}
    for line in text.splitlines():
        if line.startswith("FROM:"):
            cur = {"from": line[5:].strip()}
        elif line.startswith("SUBJ:"):
            cur["subject"] = line[5:].strip()
        elif line.startswith("DATE:"):
            cur["date"] = line[5:].strip()
        elif line.startswith("BODY:"):
            cur["body"] = line[5:].strip()
        elif line.strip() == "---":
            if cur:
                messages.append(cur)
            cur = {}
    if cur:
        messages.append(cur)
    return messages


_DRAFT_SCRIPT = """
tell application "Mail"
    set newMsg to make new outgoing message with properties {{visible:true, subject:"{subject}", content:"{body}"}}
    tell newMsg
        make new to recipient at end of to recipients with properties {{address:"{to}"}}
    end tell
    activate
end tell
"""


async def _draft_via_mail_app(to: str, subject: str, body: str) -> bool:
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e",
        _DRAFT_SCRIPT.format(to=_esc(to), subject=_esc(subject), body=_esc(body)),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# IMAP backend
# ---------------------------------------------------------------------------
def _read_via_imap_sync(host: str, user: str, password: str, limit: int) -> list[dict]:
    M = imaplib.IMAP4_SSL(host)
    try:
        M.login(user, password)
        M.select("INBOX")
        _, data = M.search(None, "UNSEEN")
        ids = data[0].split()[-limit:]
        messages: list[dict] = []
        for mid in ids:
            _, mdata = M.fetch(mid, "(RFC822)")
            if not mdata or not mdata[0]:
                continue
            msg = message_from_bytes(mdata[0][1])
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True)
                        if isinstance(body, bytes):
                            body = body.decode(errors="replace")
                        break
            else:
                body = msg.get_payload(decode=True)
                if isinstance(body, bytes):
                    body = body.decode(errors="replace")
            messages.append({
                "from": _decode(msg.get("From")),
                "subject": _decode(msg.get("Subject")),
                "date": msg.get("Date", "?"),
                "body": (body or "")[:200],
            })
        return messages
    finally:
        try:
            M.logout()
        except Exception:
            pass


async def _read_via_imap(limit: int = 5) -> list[dict]:
    host = os.environ.get("IMAP_HOST", "").strip()
    user = os.environ.get("IMAP_USER", "").strip()
    pwd = os.environ.get("IMAP_PASS", "").strip()
    if not (host and user and pwd):
        return []
    try:
        return await asyncio.to_thread(_read_via_imap_sync, host, user, pwd, limit)
    except Exception as e:
        logger.warning("IMAP read failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# /gmail
# ---------------------------------------------------------------------------
def _format(msgs: list[dict]) -> str:
    if not msgs:
        return "📭 No unread mail."
    lines = [f"📬 {len(msgs)} unread:"]
    for i, m in enumerate(msgs, 1):
        sender = m.get("from", "?")
        # Strip <…> tail if present
        if "<" in sender:
            sender = sender.split("<")[0].strip().strip('"')
        sender = sender[:40]
        lines.append(f"\n{i}. {sender}")
        lines.append(f"   {m.get('subject', '(no subject)')}")
        if m.get("date"):
            lines.append(f"   {m['date']}")
        if m.get("body"):
            lines.append(f"   {m['body'][:200]}")
    return "\n".join(lines)


async def cmd_gmail(ctx: Context) -> None:
    """Read recent unread mail or open a draft.

    Usage:
      /gmail                        list unread
      /gmail read <N>               list N unread
      /gmail draft to:a@b body:…    open compose window
    """
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""

    if args.startswith("draft"):
        if sys.platform != "darwin":
            await ctx.reply("/gmail draft only works on macOS (uses Mail.app).")
            return
        # Parse "to:foo@bar subject:hi body:hello"
        to_m = re.search(r"to:(\S+)", args)
        sub_m = re.search(r"subject:([^\n]+?)(?=\s+body:|$)", args)
        body_m = re.search(r"body:(.+)$", args, re.DOTALL)
        if not to_m:
            await ctx.reply("Usage: /gmail draft to:foo@bar.com subject:hi body:hello")
            return
        ok = await _draft_via_mail_app(
            to_m.group(1).strip(),
            (sub_m.group(1).strip() if sub_m else ""),
            (body_m.group(1).strip() if body_m else ""),
        )
        await ctx.reply("✓ draft opened in Mail.app" if ok else "❌ draft failed (is Mail.app set up?)")
        return

    limit = 5
    if args.startswith("read"):
        rest = args[4:].strip()
        try:
            limit = max(1, min(20, int(rest)))
        except (ValueError, TypeError):
            limit = 5

    if sys.platform == "darwin":
        msgs = await _read_via_mail_app(limit=limit)
        if msgs is not None and msgs:
            await ctx.reply(_format(msgs))
            return
        # Fall through to IMAP if Mail.app gave nothing

    msgs = await _read_via_imap(limit=limit)
    if not msgs:
        await ctx.reply(
            "📭 No mail or no backend configured.\n\n"
            "Either: open Mail.app and sign in (macOS), OR\n"
            "set IMAP_HOST, IMAP_USER, IMAP_PASS in .env"
        )
        return
    await ctx.reply(_format(msgs))


def register(dispatcher) -> None:
    dispatcher.command("gmail", cmd_gmail)
