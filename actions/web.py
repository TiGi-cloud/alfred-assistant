"""
General handlers exposed across all adapters: /ping, /whoami, /help, /open.

Kept here so they aren't tied to any one chat platform.
"""
from __future__ import annotations

import asyncio
import sys

from kernel.runner import Context


async def cmd_ping(ctx: Context) -> None:
    await ctx.reply("pong 🏓")


async def cmd_whoami(ctx: Context) -> None:
    u = ctx.user
    msg = ctx.message
    chat = msg.chat if msg else None
    lines = [
        f"adapter:  {ctx.adapter.name}",
        f"user.id:  {u.id}",
        f"username: @{u.username}" if u.username else "username: (none)",
        f"display:  {u.display_name or '(none)'}",
        f"chat.id:  {ctx.chat_id}",
        f"chat:     {chat.type if chat else '?'}",
    ]
    await ctx.reply("\n".join(lines))


async def cmd_help(ctx: Context) -> None:
    """Show what Alfred can do across every chat platform."""
    body = """🎩 Alfred — what I can do

Screen
  /screenshot          — take a screenshot
  /record [secs]       — record screen as video (1-60s)
  /watch [interval]    — live screen stream, /watch again to stop
  /camera              — FaceTime camera photo
  /ocr                 — extract text from a photo (or current screen)

System
  /status              — CPU, memory, disk, IP, uptime
  /processes           — top processes by CPU
  /apps                — list visible apps
  /battery             — battery info (laptops)
  /wifi                — current WiFi name
  /ip                  — local + public IP
  /uptime              — Mac uptime

Volume + audio
  /volume [0-100|mute] — show/set output volume
  /tts <text>          — speak text aloud (try /tts -v Samantha hi)

Clipboard + search
  /clipboard           — read or set Mac clipboard
  /paste               — read clipboard
  /search <query>      — Spotlight search by filename

Automation
  /shortcut [name]     — list or run Siri Shortcuts
  /focus               — toggle Do Not Disturb (needs a Shortcut)

Conversation
  /clear               — start a fresh Claude conversation
  /fork [save|load|delete] <name> — branch / switch branches
  /cost                — token usage + estimated cost for this chat

Memory (long-term recall across conversations)
  /memory              — list what I remember about you
  /memory add <fact>   — store a fact
  /memory search <q>   — search remembered facts
  /memory remove <id>  — forget one
  /memory clear        — forget everything

Other
  /ping                — sanity check
  /whoami              — your identity on this adapter
  /open <url|app>      — open URL or app on the Mac
  /help                — this menu
"""
    await ctx.reply(body)


async def cmd_open(ctx: Context) -> None:
    """Open a URL or app on the Mac. Usage: /open https://… or /open Safari"""
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""
    if not args:
        await ctx.reply("Usage: /open <url-or-app>")
        return
    if sys.platform != "darwin":
        await ctx.reply("/open: macOS only.")
        return
    cmd = ["open", "-a", args] if not args.startswith(("http://", "https://", "file://", "/")) else ["open", args]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, err = await proc.communicate()
    if proc.returncode == 0:
        await ctx.reply(f"✓ opened {args}")
    else:
        await ctx.reply(f"open failed: {err.decode().strip()[:200]}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(dispatcher) -> None:
    dispatcher.command("ping", cmd_ping)
    dispatcher.command("whoami", cmd_whoami)
    dispatcher.command("help", cmd_help)
    dispatcher.command("open", cmd_open)
