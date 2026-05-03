#!/usr/bin/env python3
"""Alfred — Mac Mini Remote Assistant via Telegram (entrypoint)."""
from __future__ import annotations

import asyncio
import signal
import logging
import importlib.util
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

import bot_state as st
import self_healing
from persistence import load_json, save_json
from config import (
    TELEGRAM_BOT_TOKEN, ALLOWED_USERS, ALLOWED_USER_IDS,
    WEBHOOK_SECRET, WEBAPP_URL,
    DATA_DIR, SNAPSHOTS_DIR, PLUGINS_DIR,
)
from core import (
    load_all_state, is_allowed, user_key, add_history,
    build_main_menu, build_back_button, save_sessions, save_machines,
    get_system_status, fmt_status, status_keyboard, check_cmd_rate,
    build_settings_text, build_automations_text,
    HELP_CATEGORIES, HELP_CAT_BUTTONS, LOG_FILE,
)
from claude_runner import run_claude, send_response
from utils.formatting import E, md_to_html, fmt_output, fmt_spoiler
from utils.helpers import async_run

# ---------------------------------------------------------------------------
# Logging (console + file)
# ---------------------------------------------------------------------------
import logging.handlers as _log_handlers
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        _log_handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
    ],
)
logger = logging.getLogger("alfred")

# ---------------------------------------------------------------------------
# Command imports
# ---------------------------------------------------------------------------
from commands.screen import cmd_screenshot, cmd_record, cmd_watch, cmd_camera
from commands.system import (
    cmd_machine, cmd_wake, cmd_status, cmd_clipboard, cmd_logs,
    cmd_paste, cmd_processes, cmd_search, cmd_volume,
    cmd_apps, send_app_launcher, cmd_guardian,
)
from commands.ai import (
    cmd_clear, cmd_clearhistory, cmd_export, cmd_model,
    cmd_undo, cmd_fork, cmd_history, cmd_research,
)
from commands.project import cmd_project, cmd_env
from commands.automation import (
    cmd_schedule, cmd_remind, cmd_timer, cmd_settings, cmd_automations,
)
from commands.misc import (
    cmd_start, cmd_help, cmd_cancel, cmd_browse, send_browse_keyboard,
    cmd_tts, cmd_terminal_tg, cmd_reload,
)
from commands.web import cmd_web
from commands.memory import cmd_memory
from commands.gmail import cmd_gmail
from handlers import (
    handle_callback, handle_message, handle_photo,
    handle_voice, handle_document, handle_location, error_handler,
)
from background import (
    run_scheduled_tasks, metrics_collector, clipboard_sync_task,
    youtube_keep_alive, health_check_loop,
)
from webhook import start_webhook_server


# ---------------------------------------------------------------------------
# Plugin loader
# ---------------------------------------------------------------------------
def _build_plugin_ctx():
    return {
        "run_claude": run_claude, "send_response": send_response,
        "async_run": async_run, "is_allowed": is_allowed,
        "user_key": user_key, "add_history": add_history,
        "save_json": save_json, "load_json": load_json,
        "E": E, "md_to_html": md_to_html, "fmt_output": fmt_output,
        "fmt_spoiler": fmt_spoiler, "ParseMode": ParseMode,
        "InlineKeyboardMarkup": InlineKeyboardMarkup,
        "InlineKeyboardButton": InlineKeyboardButton,
        "build_back_button": build_back_button,
        "user_sessions": st.user_sessions, "user_models": st.user_models,
        "user_request_count": st.user_request_count,
        "cost_tracker": st.cost_tracker, "alerts": st.alerts,
        "history": st.history, "forks": st.forks,
        "notification_enabled": st.notification_enabled,
        "user_machines": st.user_machines,
        "pending_reminders": st.pending_reminders,
        "SNAPSHOTS_DIR": SNAPSHOTS_DIR, "DATA_DIR": DATA_DIR,
    }


