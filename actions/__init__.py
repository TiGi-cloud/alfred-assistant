"""
actions — platform-agnostic command handlers for Alfred.

Each module exports a `register(dispatcher)` function that registers its
slash commands with a `kernel.runner.Dispatcher`. Handlers receive a
`kernel.runner.Context` and call methods on `ctx.adapter` — they don't
know whether the chat is Telegram, Discord, Slack, web, or iMessage.

(Named `actions/` instead of `handlers/` because the legacy `handlers.py`
still drives the Telegram-only `bot.py`. Once the legacy code retires,
this package may be renamed.)

To add a new command:

    # actions/myfeature.py
    async def cmd_thing(ctx):
        await ctx.reply("done")

    def register(d):
        d.command("thing", cmd_thing)

Then in app.py:

    from actions import myfeature
    myfeature.register(dispatcher)
"""
from . import screen, system, web

__all__ = ["screen", "system", "web"]


def register_all(dispatcher) -> None:
    """Convenience: register every handler module's commands at once."""
    screen.register(dispatcher)
    system.register(dispatcher)
    web.register(dispatcher)
