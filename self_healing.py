"""Alfred Self-Healing System Guardian (Feature 20.4).

Continuous health monitoring with learned baselines, anomaly detection,
auto-diagnosis, configuration drift detection, and post-mortem reports.
"""
from __future__ import annotations

import re
import time
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

import bot_state as st
from db import db_load, db_save
from utils.formatting import E, fmt_output
from utils.helpers import async_run

logger = logging.getLogger("alfred")

DB_BASELINES = "selfheal_baselines"
DB_DRIFT_SNAPSHOT = "selfheal_drift_snapshot"
DB_INCIDENTS = "selfheal_incidents"
DB_CONFIG = "selfheal_config"

DEFAULT_CONFIG = {
    "enabled": True,
    "auto_repair": False,      # Require approval before killing processes
    "cpu_spike_threshold": 95,  # % sustained for 3+ checks
    "mem_spike_threshold": 90,
    "disk_threshold": 92,
    "zombie_reap": True,
    "drift_check_interval": 3600,  # seconds between drift checks
    "max_incidents": 50,
}


def _get_config() -> dict:
    cfg = db_load(DB_CONFIG, {})
    return {**DEFAULT_CONFIG, **cfg}


def _save_config(cfg: dict):
    db_save(DB_CONFIG, cfg)


def _log_incident(incident_type: str, description: str, action_taken: str = "",
                  severity: str = "warning"):
    """Log an incident to the DB."""
    incidents = db_load(DB_INCIDENTS, [])
    incidents.append({
        "type": incident_type,
        "desc": description,
        "action": action_taken,
        "severity": severity,
        "ts": int(time.time()),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    max_inc = _get_config().get("max_incidents", 50)
    if len(incidents) > max_inc:
        incidents = incidents[-max_inc:]
    db_save(DB_INCIDENTS, incidents)


async def _get_top_processes(sort_by: str = "cpu", n: int = 5) -> list[dict]:
    """Get top processes by CPU or memory."""
    # macOS ps doesn't support --sort; use -r (by CPU) or -m (by memory)
    flag = "-r" if sort_by == "cpu" else "-m"
    cmd = f"ps -Ao pid,%cpu,%mem,comm {flag} | head -{n + 1}"
    rc, out, _ = await async_run(["bash", "-c", cmd], timeout=10)
    procs = []
    if rc == 0 and out.strip():
        lines = out.strip().split("\n")[1:]  # skip header
        for line in lines:
            parts = line.split(None, 3)
            if len(parts) >= 4:
                procs.append({
                    "pid": parts[0],
                    "cpu": parts[1],
                    "mem": parts[2],
                    "cmd": parts[3][:80],
                })
    return procs


async def _get_zombie_count() -> tuple[int, list[str]]:
    """Count zombie processes."""
    rc, out, _ = await async_run(
        ["bash", "-c", "ps aux | awk '$8 ~ /Z/ {print $2, $11}'"], timeout=5
    )
    zombies = []
    if rc == 0 and out.strip():
        zombies = [line.strip() for line in out.strip().split("\n") if line.strip()]
    return len(zombies), zombies


async def _check_launch_agents() -> list[dict]:
    """Enumerate LaunchAgents and LaunchDaemons."""
    items = []
    dirs = [
        "~/Library/LaunchAgents",
        "/Library/LaunchAgents",
        "/Library/LaunchDaemons",
    ]
    for d in dirs:
        rc, out, _ = await async_run(["bash", "-c", f"ls -1 {d} 2>/dev/null"], timeout=5)
        if rc == 0 and out.strip():
            for name in out.strip().split("\n"):
                items.append({"dir": d, "name": name.strip()})
    return items


async def _get_disk_smart() -> dict:
    """Get SMART data if smartctl is available."""
    rc, _, _ = await async_run(["which", "smartctl"], timeout=3)
    if rc != 0:
        return {"available": False}
    rc, out, _ = await async_run(
        ["bash", "-c", "smartctl -a /dev/disk0 2>/dev/null | grep -E '(Health|Temperature|Reallocated|Pending)'"],
        timeout=10,
    )
    return {"available": True, "output": out.strip() if rc == 0 else ""}


def _update_baselines(cpu: float, mem: int, disk: int):
    """Update rolling baselines."""
    baselines = db_load(DB_BASELINES, {"cpu": [], "mem": [], "disk": []})
    for key, val in [("cpu", cpu), ("mem", mem), ("disk", disk)]:
        arr = baselines.get(key, [])
        arr.append(val)
        # Keep last 60 readings (1 hour at 60s intervals)
        if len(arr) > 60:
            arr = arr[-60:]
        baselines[key] = arr
    db_save(DB_BASELINES, baselines)


def _get_baseline_avg(key: str) -> float:
    baselines = db_load(DB_BASELINES, {})
    arr = baselines.get(key, [])
    if not arr:
        return 0.0
    return sum(arr) / len(arr)


def _is_anomaly(current: float, key: str, threshold_pct: float = 50) -> bool:
    """Check if current value is anomalously high vs baseline."""
    avg = _get_baseline_avg(key)
    if avg < 5:  # Not enough baseline data yet
        return False
    return current > avg * (1 + threshold_pct / 100)


async def take_drift_snapshot() -> dict:
    """Capture current system configuration state."""
    snapshot = {"ts": int(time.time())}

    # Homebrew packages — count only (hash was too noisy)
    rc, out, _ = await async_run(["bash", "-c", "HOMEBREW_NO_AUTO_UPDATE=1 brew list --formula 2>/dev/null | wc -l"], timeout=15)
    snapshot["brew_count"] = out.strip() if rc == 0 else "?"

    # LaunchAgents/Daemons — count only
    agents = await _check_launch_agents()
    snapshot["launch_items"] = len(agents)

    # Ports removed from drift — too noisy, changes on every app start/stop

    # SIP status
    rc, out, _ = await async_run(["csrutil", "status"], timeout=5)
    snapshot["sip"] = "enabled" if "enabled" in out.lower() else "disabled" if rc == 0 else "?"

    # Firewall
    rc, out, _ = await async_run(
        ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"], timeout=5
    )
    snapshot["firewall"] = "enabled" if "enabled" in out.lower() else "disabled" if rc == 0 else "?"

    return snapshot


def detect_drift(current: dict, previous: dict) -> list[str]:
    """Compare snapshots and return list of drift descriptions."""
    drifts = []
    if not previous:
        return drifts

    # Brew — only alert on count change (packages added/removed)
    try:
        prev_count = int(str(previous.get("brew_count", "0")).strip())
        curr_count = int(str(current.get("brew_count", "0")).strip())
        if prev_count and curr_count and prev_count != curr_count:
            drifts.append(f"Homebrew packages changed ({prev_count} -> {curr_count})")
    except ValueError:
        pass

    # LaunchAgents — only alert on count change
    prev_items = previous.get("launch_items", 0)
    curr_items = current.get("launch_items", 0)
    if prev_items and curr_items and prev_items != curr_items:
        drifts.append(f"LaunchAgents/Daemons changed ({prev_items} -> {curr_items} items)")

    # Security-critical: SIP and Firewall — always alert
    if current.get("sip") != previous.get("sip") and previous.get("sip") not in ("?", ""):
        drifts.append(f"SIP status changed: {previous.get('sip')} -> {current.get('sip')}")

    if current.get("firewall") != previous.get("firewall") and previous.get("firewall") not in ("?", ""):
        drifts.append(f"Firewall status changed: {previous.get('firewall')} -> {current.get('firewall')}")

    return drifts


async def run_health_check(app):
    """Main health check loop — runs every 60s as a background task."""
    import asyncio

    # Wait for startup
    await asyncio.sleep(30)

    # Restore last_drift_check from DB so it persists across bot restarts
    _prev_snap = db_load(DB_DRIFT_SNAPSHOT, {})
    last_drift_check = _prev_snap.get("ts", 0) if _prev_snap else 0
    del _prev_snap
    # Track consecutive high readings for spike detection
    cpu_spike_count = 0

    while True:
        try:
            cfg = _get_config()
            if not cfg.get("enabled", True):
                await asyncio.sleep(60)
                continue

            # Get system metrics (reuse existing function)
            from core import get_system_status
            info = await get_system_status()

            cpu_raw = info.get("CPU_PCT", "0").split()[0]
            try:
                cpu = float(cpu_raw)
            except ValueError:
                cpu = 0.0
            try:
                mem_free = int(info.get("MEM_FREE_MB", "0"))
                mem_total = int(info.get("MEM_TOTAL_MB", "1"))
                mem_pct = int((mem_total - mem_free) / mem_total * 100) if mem_total else 0
            except ValueError:
                mem_pct = 0
            try:
                disk_pct = int(info.get("DISK_PCT", "0"))
            except ValueError:
                disk_pct = 0

            # Update baselines
            _update_baselines(cpu, mem_pct, disk_pct)

            # --- CPU spike detection ---
            if cpu > cfg["cpu_spike_threshold"]:
                cpu_spike_count += 1
            else:
                cpu_spike_count = 0

            if cpu_spike_count >= 3:  # 3 consecutive checks (~3 min)
                top_procs = await _get_top_processes("cpu", 3)
                proc_desc = ", ".join(f"{p['cmd'][:30]} ({p['cpu']}%)" for p in top_procs)
                desc = f"CPU at {cpu:.0f}% for 3+ min. Top: {proc_desc}"

                if cfg["auto_repair"] and top_procs:
                    # Kill the top CPU process if it's not system-critical
                    top_pid = top_procs[0]["pid"]
                    top_cmd = top_procs[0]["cmd"]
                    protected = ["kernel_task", "WindowServer", "loginwindow", "launchd", "Alfred"]
                    if not any(p in top_cmd for p in protected):
                        await async_run(["kill", "-9", top_pid], timeout=5)
                        action = f"Killed PID {top_pid} ({top_cmd[:40]})"
                        _log_incident("cpu_spike", desc, action, "critical")
                        await _notify_incident(app, "cpu_spike", desc, action)
                        cpu_spike_count = 0
                        continue

                _log_incident("cpu_spike", desc, severity="warning")
                await _notify_incident(app, "cpu_spike", desc)
                cpu_spike_count = 0

            # --- Memory pressure (disabled — RAM spikes are expected/transient) ---

            # --- Disk space ---
            if disk_pct > cfg["disk_threshold"]:
                desc = f"Disk usage at {disk_pct}%"
                _log_incident("disk_critical", desc, severity="critical")
                await _notify_incident(app, "disk_critical", desc)

            # --- Zombie processes ---
            if cfg.get("zombie_reap", True):
                zombie_count, zombie_list = await _get_zombie_count()
                if zombie_count > 5:
                    desc = f"{zombie_count} zombie processes detected"
                    _log_incident("zombies", desc, severity="info")

            # --- Configuration drift ---
            now = time.time()
            interval = cfg.get("drift_check_interval", 3600)
            if now - last_drift_check >= interval:
                last_drift_check = now
                current_snap = await take_drift_snapshot()
                previous_snap = db_load(DB_DRIFT_SNAPSHOT, {})
                drifts = detect_drift(current_snap, previous_snap)

                if drifts:
                    desc = "Configuration drift detected:\n" + "\n".join(f"  - {d}" for d in drifts)
                    _log_incident("config_drift", desc, severity="warning")
                    await _notify_incident(app, "config_drift", desc)

                db_save(DB_DRIFT_SNAPSHOT, current_snap)

        except Exception as e:
            logger.error("Self-healing check error: %s", e)

        import asyncio
        await asyncio.sleep(60)


async def _notify_incident(app, incident_type: str, description: str,
                           action_taken: str = ""):
    """Send incident notification to default chat."""
    if not st._default_chat_id:
        return

    icons = {
        "cpu_spike": "🔥",
        "mem_pressure": "💾",
        "disk_critical": "💿",
        "zombies": "🧟",
        "config_drift": "⚙️",
        "process_crash": "💥",
    }
    icon = icons.get(incident_type, "⚠️")

    text = f"{icon} <b>Self-Healing: {E(incident_type.replace('_', ' ').title())}</b>\n\n"
    text += E(description)
    if action_taken:
        text += f"\n\n<b>Action taken:</b> {E(action_taken)}"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("View Incidents", callback_data="heal:incidents"),
        InlineKeyboardButton("Settings", callback_data="heal:settings"),
    ]])

    try:
        await app.bot.send_message(
            st._default_chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb,
        )
    except Exception as e:
        logger.error("Failed to send healing notification: %s", e)


