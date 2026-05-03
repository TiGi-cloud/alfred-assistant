#!/usr/bin/env python3
"""
Local Slack adapter test harness — NEVER COMMIT.

Boots ONLY the SlackAdapter and runs an echo + diagnostic loop. Reads tokens
from the environment so they never get baked into source. Run with:

    pip install 'slack-bolt[async]>=1.18'
    SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... \\
        python3 test_slack.py

Then message your bot in Slack:
    "ping"           → bot echoes "pong"
    "whoami"         → bot reports your Slack user info
    "buttons"        → bot sends an inline keyboard; tapping a button echoes back
    "/screenshot"    → bot takes a macOS screenshot and uploads it
    anything else    → bot echoes it back

Stop with Ctrl-C.

If something doesn't work, the terminal will show full Slack API errors and
adapter logs. That's the fastest path to a fix.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import tempfile
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).parent))

from kernel import Button, CallbackPress, Keyboard, Message, MessageKind  # noqa: E402
from kernel.runner import Context, Dispatcher  # noqa: E402


def _check_env() -> tuple[str, str]:
    bot = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    app = os.environ.get("SLACK_APP_TOKEN", "").strip()
    missing = []
    if not bot:
        missing.append("SLACK_BOT_TOKEN (xoxb-…)")
    if not app:
        missing.append("SLACK_APP_TOKEN (xapp-…)")
    if missing:
        print("\n  ❌ missing env vars:", ", ".join(missing), file=sys.stderr)
        print("\n  Get them from https://api.slack.com/apps → your app:", file=sys.stderr)
        print("    • Bot Token: OAuth & Permissions → Install to Workspace", file=sys.stderr)
        print("    • App Token: Basic Information → App-Level Tokens (scope: connections:write)", file=sys.stderr)
        print("    • Socket Mode must be ON", file=sys.stderr)
        print("    • Bot scopes: chat:write, im:history, im:read, im:write, files:write, app_mentions:read", file=sys.stderr)
        print("    • Event subscriptions: message.im, app_mention\n", file=sys.stderr)
        sys.exit(2)
    if not bot.startswith("xoxb-"):
        print(f"  ⚠️  SLACK_BOT_TOKEN doesn't start with xoxb- (got: {bot[:8]}…). Are you sure?", file=sys.stderr)
    if not app.startswith("xapp-"):
        print(f"  ⚠️  SLACK_APP_TOKEN doesn't start with xapp- (got: {app[:8]}…). Are you sure?", file=sys.stderr)
    return bot, app


# ---------------------------------------------------------------------------
# Demo handlers
# ---------------------------------------------------------------------------
async def on_text(ctx: Context) -> None:
    msg = ctx.message
    text = (msg.text or "").strip().lower()

    if text == "ping":
        await ctx.reply("pong 🏓")

    elif text == "whoami":
        u = ctx.user
        lines = [
            f"adapter: {ctx.adapter.name}",
            f"user.id: `{u.id}`",
            f"user.username: {u.username or '(none)'}",
            f"user.display_name: {u.display_name or '(none)'}",
            f"chat.id: `{ctx.chat_id}`",
            f"chat.type: {msg.chat.type}",
        ]
        await ctx.reply("\n".join(lines))

    elif text == "buttons":
        kb = Keyboard.of(
            [Button(label="Yes", data="cb:yes"), Button(label="No", data="cb:no")],
            [Button(label="GitHub", url="https://github.com/TiGi-cloud/alfred-assistant")],
        )
        await ctx.reply("Pick one:", keyboard=kb)

    elif text in ("/screenshot", "screenshot"):
        if sys.platform != "darwin":
            await ctx.reply("Screenshot only works on macOS.")
            return
        fd, path = tempfile.mkstemp(prefix="alfred-shot-", suffix=".png")
        os.close(fd)
        proc = await asyncio.create_subprocess_exec(
            "screencapture", "-x", path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            await ctx.reply(f"screencapture failed: {err.decode().strip() or 'unknown'}")
            return
        await ctx.adapter.send_photo(ctx.chat_id, path, caption="📸 from Alfred test harness")

    else:
        await ctx.reply(f"echo: {msg.text}")


async def on_button(ctx: Context) -> None:
    cb = ctx.callback
    await ctx.adapter.send_text(ctx.chat_id, f"You clicked: `{cb.data}`")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    bot_token, app_token = _check_env()

    # Lazy import the adapter so the env-vars check runs first
    from adapters.slack import SlackAdapter

    allowed = [x.strip() for x in os.environ.get("SLACK_ALLOWED_USER_IDS", "").split(",") if x.strip()]
    print(f"\n  🎩 Slack test harness — bot: {bot_token[:12]}…  app: {app_token[:12]}…")
    print(f"     allowed users: {allowed or 'EVERYONE in workspace (set SLACK_ALLOWED_USER_IDS to restrict)'}\n")

    adapter = SlackAdapter(bot_token, app_token, allowed_user_ids=allowed)
    dispatcher = Dispatcher(default_handler=on_text)
    dispatcher.callback_prefix("cb:", on_button)

    await adapter.start()
    print("  ✓ connected to Slack via Socket Mode")
    print("  → message your bot in Slack with: ping, whoami, buttons, screenshot")
    print("  → Ctrl-C to stop\n")

    task = asyncio.create_task(dispatcher.run(adapter))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    await stop.wait()
    print("\n  Shutting down…")
    task.cancel()
    await adapter.stop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
