"""Miscellaneous command handlers — start, help, cancel, browse, tts, terminal, reload."""
from __future__ import annotations

import os
import re
import time
import asyncio
import hashlib
import logging
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

import bot_state as st
from config import DANGEROUS_PATTERNS
from core import (
    is_allowed, deny, set_default_chat, user_key, check_cmd_rate,
    build_main_menu,
    HELP_CATEGORIES, HELP_CAT_BUTTONS,
)
from utils.formatting import E
from utils.helpers import async_run
from utils.ui import file_icon, path_hash, build_breadcrumbs, build_back_close, compact_bytes

logger = logging.getLogger("alfred")

# ---------------------------------------------------------------------------
# Minimal bot reference (set via init()) for functions still in bot.py
# ---------------------------------------------------------------------------
_bot = None


def init(bot_module):
    global _bot
    _bot = bot_module


_deny = deny
_check_cmd_rate = check_cmd_rate


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await _deny(update)
    set_default_chat(update)

    name = update.effective_user.first_name or "there"
    ukey = user_key(update)

    if ukey in st.user_sessions and st.user_sessions[ukey]:
        # Returning user — skip the intro, personalise
        await update.message.reply_text(
            f"👋 <b>Welcome back, {E(name)}.</b>\n\n"
            "Alfred is online. Your previous conversation is still active.\n"
            "<i>Continue where you left off, or tap /clear to start fresh.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_menu(),
        )
    else:
        # First-time or cleared session — full onboarding
        await update.message.reply_text(
            f"👋 <b>Hey {E(name)}, Alfred is ready.</b>\n\n"
            "Your Mac is online. Just type any request:\n"
            "• <i>Take a screenshot</i>\n"
            "• <i>What's my CPU and disk usage?</i>\n"
            "• <i>Open Spotify and play something</i>\n"
            "• <i>Run ls -la ~/Desktop</i>\n\n"
            "Or tap a category below to explore.\n"
            "<i>Tip: /help shows all commands.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=build_main_menu(),
        )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------
_HELP_CAT_BUTTONS = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📺", callback_data="help_cat:screen"),
        InlineKeyboardButton("🖥", callback_data="help_cat:system"),
        InlineKeyboardButton("📁", callback_data="help_cat:files"),
    ],
    [
        InlineKeyboardButton("🤖", callback_data="help_cat:ai"),
        InlineKeyboardButton("⚙️", callback_data="help_cat:auto"),
        InlineKeyboardButton("🛠", callback_data="help_cat:utils"),
    ],
    [
        InlineKeyboardButton("📋 All", callback_data="help_cat:all"),
    ],
    [InlineKeyboardButton("← Menu", callback_data="menu:main")],
])


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await _deny(update)

    sections = "\n\n".join(
        f"<b>{label}</b>\n{cmds}" for _, (label, cmds) in HELP_CATEGORIES.items()
    )

    # Add plugin commands dynamically
    plugin_extra = ""
    if st.plugins:
        plugin_lines = "\n".join(
            f"  /{name} — {E(desc)}" for name, desc in sorted(st.plugins.items())
        )
        plugin_extra = f"\n\n<b>🔌 Plugins</b>\n{plugin_lines}"

    help_text = sections + plugin_extra + "\n\n<i>Just type any message to ask Alfred anything.</i>"

    await update.message.reply_text(
        f"<blockquote expandable>{help_text}</blockquote>\n"
        "<i>Tap a category for quick reference:</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=_HELP_CAT_BUTTONS,
    )


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await _deny(update)
    ukey = user_key(update)
    procs = st.user_processes.pop(ukey, [])
    killed = 0
    for proc in procs:
        if proc.returncode is None:  # still running
            try:
                proc.kill()
                killed += 1
            except ProcessLookupError:
                pass
        st.user_request_count[ukey] = max(0, st.user_request_count.get(ukey, 1) - 1)
    if killed:
        await update.message.reply_text(f"Cancelled {killed} task(s).")
    elif procs:
        await update.message.reply_text("Tasks already finished.")
    else:
        await update.message.reply_text("No running task to cancel.")


