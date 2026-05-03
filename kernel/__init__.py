"""
kernel — platform-agnostic Alfred core.

Defines the abstract types every chat adapter speaks in: Message, User, Chat,
Attachment, CallbackPress, Keyboard, Button, ChatAdapter. Adapters in
`adapters/` import from here and translate their platform-native objects
to/from these types.

The existing top-level modules (bot.py, handlers.py, webhook.py, core.py,
commands/*.py) still drive the running Telegram bot — this package is being
filled in alongside them and will absorb their platform-agnostic parts as
the migration to a multi-adapter architecture progresses.
"""

from .messages import (
    User,
    Chat,
    Attachment,
    Message,
    CallbackPress,
    Location,
    MessageKind,
    AttachmentKind,
)
from .buttons import Button, Keyboard, InlineRow
from .adapter import ChatAdapter, SentMessage

__all__ = [
    "User",
    "Chat",
    "Attachment",
    "Message",
    "CallbackPress",
    "Location",
    "MessageKind",
    "AttachmentKind",
    "Button",
    "Keyboard",
    "InlineRow",
    "ChatAdapter",
    "SentMessage",
]
