"""Alfred core — shared auth, persistence wrappers, menu builders, status formatting.

Every module imports from here instead of from bot.py, breaking circular dependencies.
"""
from __future__ import annotations

import os
import re
import time
import logging
from pathlib import Path
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

import bot_state as st
from config import (
    ALLOWED_USERS, ALLOWED_USER_IDS, CLAUDE_MODEL,
    SESSIONS_FILE, SCHEDULES_FILE, MACHINES_FILE,
    COST_FILE, MODELS_FILE, ALERTS_FILE, HISTORY_FILE, FORKS_FILE,
    NOTIF_FILE, PINNED_FILE, USER_MACHINES_FILE, DEFAULT_CHAT_FILE,
    REMINDERS_FILE, METRICS_FILE, GEOFENCES_FILE,
    PROJECTS_FILE, ACTIVE_PROJECT_FILE, GLOBAL_ENV_FILE,
    MAX_HISTORY, MENU_CATEGORIES,
)
from persistence import load_json, save_json
from utils.formatting import (
    E, fmt_elapsed, fmt_section, fmt_alert,
)
from utils.ui import (
    progress_bar as ui_progress_bar, sparkline, severity_dot,
    compact_bytes, compact_duration, fmt_time_ago, cron_to_human,
    calculate_health_score, project_status,
    build_back_close, separator_button, dashboard_tabs,
)
from utils.helpers import async_run

logger = logging.getLogger("alfred")
LOG_FILE = Path(__file__).parent / "alfred.log"

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def is_allowed(update) -> bool:
    user = update.effective_user
    if ALLOWED_USER_IDS and user.id in ALLOWED_USER_IDS:
        return True
    if user.username and user.username in ALLOWED_USERS:
        return True
    return False


async def deny(update):
    try:
        await update.message.reply_text("❌ Access denied.")
    except Exception:
        pass


def user_key(update) -> str:
    return f"{update.effective_user.id}:{update.effective_chat.id}"


async def check_cmd_rate(update, cmd: str) -> bool:
    from config import CMD_RATE_LIMITS
    limit = CMD_RATE_LIMITS.get(cmd)
    if not limit:
        return True
    ukey = user_key(update)
    rkey = f"{ukey}:{cmd}"
    now = time.time()
    timestamps = [t for t in st.cmd_rate_timestamps.get(rkey, []) if now - t < 60]
    if len(timestamps) >= limit:
        wait = int(60 - (now - timestamps[0]))
        await update.message.reply_text(f"/{cmd} rate limit: {limit}/min. Try again in ~{wait}s.")
        return False
    timestamps.append(now)
    st.cmd_rate_timestamps[rkey] = timestamps
    return True


def set_default_chat(update):
    if st._default_chat_id is None and is_allowed(update):
        st._default_chat_id = update.effective_chat.id
        logger.info("Default chat ID set to %s", st._default_chat_id)
        save_json(DEFAULT_CHAT_FILE, {"chat_id": st._default_chat_id})


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def add_history(ukey, role, text):
    if ukey not in st.history:
        st.history[ukey] = []
    now = datetime.now()
    st.history[ukey].append({
        "time": now.strftime("%H:%M"),
        "ts": now.isoformat(),
        "role": role,
        "text": text[:200],
    })
    if len(st.history[ukey]) > MAX_HISTORY:
        st.history[ukey] = st.history[ukey][-MAX_HISTORY:]
    save_json(HISTORY_FILE, st.history)


# ---------------------------------------------------------------------------
# Persistence wrappers
# ---------------------------------------------------------------------------
def load_all_state():
    from db import migrate_json_to_db
    migrate_json_to_db({
        "sessions": SESSIONS_FILE, "user_models": MODELS_FILE,
        "cost_tracker": COST_FILE, "alerts": ALERTS_FILE,
        "history": HISTORY_FILE, "forks": FORKS_FILE,
        "notifications": NOTIF_FILE, "pinned": PINNED_FILE,
        "user_machines_sel": USER_MACHINES_FILE, "default_chat": DEFAULT_CHAT_FILE,
        "reminders": REMINDERS_FILE, "schedules": SCHEDULES_FILE,
        "machines": MACHINES_FILE, "metrics": METRICS_FILE,
        "geofences": GEOFENCES_FILE,
        "projects": PROJECTS_FILE, "active_project": ACTIVE_PROJECT_FILE,
        "global_env": GLOBAL_ENV_FILE,
    })
    st.user_sessions = load_json(SESSIONS_FILE)
    st.user_models = load_json(MODELS_FILE)
    st.cost_tracker = load_json(COST_FILE)
    st.alerts = load_json(ALERTS_FILE)
    st.history = load_json(HISTORY_FILE)
    st.forks = load_json(FORKS_FILE)
    st.notification_enabled = load_json(NOTIF_FILE)
    st.pinned_status = {int(k): v for k, v in load_json(PINNED_FILE).items()}
    st.user_machines = load_json(USER_MACHINES_FILE)
    st.pending_reminders = load_json(REMINDERS_FILE)
    st.projects = load_json(PROJECTS_FILE)
    st.active_project = load_json(ACTIVE_PROJECT_FILE)
    st.global_env = load_json(GLOBAL_ENV_FILE)
    saved_chat = load_json(DEFAULT_CHAT_FILE)
    if saved_chat.get("chat_id"):
        st._default_chat_id = saved_chat["chat_id"]


