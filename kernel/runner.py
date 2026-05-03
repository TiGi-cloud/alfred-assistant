"""
Simple message dispatcher.

`Dispatcher` is a tiny event router that takes a stream of `Message`s and
`CallbackPress`es from one or more adapters and invokes the right handler.
It does authentication via the adapter's `authorize()` and falls back to a
configurable default handler for non-command messages.

Handlers are async functions:

    async def handler(ctx: Context) -> None: ...

`Context` bundles the message (or callback), the originating adapter, and a
few convenience helpers. The pattern is:

    dispatcher = Dispatcher(default_handler=on_text)
    dispatcher.command("ping", on_ping)
    dispatcher.callback_prefix("menu:", on_menu)

    await dispatcher.run(adapter)   # blocks; reads from adapter forever

Multiple adapters can share one dispatcher — call `run` for each in parallel.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Union

from .adapter import ChatAdapter, SentMessage
from .buttons import Keyboard
from .messages import CallbackPress, Message

logger = logging.getLogger("alfred.kernel.runner")


# ---------------------------------------------------------------------------
# Context object passed to handlers
# ---------------------------------------------------------------------------
@dataclass
class Context:
    """What every handler receives. Bundles message + adapter for ergonomics."""
    adapter: ChatAdapter
    message: Optional[Message] = None
    callback: Optional[CallbackPress] = None

    @property
    def chat_id(self) -> str:
        if self.message:
            return self.message.chat.id
        if self.callback:
            return self.callback.chat.id
        raise RuntimeError("Context has neither message nor callback")

    @property
    def user(self):
        if self.message:
            return self.message.user
        if self.callback:
            return self.callback.user
        raise RuntimeError("Context has neither message nor callback")

    async def reply(
        self,
        text: str,
        *,
        keyboard: Optional[Keyboard] = None,
        parse_mode: Optional[str] = None,
    ) -> SentMessage:
        return await self.adapter.send_text(
            self.chat_id,
            text,
            reply_to=self.message.id if self.message else None,
            keyboard=keyboard,
            parse_mode=parse_mode,
        )


Handler = Callable[[Context], Awaitable[None]]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
class Dispatcher:
    """Routes messages and callbacks to handlers."""

    def __init__(
        self,
        *,
        default_handler: Optional[Handler] = None,
        unauthorized_handler: Optional[Handler] = None,
    ) -> None:
        self._commands: dict[str, Handler] = {}
        self._callback_exact: dict[str, Handler] = {}
        self._callback_prefix: list[tuple[str, Handler]] = []
        self._default = default_handler
        self._unauthorized = unauthorized_handler or _default_unauthorized

    # -- Registration -------------------------------------------------------
    def command(self, name: str, handler: Handler) -> None:
        """Register a slash-command handler. `name` is without the leading `/`."""
        self._commands[name.lower()] = handler

    def callback(self, data: str, handler: Handler) -> None:
        """Register a callback handler matching `data` exactly."""
        self._callback_exact[data] = handler

    def callback_prefix(self, prefix: str, handler: Handler) -> None:
        """Register a callback handler matching any data starting with `prefix`."""
        self._callback_prefix.append((prefix, handler))

    def default(self, handler: Handler) -> None:
        """Set the handler for non-command messages."""
        self._default = handler

    # -- Run loop -----------------------------------------------------------
    async def run(self, adapter: ChatAdapter) -> None:
        """Read forever from `adapter`'s message and callback streams."""
        msg_task = asyncio.create_task(self._consume_messages(adapter))
        cb_task = asyncio.create_task(self._consume_callbacks(adapter))
        try:
            await asyncio.gather(msg_task, cb_task)
        except asyncio.CancelledError:
            msg_task.cancel()
            cb_task.cancel()
            raise

    async def _consume_messages(self, adapter: ChatAdapter) -> None:
        async for msg in adapter.messages():
            asyncio.create_task(self._dispatch_message(adapter, msg))

    async def _consume_callbacks(self, adapter: ChatAdapter) -> None:
        async for cb in adapter.callbacks():
            asyncio.create_task(self._dispatch_callback(adapter, cb))

    # -- Dispatch -----------------------------------------------------------
    async def _dispatch_message(self, adapter: ChatAdapter, msg: Message) -> None:
        ctx = Context(adapter=adapter, message=msg)
        try:
            if not await adapter.authorize(msg.user):
                await self._unauthorized(ctx)
                return
            if msg.is_command:
                handler = self._commands.get(msg.command_name or "")
                if handler:
                    await handler(ctx)
                    return
                # Unknown command — fall through to default
            if self._default:
                await self._default(ctx)
        except Exception:
            logger.exception("Handler error for message %s", msg.id)

    async def _dispatch_callback(self, adapter: ChatAdapter, cb: CallbackPress) -> None:
        ctx = Context(adapter=adapter, callback=cb)
        try:
            if not await adapter.authorize(cb.user):
                await self._unauthorized(ctx)
                return
            handler = self._callback_exact.get(cb.data)
            if handler is None:
                for prefix, h in self._callback_prefix:
                    if cb.data.startswith(prefix):
                        handler = h
                        break
            if handler:
                await handler(ctx)
        except Exception:
            logger.exception("Handler error for callback %s", cb.id)


async def _default_unauthorized(ctx: Context) -> None:
    """Fallback unauthorized response. Adapters can override per-bot."""
    try:
        await ctx.adapter.send_text(
            ctx.chat_id,
            "Sorry, you are not authorized to use this bot.",
        )
    except Exception:
        pass