def build_incidents_text(n: int = 10) -> str:
    incidents = db_load(DB_INCIDENTS, [])
    if not incidents:
        return "No incidents recorded."

    recent = incidents[-n:]
    lines = [f"<b>Recent Incidents</b> ({len(incidents)} total)\n"]
    icons = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
    for inc in reversed(recent):
        icon = icons.get(inc.get("severity", "info"), "⚪")
        lines.append(
            f"{icon} <code>{inc.get('time', '?')}</code> "
            f"<b>{E(inc.get('type', '?'))}</b>\n"
            f"   {E(inc.get('desc', '')[:120])}"
        )
        if inc.get("action"):
            lines.append(f"   -> {E(inc['action'][:80])}")

    return "\n".join(lines)


def build_heal_settings_text() -> str:
    cfg = _get_config()
    status = "Enabled" if cfg.get("enabled") else "Disabled"
    auto = "On" if cfg.get("auto_repair") else "Off (notify only)"
    lines = [
        "<b>Self-Healing Guardian Settings</b>\n",
        f"<b>Status:</b> {status}",
        f"<b>Auto-repair:</b> {auto}",
        f"<b>CPU spike:</b> >{cfg['cpu_spike_threshold']}% for 3+ min",
        f"<b>Memory spike:</b> >{cfg['mem_spike_threshold']}%",
        f"<b>Disk threshold:</b> >{cfg['disk_threshold']}%",
        f"<b>Zombie reaping:</b> {'On' if cfg.get('zombie_reap') else 'Off'}",
        f"<b>Drift check:</b> every {cfg.get('drift_check_interval', 3600) // 60} min",
    ]

    # Show baseline averages
    cpu_avg = _get_baseline_avg("cpu")
    mem_avg = _get_baseline_avg("mem")
    disk_avg = _get_baseline_avg("disk")
    if cpu_avg > 0:
        lines.append(f"\n<b>Baselines (1h avg):</b>")
        lines.append(f"  CPU: {cpu_avg:.1f}% | RAM: {mem_avg:.0f}% | Disk: {disk_avg:.0f}%")

    return "\n".join(lines)


