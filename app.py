#!/usr/bin/env python3
"""
Alfred multi-adapter entry point (work in progress).

Runs Telegram and a browser-based chat side-by-side via the kernel.ChatAdapter
interface defined in `kernel/`. Both adapters share one Dispatcher and a small
set of demo handlers to prove the abstraction holds.

This is *not* a drop-in replacement for `bot.py` yet — only a handful of demo
commands are wired up. The legacy `bot.py` still drives the full Telegram
feature set; `app.py` is the future home as commands are ported one by one.

Usage:

    # 1. .env contains TELEGRAM_BOT_TOKEN and ALLOWED_USERS (see .env.example)
    # 2. Optionally set WEB_AUTH_TOKEN to pin a token; otherwise one is
    #    generated and printed to stdout on startup.
    # 3. Run:
    python3 app.py

    # 4. Browser chat appears at the printed URL; Telegram polling starts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import socket
import sys
from pathlib import Path

# Load .env so users don't have to source it manually
try:
    from dotenv import load_dotenv  # type: ignore[import]
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv is optional

from adapters.telegram import TelegramAdapter
from adapters.web import WebAdapter
from kernel import ChatAdapter
from kernel.claude import ClaudeRunner
from kernel.runner import Context, Dispatcher
from kernel.machines import MachineRegistry
from kernel.metrics import MetricsCollector
from kernel.projects import ProjectRegistry
from kernel.scheduler import Scheduler
import actions as alfred_actions
from actions.notifications import NotificationWatcher

# Discord and Slack adapters are imported lazily inside _build_adapters()
# so missing optional dependencies (discord.py, slack-bolt) don't break
# users who only run Telegram + Web.

# Single shared Claude pipeline. Built lazily so unit tests that import
# this module don't try to spawn the binary.
_claude_runner: ClaudeRunner | None = None


_project_registry: ProjectRegistry | None = None


def _get_projects() -> ProjectRegistry:
    global _project_registry
    if _project_registry is None:
        _project_registry = ProjectRegistry()
    return _project_registry


def _get_claude() -> ClaudeRunner:
    global _claude_runner
    if _claude_runner is None:
        _claude_runner = ClaudeRunner(
            model=os.environ.get("CLAUDE_MODEL") or None,
            project_registry=_get_projects(),
        )
    return _claude_runner

logger = logging.getLogger("alfred.app")


async def default_text(ctx: Context) -> None:
    """Route any non-command text (and attached media) to Claude."""
    msg = ctx.message
    if not msg:
        return
    prompt = msg.text or ""
    if not prompt and not msg.attachments:
        return
    if not prompt and msg.attachments:
        prompt = "User sent an attachment. Describe / process it appropriately."

    runner = _get_claude()
    try:
        await runner.run(ctx, prompt, attachments=msg.attachments)
    except Exception as e:
        logger.exception("Claude run failed")
        await ctx.adapter.send_text(ctx.chat_id, f"❌ Claude error: {e}")


# ---------------------------------------------------------------------------
# Adapter factory
# ---------------------------------------------------------------------------
def _parse_allowed_user_ids(raw: str) -> list[int]:
    out: list[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            logger.warning("Ignoring invalid ALLOWED_USER_IDS entry: %s", part)
    return out


def _build_adapters() -> list[ChatAdapter]:
    adapters: list[ChatAdapter] = []

    # Telegram (skip if no token)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if token:
        allowed_users = [
            u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()
        ]
        allowed_user_ids = _parse_allowed_user_ids(os.environ.get("ALLOWED_USER_IDS", ""))
        if not allowed_users and not allowed_user_ids:
            logger.warning(
                "Telegram adapter has neither ALLOWED_USERS nor ALLOWED_USER_IDS — "
                "the bot will accept commands from ANYONE who finds the handle."
            )
        adapters.append(
            TelegramAdapter.from_token(
                token,
                allowed_users=allowed_users,
                allowed_user_ids=allowed_user_ids,
            )
        )
    else:
        logger.info("TELEGRAM_BOT_TOKEN not set — Telegram adapter disabled")

    # Web (always on for now; bind localhost only)
    if os.environ.get("WEB_DISABLED", "").lower() not in ("1", "true", "yes"):
        host = os.environ.get("WEB_HOST", "127.0.0.1")
        port = int(os.environ.get("WEB_PORT", "8765"))
        token = os.environ.get("WEB_AUTH_TOKEN") or secrets.token_urlsafe(24)
        adapters.append(WebAdapter(host=host, port=port, auth_token=token))

    # Discord (optional)
    discord_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if discord_token:
        try:
            from adapters.discord import DiscordAdapter
        except Exception as e:
            logger.warning("Discord token set but adapter unavailable: %s", e)
        else:
            allowed = [
                int(x.strip())
                for x in os.environ.get("DISCORD_ALLOWED_USER_IDS", "").split(",")
                if x.strip().isdigit()
            ]
            if not allowed:
                logger.warning(
                    "Discord adapter has no DISCORD_ALLOWED_USER_IDS — anyone "
                    "in a server with the bot can use it."
                )
            adapters.append(DiscordAdapter(discord_token, allowed_user_ids=allowed))

    # Slack (optional)
    slack_bot = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    slack_app = os.environ.get("SLACK_APP_TOKEN", "").strip()
    if slack_bot and slack_app:
        try:
            from adapters.slack import SlackAdapter
        except Exception as e:
            logger.warning("Slack tokens set but adapter unavailable: %s", e)
        else:
            allowed = [
                x.strip() for x in os.environ.get("SLACK_ALLOWED_USER_IDS", "").split(",") if x.strip()
            ]
            if not allowed:
                logger.warning(
                    "Slack adapter has no SLACK_ALLOWED_USER_IDS — anyone in "
                    "the workspace can use the bot."
                )
            adapters.append(SlackAdapter(slack_bot, slack_app, allowed_user_ids=allowed))
    elif slack_bot or slack_app:
        logger.warning(
            "Slack adapter needs BOTH SLACK_BOT_TOKEN (xoxb-…) and "
            "SLACK_APP_TOKEN (xapp-…). Skipping."
        )

    # iMessage (macOS only — opt-in via env var)
    if os.environ.get("IMESSAGE_ENABLED", "").lower() in ("1", "true", "yes"):
        if sys.platform != "darwin":
            logger.warning("IMESSAGE_ENABLED=1 but not on macOS — skipping.")
        else:
            try:
                from adapters.imessage import iMessageAdapter
            except Exception as e:
                logger.warning("iMessage adapter unavailable: %s", e)
            else:
                allowed = [
                    h.strip()
                    for h in os.environ.get("IMESSAGE_ALLOWED_HANDLES", "").split(",")
                    if h.strip()
                ]
                if not allowed:
                    logger.warning(
                        "iMessage adapter has no IMESSAGE_ALLOWED_HANDLES — "
                        "Alfred will accept messages from anyone you DM."
                    )
                adapters.append(iMessageAdapter(allowed_handles=allowed))

    return adapters


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
async def run() -> None:
    adapters = _build_adapters()
    if not adapters:
        logger.error("No adapters configured. Set TELEGRAM_BOT_TOKEN or enable WEB.")
        sys.exit(2)

    dispatcher = Dispatcher(default_handler=default_text)
    scheduler = Scheduler()
    machines_registry = MachineRegistry()
    notif_watcher = NotificationWatcher()
    metrics = MetricsCollector()
    claude = _get_claude()

    for a in adapters:
        scheduler.register_adapter(a)
        notif_watcher.register_adapter(a)
        # Wire dashboard service references into the WebAdapter so /api/*
        # endpoints can read kernel state.
        if isinstance(a, WebAdapter):
            a._claude_runner = claude
            a._scheduler = scheduler
            a._machines = machines_registry
            a._metrics = metrics
            a._dispatcher = dispatcher

    alfred_actions.register_all(
        dispatcher,
        claude_runner=claude,
        scheduler_instance=scheduler,
        machines_registry=machines_registry,
        project_registry=_get_projects(),
    )

    # Start every adapter, then the kernel services
    for a in adapters:
        await a.start()
    await scheduler.start()
    await metrics.start()
    if sys.platform == "darwin":
        await notif_watcher.start()

    # Print the web URLs so users know where to click
    for a in adapters:
        if isinstance(a, WebAdapter):
            tok = a._auth_token
            base = f"http://{a._host}:{a._port}"
            print(f"\n  🎩 Web chat:   {base}/?token={tok}" if tok else f"\n  🎩 Web chat:   {base}/")
            print(f"     Dashboard:  {base}/dashboard?token={tok}" if tok else f"     Dashboard:  {base}/dashboard")
            print()

    # Run dispatcher against every adapter concurrently
    tasks = [asyncio.create_task(dispatcher.run(a), name=f"dispatch-{a.name}") for a in adapters]

    # Wait for SIGINT/SIGTERM
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass
    await stop.wait()

    logger.info("Shutting down…")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await scheduler.stop()
    await metrics.stop()
    await notif_watcher.stop()
    try:
        from kernel.browser import shutdown_pool
        await shutdown_pool()
    except Exception:
        pass
    for a in adapters:
        try:
            await a.stop()
        except Exception:
            logger.exception("Error stopping adapter %s", a.name)


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


def _check_port(host: str, port: int) -> bool:
    """Return True if `port` is available on `host`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _needs_setup() -> bool:
    """First-run check: no .env AND no env vars in the parent shell."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        return False
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        return False
    return True


def main() -> None:
    _setup_logging()

    if "--setup" in sys.argv or _needs_setup():
        print(
            "\n  No .env found — launching first-run setup wizard.\n"
            "  After saving, restart with: python3 app.py\n",
            flush=True,
        )
        import setup_wizard
        try:
            asyncio.run(setup_wizard.serve())
        except (KeyboardInterrupt, SystemExit):
            pass
        return

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
