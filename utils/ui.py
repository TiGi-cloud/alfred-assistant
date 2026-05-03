"""UI utility functions for Alfred Telegram bot — keyboards, visual indicators, formatting."""
from __future__ import annotations

import os
import re
import time
import hashlib
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


# ---------------------------------------------------------------------------
# Visual indicators
# ---------------------------------------------------------------------------

def progress_bar(pct: float, width: int = 10, fill: str = '▓', empty: str = '░') -> str:
    """Render a Unicode progress bar."""
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    return fill * filled + empty * (width - filled)


def sparkline(values: list, width: int = 24) -> str:
    """Render a list of numbers as a sparkline string."""
    chars = '▁▂▃▄▅▆▇█'
    if not values:
        return ''
    recent = values[-width:]
    mn, mx = min(recent), max(recent)
    rng = mx - mn
    if rng == 0:
        return chars[4] * len(recent)
    return ''.join(chars[min(int((v - mn) / rng * 7), 7)] for v in recent)


def severity_dot(pct: float, warn: int = 60, high: int = 80, crit: int = 95) -> str:
    """Return a colored dot emoji based on threshold."""
    if pct >= crit:
        return '🔴'
    if pct >= high:
        return '🟠'
    if pct >= warn:
        return '🟡'
    return '🟢'


def status_dot(is_active: bool) -> str:
    """Return a filled or empty dot."""
    return '●' if is_active else '○'


def run_streak(history: list, count: int = 12) -> str:
    """Render last N run results as ✅/❌ streak."""
    recent = history[-count:]
    return ''.join('✅' if r.get('success', r.get('result') == 'success') else '❌' for r in recent)


def trend_arrow(current: float, previous: float) -> str:
    """Return ↑ ↓ or → based on change."""
    diff = current - previous
    if abs(diff) < 1:
        return '→'
    return '↑' if diff > 0 else '↓'


# ---------------------------------------------------------------------------
# Compact formatters
# ---------------------------------------------------------------------------

def compact_bytes(b: float) -> str:
    """Format bytes as compact string (e.g., 1.2G)."""
    for unit in ['B', 'K', 'M', 'G', 'T']:
        if b < 1024:
            return f"{b:.1f}{unit}" if b != int(b) else f"{int(b)}{unit}"
        b /= 1024
    return f"{b:.1f}P"


def compact_duration(seconds: int) -> str:
    """Format seconds as compact duration (e.g., 2d14h, 3h22m)."""
    if seconds < 0:
        return "0s"
    if seconds >= 86400:
        return f"{seconds // 86400}d{(seconds % 86400) // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds}s"