# ---------------------------------------------------------------------------
# /browse
# ---------------------------------------------------------------------------
async def cmd_browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await _deny(update)
    path = " ".join(context.args) if context.args else str(Path.home() / "Desktop")
    await send_browse_keyboard(update, path)


async def send_browse_keyboard(update_or_query, path: str, page: int = 0):
    PAGE_SIZE = 15
    path = os.path.expanduser(path)
    if not os.path.isdir(path):
        text = f"Not a directory: <code>{E(path)}</code>"
        if hasattr(update_or_query, 'message') and update_or_query.message:
            await update_or_query.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML)
        return

    try:
        all_entries = sorted(os.listdir(path))
        # Filter hidden files by default
        entries_visible = [e for e in all_entries if not e.startswith('.')]
    except PermissionError:
        text = f"Permission denied: <code>{E(path)}</code>"
        if hasattr(update_or_query, 'message') and update_or_query.message:
            await update_or_query.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
            await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML)
        return

    # Sort: dirs first, then files
    dirs = sorted([e for e in entries_visible if os.path.isdir(os.path.join(path, e))])
    files = sorted([e for e in entries_visible if not os.path.isdir(os.path.join(path, e))])
    entries = dirs + files

    # Pagination
    total_pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_entries = entries[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

    buttons = []

    # Breadcrumbs
    breadcrumb_rows = build_breadcrumbs(path)
    buttons.extend(breadcrumb_rows)

    for entry in page_entries:
        full = os.path.join(path, entry)
        cb_data = f"browse:{full}" if os.path.isdir(full) else f"sendfile:{full}"
        if len(cb_data.encode()) > 64:
            h = hashlib.sha256(full.encode()).hexdigest()[:8]
            Path(f"/tmp/alfred_path_{h}").write_text(full)
            cb_data = f"browse_h:{h}" if os.path.isdir(full) else f"sendfile_h:{h}"

        if os.path.isdir(full):
            buttons.append([InlineKeyboardButton(f"📁 {entry}", callback_data=cb_data)])
        else:
            icon = file_icon(entry)
            try:
                size = os.path.getsize(full)
                size_str = compact_bytes(size)
            except OSError:
                size_str = "?"
            buttons.append([InlineKeyboardButton(f"{icon} {entry} ({size_str})", callback_data=cb_data)])

    # Pagination row
    if total_pages > 1:
        ph = path_hash(path)
        Path(f"/tmp/alfred_path_{ph}").write_text(path)
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("‹ Prev", callback_data=f"bp:{page - 1}:{ph}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ›", callback_data=f"bp:{page + 1}:{ph}"))
        buttons.append(nav)

    # Footer
    display = path.replace(str(Path.home()), "~")
    count_info = f"{len(dirs)} folders, {len(files)} files"
    buttons.append(build_back_close())

    text = f"📂 <code>{E(display)}</code>\n<i>{count_info}</i>"
    if hasattr(update_or_query, 'message') and update_or_query.message:
        await update_or_query.message.reply_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )
    else:
        await update_or_query.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
        )


