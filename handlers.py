"""Alfred event handlers — callback queries, messages, photos, voice, documents, location."""
from __future__ import annotations

import os
import re
import time
import asyncio
import logging
import hashlib
from datetime import datetime
from pathlib import Path

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

import bot_state as st
from config import (
    CLAUDE_MODEL, FORKS_FILE,
    NOTIF_FILE, GEOFENCES_FILE,
    REMINDERS_FILE, BUFFER_DELAY, RATE_LIMIT_MAX,
    MAX_CONCURRENT_PER_USER, DANGEROUS_PATTERNS, SNAPSHOTS_DIR,
    MODELS_FILE, SCHEDULES_FILE, MACHINES_FILE,
)
from persistence import load_json, save_json
from utils.formatting import E, md_to_html, fmt_output, fmt_spoiler, progress_bar
from utils.helpers import async_run, take_screenshot, cleanup_temp, ocr_image
from utils.ui import (
    compact_duration, fmt_time_ago, cron_to_human, build_back_close,
)
from core import (
    is_allowed as _is_allowed, deny as _deny, user_key as _user_key,
    set_default_chat as _set_default_chat, add_history as _add_history,
    get_system_status as _get_system_status, fmt_status as _fmt_status,
    save_sessions as _save_sessions, save_machines as _save_machines,
    load_machines as _load_machines, save_projects as _save_projects,
    build_main_menu as _build_main_menu, build_sub_menu as _build_sub_menu,
    build_back_button as _build_back_button,
    build_settings_text as _build_settings_text,
    build_automations_text as _build_automations_text,
    status_keyboard as _status_keyboard,
    LOG_FILE as _LOG_FILE_PATH,
    HELP_CATEGORIES as _HELP_CATEGORIES,
    HELP_CAT_BUTTONS as _HELP_CAT_BUTTONS,
)
from claude_runner import run_claude as _run_claude, send_response as _send_response, send_typing as _send_typing, _send_with_retry

logger = logging.getLogger("alfred")

# Minimal bot reference for functions not yet extracted to core/commands
# (send_browse_keyboard, send_app_launcher, cmd_help)
_bot = None

def init(bot_module):
    global _bot
    _bot = bot_module

async def _send_browse_keyboard(*a, **kw): return await _bot.send_browse_keyboard(*a, **kw)
def _send_app_launcher(*a, **kw): return _bot.send_app_launcher(*a, **kw)


async def _finish_task(ukey: str):
    """Decrement task count and process next queued message if any."""
    st.user_request_count[ukey] = max(0, st.user_request_count.get(ukey, 0) - 1)
    # Process queued messages
    queue = st.message_queue.get(ukey, [])
    if queue and not st._queue_processing.get(ukey):
        st._queue_processing[ukey] = True
        try:
            item = queue.pop(0)
            st.message_queue[ukey] = queue
            # Re-invoke handle_message with the queued update
            await handle_message(item["update"], item["context"])
        except Exception as exc:
            logger.error("Queue processing error for %s: %s", ukey, exc)
        finally:
            st._queue_processing[ukey] = False