def compact_num(n: float) -> str:
    """Format number compactly (e.g., 1.2K, 3.4M)."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def fmt_time_ago(ts: float) -> str:
    """Format a timestamp as relative time (e.g., '2h ago', 'just now')."""
    if not ts:
        return 'never'
    diff = int(time.time() - ts)
    if diff < 60:
        return 'just now'
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def cron_to_human(expr: str) -> str:
    """Translate common cron patterns to readable text."""
    shortcuts = {
        "* * * * *": "Every minute",
        "*/5 * * * *": "Every 5 min",
        "*/10 * * * *": "Every 10 min",
        "*/15 * * * *": "Every 15 min",
        "*/30 * * * *": "Every 30 min",
        "0 * * * *": "Every hour",
        "0 */2 * * *": "Every 2 hours",
        "0 */4 * * *": "Every 4 hours",
        "0 */6 * * *": "Every 6 hours",
        "0 */12 * * *": "Every 12 hours",
    }
    if expr in shortcuts:
        return shortcuts[expr]

    parts = expr.split()
    if len(parts) != 5:
        return expr

    minute, hour, dom, month, dow = parts
    days_map = {
        "0": "Sun", "1": "Mon", "2": "Tue", "3": "Wed",
        "4": "Thu", "5": "Fri", "6": "Sat", "7": "Sun",
    }

    def _fmt_time(h: str, m: str) -> str:
        hi, mi = int(h), int(m)
        period = "AM" if hi < 12 else "PM"
        dh = hi % 12 or 12
        return f"{dh}:{mi:02d} {period}"

    # "0 3 * * *" → "Daily at 3:00 AM"
    if dom == "*" and month == "*" and dow == "*" and minute.isdigit() and hour.isdigit():
        return f"Daily at {_fmt_time(hour, minute)}"

    # "0 9 * * 1" → "Mon at 9:00 AM"
    if dom == "*" and month == "*" and dow in days_map and minute.isdigit() and hour.isdigit():
        return f"{days_map[dow]} at {_fmt_time(hour, minute)}"

    # "0 9 * * 1-5" → "Weekdays at 9:00 AM"
    if dom == "*" and month == "*" and dow == "1-5" and minute.isdigit() and hour.isdigit():
        return f"Weekdays at {_fmt_time(hour, minute)}"

    # "0 0 1 * *" → "1st of every month"
    if month == "*" and dow == "*" and dom.isdigit():
        return f"Day {dom} monthly"

    return expr  # Fallback to raw


# ---------------------------------------------------------------------------
# Health score
# ---------------------------------------------------------------------------

def calculate_health_score(cpu: float, ram: float, disk: float, services_down: int = 0) -> tuple[int, str]:
    """Calculate composite health score 0-100 and label."""
    s_cpu = max(0, 100 - (max(cpu - 30, 0) / 70 * 100))
    s_ram = max(0, 100 - (max(ram - 50, 0) / 50 * 100))
    if disk < 60:
        s_disk = 100
    elif disk < 90:
        s_disk = 100 - ((disk - 60) / 30 * 60)
    else:
        s_disk = max(0, 40 - ((disk - 90) / 10 * 40))
    s_svc = max(0, 100 - services_down * 30)

    weights = {'cpu': 25, 'ram': 25, 'disk': 20, 'svc': 30}
    scores = {'cpu': s_cpu, 'ram': s_ram, 'disk': s_disk, 'svc': s_svc}
    total = sum(scores[k] * weights[k] for k in weights)
    score = round(total / sum(weights.values()))

    if score >= 90:
        label = "💚 Great"
    elif score >= 70:
        label = "💛 Good"
    elif score >= 50:
        label = "🟠 Fair"
    elif score >= 30:
        label = "🔴 Poor"
    else:
        label = "🚨 Critical"
    return score, label


# ---------------------------------------------------------------------------
# Project status
# ---------------------------------------------------------------------------

def project_status(proj: dict) -> tuple[str, str]:
    """Return (icon, label) for a project based on activity and state."""
    last = proj.get("last_used", proj.get("created", 0))
    age = time.time() - last if last else float('inf')
    has_pending = bool(proj.get("pending", ""))

    if has_pending and age > 86400:
        return ("🔴", "blocked")
    if has_pending:
        return ("🟡", "pending")
    if age < 3600:
        return ("🟢", "active")
    if age < 86400:
        return ("⚪", "idle")
    if age < 86400 * 7:
        return ("💤", "dormant")
    return ("🪦", "stale")


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def paginated_keyboard(
    items: list,
    page: int,
    per_page: int = 5,
    item_cb=None,
    nav_prefix: str = "page",
    extra_rows: list | None = None,
) -> InlineKeyboardMarkup:
    """Generic paginator. item_cb(item, index) -> InlineKeyboardButton or list of buttons."""
    total_pages = max(1, (len(items) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = items[start: start + per_page]

    rows = []
    for i, item in enumerate(chunk):
        btn = item_cb(item, start + i)
        if isinstance(btn, list):
            rows.append(btn)
        else:
            rows.append([btn])

    # Navigation row
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("‹ Prev", callback_data=f"{nav_prefix}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ›", callback_data=f"{nav_prefix}:{page + 1}"))
        rows.append(nav)

    if extra_rows:
        rows.extend(extra_rows)
    return InlineKeyboardMarkup(rows)


def toggle_button(label: str, key: str, is_on: bool) -> InlineKeyboardButton:
    """Create a toggle button showing on/off state."""
    icon = "✅" if is_on else "⬜"
    return InlineKeyboardButton(f"{icon} {label}", callback_data=f"tg:{key}")


def build_back_close(back_target: str = "menu:main") -> list:
    """Return a row with Back and Close buttons."""
    return [
        InlineKeyboardButton("← Back", callback_data=back_target),
        InlineKeyboardButton("✖ Close", callback_data="close_menu"),
    ]


def separator_button(label: str = "───") -> list:
    """Return a visual separator row (noop button)."""
    return [InlineKeyboardButton(label, callback_data="noop")]


def dashboard_tabs(active_tab: str, tabs: list[tuple[str, str]], prefix: str = "tab") -> list:
    """Build a tab row. tabs = [(label, key), ...]. Returns a button row."""
    buttons = []
    for label, key in tabs:
        display = f"·{label}·" if key == active_tab else label
        buttons.append(InlineKeyboardButton(display, callback_data=f"{prefix}:{key}"))
    return buttons


# ---------------------------------------------------------------------------
# File browser helpers
# ---------------------------------------------------------------------------

ICON_MAP = {
    '.png': '🖼', '.jpg': '🖼', '.jpeg': '🖼', '.gif': '🖼',
    '.webp': '🖼', '.heic': '🖼', '.bmp': '🖼', '.svg': '🖼',
    '.mp4': '🎬', '.mov': '🎬', '.avi': '🎬', '.mkv': '🎬', '.webm': '🎬',
    '.mp3': '🎵', '.ogg': '🎵', '.wav': '🎵', '.m4a': '🎵', '.flac': '🎵',
    '.pdf': '📕',
    '.zip': '📦', '.tar': '📦', '.gz': '📦', '.rar': '📦', '.7z': '📦',
    '.py': '🐍', '.js': '🟨', '.ts': '🔷', '.go': '🔵', '.rs': '🦀',
    '.swift': '🧡', '.java': '☕', '.rb': '💎',
    '.html': '🌐', '.css': '🎨', '.scss': '🎨',
    '.sql': '🗃', '.db': '🗃', '.sqlite': '🗃',
    '.env': '🔒', '.pem': '🔑', '.key': '🔑',
    '.sh': '⚡', '.bash': '⚡', '.zsh': '⚡',
    '.json': '📋', '.yaml': '📋', '.yml': '📋', '.toml': '📋',
    '.md': '📝', '.txt': '📝', '.log': '📜', '.csv': '📊',
    '.xls': '📊', '.xlsx': '📊', '.doc': '📘', '.docx': '📘',
    '.dmg': '💿', '.iso': '💿', '.app': '🚀',
    '.c': '🔧', '.cpp': '🔧', '.h': '🔧',
}

FILENAME_ICONS = {
    'Dockerfile': '🐳', 'Makefile': '🔨', '.gitignore': '🙈',
    'LICENSE': '📜', 'README.md': '📖', 'package.json': '📦',
    'Cargo.toml': '🦀', 'go.mod': '🔵', '.env': '🔒',
}


def file_icon(name: str) -> str:
    """Get an emoji icon for a file by name or extension."""
    if name in FILENAME_ICONS:
        return FILENAME_ICONS[name]
    ext = os.path.splitext(name)[1].lower()
    return ICON_MAP.get(ext, '📄')


def path_hash(path: str) -> str:
    """Generate a short hash for a file path (for callback_data)."""
    return hashlib.sha256(path.encode()).hexdigest()[:8]


def build_breadcrumbs(path: str, home: str = None) -> list[list]:
    if home is None:
        home = os.path.expanduser("~")
    """Build breadcrumb button rows from a path."""
    display = path.replace(home, "~")
    parts = [p for p in display.split("/") if p]
    buttons = []
    for i, part in enumerate(parts):
        if parts[0] == "~":
            real = home + "/" + "/".join(parts[1:i + 1]) if i > 0 else home
        else:
            real = "/" + "/".join(parts[:i + 1])
        label = part if len(part) <= 12 else part[:10] + ".."
        cb = f"browse:{real}" if len(real) <= 55 else f"browse_h:{path_hash(real)}"
        buttons.append(InlineKeyboardButton(label, callback_data=cb))
    # Split into rows of 4
    return [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