def build_heal_settings_kb() -> InlineKeyboardMarkup:
    cfg = _get_config()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'Disable' if cfg.get('enabled') else 'Enable'} Guardian",
                callback_data="heal:toggle",
            ),
            InlineKeyboardButton(
                f"Auto-repair: {'Off' if cfg.get('auto_repair') else 'On'}",
                callback_data="heal:autorepair",
            ),
        ],
        [
            InlineKeyboardButton("View Incidents", callback_data="heal:incidents"),
            InlineKeyboardButton("Check Drift Now", callback_data="heal:drift_now"),
        ],
        [
            InlineKeyboardButton("Run Full Audit", callback_data="heal:audit"),
            InlineKeyboardButton("<- Back", callback_data="menu:main"),
        ],
    ])


async def handle_heal_callback(query, ukey: str):
    """Handle self-healing callback queries. Returns True if handled."""
    data = query.data
    if not data.startswith("heal:"):
        return False

    action = data[5:]

    if action == "settings":
        text = build_heal_settings_text()
        kb = build_heal_settings_kb()
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return True

    if action == "incidents":
        text = build_incidents_text()
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Clear All", callback_data="heal:clear_incidents"),
            InlineKeyboardButton("<- Settings", callback_data="heal:settings"),
        ]])
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return True

    if action == "clear_incidents":
        db_save(DB_INCIDENTS, [])
        await query.message.edit_text("Incidents cleared.", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("<- Settings", callback_data="heal:settings")]
        ]))
        return True

    if action == "toggle":
        cfg = _get_config()
        cfg["enabled"] = not cfg.get("enabled", True)
        _save_config(cfg)
        text = build_heal_settings_text()
        kb = build_heal_settings_kb()
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return True

    if action == "autorepair":
        cfg = _get_config()
        cfg["auto_repair"] = not cfg.get("auto_repair", False)
        _save_config(cfg)
        text = build_heal_settings_text()
        kb = build_heal_settings_kb()
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return True

    if action == "drift_now":
        await query.message.edit_text("Checking configuration drift...")
        current_snap = await take_drift_snapshot()
        previous_snap = db_load(DB_DRIFT_SNAPSHOT, {})
        drifts = detect_drift(current_snap, previous_snap)
        db_save(DB_DRIFT_SNAPSHOT, current_snap)

        if drifts:
            text = "<b>Configuration Drift Detected:</b>\n\n"
            text += "\n".join(f"  - {E(d)}" for d in drifts)
            _log_incident("config_drift", "\n".join(drifts), severity="warning")
        else:
            text = "No configuration drift detected. System matches baseline."

        # Show snapshot summary
        text += f"\n\n<b>Current snapshot:</b>"
        text += f"\n  Brew packages: {current_snap.get('brew_count', '?')}"
        text += f"\n  Launch items: {current_snap.get('launch_items', '?')}"
        text += f"\n  SIP: {current_snap.get('sip', '?')}"
        text += f"\n  Firewall: {current_snap.get('firewall', '?')}"

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("<- Settings", callback_data="heal:settings"),
        ]])
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return True

    if action == "audit":
        await query.message.edit_text("Running full security audit...")

        lines = ["<b>System Security Audit</b>\n"]

        # SIP
        rc, out, _ = await async_run(["csrutil", "status"], timeout=5)
        sip = "enabled" in out.lower() if rc == 0 else None
        lines.append(f"{'✅' if sip else '❌'} SIP: {'enabled' if sip else 'disabled' if sip is not None else 'unknown'}")

        # Firewall
        rc, out, _ = await async_run(
            ["/usr/libexec/ApplicationFirewall/socketfilterfw", "--getglobalstate"], timeout=5
        )
        fw = "enabled" in out.lower() if rc == 0 else None
        lines.append(f"{'✅' if fw else '❌'} Firewall: {'enabled' if fw else 'disabled' if fw is not None else 'unknown'}")

        # FileVault
        rc, out, _ = await async_run(["fdesetup", "status"], timeout=5)
        fv = "on" in out.lower() if rc == 0 else None
        lines.append(f"{'✅' if fv else '❌'} FileVault: {'on' if fv else 'off' if fv is not None else 'unknown'}")

        # Software updates
        rc, out, _ = await async_run(["softwareupdate", "-l", "--no-scan"], timeout=10)
        has_updates = "no new software" not in out.lower() if rc == 0 else None
        lines.append(f"{'🟡' if has_updates else '✅'} Updates: {'available' if has_updates else 'up to date' if has_updates is not None else 'unknown'}")

        # Zombies
        z_count, _ = await _get_zombie_count()
        lines.append(f"{'🟡' if z_count > 0 else '✅'} Zombies: {z_count}")

        # Open ports
        rc, out, _ = await async_run(
            ["bash", "-c", "lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | awk 'NR>1{print $1, $9}' | sort -u | head -10"],
            timeout=10,
        )
        if out.strip():
            port_lines = out.strip().split("\n")
            lines.append(f"\n<b>Listening ports ({len(port_lines)}):</b>")
            for pl in port_lines[:10]:
                lines.append(f"  <code>{E(pl)}</code>")

        # Launch items
        agents = await _check_launch_agents()
        lines.append(f"\n<b>Launch items:</b> {len(agents)}")

        # Top CPU
        top_procs = await _get_top_processes("cpu", 3)
        if top_procs:
            lines.append(f"\n<b>Top CPU:</b>")
            for p in top_procs:
                lines.append(f"  <code>{p['cpu']}%</code> {E(p['cmd'][:50])}")

        text = "\n".join(lines)
        _log_incident("audit", "Full security audit run", severity="info")

        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("<- Settings", callback_data="heal:settings"),
            InlineKeyboardButton("<- Menu", callback_data="menu:main"),
        ]])
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return True

    return False
