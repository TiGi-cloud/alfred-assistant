from __future__ import annotations

import re
import asyncio
import time
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

from core import is_allowed, deny, user_key, build_settings_text, build_automations_text, build_back_button
from utils.formatting import E, fmt_elapsed, fmt_section
from utils.ui import compact_duration, cron_to_human, build_back_close
from utils.helpers import parse_natural_schedule
from persistence import load_json, save_json
import bot_state as st
from config import SCHEDULES_FILE, REMINDERS_FILE


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    if not context.args:
        all_scheds = load_json(SCHEDULES_FILE, [])
        ukey = user_key(update)
        # Show only this user's schedules
        user_scheds = [(i, s) for i, s in enumerate(all_scheds) if s.get("user_key") == ukey]
        if user_scheds:
            lines = []
            for local_idx, (_, s) in enumerate(user_scheds):
                next_str = ""
                if HAS_CRONITER and re.match(r'^[\d\*\/\-\,\s]+$', s.get("cron", "")):
                    try:
                        last_run = s.get("last_run")
                        last_dt = datetime.fromisoformat(last_run) if last_run else datetime.now()
                        nxt = croniter(s["cron"], last_dt).get_next(datetime)
                        next_str = f" (next: {nxt.strftime('%H:%M %d/%m')})"
                    except Exception:
                        pass
                lines.append(f"  {local_idx+1}. <code>{E(s['cron'])}</code> -- {E(s['task'])}{next_str}")
            msg = f"<b>Your scheduled tasks:</b>\n" + "\n".join(lines)
        else:
            msg = (
                "<b>No scheduled tasks.</b>\n\n"
                '<code>/schedule "every hour" "check disk"</code>\n'
                '<code>/schedule "*/5 * * * *" "check nginx"</code>\n'
                "<code>/schedule remove 1</code>"
            )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=build_back_button())
        return

    args_text = " ".join(context.args)
    if args_text.startswith("remove "):
        try:
            remove_idx = int(args_text.split()[1]) - 1
            all_scheds = load_json(SCHEDULES_FILE, [])
            ukey = user_key(update)
            user_scheds = [(i, s) for i, s in enumerate(all_scheds) if s.get("user_key") == ukey]
            if remove_idx < 0 or remove_idx >= len(user_scheds):
                raise IndexError("Invalid index")
            global_idx, removed = user_scheds[remove_idx]
            all_scheds.pop(global_idx)
            save_json(SCHEDULES_FILE, all_scheds)
            await update.message.reply_text(f"Removed: {E(removed['task'])}")
        except Exception as e:
            await update.message.reply_text(f"Error: {E(str(e))}")
        return

    parts = re.findall(r'"([^"]*)"', args_text)
    if len(parts) < 2:
        await update.message.reply_text(
            'Usage: <code>/schedule "every hour" "check disk"</code>',
            parse_mode=ParseMode.HTML,
        )
        return

    cron_expr = parse_natural_schedule(parts[0])
    if HAS_CRONITER and re.match(r'^[\d\*\/\-\,\s]+$', cron_expr):
        try:
            croniter(cron_expr)
        except Exception:
            await update.message.reply_text(f"Invalid cron: <code>{E(cron_expr)}</code>", parse_mode=ParseMode.HTML)
            return

    schedules = load_json(SCHEDULES_FILE, [])
    schedules.append({
        "cron": cron_expr, "task": parts[1],
        "chat_id": update.effective_chat.id,
        "user_key": user_key(update), "last_run": None,
    })
    save_json(SCHEDULES_FILE, schedules)
    await update.message.reply_text(
        f"Scheduled: <code>{E(cron_expr)}</code> -- {E(parts[1])}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /remind 10m Check laundry  |  /remind list  |  /remind cancel <id>"""
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)

    if not context.args:
        return await update.message.reply_text(
            "<b>Reminder</b>\n\n"
            "<code>/remind 10m Check laundry</code>\n"
            "<code>/remind 2h Call John</code>\n"
            "<code>/remind 1d Submit report</code>\n"
            "<code>/remind list</code> — see pending reminders\n"
            "<code>/remind cancel &lt;id&gt;</code> — cancel a reminder\n\n"
            "Supports: 30s, 5m, 2h, 1d (max 7 days)",
            parse_mode=ParseMode.HTML,
        )

    if context.args[0].lower() == "list":
        user_rems = st.pending_reminders.get(ukey, [])
        now = time.time()
        active = [r for r in user_rems if r["fire_time"] > now]
        if not active:
            return await update.message.reply_text("No pending reminders. Set one with <code>/remind 10m text</code>", parse_mode=ParseMode.HTML)
        lines = []
        for r in active:
            remaining = int(r["fire_time"] - now)
            lines.append(f"  <b>{r['id']}</b>. <i>{E(r['text'])}</i> — in {fmt_elapsed(remaining)}")
        await update.message.reply_text(
            "<b>⏰ Pending reminders:</b>\n" + "\n".join(lines) +
            "\n\n<code>/remind cancel &lt;id&gt;</code>",
            parse_mode=ParseMode.HTML, reply_markup=build_back_button(),
        )
        return

    if context.args[0].lower() == "cancel" and len(context.args) > 1:
        try:
            rid = int(context.args[1])
        except ValueError:
            return await update.message.reply_text("Usage: <code>/remind cancel &lt;id&gt;</code>", parse_mode=ParseMode.HTML)
        user_rems = st.pending_reminders.get(ukey, [])
        orig_len = len(user_rems)
        st.pending_reminders[ukey] = [r for r in user_rems if r["id"] != rid]
        save_json(REMINDERS_FILE, st.pending_reminders)
        if len(st.pending_reminders[ukey]) < orig_len:
            await update.message.reply_text(f"Reminder {rid} cancelled.")
        else:
            await update.message.reply_text(f"Reminder {rid} not found.")
        return

    if len(context.args) < 2:
        return await update.message.reply_text(
            "Usage: <code>/remind 10m Check laundry</code>",
            parse_mode=ParseMode.HTML,
        )

    raw = context.args[0].lower()
    m = re.match(r'^(\d+)(s|m|h|d)$', raw)
    if not m:
        return await update.message.reply_text("Invalid time. Use e.g. <code>10m</code>, <code>2h</code>", parse_mode=ParseMode.HTML)
    amount = int(m.group(1))
    unit = m.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if seconds > 86400 * 7:
        return await update.message.reply_text("Max reminder duration is 7 days.")
    note = " ".join(context.args[1:])
    chat_id = update.effective_chat.id
    fire_time = time.time() + seconds
    fire_dt = datetime.fromtimestamp(fire_time)

    # Assign reminder ID (next sequential ID for user)
    user_rems = st.pending_reminders.get(ukey, [])
    rid = max((r["id"] for r in user_rems), default=0) + 1
    user_rems.append({"id": rid, "text": note, "fire_time": fire_time, "chat_id": chat_id})
    st.pending_reminders[ukey] = user_rems
    save_json(REMINDERS_FILE, st.pending_reminders)

    await update.message.reply_text(
        f"⏰ Reminder <b>#{rid}</b> set for <b>{E(raw)}</b> (at {fire_dt.strftime('%H:%M')})\n"
        f"<i>{E(note)}</i>\n\n"
        f"<code>/remind cancel {rid}</code> to cancel",
        parse_mode=ParseMode.HTML,
    )

    async def _fire():
        await asyncio.sleep(seconds)
        try:
            snooze_kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💤 +5m", callback_data=f"remind_snooze:{ukey}:{rid}:{note[:40]}:5"),
                InlineKeyboardButton("💤 +15m", callback_data=f"remind_snooze:{ukey}:{rid}:{note[:40]}:15"),
                InlineKeyboardButton("✅ Done", callback_data=f"remind_done:{rid}"),
            ]])
            await context.bot.send_message(
                chat_id,
                f"⏰ <b>Reminder #{rid}</b>\n{E(note)}",
                parse_mode=ParseMode.HTML,
                reply_markup=snooze_kb,
            )
        except Exception:
            pass
        finally:
            # Remove from pending list
            if ukey in st.pending_reminders:
                st.pending_reminders[ukey] = [r for r in st.pending_reminders[ukey] if r["id"] != rid]
                save_json(REMINDERS_FILE, st.pending_reminders)

    t = asyncio.create_task(_fire())
    st.reminder_tasks.add(t)
    t.add_done_callback(st.reminder_tasks.discard)


