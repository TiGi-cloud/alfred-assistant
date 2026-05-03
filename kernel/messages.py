"""
Platform-agnostic message types.

Every chat adapter (Telegram, Discord, Slack, Web, ...) converts its native
incoming objects to these types before handing them to the bot's core logic.
That way command handlers and the Claude pipeline don't need to know which
chat platform produced the input.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class MessageKind(str, Enum):
    """High-level kind of an incoming message."""
    TEXT = "text"
    COMMAND = "command"        # /xxx slash command (text starting with /)
    PHOTO = "photo"
    VOICE = "voice"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"
    LOCATION = "location"
    OTHER = "other"            # platform-specific events not yet abstracted


class AttachmentKind(str, Enum):
    PHOTO = "photo"
    VOICE = "voice"
    AUDIO = "audio"
    VIDEO = "video"
    DOCUMENT = "document"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class User:
    """Platform-agnostic identity of a message author.

    The `id` is whatever the adapter uses internally (Telegram numeric id,
    Discord snowflake, Slack member id, web-session uuid, …). It is opaque
    to core logic — the only guarantee is that it's stable per user within
    a single adapter.
    """
    id: str
    username: Optional[str] = None       # @handle, may be None on some platforms
    display_name: Optional[str] = None   # human-friendly name
    is_bot: bool = False


@dataclass(frozen=True)
class Chat:
    """A conversation context (DM, group, channel, web session, …)."""
    id: str
    type: str = "direct"                 # direct | group | channel | thread
    title: Optional[str] = None


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------
@dataclass
class Attachment:
    """A media or file attached to an incoming message.

    `local_path` is set after the adapter has downloaded the file. Until then
    only `remote_id` (the platform-side identifier you can fetch with) and
    metadata are available.
    """
    kind: AttachmentKind
    remote_id: Optional[str] = None
    local_path: Optional[Path] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    name: Optional[str] = None           # original filename for documents
    duration_secs: Optional[int] = None  # for voice/audio/video
    width: Optional[int] = None
    height: Optional[int] = None
    caption: Optional[str] = None        # text caption attached to the media


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    horizontal_accuracy: Optional[float] = None  # meters
    label: Optional[str] = None                  # e.g. "Home", venue title


# ---------------------------------------------------------------------------
# Messages and callbacks
# ---------------------------------------------------------------------------
@dataclass
class Message:
    """An incoming message from any chat platform."""
    id: str                                       # platform-native message id, stringified
    chat: Chat
    user: User
    kind: MessageKind
    text: Optional[str] = None
    attachments: list[Attachment] = field(default_factory=list)
    location: Optional[Location] = None
    reply_to_id: Optional[str] = None             # if this is a reply
    reply_to_text: Optional[str] = None           # text of the replied-to message, if known
    timestamp: float = field(default_factory=time.time)
    raw: Any = None                               # adapter-specific raw object (escape hatch)

    @property
    def is_command(self) -> bool:
        return self.kind is MessageKind.COMMAND

    @property
    def command_name(self) -> Optional[str]:
        """If this is a `/foo bar` message, returns 'foo' (no slash)."""
        if not self.is_command or not self.text:
            return None
        first = self.text.split(maxsplit=1)[0]
        if first.startswith("/"):
            return first[1:].split("@", 1)[0].lower() or None
        return None

    @property
    def command_args(self) -> str:
        """Raw arg tail of a slash command, or '' if none / not a command."""
        if not self.is_command or not self.text:
            return ""
        parts = self.text.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""


@dataclass
class CallbackPress:
    """A user pressed an inline button.

    `data` is the payload that was set when the button was created
    (Telegram's `callback_data`, Discord's `custom_id`, etc.).
    """
    id: str                       # callback id, used to ack on platforms that need it
    chat: Chat
    user: User
    data: str
    message_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    raw: Any = None
