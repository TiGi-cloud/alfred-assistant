"""Alfred shared mutable state — imported by bot.py and all sub-modules."""
from __future__ import annotations

import asyncio
import time

# Session/user state
user_sessions: dict[str, str] = {}
user_models: dict[str, str] = {}
user_processes: dict[str, list[asyncio.subprocess.Process]] = {}  # ukey -> list of active processes
user_request_count: dict[str, int] = {}
user_machines: dict[str, str] = {}
watch_tasks: dict[str, asyncio.Task] = {}
plugins: dict[str, str] = {}
pending_approvals: dict[str, dict] = {}
cost_tracker: dict[str, dict] = {}
alerts: dict[str, list] = {}
history: dict[str, list] = {}

# Streaming terminal state
terminal_processes: dict[str, dict] = {}  # id -> {proc, output, done, returncode}
_term_id_counter = 0

forks: dict[str, dict] = {}
projects: dict[str, dict] = {}          # ukey -> {name -> project_data}
active_project: dict[str, str] = {}     # ukey -> project_name
global_env: dict[str, str] = {}         # shared env vars across all projects (e.g. CLOUDFLARE_API_TOKEN)
notification_enabled: dict[str, bool] = {}
message_buffers: dict[str, list] = {}
buffer_tasks: dict[str, asyncio.Task] = {}
pinned_status: dict[int, int] = {}  # chat_id -> message_id
alert_cooldowns: dict[str, float] = {}  # alert_id -> last_fired timestamp
alert_firing: dict[str, bool] = {}  # alert_id -> was_firing last check (for all-clear)
alert_msg_ids: dict[str, int] = {}  # alert_id -> message_id of firing alert
reminder_tasks: set = set()  # Strong references to fire-and-forget reminder tasks
pending_reminders: dict[str, list] = {}  # ukey -> list of {id, text, fire_time, chat_id}
_plugin_handler_objects: list = []  # Track handlers added by plugins so /reload can remove them
user_rate_timestamps: dict[str, list] = {}  # ukey -> list of request timestamps
cmd_rate_timestamps: dict[str, list] = {}  # "ukey:cmd" -> list of request timestamps

_app_ref = None
_default_chat_id = None
_start_time = time.time()

# Metrics history for sparkline charts (CPU, mem, disk every 60s, keep 24h = 1440 entries)
metrics_history: list[dict] = []

# Clipboard sync state
_last_clipboard_hash = ""
clipboard_sync_enabled: dict[str, bool] = {}  # ukey -> enabled

# Geofencing
geofences: list[dict] = []  # [{name, lat, lon, radius_m, action, enter_or_exit}]
_last_location: dict = {}  # {lat, lon, ts}

# Chat sessions for webapp (separate from Telegram sessions)
webapp_chat_sessions: dict[str, list] = {}  # session_id -> [{role, content}]
_webapp_chat_counter = 0

# IP cache (public IP with TTL)
_ip_cache: dict[str, object] = {}  # {"ip": str, "ts": float}

# Conversation memory: inject system snapshot every N messages
_msg_count_since_snapshot: dict[str, int] = {}  # ukey -> count
SNAPSHOT_INTERVAL = 10  # inject system context every N messages

# Command queue for when concurrent limit is hit
message_queue: dict[str, list] = {}  # ukey -> [{text, update, context, priority}]
_queue_processing: dict[str, bool] = {}  # ukey -> is_processing

# Usage tracking with time windows
usage_hourly: dict[str, list] = {}   # ukey -> [{ts, in, out}] requests in current hour
usage_weekly: dict[str, list] = {}   # ukey -> [{ts, in, out}] requests in current week

# Docker health monitoring
health_status: dict[str, dict] = {}       # "machine:container" -> status dict
health_last_full_check: float = 0.0
