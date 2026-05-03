"""
iMessage adapter — polls macOS Messages.app's chat.db and sends via AppleScript.

This is the unsupported-by-Apple route. There is no public iMessage API; we
read the same SQLite database that Messages.app uses, and send replies by
asking Messages.app over Apple Events. Both have well-known fragility:

  * macOS upgrades change the chat.db schema roughly once a year.
  * AppleScript send_text fails ~5% of the time under load.
  * Newer macOS hides the plain text in `attributedBody` (NSAttributedString
    BLOB). We try to extract it, but some messages come through with empty
    text. The user can still get content via attachments and metadata.
  * Group chats are intentionally skipped in v1 — only 1:1 chats are processed.

What it does support:
  * Inbound: text messages from anyone in `allowed_handles`, plus attachments
    (downloaded by reading the file at ~/Library/Messages/Attachments/...).
  * Outbound: text and file send via AppleScript.

Required macOS permissions:
  * **Full Disk Access** for the Python interpreter — chat.db is privacy-protected.
  * **Automation → Messages** the first time it sends — accept the prompt.
  * **Accessibility** sometimes needed for AppleScript Messages to behave.

Auth: pass `allowed_handles` — phone numbers (e.g. "+15551234567") or
email addresses (Apple-ID handles). Empty allowlist = anyone.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

from kernel import (
    Attachment,
    AttachmentKind,
    CallbackPress,
    Chat,
    ChatAdapter,
    Keyboard,
    Message,
    MessageKind,
    SentMessage,
    User,
)
from kernel.adapter import PathLike

logger = logging.getLogger("alfred.adapters.imessage")


CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
ATTACHMENTS_DIR = Path.home() / "Library" / "Messages" / "Attachments"
APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01


# ---------------------------------------------------------------------------
# attributedBody decoder (best-effort)
# ---------------------------------------------------------------------------
# Newer macOS stores message text inside an NSAttributedString BLOB instead
# of (or alongside) the plain `text` column. We don't need the full
# typedstream decoder — Alfred just needs the raw string. The text is always
# stored in a UTF-8 segment after a "NSString" / "NSMutableString" marker.
def _decode_attributed_body(blob: Optional[bytes]) -> str:
    if not blob:
        return ""
    # Find a known marker
    for marker in (b"NSString", b"NSMutableString"):
        idx = blob.find(marker)
        if idx < 0:
            continue
        # The byte sequence after the class name has a length prefix:
        #   0x86 0x84 + (1 or 2) byte length, or 0x40 length for short.
        # Easiest: scan forward until we find a long-ish run of printable UTF-8.
        cursor = idx + len(marker)
        # Try every offset for ~30 bytes; pick the first valid UTF-8 chunk
        # of length >= 1.
        end = min(cursor + 200, len(blob))
        for start in range(cursor, end):
            for stop in range(start + 1, min(start + 8192, len(blob))):
                chunk = blob[start:stop]
                # Stop at obvious binary marker bytes
                if chunk.endswith(b"\x86\x84") or b"\x00" in chunk[-3:]:
                    break
                try:
                    text = chunk.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                # Heuristic: at least 1 char, mostly printable
                if len(text) >= 1 and sum(c.isprintable() for c in text) >= len(text) * 0.7:
                    # Try to extend further; if extension fails, return current
                    return text.strip("\x00\x86\x84")
        # If we found the marker but couldn't decode, return empty
        return ""
    return ""


# ---------------------------------------------------------------------------
# AppleScript helpers
# ---------------------------------------------------------------------------
async def _osascript(script: str) -> tuple[int, str, str]:
    """Run an AppleScript and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode().strip(), err.decode().strip()


