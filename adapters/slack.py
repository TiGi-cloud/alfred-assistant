"""
Slack adapter — wraps `slack-bolt` (Socket Mode) in the kernel.ChatAdapter
interface.

Setup (one-time, ~5 minutes):

  1. https://api.slack.com/apps → Create New App → From scratch
  2. Under "Socket Mode" → enable, generate an App-level token
     (xapp-...) with the `connections:write` scope.
  3. Under "OAuth & Permissions" → add Bot Token Scopes:
       - chat:write
       - im:history
       - im:read
       - files:write
       - app_mentions:read
       - channels:history (if you want bot to see channel messages)
     Then "Install to Workspace" and copy the Bot User OAuth Token (xoxb-...)
  4. Under "Event Subscriptions" → enable, subscribe to bot events:
       - message.im
       - app_mention
       - (optional) message.channels for channels the bot is invited to
  5. Add to .env:
       SLACK_BOT_TOKEN=xoxb-...
       SLACK_APP_TOKEN=xapp-...
       SLACK_ALLOWED_USER_IDS=U01ABCDEF,U02XYZ...

Allowlist: pass `allowed_user_ids` (Slack member IDs starting with U or W).
Empty list = anyone in the workspace can talk to the bot.

Optional dependency: `slack-bolt` (`pip install slack-bolt[async]`). The
import is lazy so the rest of Alfred works without it installed.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

import aiohttp

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

logger = logging.getLogger("alfred.adapters.slack")


# ---------------------------------------------------------------------------
# Lazy imports
# ---------------------------------------------------------------------------
def _import_bolt():
    try:
        from slack_bolt.async_app import AsyncApp  # type: ignore[import]
        from slack_bolt.adapter.socket_mode.async_handler import (  # type: ignore[import]
            AsyncSocketModeHandler,
        )
        return AsyncApp, AsyncSocketModeHandler
    except ImportError as e:
        raise RuntimeError(
            "Slack adapter requires `slack-bolt`. Install with: "
            "pip install 'slack-bolt>=1.18'"
        ) from e


# ---------------------------------------------------------------------------
# Type translation helpers
# ---------------------------------------------------------------------------
_KIND_BY_MIMETYPE_PREFIX = {
    "image/": AttachmentKind.PHOTO,
    "audio/": AttachmentKind.AUDIO,
    "video/": AttachmentKind.VIDEO,
}


def _attachment_kind(mime: Optional[str]) -> AttachmentKind:
    if not mime:
        return AttachmentKind.DOCUMENT
    for prefix, kind in _KIND_BY_MIMETYPE_PREFIX.items():
        if mime.startswith(prefix):
            return kind
    return AttachmentKind.DOCUMENT


def _slack_files_to_attachments(files: list[dict]) -> list[Attachment]:
    out: list[Attachment] = []
    for f in files or []:
        kind = _attachment_kind(f.get("mimetype"))
        out.append(
            Attachment(
                kind=kind,
                # Slack files need an OAuth token to download — keep both
                # the public id and the private URL on the Attachment so the
                # adapter's download_attachment can authenticate properly.
                remote_id=f.get("id"),
                mime_type=f.get("mimetype"),
                size_bytes=f.get("size"),
                name=f.get("name"),
                width=f.get("original_w"),
                height=f.get("original_h"),
            )
        )
    return out


def _kb_to_blocks(keyboard: Optional[Keyboard], text: str) -> Optional[list[dict]]:
    """Render a Keyboard as Slack Block Kit blocks (text + actions)."""
    if keyboard is None or keyboard.is_empty():
        return None
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    for row_idx, row in enumerate(keyboard.rows):
        elements = []
        for btn_idx, btn in enumerate(row):
            elt = {
                "type": "button",
                "text": {"type": "plain_text", "text": btn.label[:75], "emoji": True},
                "action_id": (btn.data or btn.url or f"row-{row_idx}-{btn_idx}")[:255],
            }
            if btn.url:
                elt["url"] = btn.url
            blocks.append({"type": "actions", "elements": []})  # placeholder
            blocks[-1]["elements"].append(elt) if False else None
            elements.append(elt)
        if elements:
            blocks.append({
                "type": "actions",
                "block_id": f"row-{row_idx}-{uuid.uuid4().hex[:6]}",
                "elements": elements,
            })
    # Remove duplicate empty actions blocks the loop may have added.
    return [b for b in blocks if b["type"] != "actions" or b.get("elements")]


# ---------------------------------------------------------------------------
# SlackAdapter
# ---------------------------------------------------------------------------
class SlackAdapter(ChatAdapter):
    """Slack Bolt async app driven over Socket Mode."""

    name = "slack"

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        *,
        allowed_user_ids: Iterable[str] = (),
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._allowed_ids = {str(x) for x in allowed_user_ids if str(x).strip()}
        self._messages: asyncio.Queue[Message] = asyncio.Queue()
        self._callbacks: asyncio.Queue[CallbackPress] = asyncio.Queue()
        self._app = None
        self._handler = None
        self._handler_task: Optional[asyncio.Task] = None

    # -- Lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if self._handler_task:
            return
        AsyncApp, AsyncSocketModeHandler = _import_bolt()
        self._app = AsyncApp(token=self._bot_token)

        @self._app.event("message")
        async def on_message(event, _client):  # noqa: ARG001
            # Skip bot/system messages and message_changed/deleted events
            if event.get("subtype") in ("bot_message", "message_changed", "message_deleted"):
                return
            if event.get("bot_id"):
                return
            await self._messages.put(self._to_message(event))

        @self._app.event("app_mention")
        async def on_mention(event, _client):  # noqa: ARG001
            await self._messages.put(self._to_message(event))

        @self._app.action({"type": "block_actions"})
        async def on_action(ack, body, _client):  # noqa: ARG001
            await ack()
            actions = body.get("actions", [])
            if not actions:
                return
            action = actions[0]
            cb = CallbackPress(
                id=str(body.get("trigger_id", uuid.uuid4().hex)),
                chat=Chat(
                    id=body.get("channel", {}).get("id", ""),
                    type="direct" if body.get("channel", {}).get("name") == "directmessage" else "group",
                ),
                user=User(
                    id=body.get("user", {}).get("id", ""),
                    username=body.get("user", {}).get("username"),
                    display_name=body.get("user", {}).get("name"),
                ),
                data=action.get("action_id", ""),
                message_id=body.get("message", {}).get("ts"),
                raw=body,
            )
            await self._callbacks.put(cb)

        # NB: action_id can also be matched explicitly per-button if needed.

        self._handler = AsyncSocketModeHandler(self._app, self._app_token)
        self._handler_task = asyncio.create_task(self._handler.start_async())
        # Give Socket Mode a beat to connect before the first send_text
        await asyncio.sleep(0.5)
        logger.info("Slack adapter started (Socket Mode)")

    async def stop(self) -> None:
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception:
                pass
        if self._handler_task:
            self._handler_task.cancel()
            try:
                await self._handler_task
            except (asyncio.CancelledError, Exception):
                pass
            self._handler_task = None
        logger.info("Slack adapter stopped")

    # -- Inbound ------------------------------------------------------------
    def _to_message(self, event: dict) -> Message:
        text = event.get("text") or ""
        attachments = _slack_files_to_attachments(event.get("files") or [])
        chan = event.get("channel", "")
        chan_type = event.get("channel_type", "")
        return Message(
            id=str(event.get("ts") or event.get("event_ts") or time.time()),
            chat=Chat(
                id=chan,
                type="direct" if chan_type == "im" else "group",
            ),
            user=User(
                id=event.get("user", ""),
                username=None,
                display_name=event.get("username"),
            ),
            kind=(
                MessageKind.COMMAND if text.startswith("/")
                else MessageKind.PHOTO if any(a.kind == AttachmentKind.PHOTO for a in attachments)
                else MessageKind.VOICE if any(a.kind == AttachmentKind.VOICE for a in attachments)
                else MessageKind.AUDIO if any(a.kind == AttachmentKind.AUDIO for a in attachments)
                else MessageKind.VIDEO if any(a.kind == AttachmentKind.VIDEO for a in attachments)
                else MessageKind.DOCUMENT if attachments
                else MessageKind.TEXT
            ),
            text=text or None,
            attachments=attachments,
            reply_to_id=event.get("thread_ts") if event.get("thread_ts") != event.get("ts") else None,
            timestamp=float(event.get("ts") or 0),
            raw=event,
        )

    async def messages(self) -> AsyncIterator[Message]:
        while True:
            yield await self._messages.get()

    async def callbacks(self) -> AsyncIterator[CallbackPress]:
        while True:
            yield await self._callbacks.get()

    # -- Outbound: text -----------------------------------------------------
    @property
    def _client(self):
        if self._app is None:
            raise RuntimeError("Slack adapter not started")
        return self._app.client

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
        blocks = _kb_to_blocks(keyboard, text)
        kwargs = {
            "channel": chat_id,
            "text": text,
            "unfurl_links": not disable_preview,
            "unfurl_media": not disable_preview,
        }
        if blocks:
            kwargs["blocks"] = blocks
        if reply_to:
            kwargs["thread_ts"] = reply_to
        resp = await self._client.chat_postMessage(**kwargs)
        return SentMessage(chat_id=resp["channel"], message_id=resp["ts"])

    async def edit_text(
        self,
        sent: SentMessage,
        text: str,
        *,
        keyboard: Optional[Keyboard] = None,
        parse_mode: Optional[str] = None,
    ) -> None:
        kwargs = {"channel": sent.chat_id, "ts": sent.message_id, "text": text}
        blocks = _kb_to_blocks(keyboard, text)
        if blocks:
            kwargs["blocks"] = blocks
        await self._client.chat_update(**kwargs)

    async def delete(self, sent: SentMessage) -> None:
        await self._client.chat_delete(channel=sent.chat_id, ts=sent.message_id)

    # -- Outbound: media ----------------------------------------------------
    async def _upload_file(
        self,
        chat_id: str,
        path: PathLike,
        *,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> SentMessage:
        p = Path(path)
        resp = await self._client.files_upload_v2(
            channel=chat_id,
            file=str(p),
            filename=filename or p.name,
            initial_comment=caption,
        )
        # files_upload_v2 returns a list; grab the first entry's channel/ts
        files = resp.get("files") or []
        ts = (files[0].get("shares", {}).get("public", {}).values() or [None])
        return SentMessage(chat_id=chat_id, message_id=str(time.time()))

    async def send_photo(
        self,
        chat_id: str,
        photo: PathLike,
        *,
        caption: Optional[str] = None,
        keyboard: Optional[Keyboard] = None,
    ) -> SentMessage:
        return await self._upload_file(chat_id, photo, caption=caption)

    async def send_video(
        self,
        chat_id: str,
        video: PathLike,
        *,
        caption: Optional[str] = None,
    ) -> SentMessage:
        return await self._upload_file(chat_id, video, caption=caption)

    async def send_voice(self, chat_id: str, voice: PathLike) -> SentMessage:
        return await self._upload_file(chat_id, voice)

    async def send_document(
        self,
        chat_id: str,
        path: PathLike,
        *,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> SentMessage:
        return await self._upload_file(chat_id, path, caption=caption, filename=filename)

    # -- Presence -----------------------------------------------------------
    async def send_typing(self, chat_id: str) -> None:
        # Slack doesn't have a public "bot is typing" API; fall back to a
        # short ephemeral status message could be added later.
        return

    # -- Auth + downloads ---------------------------------------------------
    async def authorize(self, user: User) -> bool:
        if not self._allowed_ids:
            return True
        return user.id in self._allowed_ids

    async def download_attachment(
        self,
        attachment: Attachment,
        dest: Optional[Path] = None,
    ) -> Path:
        # Slack returns a file_id (`F12345`); fetch metadata for url_private.
        if not attachment.remote_id:
            raise ValueError("Attachment has no remote_id")
        info = await self._client.files_info(file=attachment.remote_id)
        url = info["file"]["url_private"]
        if dest is None:
            suffix = Path(attachment.name).suffix if attachment.name else ""
            fd, tmp = tempfile.mkstemp(prefix="alfred-slack-", suffix=suffix)
            import os
            os.close(fd)
            dest = Path(tmp)
        headers = {"Authorization": f"Bearer {self._bot_token}"}
        async with aiohttp.ClientSession(headers=headers) as sess:
            async with sess.get(url) as r:
                r.raise_for_status()
                dest.write_bytes(await r.read())
        attachment.local_path = dest
        return dest

    def native(self):
        return self._app