def load_plugins(app):
    for h in st._plugin_handler_objects:
        try:
            app.remove_handler(h, group=0)
        except Exception:
            pass
    st._plugin_handler_objects = []

    if not PLUGINS_DIR.exists():
        PLUGINS_DIR.mkdir(exist_ok=True)
        (PLUGINS_DIR / "example.py").write_text(
            'COMMAND = "hello"\n'
            'DESCRIPTION = "Say hello (example plugin)"\n\n'
            '_ctx = {}\n\n'
            'def setup(ctx):\n'
            '    global _ctx\n'
            '    _ctx = ctx\n\n'
            'async def handler(update, context):\n'
            '    if _ctx and not _ctx["is_allowed"](update):\n'
            '        return\n'
            '    await update.message.reply_text("Hello from a plugin!")\n'
        )
        return

    plugin_ctx = _build_plugin_ctx()
    for plugin_file in PLUGINS_DIR.glob("*.py"):
        if plugin_file.name.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(plugin_file.stem, plugin_file)
            if spec is None or spec.loader is None:
                logger.warning("Could not load plugin spec: %s", plugin_file.name)
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "setup"):
                mod.setup(plugin_ctx)
            if hasattr(mod, "COMMAND") and hasattr(mod, "handler"):
                h = CommandHandler(mod.COMMAND, mod.handler)
                app.add_handler(h)
                st._plugin_handler_objects.append(h)
                st.plugins[mod.COMMAND] = getattr(mod, "DESCRIPTION", "Plugin")
                logger.info("Loaded plugin: /%s", mod.COMMAND)
        except Exception as e:
            logger.error("Plugin %s failed: %s", plugin_file.name, e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    load_all_state()

    # Initialize sub-modules that still need bot references
    import sys
    import handlers as _handlers_mod
    _handlers_mod.init(sys.modules[__name__])
    from commands import misc as _misc_mod
    _misc_mod.init(sys.modules[__name__])

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    st._app_ref = app

    # Commands
    for cmd_name, handler in [
        ("start", cmd_start), ("help", cmd_help), ("clear", cmd_clear),
        ("clearhistory", cmd_clearhistory), ("cancel", cmd_cancel),
        ("screenshot", cmd_screenshot), ("record", cmd_record),
        ("watch", cmd_watch), ("camera", cmd_camera),
        ("status", cmd_status), ("clipboard", cmd_clipboard),
        ("model", cmd_model), ("browse", cmd_browse),
        ("machine", cmd_machine), ("wake", cmd_wake),
        ("export", cmd_export), ("schedule", cmd_schedule),
        ("apps", cmd_apps),
        ("logs", cmd_logs), ("undo", cmd_undo),
        ("fork", cmd_fork), ("project", cmd_project), ("env", cmd_env),
        ("history", cmd_history),
        ("automations", cmd_automations),
        ("remind", cmd_remind), ("timer", cmd_timer),
        ("tts", cmd_tts),
        ("terminal", cmd_terminal_tg),
        ("search", cmd_search), ("volume", cmd_volume),
        ("paste", cmd_paste),
        ("processes", cmd_processes), ("research", cmd_research),
        ("settings", cmd_settings), ("reload", cmd_reload),
        ("guardian", cmd_guardian),
        ("web", cmd_web),
        ("memory", cmd_memory), ("gmail", cmd_gmail),
    ]:
        app.add_handler(CommandHandler(cmd_name, handler))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.add_error_handler(error_handler)
    load_plugins(app)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        async with app:
            await app.start()
            await app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )

            commands = [
                BotCommand("screenshot", "Take a screenshot of the Mac screen"),
                BotCommand("status",     "Show CPU, RAM, disk & uptime"),
                BotCommand("research",   "Deep research [topic] — 15 parallel agents"),
                BotCommand("terminal",   "Run a shell command [cmd]"),
                BotCommand("settings",   "View and change all settings"),
                BotCommand("remind",     "Set a reminder [time] [note]"),
                BotCommand("project",    "Switch between project conversations"),
                BotCommand("clear",      "Start a fresh AI conversation"),
                BotCommand("help",       "List all commands by category"),
                BotCommand("start",      "Show the main menu"),
                BotCommand("cancel",     "Stop the current running task"),
                BotCommand("automations","View active schedules, alerts & reminders"),
            ]
            await app.bot.set_my_commands(commands)

            if WEBAPP_URL:
                try:
                    from telegram import MenuButtonWebApp, WebAppInfo
                    await app.bot.set_chat_menu_button(
                        menu_button=MenuButtonWebApp(
                            text="Dashboard",
                            web_app=WebAppInfo(url=WEBAPP_URL),
                        )
                    )
                    logger.info("Menu button set to Mini App: %s", WEBAPP_URL)
                except Exception as e:
                    logger.warning("Failed to set menu button: %s", e)

            if not WEBHOOK_SECRET:
                logger.warning("WEBHOOK_SECRET is not set — webhook API is unprotected!")
            if not ALLOWED_USERS and not ALLOWED_USER_IDS:
                logger.warning("No ALLOWED_USERS or ALLOWED_USER_IDS set — bot is open to ANYONE!")

            logger.info("Alfred started. Users: %s, IDs: %s, Plugins: %s",
                        ALLOWED_USERS, ALLOWED_USER_IDS, list(st.plugins.keys()))

            if st._default_chat_id:
                try:
                    plugin_info = f" | {len(st.plugins)} plugin{'s' if len(st.plugins) != 1 else ''}" if st.plugins else ""
                    session_info = f" | {len(st.user_sessions)} session{'s' if len(st.user_sessions) != 1 else ''}" if st.user_sessions else ""
                    await app.bot.send_message(
                        st._default_chat_id,
                        f"<b>Alfred is online.</b>{plugin_info}{session_info}",
                        parse_mode=ParseMode.HTML,
                        reply_markup=build_main_menu(),
                    )
                except Exception:
                    pass

            tasks = [
                asyncio.create_task(run_scheduled_tasks(app)),
                asyncio.create_task(start_webhook_server(app)),
                asyncio.create_task(metrics_collector()),
                asyncio.create_task(clipboard_sync_task(app)),
                asyncio.create_task(self_healing.run_health_check(app)),
                asyncio.create_task(youtube_keep_alive()),
                asyncio.create_task(health_check_loop(app)),
            ]

            stop_event = asyncio.Event()
            current_loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                current_loop.add_signal_handler(sig, stop_event.set)
            await stop_event.wait()

            for t in tasks:
                t.cancel()
            for t in list(st.watch_tasks.values()):
                t.cancel()
            for t in list(st.buffer_tasks.values()):
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Close browser sessions
            from utils.browser import close_all as _close_browsers
            await _close_browsers()
            await app.updater.stop()
            await app.stop()

    try:
        loop.run_until_complete(run())
    finally:
        import db
        db.close()


if __name__ == "__main__":
    main()