async def cmd_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Countdown timer: /timer 5m"""
    if not is_allowed(update):
        return await deny(update)
    if not context.args:
        return await update.message.reply_text(
            "Usage: <code>/timer 5m</code>  (supports s/m/h)", parse_mode=ParseMode.HTML
        )
    raw = context.args[0].lower()
    m = re.match(r'^(\d+)(s|m|h)$', raw)
    if not m:
        return await update.message.reply_text("Invalid time. Use e.g. <code>30s</code>, <code>5m</code>, <code>1h</code>", parse_mode=ParseMode.HTML)
    amount = int(m.group(1))
    unit = m.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600}[unit]
    if seconds > 3600 * 4:
        return await update.message.reply_text("Max timer is 4 hours.")
    label = " ".join(context.args[1:]) or "Timer"
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"\u23f1 <b>{E(label)}</b> started ({E(raw)})", parse_mode=ParseMode.HTML
    )
    async def _run():
        await asyncio.sleep(seconds)
        try:
            await context.bot.send_message(
                chat_id, f"\u2705 <b>{E(label)}</b> done! ({E(raw)})", parse_mode=ParseMode.HTML
            )
        except Exception:
            pass
    t = asyncio.create_task(_run())
    st.reminder_tasks.add(t)
    t.add_done_callback(st.reminder_tasks.discard)


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Unified settings panel showing all current configuration at a glance."""
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    text = await build_settings_text(ukey)
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
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_automations(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a unified dashboard of all active automations for the current user."""
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    text = build_automations_text(ukey)
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Schedules", callback_data="act:schedule_list"),
            InlineKeyboardButton("⏰ Reminders", callback_data="act:remind_list"),
        ],
        [
            InlineKeyboardButton("⏱ Timer", callback_data="act:timer_picker"),
            InlineKeyboardButton("↻ Refresh", callback_data="act:automations_refresh"),
        ],
        build_back_close(),
    ])
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
