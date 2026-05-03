#!/usr/bin/env python3
"""
Local iMessage adapter test harness — macOS only.

Boots ONLY the iMessage adapter and runs an echo + diagnostic loop.

  pip install nothing extra (uses macOS built-ins)
  IMESSAGE_ALLOWED_HANDLES="+15551234567" python3 test_imessage.py

Then text the Mac's Apple-ID phone/email from your iPhone:
  "ping"        → "pong"
  "whoami"      → adapter / user / chat metadata
  "/screenshot" → screenshot uploaded as image attachment
  anything else → echoed back

Stop with Ctrl-C.

Requirements:
  • macOS Messages.app signed in to your Apple ID
  • Full Disk Access granted to the Python interpreter
    (System Settings → Privacy & Security → Full Disk Access → +)
  • On the first send, accept the "wants to control Messages" prompt.
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

from kernel.runner import Context, Dispatcher  # noqa: E402


def _check_platform() -> None:
    if sys.platform != "darwin":
        print("\n  ❌ iMessage adapter only runs on macOS.\n", file=sys.stderr)
        sys.exit(2)
    chat_db = Path.home() / "Library/Messages/chat.db"
    if not chat_db.exists():
        print(f"\n  ❌ chat.db not found at {chat_db}.", file=sys.stderr)
        print("     Open Messages.app and sign in to your Apple ID first.\n", file=sys.stderr)
        sys.exit(2)


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
            f"user.id (handle): {u.id}",
            f"chat.id: {ctx.chat_id}",
            f"chat.type: {msg.chat.type}",
        ]
        await ctx.reply("\n".join(lines))

    elif text in ("/screenshot", "screenshot"):
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
        await ctx.adapter.send_photo(ctx.chat_id, path, caption="📸 from Alfred test")

    else:
        await ctx.reply(f"echo: {msg.text}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    _check_platform()

    from adapters.imessage import iMessageAdapter

    allowed = [h.strip() for h in os.environ.get("IMESSAGE_ALLOWED_HANDLES", "").split(",") if h.strip()]
    print("\n  🎩 iMessage test harness")
    print(f"     allowed handles: {allowed or 'EVERYONE (set IMESSAGE_ALLOWED_HANDLES to restrict)'}\n")

    adapter = iMessageAdapter(allowed_handles=allowed)
    dispatcher = Dispatcher(default_handler=on_text)

    try:
        await adapter.start()
    except RuntimeError as e:
        print(f"\n  ❌ {e}\n", file=sys.stderr)
        sys.exit(2)

    print("  ✓ polling chat.db every 1.5s")
    print("  → text your Mac's Apple-ID handle from your iPhone with: ping, whoami, screenshot")
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