def save_sessions():
    save_json(SESSIONS_FILE, st.user_sessions)


def save_projects():
    save_json(PROJECTS_FILE, st.projects)
    save_json(ACTIVE_PROJECT_FILE, st.active_project)


def load_machines() -> dict:
    return load_json(MACHINES_FILE)


def save_machines(machines: dict):
    save_json(MACHINES_FILE, machines)


# ---------------------------------------------------------------------------
# System status
# ---------------------------------------------------------------------------
async def get_system_status():
    _, out, _ = await async_run([
        "bash", "-c",
        'echo "HOST:$(hostname)"; '
        'echo "UPTIME:$(uptime | sed \'s/.*up //;s/,.*load.*//\' | xargs)"; '
        'echo "DISK_PCT:$(df / | tail -1 | awk \'{print $5}\' | tr -d \"%\")"; '
        'echo "DISK_FREE:$(df -h / | tail -1 | awk \'{print $4 " free / " $2}\')"; '
        'echo "MEM_FREE_MB:$(vm_stat | awk \'/Pages free/{free=$3} /Pages inactive/{inact=$3} END{printf "%d", (free+inact)*4096/1048576}\')"; '
        'echo "MEM_TOTAL_MB:$(sysctl -n hw.memsize | awk \'{printf "%d", $1/1048576}\')"; '
        'echo "CPU_PCT:$(top -l 2 -n 0 | grep "CPU usage" | tail -1 | awk \'{print $3}\' | tr -d \"%\")"; '
        'echo "IP:$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo N/A)"'
    ])
    info = {}
    for line in out.strip().splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            info[k.strip()] = v.strip()
    return info


def fmt_status(info: dict, ukey: str, uptime_secs: int) -> str:
    hostname = info.get("HOST", "unknown")
    uptime_mac = info.get("UPTIME", "?")
    disk_pct = info.get("DISK_PCT", "0")
    disk_free = info.get("DISK_FREE", "?")
    ip = info.get("IP", "N/A")
    cpu_raw = info.get("CPU_PCT", "0").split()[0]

    try:
        disk_p = int(disk_pct)
    except ValueError:
        disk_p = 0
    try:
        mem_free = int(info.get("MEM_FREE_MB", "0"))
        mem_total = int(info.get("MEM_TOTAL_MB", "1"))
        mem_used = mem_total - mem_free
        mem_pct = int(mem_used / mem_total * 100) if mem_total else 0
    except ValueError:
        mem_used, mem_total, mem_pct = 0, 0, 0
    try:
        cpu_p = float(cpu_raw)
    except ValueError:
        cpu_p = 0.0

    cpu_bar = ui_progress_bar(cpu_p)
    mem_bar = ui_progress_bar(mem_pct)

    cpu_spark = ""
    if st.metrics_history:
        cpu_vals = [m.get("cpu", 0) for m in st.metrics_history[-24:]]
        if cpu_vals:
            cpu_spark = f"\n\n<b>▸ CPU 24h</b>\n<code>{sparkline(cpu_vals)}</code>"

    health_score, health_label = calculate_health_score(cpu_p, mem_pct, disk_p)
    model = st.user_models.get(ukey) or CLAUDE_MODEL or "default"
    machine = st.user_machines.get(ukey, "local")
    uptime_alfred = fmt_elapsed(uptime_secs)

    alerts = ""
    if disk_p >= 90:
        alerts += f"\n\n{fmt_alert('⚠ Disk usage critical — ' + disk_free)}"
    if mem_pct >= 90:
        alerts += f"\n\n{fmt_alert('⚠ Memory usage high — ' + str(mem_used) + 'MB / ' + str(mem_total) + 'MB')}"

    return (
        f"{fmt_section(hostname)}\n"
        f"<code>{severity_dot(cpu_p)} CPU  {cpu_bar} {cpu_p:>3.0f}%</code>\n"
        f"<code>{severity_dot(mem_pct)} MEM  {mem_bar} {mem_pct:>3.0f}%  {mem_used}M/{mem_total}M</code>\n"
        f"<code>{severity_dot(disk_p)} DISK {ui_progress_bar(disk_p)} {disk_p:>3.0f}%  {disk_free}</code>"
        f"{cpu_spark}"
        f"{alerts}\n\n"
        f"🏥 Health: <b>{health_score}/100</b> {health_label}\n"
        f"🤖 <code>{E(model)}</code> · 🖥 <code>{E(machine)}</code> · 🌐 <code>{E(ip)}</code>\n"
        f"<i>{fmt_elapsed(uptime_secs)} up · Mac {E(uptime_mac)}</i>"
    )


