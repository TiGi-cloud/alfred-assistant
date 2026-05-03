"""
Telegram adapter — wraps python-telegram-bot's Application in the
kernel.ChatAdapter interface.

The adapter runs PTB in long-polling mode, drops every incoming Update onto
two internal queues (messages, callbacks), and exposes them as async iterators.
Outgoing operations are translated into PTB Bot API calls.

Usage:

    from adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter.from_token(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        allowed_users=["alice", "bob"],
        allowed_user_ids=[123456789],
    )
    await adapter.start()
    async for msg in adapter.messages():
        ...
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from kernel import (
    Attachment,
    AttachmentKind,
    CallbackPress,
    Chat,
    ChatAdapter,
    Keyboard,
    Location,
    Message,
    MessageKind,
    SentMessage,
    User,
)
from kernel.adapter import PathLike

logger = logging.getLogger("alfred.adapters.telegram")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_PARSE_MODE_MAP = {
    None: None,
    "html": ParseMode.HTML,
    "markdown": ParseMode.MARKDOWN_V2,
    "markdown_v1": ParseMode.MARKDOWN,
}


def _to_keyboard(kb: Optional[Keyboard]) -> Optional[InlineKeyboardMarkup]:
    if kb is None or kb.is_empty():
        return None
    rows = []
    for row in kb.rows:
        out_row = []
        for b in row:
            if b.data is not None:
                out_row.append(InlineKeyboardButton(b.label, callback_data=b.data))
            elif b.url is not None:
                out_row.append(InlineKeyboardButton(b.label, url=b.url))
            elif b.webapp_url is not None:
                out_row.append(
                    InlineKeyboardButton(b.label, web_app=WebAppInfo(url=b.webapp_url))
                )
        if out_row:
            rows.append(out_row)
    return InlineKeyboardMarkup(rows) if rows else None


def _user_from_tg(tg_user) -> User:
    return User(
        id=str(tg_user.id),
        username=tg_user.username,
        display_name=tg_user.full_name or tg_user.first_name,
        is_bot=bool(getattr(tg_user, "is_bot", False)),
    )


def _chat_from_tg(tg_chat) -> Chat:
    type_map = {
        "private": "direct",
        "group": "group",
        "supergroup": "group",
        "channel": "channel",
    }
    return Chat(
        id=str(tg_chat.id),
        type=type_map.get(tg_chat.type, "direct"),
        title=tg_chat.title,
    )


def _kind_for_message(tg_message) -> MessageKind:
    if tg_message.photo:
        return MessageKind.PHOTO
    if tg_message.voice:
        return MessageKind.VOICE
    if tg_message.audio:
        return MessageKind.AUDIO
    if tg_message.video or tg_message.video_note:
        return MessageKind.VIDEO
    if tg_message.document:
        return MessageKind.DOCUMENT
    if tg_message.location or tg_message.venue:
        return MessageKind.LOCATION
    if tg_message.text and tg_message.text.startswith("/"):
        return MessageKind.COMMAND
    if tg_message.text:
        return MessageKind.TEXT
    return MessageKind.OTHER


def _attachments_from_tg(tg_message) -> list[Attachment]:
    out: list[Attachment] = []
    if tg_message.photo:
        # Telegram sends multiple sizes; the last is the largest
        biggest = tg_message.photo[-1]
        out.append(
            Attachment(
                kind=AttachmentKind.PHOTO,
                remote_id=biggest.file_id,
                mime_type="image/jpeg",
                size_bytes=biggest.file_size,
                width=biggest.width,
                height=biggest.height,
                caption=tg_message.caption,
            )
        )
    if tg_message.voice:
        v = tg_message.voice
        out.append(
            Attachment(
                kind=AttachmentKind.VOICE,
                remote_id=v.file_id,
                mime_type=v.mime_type,
                size_bytes=v.file_size,
                duration_secs=v.duration,
            )
        )
    if tg_message.audio:
        a = tg_message.audio
        out.append(
            Attachment(
                kind=AttachmentKind.AUDIO,
                remote_id=a.file_id,
                mime_type=a.mime_type,
                size_bytes=a.file_size,
                duration_secs=a.duration,
                name=a.file_name,
            )
        )
    if tg_message.video:
        v = tg_message.video
        out.append(
            Attachment(
                kind=AttachmentKind.VIDEO,
                remote_id=v.file_id,
                mime_type=v.mime_type,
                size_bytes=v.file_size,
                duration_secs=v.duration,
                width=v.width,
                height=v.height,
                caption=tg_message.caption,
            )
        )
    if tg_message.document:
        d = tg_message.document
        out.append(
            Attachment(
                kind=AttachmentKind.DOCUMENT,
                remote_id=d.file_id,
                mime_type=d.mime_type,
                size_bytes=d.file_size,
                name=d.file_name,
                caption=tg_message.caption,
            )
        )
    return out


def _location_from_tg(tg_message) -> Optional[Location]:
    if tg_message.location:
        loc = tg_message.location
        return Location(
            latitude=loc.latitude,
            longitude=loc.longitude,
            horizontal_accuracy=loc.horizontal_accuracy,
            label=tg_message.venue.title if tg_message.venue else None,
        )
    return None


def _to_message(tg_message) -> Optional[Message]:
    if tg_message is None or tg_message.from_user is None:
        return None
    text = tg_message.text or tg_message.caption
    return Message(
        id=str(tg_message.message_id),
        chat=_chat_from_tg(tg_message.chat),
        user=_user_from_tg(tg_message.from_user),
        kind=_kind_for_message(tg_message),
        text=text,
        attachments=_attachments_from_tg(tg_message),
        location=_location_from_tg(tg_message),
        reply_to_id=(
            str(tg_message.reply_to_message.message_id)
            if tg_message.reply_to_message
            else None
        ),
        reply_to_text=(
            tg_message.reply_to_message.text or tg_message.reply_to_message.caption
            if tg_message.reply_to_message
            else None
        ),
        timestamp=tg_message.date.timestamp() if tg_message.date else 0.0,
        raw=tg_message,
    )


def _to_callback(query) -> Optional[CallbackPress]:
    if query is None or query.from_user is None:
        return None
    return CallbackPress(
        id=str(query.id),
        chat=_chat_from_tg(query.message.chat) if query.message else Chat(id=""),
        user=_user_from_tg(query.from_user),
        data=query.data or "",
        message_id=str(query.message.message_id) if query.message else None,
        timestamp=query.message.date.timestamp() if query.message and query.message.date else 0.0,
        raw=query,
    )


# ---------------------------------------------------------------------------
# TelegramAdapter
# ---------------------------------------------------------------------------
class TelegramAdapter(ChatAdapter):
    """python-telegram-bot Application wrapped as a kernel.ChatAdapter."""

    name = "telegram"

    def __init__(
        self,
        app: Application,
        *,
        allowed_users: Iterable[str] = (),
        allowed_user_ids: Iterable[int] = (),
        message_queue_size: int = 0,
    ) -> None:
        self._app = app
        self._allowed_users = {u.lower() for u in allowed_users}
        self._allowed_user_ids = {int(uid) for uid in allowed_user_ids}
        self._messages: asyncio.Queue[Message] = asyncio.Queue(maxsize=message_queue_size)
        self._callbacks: asyncio.Queue[CallbackPress] = asyncio.Queue(maxsize=message_queue_size)
        self._started = False

    @classmethod
    def from_token(
        cls,
        token: str,
        *,
        allowed_users: Iterable[str] = (),
        allowed_user_ids: Iterable[int] = (),
    ) -> "TelegramAdapter":
        app = Application.builder().token(token).build()
        return cls(
            app,
            allowed_users=allowed_users,
            allowed_user_ids=allowed_user_ids,
        )

    # -- Lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        # Pipe every incoming update through to the kernel queues.
        self._app.add_handler(MessageHandler(filters.ALL, self._handle_message))
        self._app.add_handler(CallbackQueryHandler(self._handle_callback))
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        self._started = True
        logger.info("Telegram adapter started")

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            if self._app.updater:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        finally:
            self._started = False
            logger.info("Telegram adapter stopped")

    # -- Inbound handlers ---------------------------------------------------
    async def _handle_message(self, update: Update, _context) -> None:
        msg = _to_message(update.effective_message)
        if msg is not None:
            await self._messages.put(msg)

    async def _handle_callback(self, update: Update, _context) -> None:
        cb = _to_callback(update.callback_query)
        if cb is not None:
            await self._callbacks.put(cb)

    async def messages(self) -> AsyncIterator[Message]:
        while True:
            yield await self._messages.get()

    async def callbacks(self) -> AsyncIterator[CallbackPress]:
        while True:
            yield await self._callbacks.get()

    # -- Outbound: text -----------------------------------------------------
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
        msg = await self._app.bot.send_message(
            chat_id=int(chat_id),
            text=text,
            reply_to_message_id=int(reply_to) if reply_to else None,
            reply_markup=_to_keyboard(keyboard),
            parse_mode=_PARSE_MODE_MAP.get(parse_mode, _PARSE_MODE_MAP[None]),
            disable_web_page_preview=disable_preview,
        )
        return SentMessage(chat_id=str(msg.chat_id), message_id=str(msg.message_id))

    async def edit_text(
        self,
        sent: SentMessage,
        text: str,
        *,
        keyboard: Optional[Keyboard] = None,
        parse_mode: Optional[str] = None,
    ) -> None:
        await self._app.bot.edit_message_text(
            chat_id=int(sent.chat_id),
            message_id=int(sent.message_id),
            text=text,
            reply_markup=_to_keyboard(keyboard),
            parse_mode=_PARSE_MODE_MAP.get(parse_mode, _PARSE_MODE_MAP[None]),
        )

    async def delete(self, sent: SentMessage) -> None:
        await self._app.bot.delete_message(
            chat_id=int(sent.chat_id),
            message_id=int(sent.message_id),
        )

    # -- Outbound: media ----------------------------------------------------
    async def send_photo(
        self,
        chat_id: str,
        photo: PathLike,
        *,
        caption: Optional[str] = None,
        keyboard: Optional[Keyboard] = None,
    ) -> SentMessage:
        path = Path(photo)
        with path.open("rb") as f:
            msg = await self._app.bot.send_photo(
                chat_id=int(chat_id),
                photo=f,
                caption=caption,
                reply_markup=_to_keyboard(keyboard),
            )
        return SentMessage(chat_id=str(msg.chat_id), message_id=str(msg.message_id))

    async def send_video(
        self,
        chat_id: str,
        video: PathLike,
        *,
        caption: Optional[str] = None,
    ) -> SentMessage:
        path = Path(video)
        with path.open("rb") as f:
            msg = await self._app.bot.send_video(
                chat_id=int(chat_id),
                video=f,
                caption=caption,
            )
        return SentMessage(chat_id=str(msg.chat_id), message_id=str(msg.message_id))

    async def send_voice(self, chat_id: str, voice: PathLike) -> SentMessage:
        path = Path(voice)
        with path.open("rb") as f:
            msg = await self._app.bot.send_voice(
                chat_id=int(chat_id),
                voice=f,
            )
        return SentMessage(chat_id=str(msg.chat_id), message_id=str(msg.message_id))

    async def send_document(
        self,
        chat_id: str,
        path: PathLike,
        *,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> SentMessage:
        p = Path(path)
        with p.open("rb") as f:
            msg = await self._app.bot.send_document(
                chat_id=int(chat_id),
                document=f,
                filename=filename or p.name,
                caption=caption,
            )
        return SentMessage(chat_id=str(msg.chat_id), message_id=str(msg.message_id))

    # -- Outbound: presence -------------------------------------------------
    async def send_typing(self, chat_id: str) -> None:
        await self._app.bot.send_chat_action(
            chat_id=int(chat_id),
            action=ChatAction.TYPING,
        )

    # -- Auth + downloads ---------------------------------------------------
    async def authorize(self, user: User) -> bool:
        # If neither allowlist is configured, everyone is allowed (the bot logs
        # a loud warning at startup; preserved here for backwards compat).
        if not self._allowed_users and not self._allowed_user_ids:
            return True
        if user.username and user.username.lower() in self._allowed_users:
            return True
        try:
            if int(user.id) in self._allowed_user_ids:
                return True
        except (TypeError, ValueError):
            pass
        return False

    async def download_attachment(
        self,
        attachment: Attachment,
        dest: Optional[Path] = None,
    ) -> Path:
        if not attachment.remote_id:
            raise ValueError("Attachment has no remote_id; nothing to download")
        tg_file = await self._app.bot.get_file(attachment.remote_id)
        if dest is None:
            suffix = ""
            if attachment.name:
                suffix = "." + attachment.name.rsplit(".", 1)[-1]
            elif attachment.mime_type and "/" in attachment.mime_type:
                suffix = "." + attachment.mime_type.split("/", 1)[1]
            fd, tmp_path = tempfile.mkstemp(prefix="alfred-att-", suffix=suffix)
            os.close(fd)
            dest = Path(tmp_path)
        await tg_file.download_to_drive(custom_path=str(dest))
        attachment.local_path = dest
        return dest

    def native(self) -> Application:
        """Escape hatch: return the underlying PTB Application."""
        return self._app
