"""Alfred configuration — environment variables, paths, and constants."""

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_USERS = [u.strip() for u in os.environ.get("ALLOWED_USERS", "").split(",") if u.strip()]
ALLOWED_USER_IDS = []
for _uid in os.environ.get("ALLOWED_USER_IDS", "").split(","):
    _uid = _uid.strip()
    if _uid:
        try:
            ALLOWED_USER_IDS.append(int(_uid))
        except ValueError:
            pass
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")  # Mini App HTTPS URL (optional)
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")  # Default model override (e.g. claude-sonnet-4-6)

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is required")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BOT_DIR = Path(__file__).parent
DATA_DIR = BOT_DIR  # Alias used by plugin context
SESSIONS_FILE = BOT_DIR / "sessions.json"
SCHEDULES_FILE = BOT_DIR / "schedules.json"
MACHINES_FILE = BOT_DIR / "machines.json"
COST_FILE = BOT_DIR / "cost_tracker.json"
MODELS_FILE = BOT_DIR / "user_models.json"
ALERTS_FILE = BOT_DIR / "alerts.json"
HISTORY_FILE = BOT_DIR / "history.json"
FORKS_FILE = BOT_DIR / "forks.json"
NOTIF_FILE = BOT_DIR / "notifications.json"
PINNED_FILE = BOT_DIR / "pinned.json"
USER_MACHINES_FILE = BOT_DIR / "user_machines_sel.json"
DEFAULT_CHAT_FILE = BOT_DIR / "default_chat.json"
REMINDERS_FILE = BOT_DIR / "reminders.json"
METRICS_FILE = BOT_DIR / "metrics.json"
GEOFENCES_FILE = BOT_DIR / "geofences.json"
PROJECTS_FILE = BOT_DIR / "projects.json"
ACTIVE_PROJECT_FILE = BOT_DIR / "active_project.json"
GLOBAL_ENV_FILE = BOT_DIR / "global_env.json"
WEBAPP_PIN_FILE = BOT_DIR / "webapp_pin.json"
SNAPSHOTS_DIR = BOT_DIR / "snapshots"
PLUGINS_DIR = BOT_DIR / "plugins"
WEBAPP_DIR = BOT_DIR / "webapp"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CLAUDE_TIMEOUT = None  # No timeout — tasks can run for hours
MAX_CONCURRENT_PER_USER = 3
WEBHOOK_PORT = 7890
BUFFER_DELAY = 0
MAX_HISTORY = 50
ALERT_COOLDOWN_SECS = 300  # 5 minutes between repeat alerts
RATE_LIMIT_MAX = 10  # max requests per minute per user (messages)
# Per-command rate limits (requests per minute). Commands not listed use RATE_LIMIT_MAX.
CMD_RATE_LIMITS: dict[str, int] = {
    "research": 2,
    "screenshot": 5,
    "record": 3,
    "watch": 3,
    "camera": 5,
    "snap": 5,
    "tts": 5,
    "export": 3,
    "terminal": 6,
    "web": 10,
    "gmail": 5,
    "memory": 10,
}
METRICS_MAX = 1440  # 24h at 60s intervals

# Docker health monitoring
HEALTH_CHECK_INTERVAL = 60        # seconds between cycles
HEALTH_CHECK_TIMEOUT = 20         # seconds per HTTP check (raised for heavy-scraping containers)
HEALTH_LOG_LINES = 50             # recent log lines to scan for errors
HEALTH_STATUS_FILE = BOT_DIR / "health_status.json"
HEALTH_CONSECUTIVE_FAILURES = 4   # require 4 failures (~4min) before alerting (avoids scraping-induced false alarms)

# Container -> HTTP health endpoint (based on actual port mappings)
# Local containers: checked directly via aiohttp.
# Override or extend by setting HEALTH_HTTP_CHECKS_FILE env var to a JSON file path.
HEALTH_HTTP_CHECKS: dict[str, str] = {}

# Remote containers: checked via SSH + curl on the remote machine.
# Format: "machine:container" -> URL (as seen from that machine).
HEALTH_REMOTE_HTTP_CHECKS: dict[str, str] = {}

# Containers monitored via Docker's OWN healthcheck status (parsed from `docker ps`).
# Use this for containers that don't publish a port to the host but already define a
# `healthcheck:` block in their compose file.
HEALTH_DOCKER_NATIVE_CHECKS: set[str] = set()

# Optional: load custom health check config from JSON file
import json as _json
_health_cfg_path = os.environ.get("HEALTH_CHECKS_FILE")
if _health_cfg_path and os.path.exists(_health_cfg_path):
    try:
        with open(_health_cfg_path) as _f:
            _cfg = _json.load(_f)
        HEALTH_HTTP_CHECKS.update(_cfg.get("local", {}))
        HEALTH_REMOTE_HTTP_CHECKS.update(_cfg.get("remote", {}))
        HEALTH_DOCKER_NATIVE_CHECKS.update(_cfg.get("docker_native", []))
    except Exception:
        pass