# ---------------------------------------------------------------------------
# /tts
# ---------------------------------------------------------------------------
async def cmd_tts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text-to-speech using macOS say command."""
    if not is_allowed(update):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text(
            "Usage: <code>/tts Hello world</code>\n"
            "With voice: <code>/tts -v Samantha Hello</code>",
            parse_mode=ParseMode.HTML,
        )
    args = context.args
    voice = None
    text_args = args
    if len(args) >= 3 and args[0] == "-v":
        voice = args[1]
        text_args = args[2:]
    text = " ".join(text_args)
    if len(text) > 500:
        return await update.message.reply_text("Text too long (max 500 chars).")
    cmd = ["say"]
    if voice:
        if not re.match(r'^[a-zA-Z0-9][\w\s\-]{0,50}$', voice):
            return await update.message.reply_text("Invalid voice name (use letters, numbers, spaces, hyphens only).")
        cmd += ["-v", voice]
    cmd.append(text)
    rc, _, err = await async_run(cmd, timeout=30)
    if rc == 0:
        await update.message.reply_text(f"\U0001f50a Said: <i>{E(text[:100])}</i>", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"TTS failed: {E(err[:200])}", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /terminal
# ---------------------------------------------------------------------------
async def cmd_terminal_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stream shell command output directly in Telegram."""
    if not is_allowed(update):
        return await _deny(update)
    if not await _check_cmd_rate(update, "terminal"):
        return
    if not context.args:
        return await update.message.reply_text(
            "Usage: <code>/terminal ls -la</code>", parse_mode=ParseMode.HTML
        )
    _EXIT_EXPLANATIONS = {
        0: "Success", 1: "Error", 2: "Misuse", 126: "Permission denied",
        127: "Command not found", 130: "Interrupted", 137: "Killed (SIGKILL)",
        143: "Terminated (SIGTERM)",
    }
    cmd_str = " ".join(context.args)
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd_str, re.IGNORECASE):
            return await update.message.reply_text("❌ Blocked: destructive command.")
    msg = await update.message.reply_text(
        f"⏳ Running: <code>{E(cmd_str[:100])}</code>\n<i>Send /cancel to stop</i>",
        parse_mode=ParseMode.HTML,
    )
    ukey = user_key(update)
    proc = await asyncio.create_subprocess_shell(
        cmd_str,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, "PATH": f"/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin:{os.environ.get('PATH', '')}"},
    )
    st.user_processes.setdefault(ukey, []).append(proc)
    output_chunks = []
    last_update = time.time()
    _spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    _spin_idx = 0
    timed_out = False
    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
            if not line:
                break
            output_chunks.append(line.decode(errors="replace"))
            total = "".join(output_chunks)
            if time.time() - last_update >= 2:
                preview = total[-3000:]
                spin = _spinners[_spin_idx % len(_spinners)]
                _spin_idx += 1
                try:
                    await msg.edit_text(
                        f"{spin} <b>Running</b> <code>{E(cmd_str[:60])}</code>\n<pre>{E(preview)}</pre>",
                        parse_mode=ParseMode.HTML,
                    )
                    last_update = time.time()
                except Exception:
                    pass
    except asyncio.TimeoutError:
        proc.kill()
        timed_out = True
    finally:
        procs = st.user_processes.get(ukey, [])
        if proc in procs:
            procs.remove(proc)
        if not procs:
            st.user_processes.pop(ukey, None)
    await proc.wait()
    total = "".join(output_chunks) or "(no output)"
    rc = proc.returncode

    if timed_out:
        try:
            await msg.edit_text(
                f"⚠️ <b>Timed out</b> (30s, no output) — process killed\n"
                f"<code>{E(cmd_str[:80])}</code>\n<pre>{E(total[-2000:])}</pre>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        return

    final = total[-3800:]
    truncated = f"\n⚠️ <i>Output truncated ({len(total):,} chars → 3800)</i>" if len(total) > 3800 else ""
    explanation = _EXIT_EXPLANATIONS.get(rc, "Error")
    icon = "✅" if rc == 0 else "❌"
    status_line = f"{icon} <b>Exit {rc}</b> ({explanation}) — <code>{E(cmd_str[:60])}</code>{truncated}"
    try:
        await msg.edit_text(
            f"{status_line}\n<pre>{E(final)}</pre>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        await update.message.reply_text(f"{status_line}\n<pre>{E(final[:3800])}</pre>", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /reload
# ---------------------------------------------------------------------------
async def cmd_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hot-reload plugins without restarting the bot."""
    if not is_allowed(update):
        return await _deny(update)
    old_plugins = set(st.plugins.keys())
    st.plugins.clear()
    _bot.load_plugins(context.application)
    new_plugins = set(st.plugins.keys())
    added = new_plugins - old_plugins
    removed = old_plugins - new_plugins
    parts = [f"Reloaded <b>{len(new_plugins)}</b> plugin(s)."]
    if added:
        parts.append(f"Added: {', '.join(f'<code>/{p}</code>' for p in sorted(added))}")
    if removed:
        parts.append(f"Removed: {', '.join(f'<code>/{p}</code>' for p in sorted(removed))}")
    await update.message.reply_text(" ".join(parts), parse_mode=ParseMode.HTML)
