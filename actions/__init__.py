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
from . import (
    gmail,
    machines,
    memory,
    menu,
    notifications,
    projects,
    research,
    scheduler,
    screen,
    session,
    system,
    web,
    web_browse,
)

__all__ = [
    "gmail", "machines", "memory", "menu", "notifications", "projects",
    "research", "scheduler", "screen", "session", "system", "web", "web_browse",
]


def register_all(
    dispatcher,
    *,
    claude_runner=None,
    scheduler_instance=None,
    machines_registry=None,
    project_registry=None,
) -> None:
    """Convenience: register every handler module's commands at once.

    Optional dependencies:
      `claude_runner`     → /clear /fork /cost
      `scheduler_instance`→ /remind /timer /schedule /alert
      `machines_registry` → /machine /wake
      `project_registry`  → /project
    Modules without their dependency become friendly no-ops.
    """
    screen.register(dispatcher)
    system.register(dispatcher)
    web.register(dispatcher)
    web_browse.register(dispatcher)
    memory.register(dispatcher)
    session.register(dispatcher, runner=claude_runner)
    scheduler.register(dispatcher, scheduler=scheduler_instance)
    machines.register(dispatcher, registry=machines_registry)
    projects.register(dispatcher, registry=project_registry)
    notifications.register(dispatcher)
    research.register(dispatcher)
    gmail.register(dispatcher)
    # Menu is last so it can iterate over all registered commands
    menu.register(dispatcher)