# Callback query handler — menus, actions, hints
# ---------------------------------------------------------------------------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not _is_allowed(update):
        return await _deny(update)

    data = query.data
    ukey = _user_key(update)

    # --- Menu navigation ---
    if data == "menu:main":
        await query.edit_message_text(
            "<b>Alfred</b> \u2014 tap a category:",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_main_menu(),
        )
        return

    if data.startswith("menu:"):
        category = data[5:]
        kb, label = _build_sub_menu(category)
        if kb:
            await query.edit_message_text(
                f"<b>{E(label)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )
        return

    # --- Quick reply suggestions ---
    if data.startswith("suggest:"):
        suggestion = data[8:]
        thinking = await query.message.reply_text("\u280b <b>Working...</b>", parse_mode=ParseMode.HTML)
        st.user_request_count[ukey] = st.user_request_count.get(ukey, 0) + 1
        try:
            resp = await _run_claude(suggestion, ukey, thinking_msg=thinking, context=context, chat_id=query.message.chat_id)
        except Exception as e:
            resp = f"Error: {e}"
        finally:
            await _finish_task(ukey)
        try:
            await thinking.delete()
        except Exception:
            pass
        await _send_response(update, resp)
        return

    # --- Hints (show usage instructions) ---
    if data.startswith("hint:"):
        hint_text = data[5:]
        await query.message.reply_text(
            f"<code>{E(hint_text)}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_back_button(),
        )
        return

    # --- Actions (trigger commands without arguments) ---
    if data.startswith("act:"):
        action = data[4:]
        if action == "camera":
            path = "/tmp/camera_capture.jpg"
            rc, _, _ = await async_run(["which", "imagesnap"], timeout=5)
            if rc == 0:
                rc, _, _ = await async_run(["imagesnap", "-w", "1", path], timeout=15)
            else:
                rc, _, _ = await async_run([
                    "ffmpeg", "-y", "-f", "avfoundation", "-framerate", "30",
                    "-i", "0", "-frames:v", "1", path
                ], timeout=15)
            if rc == 0 and os.path.isfile(path):
                with open(path, 'rb') as f:
                    await query.message.reply_photo(photo=f, caption="FaceTime camera")
                cleanup_temp(path)
            else:
                await query.message.reply_text("Camera failed. Install: brew install imagesnap")

        elif action == "logs":
            if _LOG_FILE_PATH.exists():
                rc, out, _ = await async_run(["tail", "-n", "20", str(_LOG_FILE_PATH)])
                if out.strip():
                    await query.message.reply_text(
                        f"<b>Last 20 log lines:</b>\n{fmt_output(out.strip())}",
                        parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
                    )

        elif action == "clear":
            st.user_sessions.pop(ukey, None)
            _save_sessions()
            active_proj = st.active_project.get(ukey)
            if active_proj and ukey in st.projects and active_proj in st.projects[ukey]:
                st.projects[ukey][active_proj]["session_id"] = ""
                st.projects[ukey][active_proj]["pending"] = ""
                _save_projects()
            await query.message.reply_text("Conversation cleared.", reply_markup=_build_back_button())

        elif action == "undo":
            if ukey not in st.user_sessions:
                await query.message.reply_text("No active session.")
                return
            thinking = await query.message.reply_text("\u280b <b>Undoing...</b>", parse_mode=ParseMode.HTML)
            st.user_request_count[ukey] = st.user_request_count.get(ukey, 0) + 1
            try:
                resp = await _run_claude(
                    "Undo the last action you performed. Revert changes and tell me what you undid.",
                    ukey, thinking_msg=thinking, context=context, chat_id=query.message.chat_id,
                )
            except Exception as e:
                resp = f"Error: {e}"
            finally:
                await _finish_task(ukey)
            try:
                await thinking.delete()
            except Exception:
                pass
            await _send_response(update, resp)

        elif action == "export":
            session_id = st.user_sessions.get(ukey)
            if not session_id:
                await query.message.reply_text("No active conversation.")
                return
            export_path = f"/tmp/alfred_export_{session_id[:8]}.md"
            await async_run(["claude", "export", session_id, "-o", export_path])
            if os.path.isfile(export_path) and os.path.getsize(export_path) > 0:
                with open(export_path, 'rb') as f:
                    await query.message.reply_document(document=f, filename=f"conversation_{session_id[:8]}.md")
                cleanup_temp(export_path)
            else:
                await query.message.reply_text(f"Export unavailable. Session: {fmt_spoiler(session_id)}", parse_mode=ParseMode.HTML)

        elif action == "history":
            user_history = st.history.get(ukey, [])
            if not user_history:
                await query.message.reply_text("No history yet.", reply_markup=_build_back_button())
            else:
                lines = []
                for e in user_history[-20:]:
                    role = "You" if e["role"] == "user" else "Alfred"
                    lines.append(f"<code>{e['time']}</code> <b>{role}:</b> {E(e['text'][:80])}")
                msg = "<b>Recent history:</b>\n" + "\n".join(lines)
                if len(msg) > 3500:
                    msg = f"<blockquote expandable>{msg}</blockquote>"
                await query.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=_build_back_button())

        elif action == "projects":
            user_projects = st.projects.get(ukey, {})
            active = st.active_project.get(ukey)
            if not user_projects:
                await query.message.reply_text(
                    "📂 <b>No projects.</b> Use <code>/project new &lt;name&gt;</code>",
                    parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
                )
            else:
                lines = []
                buttons = []
                for name, proj in sorted(user_projects.items(), key=lambda x: x[1].get("last_used", 0), reverse=True):
                    marker = "▸ " if name == active else "  "
                    active_tag = " (active)" if name == active else ""
                    desc = proj.get("description", "")
                    desc_str = f" — {E(desc)}" if desc else ""
                    lines.append(f"<code>{marker}{E(name)}</code>{active_tag}{desc_str}")
                    if name != active:
                        buttons.append([InlineKeyboardButton(f"→ {name}", callback_data=f"proj_sw:{name[:50]}")])
                buttons.append([InlineKeyboardButton("← Back", callback_data="menu:main")])
                await query.message.reply_text(
                    "📂 <b>Projects:</b>\n\n" + "\n".join(lines),
                    parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons),
                )

        elif action == "shortcuts":
            rc, stdout, _ = await async_run(["shortcuts", "list"])
            if rc == 0 and stdout.strip():
                items = "\n".join(f"  <code>{E(s)}</code>" for s in stdout.strip().split('\n')[:20])
                await query.message.reply_text(
                    f"<b>Siri Shortcuts:</b>\n{items}",
                    parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
                )
            else:
                await query.message.reply_text("No Siri Shortcuts found.", reply_markup=_build_back_button())

        elif action == "model_picker":
            current = st.user_models.get(ukey) or CLAUDE_MODEL or "default"
            _aliases = {"claude-opus-4-6": "opus", "claude-sonnet-4-6": "sonnet", "claude-haiku-4-5-20251001": "haiku"}
            _cur_short = _aliases.get(current, current.split("-")[1] if "-" in current else current)
            def _mlabel(name):
                return f"✓ {name}" if _cur_short == name.lower() else name
            buttons = [[
                InlineKeyboardButton(_mlabel("Opus"), callback_data="setmodel:opus"),
                InlineKeyboardButton(_mlabel("Sonnet"), callback_data="setmodel:sonnet"),
                InlineKeyboardButton(_mlabel("Haiku"), callback_data="setmodel:haiku"),
            ], build_back_close()]
            await query.message.reply_text(
                f"Current model: <code>{E(current)}</code>\n"
                f"<i>Opus — most capable  ·  Sonnet — balanced  ·  Haiku — fastest</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif action == "schedule_list":
            all_scheds = load_json(SCHEDULES_FILE, [])
            user_scheds = [s for s in all_scheds if s.get("user_key") == ukey]
            if not user_scheds:
                await query.message.reply_text(
                    "<b>📅 No scheduled tasks.</b>\n\n"
                    '<code>/schedule "every hour" "check disk"</code>',
                    parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
                )
            else:
                lines = []
                for i, s in enumerate(user_scheds):
                    enabled = s.get("enabled", True)
                    dot = "🟢" if enabled else "⚫"
                    human = cron_to_human(s.get("cron", ""))
                    task_short = s['task'][:35] + ('...' if len(s['task']) > 35 else '')
                    lines.append(f"<code>{dot} {i+1}. {E(task_short)}</code>\n<code>   {E(human)}</code>")
                await query.message.reply_text(
                    f"<b>📅 Schedules ({len(user_scheds)})</b>\n\n" + "\n".join(lines) +
                    "\n\n<code>/schedule remove N</code> to delete",
                    parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
                )

        elif action == "remind_list":
            now = time.time()
            user_rems = [r for r in st.pending_reminders.get(ukey, []) if r["fire_time"] > now]
            if not user_rems:
                await query.message.reply_text(
                    "<b>⏰ No pending reminders.</b>\n\n<code>/remind 10m Check laundry</code>",
                    parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
                )
            else:
                lines = []
                for r in sorted(user_rems, key=lambda x: x["fire_time"]):
                    remaining = int(r["fire_time"] - now)
                    lines.append(f"  <b>{r['id']}</b>. <i>{E(r['text'])}</i> — in {compact_duration(remaining)}")
                await query.message.reply_text(
                    "<b>⏰ Pending reminders:</b>\n" + "\n".join(lines) +
                    "\n\n<code>/remind cancel &lt;id&gt;</code>",
                    parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
                )

        elif action == "timer_picker":
            buttons = [
                [
                    InlineKeyboardButton("⏱ 5m", callback_data="suggest:/timer 5m"),
                    InlineKeyboardButton("⏱ 15m", callback_data="suggest:/timer 15m"),
                    InlineKeyboardButton("⏱ 30m", callback_data="suggest:/timer 30m"),
                ],
                [
                    InlineKeyboardButton("⏱ 1h", callback_data="suggest:/timer 1h"),
                    InlineKeyboardButton("⏱ 2h", callback_data="suggest:/timer 2h"),
                ],
                build_back_close(),
            ]
            await query.message.reply_text(
                "<b>⏱ Quick Timer</b>\nSelect a preset or use <code>/timer 5m label</code>",
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif action == "wake_picker":
            machines = _load_machines()
            wake_machines = {n: m for n, m in machines.items()
                            if isinstance(m, dict) and m.get("mac")}
            if not wake_machines:
                await query.message.reply_text(
                    "<b>⚡ No machines with MAC address.</b>\n\n"
                    "<code>/machine add name host AA:BB:CC:DD:EE:FF</code>",
                    parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
                )
            else:
                buttons = []
                row = []
                for name in sorted(wake_machines):
                    row.append(InlineKeyboardButton(f"⚡ {name}", callback_data=f"suggest:/wake {name}"))
                    if len(row) == 2:
                        buttons.append(row)
                        row = []
                if row:
                    buttons.append(row)
                buttons.append(build_back_close())
                await query.message.reply_text(
                    "<b>⚡ Wake-on-LAN</b>\nSelect a machine to wake:",
                    parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons),
                )

        elif action == "machine_list":
            machines = _load_machines()
            current = st.user_machines.get(ukey, "local")
            lines = [f"<b>Active:</b> <code>{E(current)}</code>\n"]
            buttons = []
            # Local option
            if current != "local":
                buttons.append([InlineKeyboardButton("→ local (this Mac)", callback_data="suggest:/machine local")])
            for name, info in sorted(machines.items()):
                host = info if isinstance(info, str) else info.get("host", "?")
                lines.append(f"  <code>{E(name)}</code> — {E(host)}")
                if name != current:
                    cb = f"suggest:/machine {name}"
                    if len(cb.encode()) <= 64:
                        buttons.append([InlineKeyboardButton(f"→ {name}", callback_data=cb)])
            buttons.append(build_back_close())
            await query.message.reply_text(
                "<b>🖥 Machines</b>\n" + "\n".join(lines),
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif action == "guardian_status":
            import self_healing
            text = self_healing.build_heal_settings_text()
            kb = self_healing.build_heal_settings_kb()
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

        elif action == "branch_list":
            user_forks = st.forks.get(ukey, {})
            current = st.user_sessions.get(ukey, "none")
            if not user_forks:
                await query.message.reply_text(
                    "<b>🌿 No branches.</b>\n\n<code>/fork save name</code> to create one.",
                    parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
                )
            else:
                lines = []
                buttons = []
                for n, s in user_forks.items():
                    is_current = s == current
                    marker = "▸ " if is_current else "  "
                    lines.append(f"<code>{marker}{E(n)}</code>")
                    if not is_current:
                        cb = f"suggest:/fork load {n}"
                        if len(cb.encode()) <= 64:
                            buttons.append([InlineKeyboardButton(f"→ {n}", callback_data=cb)])
                buttons.append(build_back_close())
                await query.message.reply_text(
                    "<b>🌿 Branches</b>\n\n" + "\n".join(lines),
                    parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons),
                )

        elif action == "watch_toggle":
            if ukey in st.watch_tasks:
                st.watch_tasks[ukey].cancel()
                del st.watch_tasks[ukey]
                await query.message.reply_text("⏹ Screen watch stopped.", reply_markup=_build_back_button())
            else:
                buttons = [
                    [
                        InlineKeyboardButton("👁 2s", callback_data="suggest:/watch 2"),
                        InlineKeyboardButton("👁 5s", callback_data="suggest:/watch 5"),
                        InlineKeyboardButton("👁 10s", callback_data="suggest:/watch 10"),
                    ],
                    build_back_close(),
                ]
                await query.message.reply_text(
                    "<b>👁 Screen Watch</b>\nSelect refresh interval:",
                    parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons),
                )

        elif action == "record_picker":
            buttons = [
                [
                    InlineKeyboardButton("⏺ 10s", callback_data="suggest:/record 10"),
                    InlineKeyboardButton("⏺ 30s", callback_data="suggest:/record 30"),
                    InlineKeyboardButton("⏺ 60s", callback_data="suggest:/record 60"),
                ],
                build_back_close(),
            ]
            await query.message.reply_text(
                "<b>⏺ Screen Record</b>\nSelect duration:",
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons),
            )

        elif action == "automations":
            text = _build_automations_text(ukey)
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📅 Schedules", callback_data="act:schedule_list"),
                    InlineKeyboardButton("⏰ Reminders", callback_data="act:remind_list"),
                ],
                [
                    InlineKeyboardButton("⏱ Timer", callback_data="act:timer_picker"),
                    InlineKeyboardButton("↻ Refresh", callback_data="act:automations"),
                ],
                build_back_close(),
            ])
            await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

        elif action == "automations_refresh":
            text = _build_automations_text(ukey)
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📅 Schedules", callback_data="act:schedule_list"),
                    InlineKeyboardButton("⏰ Reminders", callback_data="act:remind_list"),
                ],
                [
                    InlineKeyboardButton("⏱ Timer", callback_data="act:timer_picker"),
                    InlineKeyboardButton("↻ Refresh", callback_data="act:automations"),
                ],
                build_back_close(),
            ])
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass

        return

    # --- Noop (separator/page-indicator buttons) ---
    if data == "noop":
        return

    # --- Close menu ---
    if data == "close_menu":
        try:
            await query.message.delete()
        except Exception:
            await query.message.edit_text("Closed.", reply_markup=None)
        return

    # --- Pin status message ---
    if data == "pin_status":
        chat_id = query.message.chat_id
        st.pinned_status[chat_id] = query.message.message_id
        from config import PINNED_FILE
        save_json(PINNED_FILE, st.pinned_status)
        try:
            await context.bot.pin_chat_message(chat_id, query.message.message_id, disable_notification=True)
            await query.answer("Status pinned.", show_alert=False)
        except Exception:
            await query.answer("Could not pin (need admin rights).", show_alert=True)
        return

    # --- Dashboard tabs (status) ---
    if data.startswith("dash:"):
        tab = data[5:]
        if tab == "procs":
            # Show process tab inline
            rc, out, _ = await async_run(["ps", "axo", "pid,pcpu,pmem,comm"], timeout=10)
            if rc == 0 and out.strip():
                from utils.ui import severity_dot
                lines_raw = out.strip().split('\n')
                data_rows = [l for l in lines_raw[1:] if l.strip()]
                data_rows.sort(key=lambda l: float(l.split()[1]) if len(l.split()) > 1 else 0, reverse=True)
                rows = []
                for i, row in enumerate(data_rows[:8]):
                    pts = row.split()
                    if len(pts) < 4:
                        continue
                    cpu_pct, mem_pct = pts[1], pts[2]
                    name = ' '.join(pts[3:]).split('/')[-1][:20]
                    try:
                        cpu_val = float(cpu_pct)
                    except ValueError:
                        cpu_val = 0
                    dot = severity_dot(cpu_val, warn=20, high=50, crit=80)
                    rows.append(f"<code>{dot} {name:<20} {cpu_pct:>5}% {mem_pct:>5}%</code>")
                text = "<b>━━ TOP PROCESSES ━━━━━━━━━━━━━</b>\n" + "\n".join(rows)
            else:
                text = "Could not get process list."
            from utils.ui import dashboard_tabs
            kb = InlineKeyboardMarkup([
                dashboard_tabs("procs", [("System", "system"), ("Procs", "procs"), ("Net", "net")], prefix="dash"),
                [InlineKeyboardButton("↻ Refresh", callback_data="dash:procs")],
                build_back_close(),
            ])
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass
        elif tab == "net":
            rc, out, _ = await async_run(["bash", "-c",
                'echo "IP: $(ipconfig getifaddr en0 2>/dev/null || echo N/A)"; '
                'echo "DNS: $(scutil --dns | grep "nameserver" | head -2 | awk \'{print $3}\' | tr "\\n" " ")"; '
                'netstat -ib | head -5'
            ], timeout=10)
            text = f"<b>━━ NETWORK ━━━━━━━━━━━━━</b>\n<pre>{E(out.strip()[:2000])}</pre>" if out.strip() else "No network info."
            from utils.ui import dashboard_tabs
            kb = InlineKeyboardMarkup([
                dashboard_tabs("net", [("System", "system"), ("Procs", "procs"), ("Net", "net")], prefix="dash"),
                [InlineKeyboardButton("↻ Refresh", callback_data="dash:net")],
                build_back_close(),
            ])
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass
        else:
            # System tab — default status refresh
            info = await _get_system_status()
            uptime_secs = int(time.time() - st._start_time)
            text = _fmt_status(info, ukey, uptime_secs)
            kb = _status_keyboard()
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass
        return

    # --- Browse pagination ---
    if data.startswith("bp:"):
        parts = data[3:].split(":", 1)
        if len(parts) == 2:
            try:
                page = int(parts[0])
            except ValueError:
                page = 0
            ph = parts[1]
            hfile = Path(f"/tmp/alfred_path_{ph}")
            if hfile.exists():
                path = hfile.read_text()
                await _send_browse_keyboard(query, path, page=page)
        return

    # --- Toggle handler ---
    if data.startswith("tg:"):
        toggle_key = data[3:]
        if toggle_key == "notif":
            current = st.notification_enabled.get(ukey, False)
            st.notification_enabled[ukey] = not current
            save_json(NOTIF_FILE, st.notification_enabled)
            state_str = "on" if not current else "off"
            await query.answer(f"Notifications: {state_str}", show_alert=False)
            # Refresh settings panel
            text = await _build_settings_text(ukey)
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🤖 Model", callback_data="act:model_picker"),
                    InlineKeyboardButton("📅 Schedule", callback_data="act:schedule_list"),
                ],
                [
                    InlineKeyboardButton("🖥 Machine", callback_data="act:machine_list"),
                    InlineKeyboardButton("🔔 Notifications", callback_data="tg:notif"),
                ],
                [
                    InlineKeyboardButton("↻ Refresh", callback_data="settings_refresh"),
                    InlineKeyboardButton("✖ Close", callback_data="settings_close"),
                ],
            ])
            try:
                await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                pass
        return

    # --- Quick actions ---
    if data == "quick_screenshot":
        path = "/tmp/screenshot.png"
        if await take_screenshot(path):
            try:
                with open(path, 'rb') as f:
                    await query.message.reply_photo(photo=f)
            finally:
                cleanup_temp(path)

    elif data == "quick_status":
        info = await _get_system_status()
        uptime_secs = int(time.time() - st._start_time)
        text = _fmt_status(info, ukey, uptime_secs)
        kb = _status_keyboard()
        chat_id = query.message.chat_id
        if chat_id in st.pinned_status:
            try:
                await context.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=st.pinned_status[chat_id],
                    parse_mode=ParseMode.HTML, reply_markup=kb,
                )
                return
            except Exception:
                pass
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "quick_clipboard":
        rc, text, _ = await async_run(["pbpaste"])
        text = text.strip()[:2000] or "(empty)"
        await query.message.reply_text(
            f"<b>Clipboard:</b>\n{fmt_output(text, 500)}",
            parse_mode=ParseMode.HTML, reply_markup=_build_back_button(),
        )

    elif data.startswith("proc_refresh:"):
        parts = data[13:].split(":", 1)
        sort_by = parts[0] if parts else "cpu"
        n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
        rc, out, _ = await async_run(["ps", "axo", "pid,pcpu,pmem,comm"], timeout=10)
        if rc == 0 and out.strip():
            lines = out.strip().split('\n')
            data_rows = [l for l in lines[1:] if l.strip()]
            col = 1 if sort_by == "cpu" else 2
            try:
                data_rows.sort(key=lambda l: float(l.split()[col]), reverse=True)
            except Exception:
                pass
            rows = []
            kill_buttons = []
            for i, row in enumerate(data_rows[:n]):
                pts = row.split()
                if len(pts) < 4:
                    continue
                pid, cpu_pct, mem_pct = pts[0], pts[1], pts[2]
                name = ' '.join(pts[3:]).split('/')[-1][:22]
                rows.append(
                    f"<code>{i+1:>2}. {name:<22} CPU {cpu_pct:>5}%  MEM {mem_pct:>5}%</code>"
                )
                if i < 5:
                    kill_buttons.append(InlineKeyboardButton(f"🛑 {name[:15]}", callback_data=f"killpid:{pid}"))
            kb_rows = [kill_buttons[i:i+3] for i in range(0, len(kill_buttons), 3)]
            kb_rows.append([
                InlineKeyboardButton("📊 CPU", callback_data=f"proc_refresh:cpu:{n}"),
                InlineKeyboardButton("💾 MEM", callback_data=f"proc_refresh:mem:{n}"),
                InlineKeyboardButton("← Back", callback_data="menu:main"),
            ])
            await query.message.reply_text(
                f"<b>📊 Top {n} by {sort_by.upper()}</b>\n\n" + "\n".join(rows),
                parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb_rows),
            )

    elif data == "app_launcher":
        await _send_app_launcher(query.message)

    elif data.startswith("launch:"):
        app_name = data[7:]
        await async_run(["open", "-a", app_name])
        await query.message.reply_text(f"Launched <b>{E(app_name)}</b>", parse_mode=ParseMode.HTML)

    elif data.startswith("browse:"):
        await _send_browse_keyboard(query, data[7:])

    elif data.startswith("browse_h:"):
        hfile = Path(f"/tmp/alfred_path_{data[9:]}")
        if hfile.exists():
            await _send_browse_keyboard(query, hfile.read_text())

    elif data.startswith("sendfile:"):
        fpath = data[9:]
        if os.path.isfile(fpath):
            fsize = os.path.getsize(fpath)
            if fsize > 50 * 1024 * 1024:
                await query.message.reply_text(f"File too large: {fsize // 1024 // 1024}MB (max 50MB)")
            else:
                ext = os.path.splitext(fpath)[1].lower()
                try:
                    with open(fpath, 'rb') as f:
                        if ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
                            await query.message.reply_photo(photo=f)
                        else:
                            await query.message.reply_document(document=f)
                except Exception as e:
                    await query.message.reply_text(f"Failed to send file: {E(str(e))}", parse_mode=ParseMode.HTML)

    elif data.startswith("sendfile_h:"):
        hfile = Path(f"/tmp/alfred_path_{data[11:]}")
        if hfile.exists():
            fpath = hfile.read_text()
            try:
                hfile.unlink(missing_ok=True)
            except Exception:
                pass
            if os.path.isfile(fpath):
                try:
                    with open(fpath, 'rb') as f:
                        await query.message.reply_document(document=f)
                except Exception as e:
                    await query.message.reply_text(f"Failed to send file: {E(str(e))}", parse_mode=ParseMode.HTML)

    elif data.startswith("web:"):
        web_action = data[4:]
        if web_action == "screenshot":
            from commands.web import _screenshot_only
            await _screenshot_only(update, context, ukey)
        elif web_action == "snapshot":
            from commands.web import _snapshot
            await _snapshot(update, context, ukey)
        elif web_action == "scroll_up":
            from commands.web import _scroll
            await _scroll(update, context, ukey, "up")
        elif web_action == "scroll_down":
            from commands.web import _scroll
            await _scroll(update, context, ukey, "down")
        elif web_action == "text":
            from commands.web import _text_content
            await _text_content(update, context, ukey)
        elif web_action == "close":
            from utils.browser import close_session as _close_browser
            await _close_browser(ukey)
            await query.message.reply_text("🌐 Browser session closed.")

    elif data.startswith("approve:"):
        approval_id = data[8:]
        if approval_id in st.pending_approvals:
            info = st.pending_approvals.pop(approval_id)
            thinking = await query.message.reply_text("\u280b <b>Approved. Running...</b>", parse_mode=ParseMode.HTML)
            approved_ukey = info["ukey"]
            st.user_request_count[approved_ukey] = st.user_request_count.get(approved_ukey, 0) + 1
            try:
                response = await _run_claude(
                    info["prompt"], approved_ukey,
                    thinking_msg=thinking, context=context, chat_id=query.message.chat_id,
                )
            except Exception as e:
                response = f"Error: {e}"
            finally:
                await _finish_task(approved_ukey)
            try:
                await thinking.delete()
            except Exception:
                pass
            await _send_response(update, response)

    elif data.startswith("deny:"):
        approval_id = data[5:]
        info = st.pending_approvals.pop(approval_id, None)
        if info:
            snippet = E(info.get("prompt", "")[:80])
            await query.message.edit_text(
                f"❌ <b>Denied.</b>\n<code>{snippet}</code>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await query.message.edit_text("❌ <b>Request expired or already handled.</b>", parse_mode=ParseMode.HTML)

    elif data.startswith("cancel:"):
        target_ukey = data[7:]
        procs = st.user_processes.pop(target_ukey, [])
        killed = 0
        for proc in procs:
            if proc.returncode is None:
                try:
                    proc.kill()
                    killed += 1
                except Exception:
                    pass
            await _finish_task(target_ukey)
        if killed:
            await query.message.edit_text(f"Cancelled {killed} task(s).")
        elif procs:
            await query.message.edit_text("Tasks already finished.")

    elif data.startswith("setmodel:"):
        model_name = data[9:].strip()
        aliases = {"opus": "claude-opus-4-6", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"}
        if model_name in aliases:
            st.user_models[ukey] = aliases[model_name]
        elif re.match(r'^claude-[\w.\-]+$', model_name):
            st.user_models[ukey] = model_name
        else:
            await query.message.edit_text("Unknown model. Use: opus, sonnet, or haiku.")
            return
        save_json(MODELS_FILE, st.user_models)
        _set_name = st.user_models[ukey].split("-")[1].capitalize() if "-" in st.user_models[ukey] else st.user_models[ukey]
        await query.answer(f"Model: {_set_name}", show_alert=False)
        await query.message.edit_text(
            f"Model set to: <code>{E(st.user_models[ukey])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_build_back_button(),
        )

    # --- Reminder snooze/done ---
    elif data.startswith("remind_snooze:"):
        # Format: remind_snooze:{ukey}:{rid}:{note}:{minutes}
        parts = data[14:].split(":", 3)
        if len(parts) == 4:
            target_ukey, rid_str, note_text, mins_str = parts
            try:
                mins = int(mins_str)
                snooze_secs = mins * 60
                fire_time = time.time() + snooze_secs
                # Re-add to pending reminders
                rems = st.pending_reminders.get(target_ukey, [])
                new_rid = max((r["id"] for r in rems), default=0) + 1
                chat_id_snooze = query.message.chat_id
                rems.append({"id": new_rid, "text": note_text, "fire_time": fire_time, "chat_id": chat_id_snooze})
                st.pending_reminders[target_ukey] = rems
                save_json(REMINDERS_FILE, st.pending_reminders)
                fire_dt = datetime.fromtimestamp(fire_time)

                async def _snooze_fire():
                    await asyncio.sleep(snooze_secs)
                    try:
                        await query.message.reply_text(
                            f"⏰ <b>Reminder (snoozed):</b> {E(note_text)}",
                            parse_mode=ParseMode.HTML,
                        )
                    except Exception:
                        pass
                    finally:
                        if target_ukey in st.pending_reminders:
                            st.pending_reminders[target_ukey] = [r for r in st.pending_reminders[target_ukey] if r["id"] != new_rid]
                            save_json(REMINDERS_FILE, st.pending_reminders)

                t = asyncio.create_task(_snooze_fire())
                st.reminder_tasks.add(t)
                t.add_done_callback(st.reminder_tasks.discard)
                await query.message.edit_text(
                    f"💤 Snoozed {mins}m — will remind at {fire_dt.strftime('%H:%M')}",
                    reply_markup=None,
                )
            except (ValueError, IndexError):
                await query.answer("Could not snooze.", show_alert=False)

    elif data.startswith("remind_done:"):
        await query.message.edit_text("✅ Reminder dismissed.", reply_markup=None)

    # --- Settings panel ---
    elif data == "settings_refresh":
        text = await _build_settings_text(ukey)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🤖 Model", callback_data="act:model_picker"),
                InlineKeyboardButton("📅 Schedule", callback_data="act:schedule_list"),
            ],
            [
                InlineKeyboardButton("🖥 Machine", callback_data="act:machine_list"),
                InlineKeyboardButton("🔔 Notifications", callback_data="tg:notif"),
            ],
            [
                InlineKeyboardButton("↻ Refresh", callback_data="settings_refresh"),
                InlineKeyboardButton("✖ Close", callback_data="settings_close"),
            ],
        ])
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "settings_close":
        try:
            await query.message.delete()
        except Exception:
            await query.message.edit_text("Settings closed.", reply_markup=None)

    # --- Help category drill-down ---
    elif data.startswith("help_cat:"):
        cat = data[9:]
        cats = _HELP_CATEGORIES
        if cat == "all":
            sections = "\n\n".join(
                f"<b>{label}</b>\n{cmds}" for _, (label, cmds) in cats.items()
            )
            body = f"<blockquote expandable>{sections}\n\n<i>Just type any message to ask Alfred anything.</i></blockquote>\n<i>Tap a category for quick reference:</i>"
        elif cat in cats:
            label, cmds = cats[cat]
            body = f"<b>{label}</b>\n\n{cmds}\n\n<i>Tap another category or All for full list.</i>"
        else:
            body = "Unknown category."
        await query.answer()
        try:
            await query.message.edit_text(body, parse_mode=ParseMode.HTML, reply_markup=_HELP_CAT_BUTTONS)
        except Exception:
            pass

    # --- Confirmation dialogs ---
    elif data.startswith("snap_del_confirm:"):
        snap_name = data[17:]
        snap_path = SNAPSHOTS_DIR / f"{snap_name}.png"
        if snap_path.exists():
            snap_path.unlink()
            await query.message.edit_text(f"Deleted snapshot: <code>{E(snap_name)}</code>", parse_mode=ParseMode.HTML)
        else:
            await query.message.edit_text("Snapshot not found.")

    elif data == "snap_del_cancel":
        await query.message.edit_text("Cancelled.")

    elif data.startswith("machine_rem_confirm:"):
        machine_name = data[20:]
        machines = _load_machines()
        machines.pop(machine_name, None)
        _save_machines(machines)
        await query.message.edit_text(f"Removed machine: <code>{E(machine_name)}</code>", parse_mode=ParseMode.HTML)

    elif data == "machine_rem_cancel":
        await query.message.edit_text("Cancelled.")

    elif data.startswith("fork_del_confirm:"):
        fork_name = data[17:]
        st.forks.get(ukey, {}).pop(fork_name, None)
        save_json(FORKS_FILE, st.forks)
        await query.message.edit_text(f"Deleted branch: <code>{E(fork_name)}</code>", parse_mode=ParseMode.HTML)

    elif data == "fork_del_cancel":
        await query.message.edit_text("Cancelled.")

    # Project switching/deletion callbacks
    elif data.startswith("proj_sw:"):
        proj_name = data[8:]
        user_projects = st.projects.get(ukey, {})
        if proj_name not in user_projects:
            await query.message.edit_text(f"Project not found: {E(proj_name)}", parse_mode=ParseMode.HTML)
            return
        bot_mod = _bot
        old_project = st.active_project.get(ukey)
        if old_project and old_project != proj_name and old_project in user_projects:
            await bot_mod._save_current_project(ukey)
        await bot_mod._switch_to_project(ukey, proj_name)
        proj = user_projects[proj_name]
        desc = proj.get("description", "")
        desc_str = f" — {E(desc)}" if desc else ""
        has_session = "Conversation restored." if proj.get("session_id") else "Fresh conversation."
        await query.message.edit_text(
            f"📂 <b>Switched to:</b> <code>{E(proj_name)}</code>{desc_str}\n{has_session}",
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("proj_del_y:"):
        proj_name = data[11:]
        st.projects.get(ukey, {}).pop(proj_name, None)
        if st.active_project.get(ukey) == proj_name:
            st.active_project.pop(ukey, None)
            st.user_sessions.pop(ukey, None)
            _save_sessions()
        _save_projects()
        await query.message.edit_text(f"✅ Deleted project: <code>{E(proj_name)}</code>", parse_mode=ParseMode.HTML)

    elif data == "proj_del_n":
        await query.message.edit_text("Cancelled.")

    elif data == "proj_clear_all":
        user_projects = st.projects.get(ukey, {})
        for name, proj in user_projects.items():
            proj["session_id"] = ""
            proj["pending"] = ""
        st.user_sessions.pop(ukey, None)
        _save_sessions()
        _save_projects()
        await query.message.edit_text("✅ All project sessions cleared. Configs preserved.")

    elif data.startswith("vol:"):
        try:
            level = int(data[4:])
            await async_run(["osascript", "-e", f"set volume output volume {level}"], timeout=5)
            await query.message.edit_text(f"\U0001f50a Volume set to <b>{level}%</b>", parse_mode=ParseMode.HTML)
        except Exception:
            pass

    elif data.startswith("killpid:"):
        pid_str = data[8:]
        try:
            pid = int(pid_str)
        except ValueError:
            await query.message.reply_text("Invalid PID.")
            return
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, kill it", callback_data=f"killconfirm:{pid}"),
            InlineKeyboardButton("❌ Cancel", callback_data="menu:main"),
        ]])
        await query.message.reply_text(
            f"⚠️ Kill PID <b>{pid}</b>?\nThis cannot be undone.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

    elif data.startswith("killconfirm:"):
        pid_str = data[12:]
        try:
            pid = int(pid_str)
            rc, _, err = await async_run(["kill", str(pid)], timeout=5)
            if rc == 0:
                await query.message.edit_text(f"✅ Killed PID {pid}", reply_markup=_build_back_button())
            else:
                await query.message.edit_text(f"❌ Kill failed: {E(err[:200])}", parse_mode=ParseMode.HTML)
        except (ValueError, Exception) as e:
            await query.message.reply_text(f"Error: {E(str(e))}", parse_mode=ParseMode.HTML)

    elif data.startswith("watch_stop:"):
        target_ukey = data[11:]
        task = st.watch_tasks.pop(target_ukey, None)
        if task:
            task.cancel()
            await query.message.edit_text("⏹ Screen watch stopped.", reply_markup=_build_back_button())
        else:
            await query.answer("Watch is not running (already stopped).", show_alert=False)

    # --- Self-healing callbacks ---
    elif data.startswith("heal:"):
        import self_healing
        await self_healing.handle_heal_callback(query, ukey)


# ---------------------------------------------------------------------------
# Main message handler (multi-message grouping + reply context)
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return await _deny(update)

    _set_default_chat(update)
    user_text = update.message.text
    if not user_text:
        return

    ukey = _user_key(update)

    await _send_typing(context, update.effective_chat.id)

    if ukey not in st.message_buffers:
        st.message_buffers[ukey] = []

    reply_context = ""
    if update.message.reply_to_message:
        replied = update.message.reply_to_message
        if replied.text:
            reply_context = f'\n[Replying to: "{replied.text[:500]}"]'

    st.message_buffers[ukey].append({
        "text": user_text, "reply_context": reply_context,
        "update": update, "context": context,
    })

    if ukey in st.buffer_tasks:
        st.buffer_tasks[ukey].cancel()

    st.buffer_tasks[ukey] = asyncio.create_task(_process_buffered(ukey))


async def _process_buffered(ukey):
    await asyncio.sleep(BUFFER_DELAY)

    messages = st.message_buffers.pop(ukey, [])
    st.buffer_tasks.pop(ukey, None)
    if not messages:
        return

    last = messages[-1]
    update = last["update"]
    context = last["context"]

    if len(messages) == 1:
        combined = messages[0]["text"]
        reply_ctx = messages[0]["reply_context"]
    else:
        combined = "\n".join(m["text"] for m in messages)
        reply_ctx = next((m["reply_context"] for m in messages if m["reply_context"]), "")

    prompt = combined + reply_ctx

    # Natural language help — catch before sending to Claude
    _help_re = re.compile(
        r"^\s*(\?+|help|assist|what can you do|what do you do|how does this work|"
        r"how do i|what('s| is) alfred|commands|menu)\s*$",
        re.IGNORECASE,
    )
    if _help_re.match(combined):
        await _bot.cmd_help(update, context)
        return

    # Natural language thanks/done — suggest next actions
    _thanks_re = re.compile(
        r"^\s*(thanks?|thank you|thx|ty|cheers|great|awesome|perfect|nice|good job|"
        r"done|got it|ok thanks?|👍+|🙏+)\s*[!.]*\s*$",
        re.IGNORECASE,
    )
    if _thanks_re.match(combined):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📸 Screenshot", callback_data="quick_screenshot"),
            InlineKeyboardButton("📊 Status", callback_data="quick_status"),
            InlineKeyboardButton("🔬 Research", callback_data="hint:/research <topic>"),
        ]])
        await update.message.reply_text(
            "👍 Anything else?\n"
            "<i>Some things you might find useful:</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    # Two-stage confirmation for dangerous commands
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, prompt, re.IGNORECASE):
            approval_id = hashlib.sha256(f"{ukey}{time.time()}".encode()).hexdigest()[:12]
            st.pending_approvals[approval_id] = {"prompt": prompt, "ukey": ukey, "created_at": time.time()}
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("\u2705 Yes, execute", callback_data=f"approve:{approval_id}"),
                    InlineKeyboardButton("\u274c Cancel", callback_data=f"deny:{approval_id}"),
                ]
            ])
            await update.message.reply_text(
                f"<b>\u26a0\ufe0f Destructive command detected</b>\n\n"
                f"<pre>{E(prompt[:300])}</pre>\n\n"
                f"This could cause data loss or system changes that are hard to reverse.\n"
                f"Are you sure?",
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            return

    # Per-minute rate limiting
    now_ts = time.time()
    timestamps = st.user_rate_timestamps.get(ukey, [])
    timestamps = [t for t in timestamps if now_ts - t < 60]  # keep last minute
    if len(timestamps) >= RATE_LIMIT_MAX:
        wait = int(60 - (now_ts - timestamps[0]))
        await update.message.reply_text(
            f"Rate limit: {RATE_LIMIT_MAX} requests/min. Try again in ~{wait}s.",
        )
        return
    timestamps.append(now_ts)
    st.user_rate_timestamps[ukey] = timestamps

    count = st.user_request_count.get(ukey, 0)
    if count >= MAX_CONCURRENT_PER_USER:
        # Queue the message instead of rejecting
        queue = st.message_queue.get(ukey, [])
        if len(queue) >= 5:
            await update.message.reply_text(
                f"Queue full ({len(queue)} pending). Wait or /cancel first.",
            )
            return
        queue.append({"text": combined, "update": update, "context": context})
        st.message_queue[ukey] = queue
        await update.message.reply_text(
            f"⏳ Queued (position {len(queue)}). {count} tasks running.",
        )
        return

    u = update.effective_user
    user_label = f"@{u.username}" if u.username else (u.first_name or str(u.id))
    logger.info("Task from %s: %s", user_label, combined[:100])
    _add_history(ukey, "user", combined)

    _active_model = st.user_models.get(ukey) or CLAUDE_MODEL or "default"
    _model_short = next((k for k in ("opus", "sonnet", "haiku") if k in _active_model.lower()), _active_model.split("-")[1] if "-" in _active_model else _active_model)
    _machine = st.user_machines.get(ukey, "local")
    _machine_hint = f" · {E(_machine)}" if _machine != "local" else ""
    _proj = st.active_project.get(ukey)
    _proj_hint = f" · {E(_proj)}" if _proj else ""
    thinking_msg = await update.message.reply_text(
        f"⁢ <b>Working...</b> <i>({E(_model_short)}{_machine_hint}{_proj_hint})</i>", parse_mode=ParseMode.HTML,
    )
    st.user_request_count[ukey] = count + 1

    try:
        response = await _run_claude(
            prompt, ukey,
            thinking_msg=thinking_msg, context=context, chat_id=update.effective_chat.id,
        )
    except Exception as e:
        logger.error("Failed to run claude: %s", e)
        response = f"Failed to run claude: {e}"
    finally:
        await _finish_task(ukey)

    try:
        await thinking_msg.delete()
    except Exception:
        pass

    _add_history(ukey, "alfred", response[:200])
    await _send_response(update, response)


# ---------------------------------------------------------------------------
# Media handlers
# ---------------------------------------------------------------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return await _deny(update)
    _set_default_chat(update)
    ukey = _user_key(update)
    await _send_typing(context, update.effective_chat.id)

    photo = update.message.photo[-1]
    file = await photo.get_file()
    photo_path = f"/tmp/telegram_photo_{update.message.message_id}.jpg"
    await file.download_to_drive(photo_path)

    caption = update.message.caption or "The user sent this image. Describe what you see and ask how you can help."
    ocr_text = await ocr_image(photo_path)
    ocr_note = f"\n\n[OCR text extracted from image: {ocr_text}]" if ocr_text else ""

    thinking_msg = await update.message.reply_text("\u280b <b>Analyzing image...</b>", parse_mode=ParseMode.HTML)
    st.user_request_count[ukey] = st.user_request_count.get(ukey, 0) + 1

    prompt = f'The user sent an image saved at {photo_path}. Their message: "{caption}". Use the Read tool to view the image, then respond.{ocr_note}'
    try:
        response = await _run_claude(prompt, ukey, thinking_msg=thinking_msg, context=context, chat_id=update.effective_chat.id)
    except Exception as e:
        response = f"Error: {e}"
    finally:
        await _finish_task(ukey)
        cleanup_temp(photo_path)

    try:
        await thinking_msg.delete()
    except Exception:
        pass
    await _send_response(update, response)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return await _deny(update)
    _set_default_chat(update)
    ukey = _user_key(update)
    await _send_typing(context, update.effective_chat.id)

    voice = update.message.voice or update.message.audio
    if voice is None:
        await update.message.reply_text("Could not read audio from message.")
        return
    file = await voice.get_file()
    ogg_path = f"/tmp/telegram_voice_{update.message.message_id}.ogg"
    wav_path = f"/tmp/telegram_voice_{update.message.message_id}.wav"
    await file.download_to_drive(ogg_path)

    thinking_msg = await update.message.reply_text("\u280b <b>Transcribing...</b>", parse_mode=ParseMode.HTML)

    rc, _, _ = await async_run(["ffmpeg", "-y", "-i", ogg_path, "-ar", "16000", "-ac", "1", wav_path], timeout=30)
    if rc != 0:
        await thinking_msg.edit_text("Failed to convert audio. Is ffmpeg installed?")
        cleanup_temp(ogg_path)
        return

    transcription = None
    rc, _, _ = await async_run(
        ["whisper", wav_path, "--model", "base", "--output_format", "txt", "--output_dir", "/tmp"], timeout=120,
    )
    if rc == 0:
        txt_path = wav_path.replace(".wav", ".txt")
        if os.path.isfile(txt_path):
            transcription = Path(txt_path).read_text().strip()
            cleanup_temp(txt_path)

    if not transcription:
        transcription = "(Voice message -- transcription unavailable)"

    try:
        await thinking_msg.edit_text(f"<i>{E(transcription)}</i>", parse_mode=ParseMode.HTML)
    except Exception:
        await thinking_msg.edit_text(transcription)

    _add_history(ukey, "user", f"[voice] {transcription}")
    st.user_request_count[ukey] = st.user_request_count.get(ukey, 0) + 1
    try:
        response = await _run_claude(transcription, ukey, context=context, chat_id=update.effective_chat.id)
    except Exception as e:
        response = f"Error: {e}"
    finally:
        await _finish_task(ukey)
        cleanup_temp(ogg_path, wav_path)

    _add_history(ukey, "alfred", response[:200])
    await _send_response(update, response)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return await _deny(update)
    _set_default_chat(update)
    ukey = _user_key(update)
    await _send_typing(context, update.effective_chat.id)

    doc = update.message.document
    file = await doc.get_file()
    raw_name = doc.file_name or f"file_{doc.file_id[:8]}"
    safe_name = os.path.basename(raw_name)  # strip any path traversal sequences
    if not safe_name:
        safe_name = f"file_{doc.file_id[:8]}"
    save_path = f"/tmp/telegram_doc_{safe_name}"
    await file.download_to_drive(save_path)

    caption = update.message.caption or f"The user sent a file: {safe_name}. Analyze it."
    prompt = f'The user sent a file saved at {save_path} (original name: {safe_name}). Their message: "{caption}". Read the file and respond.'

    thinking_msg = await update.message.reply_text("\u280b <b>Processing file...</b>", parse_mode=ParseMode.HTML)
    st.user_request_count[ukey] = st.user_request_count.get(ukey, 0) + 1
    try:
        response = await _run_claude(prompt, ukey, thinking_msg=thinking_msg, context=context, chat_id=update.effective_chat.id)
    except Exception as e:
        response = f"Error: {e}"
    finally:
        await _finish_task(ukey)
        cleanup_temp(save_path)

    try:
        await thinking_msg.delete()
    except Exception:
        pass
    await _send_response(update, response)


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return await _deny(update)
    _set_default_chat(update)
    loc = update.message.location
    ukey = _user_key(update)
    await _send_typing(context, update.effective_chat.id)

    # Update last known location and check geofences
    import math
    prev_loc = st._last_location.copy()
    st._last_location.update({"lat": loc.latitude, "lon": loc.longitude, "ts": time.time()})

    # Check geofences
    fences = load_json(GEOFENCES_FILE, [])
    triggered = []
    for fence in fences:
        # Haversine distance
        lat1, lon1 = math.radians(loc.latitude), math.radians(loc.longitude)
        lat2, lon2 = math.radians(fence["lat"]), math.radians(fence["lon"])
        dlat, dlon = lat2 - lat1, lon2 - lon1
        a = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
        dist_m = 6371000 * 2 * math.asin(math.sqrt(a))
        inside = dist_m <= fence.get("radius_m", 100)

        # Check if we were previously outside (or no prev location)
        was_inside = False
        if prev_loc.get("lat"):
            lat1p, lon1p = math.radians(prev_loc["lat"]), math.radians(prev_loc["lon"])
            dlat_p, dlon_p = lat2 - lat1p, lon2 - lon1p
            a_p = math.sin(dlat_p/2)**2 + math.cos(lat1p)*math.cos(lat2)*math.sin(dlon_p/2)**2
            dist_prev = 6371000 * 2 * math.asin(math.sqrt(a_p))
            was_inside = dist_prev <= fence.get("radius_m", 100)

        trigger = fence.get("trigger", "enter")
        if (trigger in ("enter", "both") and inside and not was_inside) or \
           (trigger in ("exit", "both") and not inside and was_inside):
            triggered.append(fence)

    # Execute geofence actions
    for fence in triggered:
        try:
            action_type = "entered" if "enter" in fence.get("trigger", "enter") else "left"
            resp = await _run_claude(fence["action"], ukey)
            await update.message.reply_text(
                f"📍 <b>Geofence '{E(fence['name'])}'</b> ({action_type}):\n{md_to_html(resp[:3000])}",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error("Geofence action error: %s", e)

    prompt = f"The user shared location: lat={loc.latitude}, lon={loc.longitude}. Acknowledge it."
    if triggered:
        prompt += f" Also, {len(triggered)} geofence(s) triggered: {', '.join(f['name'] for f in triggered)}."
    thinking_msg = await update.message.reply_text("\u280b <b>Got location...</b>", parse_mode=ParseMode.HTML)
    st.user_request_count[ukey] = st.user_request_count.get(ukey, 0) + 1
    try:
        response = await _run_claude(prompt, ukey, thinking_msg=thinking_msg, context=context, chat_id=update.effective_chat.id)
    except Exception as e:
        response = f"Error: {e}"
    finally:
        await _finish_task(ukey)

    try:
        await thinking_msg.delete()
    except Exception:
        pass
    await _send_response(update, response)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err_str = str(context.error).lower()
    # Suppress benign Telegram errors that flood the logs
    if any(s in err_str for s in ("message is not modified", "query is too old", "message to edit not found")):
        return
    logger.error("Exception: %s", context.error, exc_info=context.error)
    # Notify the user — friendly message + technical detail in spoiler
    err_detail = E(str(context.error)[:200])
    msg = (
        f"⚠️ <b>Something went wrong.</b>\n"
        f"<tg-spoiler>Error: <code>{err_detail}</code></tg-spoiler>"
    )
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(msg, parse_mode=ParseMode.HTML)
        elif st._default_chat_id:
            await context.bot.send_message(st._default_chat_id, msg, parse_mode=ParseMode.HTML)
    except Exception:
        pass


