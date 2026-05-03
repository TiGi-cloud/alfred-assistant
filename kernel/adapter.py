"""
Abstract chat adapter interface.

Every chat platform integration (Telegram, Discord, Slack, web, ...) subclasses
ChatAdapter. The adapter is responsible for:

  1. Listening for incoming events on its platform.
  2. Translating native objects to `kernel.Message` / `kernel.CallbackPress`.
  3. Translating outgoing send_*/edit_* calls back to native API calls.
  4. Authorizing users according to its own scheme (allowlist, OAuth, ...).

Adapters expose two async iterators (`messages()`, `callbacks()`) that the bot's
event loop reads from. Outgoing operations are async methods that return
`SentMessage` references the caller can later edit or delete.

Note: this is the abstraction layer. The current Telegram bot still runs through
the legacy code at the repo root and has not been ported to this interface yet.
That migration happens incrementally during Phase 2 of the refactor.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union

from .buttons import Keyboard
from .messages import CallbackPress, Message, User


PathLike = Union[str, Path]
ParseMode = Optional[str]   # None | "html" | "markdown" — adapter maps this to its own constants


# ---------------------------------------------------------------------------
# Sent-message handle
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SentMessage:
    """Reference to a message the adapter has sent.

    Pass this back to `edit_text`, `delete`, etc. The string ids are
    platform-specific but stable for the lifetime of the message.
    """
    chat_id: str
    message_id: str


# ---------------------------------------------------------------------------
# ChatAdapter
# ---------------------------------------------------------------------------
class ChatAdapter(ABC):
    """Base class for every chat platform integration."""

    #: Short identifier used in logs and config — e.g. "telegram", "web".
    name: str = "abstract"

    # -- Lifecycle ----------------------------------------------------------
    @abstractmethod
    async def start(self) -> None:
        """Open connections, start polling/WS, etc. Must be idempotent."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop cleanly: close connections, cancel tasks, flush queues."""

    # -- Inbound streams ----------------------------------------------------
    @abstractmethod
    def messages(self) -> AsyncIterator[Message]:
        """Async iterator yielding all incoming messages (text, media, location)."""

    @abstractmethod
    def callbacks(self) -> AsyncIterator[CallbackPress]:
        """Async iterator yielding inline-button presses."""

    # -- Outbound: text and edits ------------------------------------------
    @abstractmethod
    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: Optional[str] = None,
        keyboard: Optional[Keyboard] = None,
        parse_mode: ParseMode = None,
        disable_preview: bool = False,
    ) -> SentMessage: ...

    @abstractmethod
    async def edit_text(
        self,
        sent: SentMessage,
        text: str,
        *,
        keyboard: Optional[Keyboard] = None,
        parse_mode: ParseMode = None,
    ) -> None: ...

    @abstractmethod
    async def delete(self, sent: SentMessage) -> None: ...

    # -- Outbound: media ----------------------------------------------------
    @abstractmethod
    async def send_photo(
        self,
        chat_id: str,
        photo: PathLike,
        *,
        caption: Optional[str] = None,
        keyboard: Optional[Keyboard] = None,
    ) -> SentMessage: ...

    @abstractmethod
    async def send_video(
        self,
        chat_id: str,
        video: PathLike,
        *,
        caption: Optional[str] = None,
    ) -> SentMessage: ...

    @abstractmethod
    async def send_voice(self, chat_id: str, voice: PathLike) -> SentMessage: ...

    @abstractmethod
    async def send_document(
        self,
        chat_id: str,
        path: PathLike,
        *,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> SentMessage: ...

    # -- Outbound: presence -------------------------------------------------
    @abstractmethod
    async def send_typing(self, chat_id: str) -> None:
        """Indicate the bot is processing. Adapter chooses how to render."""

    # -- Auth and downloads -------------------------------------------------
    @abstractmethod
    async def authorize(self, user: User) -> bool:
        """Return True if `user` is allowed to use the bot via this adapter."""

    @abstractmethod
    async def download_attachment(self, attachment, dest: Optional[Path] = None) -> Path:
        """Download a remote attachment to disk. Returns the local path.

        Many adapters need network/auth context to fetch media, so each
        adapter handles its own download — but the result is always a
        regular file on the local filesystem that the rest of the bot can
        read directly.
        """

    # -- Optional: platform escape hatch ------------------------------------
    def native(self) -> Any:
        """Return the underlying client object (e.g. a `telegram.Bot`).

        Useful for features that don't yet have a generic equivalent.
        Code that calls this couples itself to the adapter and should be
        kept to a minimum.
        """
        return None