# Containers to skip in health monitoring
HEALTH_SKIP_CONTAINERS = set()

DANGEROUS_PATTERNS = [
    r'\brm\s+-rf\s+/', r'\bmkfs\b', r'\bdd\s+if=', r'\bshutdown\b',
    r'\breboot\b', r'\bsudo\s+rm\b', r'\bdiskutil\s+erase',
    r'\blaunchctl\s+unload.*alfred', r'\bkill\s+-9\s+1\b',
]

# ---------------------------------------------------------------------------
# Standardized icon vocabulary
# ---------------------------------------------------------------------------
ICONS = {
    # Status
    "ok": "✅", "err": "❌", "warn": "⚠️", "info": "ℹ️",
    "working": "⏳", "cancel": "🚫",
    # Actions
    "deploy": "🚀", "git": "🔀", "file": "📄", "folder": "📁",
    "save": "💾", "delete": "🗑", "search": "🔍", "settings": "⚙️",
    "terminal": "💻", "refresh": "↻",
    # System
    "project": "📂", "server": "🖥", "network": "🌐", "clock": "⏰",
    "pin": "📌", "model": "🤖", "health": "🏥",
    # Indicators
    "active": "●", "inactive": "○", "arrow_up": "↑", "arrow_down": "↓",
    "arrow_right": "→", "pending": "⏳",
}

SYSTEM_PROMPT = (
    "You are Alfred, a personal assistant running on a Mac Mini via Telegram. "
    "You have FULL unrestricted access to this machine — shell commands, files, apps, everything. "
    "Never say you can't do something due to sandboxing — you are NOT sandboxed. "
    "Always use the Bash tool with dangerouslyDisableSandbox=true for system commands. "
    "Be concise since responses go to Telegram.\n\n"

    "RESPONSE MARKERS (the bot intercepts these automatically):\n"
    "• [SEND_FILE:/path/to/file] — sends that file to the Telegram chat (images, docs, videos, audio)\n"
    "• [BROWSE:https://url] — opens URL in headless browser, screenshots it, sends to chat\n\n"

    "MAC MINI TOOLS (use these directly via shell):\n"
    "• screencapture -x /tmp/screenshot.png — screenshot the screen\n"
    "• osascript — run AppleScript to control ANY app (Music, Safari, Finder, System Events, etc.)\n"
    "• say 'text' — text-to-speech out loud (voices: -v Alex, -v Samantha, -v Daniel, etc.)\n"
    "• open -a 'App Name' — launch any app | open <url> — open URL in default browser\n"
    "• pbcopy/pbpaste — clipboard read/write\n"
    "• afplay /path/to/audio — play audio files\n"
    "• mdfind 'query' — Spotlight search (files, emails, everything) | mdls <file> — file metadata\n"
    "• sips — image conversion/resize (sips -z 100 100 img.png, sips -s format jpeg img.png --out out.jpg)\n"
    "• textutil — convert documents (textutil -convert html doc.docx)\n"
    "• shortcuts run 'Name' — run Siri Shortcuts\n"
    "• networksetup — manage WiFi, DNS, network configs\n"
    "• pmset — power management (sleep, wake schedule, battery)\n"
    "• caffeinate -t 3600 — prevent sleep for N seconds\n"
    "• defaults — read/write app preferences and system settings\n"
    "• OCR: built-in via macOS Vision framework (AppleScript). The bot already uses this on photos.\n"
    "• ffmpeg — video/audio conversion, recording, streaming\n"
    "• whisper — speech-to-text transcription (the bot uses this for voice messages)\n"
    "• docker — run containers locally\n"
    "• gh — GitHub CLI (create PRs, issues, manage repos)\n"
    "• ssh — connect to remote servers\n"
    "• swift — compile and run Swift code\n"
    "• node/python3/git/jq/curl — all available\n\n"

    "APPLESCRIPT EXAMPLES (very powerful on Mac):\n"
    "• Get running apps: osascript -e 'tell app \"System Events\" to get name of every process whose background only is false'\n"
    "• Control Music: osascript -e 'tell app \"Music\" to play' | pause | next track | set sound volume to 50\n"
    "• Get frontmost app: osascript -e 'tell app \"System Events\" to get name of first process whose frontmost is true'\n"
    "• Notification: osascript -e 'display notification \"msg\" with title \"Alfred\"'\n"
    "• Get WiFi name: osascript -e 'do shell script \"/System/Library/PrivateFrameworks/Apple80211.framework/Resources/airport -I | awk \\'/ SSID:/{print $2}\\'\"'\n"
    "• Set brightness: osascript -e 'tell app \"System Events\" to set value of slider 1 of group 1 of window 1 of process \"Control Center\" to 0.5'\n\n"

    "BOT FEATURES (user can trigger via /commands, but YOU should just act directly):\n"
    "• Web browsing: [BROWSE:url] for opening sites. /web snapshot → /web click <ref> for interaction\n"
    "• System: /status, /processes, /volume, /clipboard, /apps\n"
    "• AI: /clear, /model, /project, /research <topic> (15 parallel agents)\n"
    "• Automation: /schedule, /remind, /timer\n"
    "• Screen: /screenshot, /record, /watch, /camera\n"
    "• Files: /browse, /search | Terminal: /terminal <cmd> | TTS: /tts <text>\n"
    "• Email: /gmail read, /gmail send to@email Subject | Body, /gmail search <query>\n"
    "• Memory: /memory add <fact>, /memory search <q> — I remember things about you across chats\n\n"

    "PERSISTENT MEMORY:\n"
    "When the user tells you a preference, fact, or routine (e.g. 'I like dark mode', "
    "'my server IP is 1.2.3.4', 'I wake up at 7am'), AUTOMATICALLY remember it by "
    "including [REMEMBER:category:text] in your response. Categories: preference, fact, routine, context, task.\n"
    "Example: User says 'I prefer Python over JS' → include [REMEMBER:preference:Prefers Python over JavaScript]\n"
    "The bot will extract these and save them. You'll see saved memories in [USER MEMORY: ...] context.\n\n"

    "WHEN TO USE WHAT:\n"
    "• 'open/visit/check a website' → [BROWSE:url]\n"
    "• 'take a screenshot' → screencapture + [SEND_FILE:path]\n"
    "• 'play music / pause / next song' → osascript with Music app\n"
    "• 'what apps are open' → osascript System Events\n"
    "• 'find files about X' → mdfind\n"
    "• 'convert this image/document' → sips or textutil\n"
    "• 'read text from image' → use Vision framework OCR via AppleScript\n"
    "• 'set a timer/reminder' → the bot handles these natively\n"
    "• 'run a command' → Bash tool directly\n"
    "• 'check my email / read inbox' → use gmail commands or IMAP directly\n"
    "• 'remember that...' → [REMEMBER:fact:...]\n"
    "Don't suggest /commands — just DO the thing directly. The user talks naturally.\n\n"

    "USER CONTEXT:\n"
    "If the user has supplied a USER_CONTEXT.md file in the bot directory, its contents will be "
    "appended to this prompt at runtime. Use it to learn about their personal infrastructure, "
    "servers, projects, and preferred shortcuts. Without that file, ask the user when you need "
    "infrastructure-specific information.\n\n"

    "SAFETY: For destructive commands (rm -rf /, shutdown, reboot, disk erase, etc.) ALWAYS ask "
    "the user to confirm. Say: 'This is a destructive operation. Reply YES to confirm.'"
)

