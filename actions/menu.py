"""
Inline menus — /start and /menu render a tappable grid of categories.

Pressing a category button opens a submenu. Pressing a leaf button invokes
the corresponding /command (so the menu is just a discoverable wrapper around
existing commands — no new behaviour to learn).
"""
from __future__ import annotations

from kernel import Button, Keyboard
from kernel.runner import Context, Dispatcher

# (label, callback_data, layout):
#   "menu:<key>"      → open submenu with that key
#   "cmd:<name>"      → run the named slash command
#   "url:<href>"      → open URL
TOP_LEVEL = (
    [Button("📸 Screenshot", data="cmd:screenshot"),
     Button("📊 Status",     data="cmd:status")],
    [Button("📺 Screen",     data="menu:screen"),
     Button("🖥 System",     data="menu:system")],
    [Button("⏰ Reminders",  data="menu:reminders"),
     Button("🧠 Memory",     data="menu:memory")],
    [Button("💬 Conversation", data="menu:conv"),
     Button("🌐 Machines",  data="menu:machines")],
    [Button("❓ Help",        data="cmd:help"),
     Button("👤 Whoami",      data="cmd:whoami")],
)

SUBMENUS: dict[str, tuple[str, tuple[tuple[Button, ...], ...]]] = {
    "screen": ("📺 Screen", (
        (Button("📸 Screenshot",  data="cmd:screenshot"),
         Button("⏺ Record",       data="cmd:record")),
        (Button("👁 Watch",        data="cmd:watch"),
         Button("📷 Camera",      data="cmd:camera")),
        (Button("🅾 OCR",          data="cmd:ocr"),
         Button("← Back",         data="menu:_root")),
    )),
    "system": ("🖥 System", (
        (Button("📊 Status",       data="cmd:status"),
         Button("📋 Clipboard",   data="cmd:clipboard")),
        (Button("📝 Apps",         data="cmd:apps"),
         Button("🔋 Battery",     data="cmd:battery")),
        (Button("📡 WiFi",         data="cmd:wifi"),
         Button("🌐 IP",           data="cmd:ip")),
        (Button("⏱ Uptime",        data="cmd:uptime"),
         Button("🔊 Volume",       data="cmd:volume")),
        (Button("← Back",          data="menu:_root"),),
    )),
    "reminders": ("⏰ Reminders + Schedules", (
        (Button("📋 List remind",  data="cmd:remind"),
         Button("📅 List schedule",data="cmd:schedule")),
        (Button("🚨 List alerts",  data="cmd:alert"),
         Button("← Back",          data="menu:_root")),
    )),
    "memory": ("🧠 Memory", (
        (Button("📚 List",         data="cmd:memory"),
         Button("← Back",          data="menu:_root")),
    )),
    "conv": ("💬 Conversation", (
        (Button("✨ New chat",     data="cmd:clear"),
         Button("📊 Cost",         data="cmd:cost")),
        (Button("🌿 Branches",     data="cmd:fork"),
         Button("← Back",          data="menu:_root")),
    )),
    "machines": ("🌐 Machines", (
        (Button("📋 List",         data="cmd:machine"),
         Button("← Back",          data="menu:_root")),
    )),
}


def _root_keyboard() -> Keyboard:
    return Keyboard.of(*TOP_LEVEL)


def _sub_keyboard(key: str) -> Keyboard:
    return Keyboard(rows=SUBMENUS[key][1])


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
async def cmd_start(ctx: Context) -> None:
    await ctx.reply("🎩 Alfred — pick a category", keyboard=_root_keyboard())


async def cmd_menu(ctx: Context) -> None:
    await cmd_start(ctx)


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------
def _make_menu_callback(dispatcher: Dispatcher):
    async def on_menu(ctx: Context) -> None:
        cb = ctx.callback
        if not cb:
            return
        key = cb.data.split(":", 1)[1] if ":" in cb.data else ""
        if key in ("_root", "main", ""):
            await ctx.adapter.send_text(ctx.chat_id, "🎩 Alfred", keyboard=_root_keyboard())
            return
        sub = SUBMENUS.get(key)
        if not sub:
            await ctx.adapter.send_text(ctx.chat_id, f"(unknown menu: {key})")
            return
        title, _ = sub
        await ctx.adapter.send_text(ctx.chat_id, title, keyboard=_sub_keyboard(key))
    return on_menu


def _make_cmd_callback(dispatcher: Dispatcher):
    """Run the named slash command from a button press."""
    from kernel.messages import Message, MessageKind

    async def on_cmd(ctx: Context) -> None:
        cb = ctx.callback
        if not cb:
            return
        cmd_name = cb.data.split(":", 1)[1] if ":" in cb.data else ""
        handler = dispatcher._commands.get(cmd_name)
        if not handler:
            await ctx.adapter.send_text(ctx.chat_id, f"(unknown command: /{cmd_name})")
            return
        # Synthesize a fake Message so the command handler reads "no args"
        synthetic = Message(
            id=cb.message_id or "cb",
            chat=cb.chat,
            user=cb.user,
            kind=MessageKind.COMMAND,
            text=f"/{cmd_name}",
        )
        cmd_ctx = Context(adapter=ctx.adapter, message=synthetic)
        await handler(cmd_ctx)
    return on_cmd


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(dispatcher: Dispatcher) -> None:
    dispatcher.command("start", cmd_start)
    dispatcher.command("menu", cmd_menu)
    dispatcher.callback_prefix("menu:", _make_menu_callback(dispatcher))
    dispatcher.callback_prefix("cmd:",  _make_cmd_callback(dispatcher))