def _escape_applescript(s: str) -> str:
    """Escape a string for inclusion inside an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------
def _connect_db() -> sqlite3.Connection:
    """Read-only connect to chat.db. Raises if Full Disk Access is missing."""
    if not CHAT_DB.exists():
        raise RuntimeError(f"chat.db not found at {CHAT_DB}. Is Messages.app set up?")
    # `mode=ro` means we never accidentally write to the user's Messages db.
    uri = f"file:{CHAT_DB}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            f"Cannot read chat.db: {e}. Grant Full Disk Access to the Python "
            "interpreter in System Settings → Privacy & Security."
        ) from e
    conn.row_factory = sqlite3.Row
    return conn


def _max_rowid(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message")
    return int(cur.fetchone()[0])


def _fetch_new_messages(conn: sqlite3.Connection, last_rowid: int) -> list[dict]:
    """Return new inbound 1:1 messages with ROWID > last_rowid.

    Filters out:
      - messages we sent (is_from_me = 1)
      - group chats (chat.style != 45)
      - empty messages (no text and no attachments)
    """
    sql = """
        SELECT
            m.ROWID            AS rowid,
            m.guid             AS guid,
            m.text             AS text,
            m.attributedBody   AS attr_body,
            m.is_from_me       AS is_from_me,
            m.date             AS date,
            m.cache_has_attachments AS has_attachments,
            h.id               AS handle,
            h.service          AS service,
            c.chat_identifier  AS chat_id,
            c.style            AS chat_style
        FROM message m
        JOIN handle h    ON m.handle_id = h.ROWID
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        JOIN chat c      ON c.ROWID = cmj.chat_id
        WHERE m.ROWID > ?
          AND m.is_from_me = 0
          AND c.style = 45
        ORDER BY m.ROWID ASC
        LIMIT 100
    """
    cur = conn.execute(sql, (last_rowid,))
    return [dict(row) for row in cur.fetchall()]


def _fetch_attachments(conn: sqlite3.Connection, message_rowid: int) -> list[Attachment]:
    sql = """
        SELECT a.filename, a.transfer_name, a.mime_type, a.total_bytes
        FROM attachment a
        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        WHERE maj.message_id = ?
    """
    out: list[Attachment] = []
    for row in conn.execute(sql, (message_rowid,)):
        filename = row["filename"] or ""
        # filename starts with ~/ in the database; expand it
        local = Path(os.path.expanduser(filename)) if filename else None
        mime = row["mime_type"] or ""
        kind = AttachmentKind.DOCUMENT
        if mime.startswith("image/"):
            kind = AttachmentKind.PHOTO
        elif mime.startswith("audio/"):
            kind = AttachmentKind.VOICE
        elif mime.startswith("video/"):
            kind = AttachmentKind.VIDEO
        out.append(Attachment(
            kind=kind,
            local_path=local,
            mime_type=mime or None,
            size_bytes=row["total_bytes"],
            name=row["transfer_name"] or (local.name if local else None),
        ))
    return out


def _apple_date_to_unix(value) -> float:
    """Convert chat.db `date` to Unix epoch.

    Values can be either nanoseconds-since-2001 (modern) or seconds-since-2001
    (older). We sniff by magnitude.
    """
    if value is None:
        return 0.0
    v = float(value)
    if v > 1e15:  # nanoseconds
        return v / 1e9 + APPLE_EPOCH_OFFSET
    return v + APPLE_EPOCH_OFFSET


# ---------------------------------------------------------------------------
# iMessageAdapter
# ---------------------------------------------------------------------------
class iMessageAdapter(ChatAdapter):
    """macOS iMessage adapter via chat.db polling + AppleScript send."""

    name = "imessage"

    def __init__(
        self,
        *,
        allowed_handles: Iterable[str] = (),
        poll_interval: float = 1.5,
        prefer_imessage_service: bool = True,
    ) -> None:
        self._allowed = {h.strip() for h in allowed_handles if h.strip()}
        self._poll_interval = float(poll_interval)
        self._prefer_imessage = prefer_imessage_service
        self._messages: asyncio.Queue[Message] = asyncio.Queue()
        self._callbacks: asyncio.Queue[CallbackPress] = asyncio.Queue()  # never filled
        self._poll_task: Optional[asyncio.Task] = None
        self._last_rowid: int = 0
        self._started = False

    # -- Lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        # Probe DB; raises early if Full Disk Access is missing
        conn = _connect_db()
        try:
            self._last_rowid = _max_rowid(conn)
        finally:
            conn.close()

        self._poll_task = asyncio.create_task(self._poll_loop(), name="imessage-poll")
        self._started = True
        logger.info(
            "iMessage adapter started; polling chat.db every %.1fs (last ROWID %d)",
            self._poll_interval, self._last_rowid,
        )

    async def stop(self) -> None:
        if not self._started:
            return
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        self._started = False
        logger.info("iMessage adapter stopped")

    # -- Polling loop -------------------------------------------------------
    async def _poll_loop(self) -> None:
        while True:
            try:
                await self._poll_once()
            except Exception:
                logger.exception("iMessage poll error")
            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        # Run blocking sqlite in a thread so we don't stall the event loop.
        rows = await asyncio.to_thread(self._fetch_pending)
        for row in rows:
            self._last_rowid = max(self._last_rowid, int(row["rowid"]))
            handle = (row["handle"] or "").strip()
            if self._allowed and handle not in self._allowed:
                logger.debug("iMessage: dropping message from unauthorised handle %r", handle)
                continue
            msg = self._row_to_message(row)
            if msg is not None:
                await self._messages.put(msg)

    def _fetch_pending(self) -> list[dict]:
        conn = _connect_db()
        try:
            rows = _fetch_new_messages(conn, self._last_rowid)
            for row in rows:
                if row["has_attachments"]:
                    row["_attachments"] = _fetch_attachments(conn, row["rowid"])
                else:
                    row["_attachments"] = []
            return rows
        finally:
            conn.close()

    def _row_to_message(self, row: dict) -> Optional[Message]:
        text = row.get("text") or _decode_attributed_body(row.get("attr_body"))
        attachments = row.get("_attachments") or []
        if not text and not attachments:
            return None
        handle = row["handle"] or ""
        chat_id = row["chat_id"] or handle
        kind: MessageKind
        if attachments:
            kinds = {a.kind for a in attachments}
            if AttachmentKind.PHOTO in kinds:
                kind = MessageKind.PHOTO
            elif AttachmentKind.VOICE in kinds:
                kind = MessageKind.VOICE
            elif AttachmentKind.VIDEO in kinds:
                kind = MessageKind.VIDEO
            elif AttachmentKind.AUDIO in kinds:
                kind = MessageKind.AUDIO
            else:
                kind = MessageKind.DOCUMENT
        elif text and text.startswith("/"):
            kind = MessageKind.COMMAND
        elif text:
            kind = MessageKind.TEXT
        else:
            kind = MessageKind.OTHER

        return Message(
            id=str(row["rowid"]),
            chat=Chat(id=chat_id, type="direct", title=handle),
            user=User(id=handle, username=handle, display_name=handle),
            kind=kind,
            text=text or None,
            attachments=attachments,
            timestamp=_apple_date_to_unix(row.get("date")),
            raw=row,
        )

    # -- Inbound streams ----------------------------------------------------
    async def messages(self) -> AsyncIterator[Message]:
        while True:
            yield await self._messages.get()

    async def callbacks(self) -> AsyncIterator[CallbackPress]:
        # iMessage has no inline buttons. Yield from an empty queue forever.
        while True:
            yield await self._callbacks.get()

    # -- Outbound: text -----------------------------------------------------
    async def _send_via_applescript(self, chat_id: str, body_lit: str) -> None:
        """Send `body_lit` (already an AppleScript-quoted expression) to chat_id."""
        service = "iMessage" if self._prefer_imessage else "SMS"
        # Try the iMessage service first; if the buddy isn't on iMessage,
        # fall back to SMS (works if the user has a paired iPhone via Continuity).
        primary = f"""
            tell application "Messages"
                try
                    set targetService to 1st service whose service type = {service}
                    set targetBuddy to buddy "{_escape_applescript(chat_id)}" of targetService
                    send {body_lit} to targetBuddy
                on error errMsg
                    -- Fall back to SMS via Continuity
                    set targetService to 1st service whose service type = SMS
                    set targetBuddy to buddy "{_escape_applescript(chat_id)}" of targetService
                    send {body_lit} to targetBuddy
                end try
            end tell
        """
        rc, out, err = await _osascript(primary)
        if rc != 0:
            raise RuntimeError(f"AppleScript send failed: {err or out}")

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: Optional[str] = None,
        keyboard: Optional[Keyboard] = None,
        parse_mode: Optional[str] = None,
        disable_preview: bool = False,
    ) -> SentMessage:
        if keyboard and not keyboard.is_empty():
            # iMessage has no inline buttons. Render as numbered text lines.
            lines = [text, ""]
            for i, row in enumerate(keyboard.rows):
                for j, btn in enumerate(row):
                    if btn.url:
                        lines.append(f"  • {btn.label}: {btn.url}")
                    else:
                        lines.append(f"  • {btn.label}")
            text = "\n".join(lines)
        body_lit = '"' + _escape_applescript(text) + '"'
        await self._send_via_applescript(chat_id, body_lit)
        # iMessage doesn't return a message id we can later edit; synthesize one.
        return SentMessage(chat_id=chat_id, message_id=f"imsg-{int(time.time() * 1000)}")

    async def edit_text(self, sent: SentMessage, text: str, **kwargs) -> None:
        # iMessage doesn't expose message editing to AppleScript for sent messages.
        # As a best-effort, send the new text as a follow-up so the user sees it.
        await self.send_text(sent.chat_id, text)

    async def delete(self, sent: SentMessage) -> None:
        # No supported delete via AppleScript either.
        return

    # -- Outbound: media ----------------------------------------------------
    async def _send_file_via_applescript(self, chat_id: str, path: PathLike) -> SentMessage:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(p)
        # POSIX file path → AppleScript alias literal
        body_lit = f'(POSIX file "{_escape_applescript(str(p))}" as alias)'
        await self._send_via_applescript(chat_id, body_lit)
        return SentMessage(chat_id=chat_id, message_id=f"imsg-{int(time.time() * 1000)}")

    async def send_photo(
        self,
        chat_id: str,
        photo: PathLike,
        *,
        caption: Optional[str] = None,
        keyboard: Optional[Keyboard] = None,
    ) -> SentMessage:
        sent = await self._send_file_via_applescript(chat_id, photo)
        if caption:
            await self.send_text(chat_id, caption)
        return sent

    async def send_video(
        self,
        chat_id: str,
        video: PathLike,
        *,
        caption: Optional[str] = None,
    ) -> SentMessage:
        sent = await self._send_file_via_applescript(chat_id, video)
        if caption:
            await self.send_text(chat_id, caption)
        return sent

    async def send_voice(self, chat_id: str, voice: PathLike) -> SentMessage:
        return await self._send_file_via_applescript(chat_id, voice)

    async def send_document(
        self,
        chat_id: str,
        path: PathLike,
        *,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> SentMessage:
        sent = await self._send_file_via_applescript(chat_id, path)
        if caption:
            await self.send_text(chat_id, caption)
        return sent

    # -- Presence -----------------------------------------------------------
    async def send_typing(self, chat_id: str) -> None:
        # iMessage has no programmatic typing indicator. No-op.
        return

    # -- Auth + downloads ---------------------------------------------------
    async def authorize(self, user: User) -> bool:
        if not self._allowed:
            return True
        return user.id in self._allowed

    async def download_attachment(
        self,
        attachment: Attachment,
        dest: Optional[Path] = None,
    ) -> Path:
        """iMessage attachments are already on disk — just return the local path.

        If `dest` is provided, copy the file there for caller convenience.
        """
        if attachment.local_path is None or not attachment.local_path.exists():
            raise FileNotFoundError(
                f"Attachment file missing: {attachment.local_path}. "
                "Has Messages.app finished syncing it from iCloud?"
            )
        if dest is None:
            return attachment.local_path
        import shutil
        shutil.copyfile(attachment.local_path, dest)
        return dest

    def native(self):
        return None