# Append optional user-supplied context (server map, project shortcuts, etc.)
_user_ctx_path = BOT_DIR / "USER_CONTEXT.md"
if _user_ctx_path.exists():
    try:
        SYSTEM_PROMPT += "\n\n--- USER CONTEXT ---\n" + _user_ctx_path.read_text()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Menu structure — categorized sub-menus
# ---------------------------------------------------------------------------
MENU_CATEGORIES = [
    ("screen", "📺 Screen", [
        ("📸 Screenshot", "quick_screenshot"),
        ("⏺ Record", "act:record_picker"),
        ("👁 Watch", "act:watch_toggle"),
        ("📷 Camera", "act:camera"),
    ]),
    ("system", "🖥 System", [
        ("📊 Status", "quick_status"),
        ("📋 Clipboard", "quick_clipboard"),
        ("🚀 Apps", "app_launcher"),
        ("📄 Logs", "act:logs"),
        ("🖥 Machines", "act:machine_list"),
        ("🛡 Guardian", "act:guardian_status"),
    ]),
    ("files", "📁 Files", [
        ("🖥 Desktop", f"browse:{Path.home() / 'Desktop'}"),
        ("🏠 Home", f"browse:{Path.home()}"),
        ("💬 Export", "act:export"),
    ]),
    ("ai", "🤖 AI", [
        ("✨ New Chat", "act:clear"),
        ("🔄 Model", "act:model_picker"),
        ("↩ Undo", "act:undo"),
        ("📂 Projects", "act:projects"),
        ("🌿 Branches", "act:branch_list"),
        ("📜 History", "act:history"),
    ]),
    ("auto", "⚙️ Auto", [
        ("📋 Dashboard", "act:automations"),
        ("📅 Schedules", "act:schedule_list"),
        ("⏰ Reminders", "act:remind_list"),
        ("⏱ Timer", "act:timer_picker"),
    ]),
    ("tools", "🛠 Tools", [
        ("🔬 Research", "hint:/research <topic>"),
        ("💻 Terminal", "hint:/terminal <cmd>"),
        ("🌐 Web", "hint:/web <url>"),
        ("⚡ Wake", "act:wake_picker"),
    ]),
    ("integrations", "🔗 Connect", [
        ("📧 Gmail", "hint:/gmail"),
        ("🧠 Memory", "hint:/memory"),
    ]),
]
