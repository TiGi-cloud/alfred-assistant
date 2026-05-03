"""System-related command handlers — /machine, /wake, /status, /clipboard,
/logs, /paste, /processes, /search, /volume, /apps, /guardian."""
from __future__ import annotations

import os
import socket
import time
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from core import (
    is_allowed, deny, user_key, get_system_status, fmt_status,
    status_keyboard, load_machines, save_machines, build_back_button,
)
from utils.formatting import E, fmt_output, fmt_spoiler, fmt_section
from utils.ui import severity_dot, build_back_close, dashboard_tabs
from utils.helpers import async_run, cleanup_temp
from persistence import load_json, save_json
from config import USER_MACHINES_FILE, PINNED_FILE
import bot_state as st
import self_healing

logger = logging.getLogger("alfred")

# Re-export LOG_FILE from the same place bot.py defines it
from pathlib import Path
LOG_FILE = Path(__file__).resolve().parent.parent / "alfred.log"


# ---------------------------------------------------------------------------
# /machine
# ---------------------------------------------------------------------------
async def cmd_machine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    machines = load_machines()

    if not context.args:
        current = st.user_machines.get(ukey, "local")
        msg = f"<b>Active:</b> <code>{E(current)}</code>\n\n<b>Available:</b>\n"
        msg += "  <code>local</code> -- this Mac\n"
        for name, info in machines.items():
            host = info if isinstance(info, str) else info.get("host", "?")
            mac = info.get("mac", "") if isinstance(info, dict) else ""
            mac_str = f" (MAC: {fmt_spoiler(mac)})" if mac else ""
            msg += f"  <code>{E(name)}</code> -- {E(host)}{mac_str}\n"
        msg += (
            "\n<blockquote expandable>"
            "<code>/machine local</code> -- switch to this Mac\n"
            "<code>/machine add myserver 192.168.1.10</code>\n"
            "<code>/machine add myserver 192.168.1.10 AA:BB:CC:DD:EE:FF</code>\n"
            "<code>/machine myserver</code> -- switch\n"
            "<code>/machine remove myserver</code>"
            "</blockquote>"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=build_back_button())
        return

    args = context.args
    if args[0] == "add" and len(args) >= 3:
        name, host = args[1], args[2]
        mac = args[3] if len(args) >= 4 else ""
        machines[name] = {"host": host, "mac": mac} if mac else host
        save_machines(machines)
        await update.message.reply_text(f"Added <code>{E(name)}</code> \u2192 {E(host)}", parse_mode=ParseMode.HTML)
    elif args[0] == "remove" and len(args) >= 2:
        safe_name = args[1][:43]  # machine_rem_confirm: = 20 chars; 20+43=63 < 64 byte limit
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u2705 Yes, delete", callback_data=f"machine_rem_confirm:{safe_name}"),
            InlineKeyboardButton("\u274c Cancel", callback_data="machine_rem_cancel"),
        ]])
        await update.message.reply_text(
            f"Remove machine <code>{E(args[1])}</code>?", parse_mode=ParseMode.HTML, reply_markup=kb
        )
    elif args[0] == "local":
        st.user_machines.pop(ukey, None)
        save_json(USER_MACHINES_FILE, st.user_machines)
        await update.message.reply_text("Switched to local Mac.")
    elif args[0] in machines:
        st.user_machines[ukey] = args[0]
        save_json(USER_MACHINES_FILE, st.user_machines)
        info = machines[args[0]]
        host = info if isinstance(info, str) else info.get("host", "?")
        await update.message.reply_text(f"Switched to <code>{E(args[0])}</code> ({E(host)})", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(f"Unknown machine: {E(args[0])}")


# ---------------------------------------------------------------------------
# /wake
# ---------------------------------------------------------------------------
async def cmd_wake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    if not context.args:
        await update.message.reply_text(
            "<b>Wake-on-LAN</b>\n\n"
            "Usage: <code>/wake &lt;machine&gt;</code>\n"
            "Machine must have MAC address.\n"
            "Add with: <code>/machine add name host AA:BB:CC:DD:EE:FF</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    name = context.args[0]
    machines = load_machines()
    if name not in machines:
        await update.message.reply_text(f"Unknown machine: {E(name)}")
        return

    info = machines[name]
    mac = info.get("mac", "") if isinstance(info, dict) else ""
    if not mac:
        await update.message.reply_text(f"No MAC address for <code>{E(name)}</code>", parse_mode=ParseMode.HTML)
        return

    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        if len(mac_bytes) != 6:
            raise ValueError("MAC address must be 6 bytes")
        magic = b'\xff' * 6 + mac_bytes * 16
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(magic, ('255.255.255.255', 9))
        finally:
            sock.close()
        await update.message.reply_text(f"WOL sent to <code>{E(name)}</code> ({fmt_spoiler(mac)})", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"WOL failed: {E(str(e))}")


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    info = await get_system_status()
    uptime_secs = int(time.time() - st._start_time)
    text = fmt_status(info, ukey, uptime_secs)

    chat_id = update.effective_chat.id
    # Pin status message — edit existing or create new
    if chat_id in st.pinned_status:
        try:
            await context.bot.edit_message_text(
                text, chat_id=chat_id, message_id=st.pinned_status[chat_id],
                parse_mode=ParseMode.HTML,
                reply_markup=status_keyboard(),
            )
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        except Exception:
            pass

    msg = await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=status_keyboard(),
    )

    # Try to pin the status message
    try:
        await context.bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        st.pinned_status[chat_id] = msg.message_id
        save_json(PINNED_FILE, st.pinned_status)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /clipboard
# ---------------------------------------------------------------------------
async def cmd_clipboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    args = " ".join(context.args) if context.args else ""
    if args:
        await async_run(["pbcopy"], input_data=args)
        await update.message.reply_text(f"Copied: <code>{E(args[:100])}</code>", parse_mode=ParseMode.HTML)
    else:
        rc, text, _ = await async_run(["pbpaste"])
        text = text.strip()
        if text:
            formatted = fmt_output(text, threshold=500)
            await update.message.reply_text(
                f"<b>Clipboard:</b>\n{formatted}",
                parse_mode=ParseMode.HTML,
                reply_markup=build_back_button(),
            )
        else:
            img_path = "/tmp/clipboard.png"
            rc, _, _ = await async_run([
                "osascript", "-e",
                f'set f to POSIX file "{img_path}"\n'
                'set img to the clipboard as <<class PNGf>>\n'
                'set fp to open for access f with write permission\n'
                'write img to fp\nclose access fp'
            ])
            if rc == 0 and os.path.isfile(img_path):
                with open(img_path, 'rb') as f:
                    await update.message.reply_photo(photo=f)
                cleanup_temp(img_path)
            else:
                await update.message.reply_text("Clipboard is empty.")


# ---------------------------------------------------------------------------
# /logs
# ---------------------------------------------------------------------------
async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /logs [n] [pattern]  — show last n lines, optionally filtered."""
    if not is_allowed(update):
        return await deny(update)
    n = 20
    pattern = None
    args = list(context.args or [])
    if args:
        try:
            n = min(int(args[0]), 200)
            args.pop(0)
        except ValueError:
            pass
    if args:
        pattern = " ".join(args)

    if not LOG_FILE.exists():
        await update.message.reply_text("No log file found.")
        return

    if pattern:
        rc, out, _ = await async_run(
            ["grep", "-i", pattern, str(LOG_FILE)], timeout=5
        )
        lines = out.strip().split("\n") if out.strip() else []
        out = "\n".join(lines[-n:])
        header = f"<b>Last {min(n, len(lines))} lines matching <code>{E(pattern)}</code>:</b>"
    else:
        rc, out, _ = await async_run(["tail", "-n", str(n), str(LOG_FILE)])
        header = f"<b>Last {n} log lines:</b>"

    if out.strip():
        await update.message.reply_text(
            f"{header}\n{fmt_output(out.strip())}",
            parse_mode=ParseMode.HTML,
            reply_markup=build_back_button(),
        )
    else:
        msg = f"No lines matching <code>{E(pattern)}</code>." if pattern else "Log file is empty."
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /paste
# ---------------------------------------------------------------------------
async def cmd_paste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send text to macOS clipboard: /paste <text>"""
    if not is_allowed(update):
        return await deny(update)
    if not context.args:
        return await update.message.reply_text(
            "Usage: <code>/paste &lt;text to copy&gt;</code>", parse_mode=ParseMode.HTML
        )
    text = " ".join(context.args)
    await async_run(["pbcopy"], input_data=text)
    await update.message.reply_text(
        f"\U0001f4cb Copied to clipboard: <code>{E(text[:200])}</code>",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# /processes
# ---------------------------------------------------------------------------
async def cmd_processes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show top CPU/memory processes."""
    if not is_allowed(update):
        return await deny(update)
    n = 10
    if context.args:
        try:
            n = min(int(context.args[0]), 20)
        except ValueError:
            pass
    sort_by = context.args[1].lower() if len(context.args) > 1 else "cpu"
    if sort_by not in ("cpu", "mem", "memory"):
        sort_by = "cpu"

    # macOS ps doesn't support --sort; fetch all and sort manually
    rc, out, _ = await async_run(["ps", "axo", "pid,pcpu,pmem,comm"], timeout=10)

    if rc == 0 and out.strip():
        lines = out.strip().split('\n')
        data = [l for l in lines[1:] if l.strip()]
        # Sort manually
        try:
            col = 1 if sort_by == "cpu" else 2
            data.sort(key=lambda l: float(l.split()[col]), reverse=True)
        except Exception:
            pass
        # Build clean readable output
        rows = []
        kill_buttons = []
        for i, row in enumerate(data[:n]):
            parts = row.split()
            if len(parts) < 4:
                continue
            pid, cpu_pct, mem_pct = parts[0], parts[1], parts[2]
            name = ' '.join(parts[3:]).split('/')[-1][:20]
            try:
                cpu_val = float(cpu_pct)
            except ValueError:
                cpu_val = 0
            dot = severity_dot(cpu_val, warn=20, high=50, crit=80)
            rows.append(
                f"<code>{dot} {name:<20} {cpu_pct:>5}% {mem_pct:>5}%</code>"
            )
            if i < 5:
                kill_buttons.append(
                    InlineKeyboardButton(f"\U0001f6d1 {name[:12]}", callback_data=f"killpid:{pid}")
                )
        kb_rows = []
        for i in range(0, len(kill_buttons), 3):
            kb_rows.append(kill_buttons[i:i+3])
        sort_label = sort_by.upper()
        kb_rows.append([
            InlineKeyboardButton(f"{'·' if sort_by == 'cpu' else ''}CPU{'·' if sort_by == 'cpu' else ''}", callback_data=f"proc_refresh:cpu:{n}"),
            InlineKeyboardButton(f"{'·' if sort_by != 'cpu' else ''}MEM{'·' if sort_by != 'cpu' else ''}", callback_data=f"proc_refresh:mem:{n}"),
            InlineKeyboardButton("\u21bb Refresh", callback_data=f"proc_refresh:{sort_by}:{n}"),
        ])
        kb_rows.append(build_back_close())
        header = f"{fmt_section('PROCESSES')}\n<code>   {'Name':<20} {'CPU':>5}  {'MEM':>5}</code>"
        text = f"{header}\n" + "\n".join(rows)
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(kb_rows),
        )
    else:
        await update.message.reply_text("Could not get process list.")


# ---------------------------------------------------------------------------
# /search
# ---------------------------------------------------------------------------
async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search files/content on this Mac: /search query [path]"""
    if not is_allowed(update):
        return await deny(update)
    if not context.args:
        return await update.message.reply_text(
            "Usage: <code>/search query [path]</code>\n"
            "Examples:\n"
            "  <code>/search todo.txt</code> \u2014 find files by name\n"
            "  <code>/search 'def main' ~/code</code> \u2014 grep in directory",
            parse_mode=ParseMode.HTML,
        )
    query_str = context.args[0]
    search_path = context.args[1] if len(context.args) > 1 else os.path.expanduser("~")
    search_path = os.path.expanduser(search_path)
    msg = await update.message.reply_text(f"\U0001f50d Searching...", parse_mode=ParseMode.HTML)

    # Try ripgrep for content search, fall back to find for filename search
    results = []
    # File name search via mdfind (Spotlight)
    rc1, out1, _ = await async_run(["mdfind", "-name", query_str, "-onlyin", search_path], timeout=10)
    if rc1 == 0 and out1.strip():
        lines = out1.strip().split('\n')[:20]
        results.append(f"<b>Files ({len(lines)}):</b>\n" + "\n".join(f"  <code>{E(l)}</code>" for l in lines))

    # Content search via grep
    rc2, out2, _ = await async_run(
        ["grep", "-r", "-l", "--include=*.py", "--include=*.txt", "--include=*.md",
         "--include=*.js", "--include=*.ts", "-m", "1", "--", query_str, search_path],
        timeout=10,
    )
    if rc2 == 0 and out2.strip():
        lines = out2.strip().split('\n')[:10]
        results.append(f"<b>Content matches ({len(lines)}):</b>\n" + "\n".join(f"  <code>{E(l)}</code>" for l in lines))

    if results:
        await msg.edit_text("\n\n".join(results), parse_mode=ParseMode.HTML)
    else:
        await msg.edit_text(f"No results for <code>{E(query_str)}</code>", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /volume
# ---------------------------------------------------------------------------
async def cmd_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get or set macOS system volume: /volume [0-100]"""
    if not is_allowed(update):
        return await deny(update)
    if context.args:
        try:
            level = int(context.args[0])
            if not (0 <= level <= 100):
                raise ValueError
        except ValueError:
            return await update.message.reply_text("Usage: <code>/volume [0-100]</code>", parse_mode=ParseMode.HTML)
        rc, _, _ = await async_run(
            ["osascript", "-e", f"set volume output volume {level}"], timeout=5
        )
        if rc == 0:
            await update.message.reply_text(f"\U0001f50a Volume set to <b>{level}%</b>", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("Failed to set volume.")
    else:
        rc, out, _ = await async_run(
            ["osascript", "-e", "output volume of (get volume settings)"], timeout=5
        )
        if rc == 0:
            vol = out.strip()
            mute_rc, mute_out, _ = await async_run(
                ["osascript", "-e", "output muted of (get volume settings)"], timeout=5
            )
            muted = mute_out.strip() == "true"
            mute_str = " (muted)" if muted else ""
            buttons = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\U0001f507 Mute", callback_data="vol:0"),
                    InlineKeyboardButton("\U0001f509 25%", callback_data="vol:25"),
                    InlineKeyboardButton("\U0001f50a 50%", callback_data="vol:50"),
                ],
                [
                    InlineKeyboardButton("\U0001f50a 75%", callback_data="vol:75"),
                    InlineKeyboardButton("\U0001f50a 100%", callback_data="vol:100"),
                ],
                [InlineKeyboardButton("\u2190 Back", callback_data="menu:main")],
            ])
            await update.message.reply_text(
                f"\U0001f50a Volume: <b>{vol}%</b>{mute_str}",
                parse_mode=ParseMode.HTML,
                reply_markup=buttons,
            )
        else:
            await update.message.reply_text("Could not get volume.")


# ---------------------------------------------------------------------------
# /apps
# ---------------------------------------------------------------------------
async def cmd_apps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    await send_app_launcher(update.message)


async def send_app_launcher(message):
    # Dynamically list installed .app bundles from /Applications
    try:
        entries = sorted(os.listdir("/Applications"))
        apps = []
        for e in entries:
            if e.endswith(".app"):
                full_name = e[:-4]  # strip .app
                short = full_name[:12]  # Telegram callback limit
                apps.append((short, full_name))
    except Exception:
        apps = [
            ("Chrome", "Google Chrome"), ("Safari", "Safari"),
            ("Terminal", "Terminal"), ("Finder", "Finder"),
        ]
    # Limit to 27 apps (9 rows of 3) to fit Telegram keyboard limits
    apps = apps[:27]
    rows = []
    row = []
    for label, app_name in apps:
        row.append(InlineKeyboardButton(label, callback_data=f"launch:{app_name}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("\u2190 Back", callback_data="menu:main")])
    await message.reply_text(
        "<b>App Launcher</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ---------------------------------------------------------------------------
# /guardian — self-healing system guardian
# ---------------------------------------------------------------------------
async def cmd_guardian(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Self-healing system guardian."""
    if not is_allowed(update):
        return await deny(update)
    if context.args and context.args[0] == "incidents":
        text = self_healing.build_incidents_text()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Clear All", callback_data="heal:clear_incidents"),
            InlineKeyboardButton("\u2190 Settings", callback_data="heal:settings"),
        ]])
    elif context.args and context.args[0] == "audit":
        text = "Running audit... use the button below."
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Run Full Audit", callback_data="heal:audit"),
            InlineKeyboardButton("\u2190 Settings", callback_data="heal:settings"),
        ]])
    elif context.args and context.args[0] == "drift":
        text = "Check drift via button below."
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Check Drift Now", callback_data="heal:drift_now"),
        ]])
    else:
        text = self_healing.build_heal_settings_text()
        kb = self_healing.build_heal_settings_kb()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
