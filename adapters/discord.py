"""
Discord adapter — wraps `discord.py` in the kernel.ChatAdapter interface.

Listens for direct messages and channel messages addressed to the bot.
Translates them into kernel.Message and pushes onto the message queue.
Inline buttons go through Discord components (discord.ui.View) and
emit kernel.CallbackPress on click.

Setup:

  1. Visit https://discord.com/developers/applications and create an app
  2. Add a Bot, copy its token
  3. Enable the *MESSAGE CONTENT INTENT* under Privileged Gateway Intents
  4. Generate an OAuth invite URL with bot + applications.commands scopes
     and the permissions you need (send messages, attach files, ...)
  5. Set DISCORD_BOT_TOKEN in .env

Authorisation: pass `allowed_user_ids` to constrain who can use the bot.
Discord IDs are 18-digit snowflakes; you can copy them from the Discord
client by enabling Developer Mode and right-clicking a user.

Optional dependency: `discord.py` (`pip install discord.py>=2.4`). The
import is lazy so the rest of Alfred works fine without it installed.
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
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

logger = logging.getLogger("alfred.adapters.discord")


# ---------------------------------------------------------------------------
# Lazy import of discord.py so missing dep doesn't break the kernel
# ---------------------------------------------------------------------------
def _import_discord():
    try:
        import discord  # type: ignore[import]
        return discord
    except ImportError as e:
        raise RuntimeError(
            "Discord adapter requires `discord.py`. Install with: "
            "pip install 'discord.py>=2.4'"
        ) from e


# ---------------------------------------------------------------------------
# Type translation helpers
# ---------------------------------------------------------------------------
_ATT_KIND_BY_MIME = {
    "image/": AttachmentKind.PHOTO,
    "audio/": AttachmentKind.VOICE,
    "video/": AttachmentKind.VIDEO,
}


def _attachment_kind(mime: Optional[str]) -> AttachmentKind:
    if not mime:
        return AttachmentKind.DOCUMENT
    for prefix, kind in _ATT_KIND_BY_MIME.items():
        if mime.startswith(prefix):
            return kind
    return AttachmentKind.DOCUMENT


def _message_kind(text: Optional[str], attachments: list[Attachment]) -> MessageKind:
    if attachments:
        kinds = {a.kind for a in attachments}
        if AttachmentKind.PHOTO in kinds:
            return MessageKind.PHOTO
        if AttachmentKind.VOICE in kinds:
            return MessageKind.VOICE
        if AttachmentKind.VIDEO in kinds:
            return MessageKind.VIDEO
        if AttachmentKind.AUDIO in kinds:
            return MessageKind.AUDIO
        return MessageKind.DOCUMENT
    if text and text.startswith("/"):
        return MessageKind.COMMAND
    if text:
        return MessageKind.TEXT
    return MessageKind.OTHER


def _to_attachments(d_attachments) -> list[Attachment]:
    out: list[Attachment] = []
    for a in d_attachments:
        kind = _attachment_kind(a.content_type)
        out.append(
            Attachment(
                kind=kind,
                remote_id=a.url,  # Discord attachments are HTTP URLs
                mime_type=a.content_type,
                size_bytes=a.size,
                name=a.filename,
                width=a.width,
                height=a.height,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Custom View that proxies button clicks to a queue
# ---------------------------------------------------------------------------
def _build_view(discord_module, keyboard: Optional[Keyboard], on_click):
    """Build a discord.ui.View for the given Keyboard.

    `on_click(interaction)` is awaited every time a non-link button fires.
    Returns None when the keyboard is empty.
    """
    if keyboard is None or keyboard.is_empty():
        return None

    class ProxyView(discord_module.ui.View):
        pass

    view = ProxyView(timeout=None)
    for row_idx, row in enumerate(keyboard.rows):
        if row_idx >= 5:  # Discord caps at 5 rows
            break
        for col_idx, btn in enumerate(row):
            if col_idx >= 5:
                break
            if btn.url:
                view.add_item(
                    discord_module.ui.Button(
                        label=btn.label[:80],
                        url=btn.url,
                        style=discord_module.ButtonStyle.link,
                        row=row_idx,
                    )
                )
            else:
                comp = discord_module.ui.Button(
                    label=btn.label[:80],
                    custom_id=(btn.data or "")[:100],
                    style=discord_module.ButtonStyle.secondary,
                    row=row_idx,
                )

                async def _cb(interaction, _h=on_click):
                    await _h(interaction)

                comp.callback = _cb
                view.add_item(comp)
    return view


# ---------------------------------------------------------------------------
# DiscordAdapter
# ---------------------------------------------------------------------------
class DiscordAdapter(ChatAdapter):
    """discord.py Client wrapped as a kernel.ChatAdapter."""

    name = "discord"

    def __init__(
        self,
        token: str,
        *,
        allowed_user_ids: Iterable[int] = (),
    ) -> None:
        self._token = token
        self._allowed_ids = {int(x) for x in allowed_user_ids}
        self._messages: asyncio.Queue[Message] = asyncio.Queue()
        self._callbacks: asyncio.Queue[CallbackPress] = asyncio.Queue()
        self._client = None
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._discord = None

    # -- Lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if self._task:
            return
        self._discord = _import_discord()
        intents = self._discord.Intents.default()
        intents.message_content = True
        self._client = self._discord.Client(intents=intents)

        @self._client.event
        async def on_ready():  # noqa: N802
            logger.info("Discord adapter ready as %s", self._client.user)
            self._ready.set()

        @self._client.event
        async def on_message(message):  # noqa: N802
            if message.author.bot or message.author == self._client.user:
                return
            await self._messages.put(self._to_message(message))

        # Drive the client until cancelled
        self._task = asyncio.create_task(self._client.start(self._token))
        # Wait briefly for connection to come up so that send() works
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("Discord client did not become ready within 15s; carrying on")

    async def stop(self) -> None:
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("Discord adapter stopped")

    # -- Inbound conversion -------------------------------------------------
    def _to_message(self, m) -> Message:
        attachments = _to_attachments(m.attachments)
        return Message(
            id=str(m.id),
            chat=Chat(
                id=str(m.channel.id),
                type="direct" if m.guild is None else "group",
                title=getattr(m.channel, "name", None),
            ),
            user=User(
                id=str(m.author.id),
                username=getattr(m.author, "name", None),
                display_name=getattr(m.author, "display_name", None),
                is_bot=m.author.bot,
            ),
            kind=_message_kind(m.content, attachments),
            text=m.content,
            attachments=attachments,
            reply_to_id=str(m.reference.message_id) if m.reference else None,
            timestamp=m.created_at.timestamp() if m.created_at else 0.0,
            raw=m,
        )

    async def _on_button(self, interaction) -> None:
        try:
            data = (interaction.data or {}).get("custom_id", "")
            cb = CallbackPress(
                id=str(interaction.id),
                chat=Chat(
                    id=str(interaction.channel_id) if interaction.channel_id else "",
                    type="direct" if interaction.guild is None else "group",
                ),
                user=User(
                    id=str(interaction.user.id),
                    username=interaction.user.name,
                    display_name=interaction.user.display_name,
                ),
                data=data,
                message_id=str(interaction.message.id) if interaction.message else None,
                raw=interaction,
            )
            await self._callbacks.put(cb)
            # Acknowledge silently; the handler will reply via send_text
            try:
                await interaction.response.defer()
            except Exception:
                pass
        except Exception:
            logger.exception("Discord interaction handler failed")

    async def messages(self) -> AsyncIterator[Message]:
        while True:
            yield await self._messages.get()

    async def callbacks(self) -> AsyncIterator[CallbackPress]:
        while True:
            yield await self._callbacks.get()

    # -- Outbound: text -----------------------------------------------------
    async def _channel(self, chat_id: str):
        if self._client is None:
            raise RuntimeError("Discord adapter not started")
        ch = self._client.get_channel(int(chat_id))
        if ch is None:
            ch = await self._client.fetch_channel(int(chat_id))
        return ch

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
        ch = await self._channel(chat_id)
        view = _build_view(self._discord, keyboard, self._on_button)
        kwargs = {"content": text, "view": view, "suppress_embeds": disable_preview}
        if reply_to:
            try:
                ref = await ch.fetch_message(int(reply_to))
                kwargs["reference"] = ref
            except Exception:
                pass
        sent = await ch.send(**{k: v for k, v in kwargs.items() if v is not None})
        return SentMessage(chat_id=str(sent.channel.id), message_id=str(sent.id))

    async def edit_text(
        self,
        sent: SentMessage,
        text: str,
        *,
        keyboard: Optional[Keyboard] = None,
        parse_mode: Optional[str] = None,
    ) -> None:
        ch = await self._channel(sent.chat_id)
        msg = await ch.fetch_message(int(sent.message_id))
        view = _build_view(self._discord, keyboard, self._on_button)
        await msg.edit(content=text, view=view)

    async def delete(self, sent: SentMessage) -> None:
        ch = await self._channel(sent.chat_id)
        msg = await ch.fetch_message(int(sent.message_id))
        await msg.delete()

    # -- Outbound: media ----------------------------------------------------
    async def _send_file(
        self,
        chat_id: str,
        path: PathLike,
        *,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> SentMessage:
        ch = await self._channel(chat_id)
        p = Path(path)
        f = self._discord.File(str(p), filename=filename or p.name)
        sent = await ch.send(content=caption or None, file=f)
        return SentMessage(chat_id=str(sent.channel.id), message_id=str(sent.id))

    async def send_photo(
        self,
        chat_id: str,
        photo: PathLike,
        *,
        caption: Optional[str] = None,
        keyboard: Optional[Keyboard] = None,
    ) -> SentMessage:
        # Discord doesn't distinguish between photo/document in the API; both
        # are file uploads that the client renders inline by mime type.
        return await self._send_file(chat_id, photo, caption=caption)

    async def send_video(
        self,
        chat_id: str,
        video: PathLike,
        *,
        caption: Optional[str] = None,
    ) -> SentMessage:
        return await self._send_file(chat_id, video, caption=caption)

    async def send_voice(self, chat_id: str, voice: PathLike) -> SentMessage:
        return await self._send_file(chat_id, voice)

    async def send_document(
        self,
        chat_id: str,
        path: PathLike,
        *,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> SentMessage:
        return await self._send_file(chat_id, path, caption=caption, filename=filename)

    # -- Presence -----------------------------------------------------------
    async def send_typing(self, chat_id: str) -> None:
        ch = await self._channel(chat_id)
        try:
            await ch.typing().__aenter__()
        except Exception:
            # Typing is purely cosmetic; ignore failures
            pass

    # -- Auth + downloads ---------------------------------------------------
    async def authorize(self, user: User) -> bool:
        if not self._allowed_ids:
            return True
        try:
            return int(user.id) in self._allowed_ids
        except (TypeError, ValueError):
            return False

    async def download_attachment(
        self,
        attachment: Attachment,
        dest: Optional[Path] = None,
    ) -> Path:
        url = attachment.remote_id
        if not url:
            raise ValueError("Attachment has no URL to download from")
        if dest is None:
            suffix = Path(attachment.name).suffix if attachment.name else ""
            fd, tmp = tempfile.mkstemp(prefix="alfred-discord-", suffix=suffix)
            import os
            os.close(fd)
            dest = Path(tmp)
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url) as r:
                r.raise_for_status()
                dest.write_bytes(await r.read())
        attachment.local_path = dest
        return dest

    def native(self):
        return self._client