def status_keyboard():
    return InlineKeyboardMarkup([
        dashboard_tabs("system", [("System", "system"), ("Procs", "procs"), ("Net", "net")], prefix="dash"),
        [
            InlineKeyboardButton("↻ Refresh", callback_data="quick_status"),
            InlineKeyboardButton("📌 Pin", callback_data="pin_status"),
        ],
        build_back_close(),
    ])


# ---------------------------------------------------------------------------
# Menu builders
# ---------------------------------------------------------------------------
def build_main_menu():
    rows = [
        [
            InlineKeyboardButton("📸 Screenshot", callback_data="quick_screenshot"),
            InlineKeyboardButton("📊 Status", callback_data="quick_status"),
        ],
        separator_button("─── Categories ───"),
    ]
    row = []
    for key, label, _ in MENU_CATEGORIES:
        row.append(InlineKeyboardButton(label, callback_data=f"menu:{key}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def build_sub_menu(category_key: str):
    for key, label, items in MENU_CATEGORIES:
        if key == category_key:
            rows = []
            row = []
            for item_label, callback in items:
                row.append(InlineKeyboardButton(item_label, callback_data=callback))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append(build_back_close())
            return InlineKeyboardMarkup(rows), label
    return None, None


def build_back_button(target="menu:main"):
    return InlineKeyboardMarkup([build_back_close(target)])


# ---------------------------------------------------------------------------
# Settings & automations text builders
# ---------------------------------------------------------------------------
async def build_settings_text(ukey: str) -> str:
    model_raw = st.user_models.get(ukey) or CLAUDE_MODEL or "default"
    _aliases = {"claude-opus-4-6": "Opus", "claude-sonnet-4-6": "Sonnet", "claude-haiku-4-5-20251001": "Haiku"}
    model_short = _aliases.get(model_raw, model_raw.split("-")[1].capitalize() if "-" in model_raw else model_raw)
    machine = st.user_machines.get(ukey, "local")
    all_scheds = load_json(SCHEDULES_FILE, [])
    sched_list = [s for s in all_scheds if s.get("user_key") == ukey]
    sched_str = f"{len(sched_list)} active" if sched_list else "none"
    remind_list = st.pending_reminders.get(ukey, [])
    remind_str = f"{len(remind_list)} pending" if remind_list else "none"
    return (
        f"<b>⚙️ Alfred Settings</b>\n\n"
        f"🤖 <b>AI Model:</b> {E(model_short)}\n"
        f"🖥 <b>Machine:</b> <code>{E(machine)}</code>\n\n"
        f"<b>Automation</b>\n"
        f"🕐 Schedules: {sched_str}\n"
        f"⏰ Reminders: {remind_str}"
    )


def build_automations_text(ukey: str) -> str:
    now = time.time()
    parts = [fmt_section("AUTOMATIONS")]

    all_scheds = load_json(SCHEDULES_FILE, [])
    user_scheds = [s for s in all_scheds if s.get("user_key") == ukey]
    if user_scheds:
        lines = []
        for i, s in enumerate(user_scheds):
            enabled = s.get("enabled", True)
            dot = "🟢" if enabled else "⚫"
            human_cron = cron_to_human(s.get("cron", ""))
            next_str = ""
            if enabled and HAS_CRONITER and re.match(r'^[\d\*\/\-\,\s]+$', s.get("cron", "")):
                try:
                    last_run = s.get("last_run")
                    last_dt = datetime.fromisoformat(last_run) if last_run else datetime.now()
                    nxt = croniter(s["cron"], last_dt).get_next(datetime)
                    diff = int((nxt - datetime.now()).total_seconds())
                    next_str = f"  next: {compact_duration(max(0, diff))}"
                except Exception:
                    pass
            task_short = s['task'][:30] + ('...' if len(s['task']) > 30 else '')
            lines.append(f"<code>{dot} {E(task_short)}</code>\n<code>   {E(human_cron)}{next_str}</code>")
        parts.append(f"\n<b>▸ Scheduled ({len(user_scheds)})</b>\n" + "\n".join(lines))
    else:
        parts.append("\n<b>▸ Scheduled</b> — none")

    user_alerts = st.alerts.get(ukey, [])
    if user_alerts:
        lines = [f"<code>🔔 {E(a.get('desc', a.get('type', '?'))[:35])}</code>" for a in user_alerts]
        parts.append(f"\n<b>▸ Alerts ({len(user_alerts)})</b>\n" + "\n".join(lines))
    else:
        parts.append("\n<b>▸ Alerts</b> — none")

    user_rems = [r for r in st.pending_reminders.get(ukey, []) if r["fire_time"] > now]
    if user_rems:
        lines = []
        for r in sorted(user_rems, key=lambda x: x["fire_time"]):
            remaining = int(r["fire_time"] - now)
            lines.append(f"<code>⏰ {E(r['text'][:30])}  in {compact_duration(remaining)}</code>")
        parts.append(f"\n<b>▸ Reminders ({len(user_rems)})</b>\n" + "\n".join(lines))
    else:
        parts.append("\n<b>▸ Reminders</b> — none")

    notif = "✅ on" if st.notification_enabled.get(ukey, False) else "❌ off"
    parts.append(f"\n🔕 Notifications: {notif}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Help categories & buttons
# ---------------------------------------------------------------------------
HELP_CATEGORIES: dict[str, tuple[str, str]] = {
    "screen": ("📺 Screen", (
        "  /screenshot — take a screenshot\n"
        "  /record [sec] — record screen (max 60s)\n"
        "  /watch [sec] — live screen stream (toggle)\n"
        "  /camera — webcam photo"
    )),
    "system": ("🖥 System", (
        "  /status — system dashboard + health score\n"
        "  /clipboard [text] — get or set clipboard\n"
        "  /paste <text> — copy text to clipboard\n"
        "  /apps — launch apps\n"
        "  /processes [n] [cpu|mem] — top processes\n"
        "  /volume [0-100] — get or set volume\n"
        "  /logs [n] [filter] — view bot logs\n"
        "  /machine [add|remove|local] — manage machines\n"
        "  /wake <machine> — Wake-on-LAN\n"
        "  /guardian — self-healing system monitor"
    )),
    "files": ("📁 Files", (
        "  /browse [path] — interactive file browser\n"
        "  /search <query> [path] — find files & content"
    )),
    "ai": ("🤖 AI", (
        "  /clear — start new conversation\n"
        "  /clearhistory — wipe history log\n"
        "  /model opus|sonnet|haiku — switch model\n"
        "  /undo — revert last action\n"
        "  /fork save|load|delete <name> — branches\n"
        "  /history [n] — recent exchanges\n"
        "  /export — download conversation\n"
        "  /research <topic> — 15-agent deep research"
    )),
    "auto": ("⚙️ Automation", (
        "  /automations — unified dashboard\n"
        '  /schedule "expr" "task" — cron tasks\n'
        "  /remind 10m <note> — one-shot reminder\n"
        "  /remind list — pending reminders\n"
        "  /remind cancel <id> — cancel a reminder\n"
        "  /timer 5m [label] — countdown timer"
    )),
    "web": ("🌐 Web Browser", (
        "  /web <url> — open a web page\n"
        "  /web snapshot — list interactive elements\n"
        "  /web click <ref> — click an element\n"
        "  /web type <ref> <text> — type into a field\n"
        "  /web key Enter|Tab — press a key\n"
        "  /web scroll [up|down] — scroll the page\n"
        "  /web text — get page text content\n"
        "  /web screenshot — capture current page\n"
        "  /web close — close browser session"
    )),
    "integrations": ("🔗 Integrations", (
        "  /gmail — read inbox (5 recent)\n"
        "  /gmail read [n] — read N emails\n"
        "  /gmail search <query> — search emails\n"
        "  /gmail send to@email Subject | Body\n"
        "  /memory — list memories\n"
        "  /memory add <text> — remember something\n"
        "  /memory search <query> — find a memory"
    )),
    "utils": ("🛠 Utilities", (
        "  /tts [-v voice] <text> — text to speech\n"
        "  /terminal <cmd> — run shell command live\n"
        "  /settings — unified settings panel\n"
        "  /cancel — stop running task\n"
        "  /reload — hot-reload plugins"
    )),
}

HELP_CAT_BUTTONS = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("📺", callback_data="help_cat:screen"),
        InlineKeyboardButton("🖥", callback_data="help_cat:system"),
        InlineKeyboardButton("📁", callback_data="help_cat:files"),
    ],
    [
        InlineKeyboardButton("🤖", callback_data="help_cat:ai"),
        InlineKeyboardButton("⚙️", callback_data="help_cat:auto"),
        InlineKeyboardButton("🌐", callback_data="help_cat:web"),
        InlineKeyboardButton("🔗", callback_data="help_cat:integrations"),
        InlineKeyboardButton("🛠", callback_data="help_cat:utils"),
    ],
    [InlineKeyboardButton("📋 All", callback_data="help_cat:all")],
    [InlineKeyboardButton("← Menu", callback_data="menu:main")],
])
