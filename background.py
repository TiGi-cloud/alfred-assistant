"""Alfred background tasks — scheduled tasks, alerts, metrics, notifications, clipboard sync, health monitoring."""
from __future__ import annotations

import os
import re
import time
import hashlib
import asyncio
import logging
from datetime import datetime

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

try:
    from croniter import croniter
    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

import bot_state as st
from config import (
    SCHEDULES_FILE, METRICS_FILE, METRICS_MAX, ALERT_COOLDOWN_SECS,
    HEALTH_CHECK_INTERVAL, HEALTH_CHECK_TIMEOUT, HEALTH_HTTP_CHECKS,
    HEALTH_REMOTE_HTTP_CHECKS, HEALTH_SKIP_CONTAINERS, HEALTH_LOG_LINES,
    HEALTH_STATUS_FILE, HEALTH_CONSECUTIVE_FAILURES,
    HEALTH_DOCKER_NATIVE_CHECKS,
)
from persistence import load_json, save_json
from utils.formatting import E, md_to_html, fmt_output, _safe_html_chunks
from utils.helpers import async_run

from claude_runner import run_claude
from core import get_system_status, load_machines

logger = logging.getLogger("alfred")


async def run_scheduled_tasks(app):
    while True:
        await asyncio.sleep(60)

        # Prune stale pending approvals (older than 10 minutes)
        now_ts = time.time()
        stale_ids = [k for k, v in list(st.pending_approvals.items()) if now_ts - v.get("created_at", now_ts) > 600]
        for sid in stale_ids:
            st.pending_approvals.pop(sid, None)

        # Clean up idle browser sessions
        try:
            from utils.browser import cleanup_idle_sessions
            await cleanup_idle_sessions()
        except Exception:
            pass

        if not SCHEDULES_FILE.exists():
            continue
        try:
            schedules = load_json(SCHEDULES_FILE, [])
        except Exception:
            continue

        now = datetime.now()
        modified = False

        for sched in schedules:
            cron = sched.get("cron", "").lower()
            should_run = False

            if HAS_CRONITER and re.match(r'^[\d\*\/\-\,\s]+$', cron):
                try:
                    last_run = sched.get("last_run")
                    last_dt = datetime.fromisoformat(last_run) if last_run else datetime.now()
                    next_run = croniter(cron, last_dt).get_next(datetime)
                    if next_run <= now:
                        should_run = True
                except Exception:
                    pass
            else:
                if "every minute" in cron:
                    should_run = True
                elif "every 5 min" in cron and now.minute % 5 == 0:
                    should_run = True
                elif "every hour" in cron and now.minute == 0:
                    should_run = True
                elif "every day" in cron and now.hour == 9 and now.minute == 0:
                    should_run = True
                elif cron.startswith("at "):
                    try:
                        if now.hour == int(cron.split()[1]) and now.minute == 0:
                            should_run = True
                    except (ValueError, IndexError):
                        pass

            if should_run:
                modified = True
                try:
                    ukey = sched.get("user_key", "")
                    response = await run_claude(sched["task"], ukey)
                    sched["last_run"] = now.isoformat()
                    chat_id = sched.get("chat_id")
                    if chat_id:
                        file_pattern = r'\[SEND_FILE:(.*?)\]'
                        files = re.findall(file_pattern, response)
                        clean = re.sub(file_pattern, '', response).strip()
                        if clean:
                            sched_text = f"<b>Scheduled:</b> {E(sched['task'])}\n\n{md_to_html(clean)}"
                            for chunk in _safe_html_chunks(sched_text):
                                await app.bot.send_message(chat_id, chunk, parse_mode=ParseMode.HTML)
                        for fpath in files:
                            fpath = fpath.strip()
                            if os.path.isfile(fpath):
                                with open(fpath, 'rb') as f:
                                    await app.bot.send_document(chat_id, document=f)
                except Exception as e:
                    logger.error("Scheduled task error: %s", e)
                    sched["last_run"] = now.isoformat()  # Still update to avoid tight retry loop

        if modified:
            save_json(SCHEDULES_FILE, schedules)


async def metrics_collector():
    # Load persisted metrics
    saved = load_json(METRICS_FILE, [])
    if isinstance(saved, list):
        st.metrics_history = saved[-METRICS_MAX:]
    while True:
        try:
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

            entry = {
                "ts": int(time.time()),
                "cpu": round(cpu, 1),
                "mem": mem_pct,
                "disk": disk_pct,
            }
            st.metrics_history.append(entry)
            if len(st.metrics_history) > METRICS_MAX:
                st.metrics_history = st.metrics_history[-METRICS_MAX:]
            # Persist every 10 entries
            if len(st.metrics_history) % 10 == 0:
                save_json(METRICS_FILE, st.metrics_history)
        except Exception as e:
            logger.debug("Metrics collector error: %s", e)
        await asyncio.sleep(60)


async def clipboard_sync_task(app):
    # Initialize with current clipboard
    try:
        _, initial, _ = await async_run(["pbpaste"], timeout=5)
        st._last_clipboard_hash = hashlib.md5(initial.encode()).hexdigest()
    except Exception:
        pass

    while True:
        await asyncio.sleep(3)
        if not any(st.clipboard_sync_enabled.values()):
            await asyncio.sleep(10)
            continue
        try:
            _, clip_text, _ = await async_run(["pbpaste"], timeout=5)
            clip_hash = hashlib.md5(clip_text.encode()).hexdigest()
            if clip_hash != st._last_clipboard_hash and clip_text.strip():
                st._last_clipboard_hash = clip_hash
                for ukey, enabled in st.clipboard_sync_enabled.items():
                    if enabled and st._default_chat_id:
                        preview = clip_text[:500]
                        if len(clip_text) > 500:
                            preview += "..."
                        await app.bot.send_message(
                            st._default_chat_id,
                            f"📋 <b>Clipboard changed:</b>\n<tg-spoiler>{E(preview)}</tg-spoiler>",
                            parse_mode=ParseMode.HTML,
                            disable_notification=True,
                        )
                        break  # Only send once even if multiple users have it enabled
        except Exception:
            pass


async def youtube_keep_alive():
    """Auto-dismiss YouTube 'Are you still watching?' / 'Video paused' popups."""
    js_dismiss = (
        "(function(){"
        "var c=document.querySelector('#confirm-button')"
        "||document.querySelector('yt-confirm-dialog-renderer button');"
        "if(c){c.click();return 'dismissed';}"
        "var p=document.querySelector('ytd-popup-container yt-button-renderer button');"
        "if(p){p.click();return 'dismissed-popup';}"
        "var v=document.querySelector('video');"
        "if(v&&v.paused&&v.currentTime>0&&!v.ended){v.play();return 'resumed';}"
        "return 'ok';})()"
    )
    while True:
        await asyncio.sleep(45)
        try:
            script = (
                'tell application "System Events"\n'
                '  if (name of processes) contains "Google Chrome" then\n'
                '    tell application "Google Chrome"\n'
                '      repeat with w in windows\n'
                '        repeat with t in tabs of w\n'
                '          if URL of t contains "youtube.com" then\n'
                f'            execute t javascript "{js_dismiss}"\n'
                '          end if\n'
                '        end repeat\n'
                '      end repeat\n'
                '    end tell\n'
                '  end if\n'
                'end tell'
            )
            await async_run(["osascript", "-e", script], timeout=10)
        except Exception:
            pass


# ===========================================================================
# Docker Health Monitoring
# ===========================================================================

_ERROR_RE = re.compile(
    r"(?i)\b(error|exception|traceback|fatal|critical|panic|segfault|"
    r"out of memory|oom|killed|connection refused)\b"
)
# Noise patterns to ignore in log scanning (known harmless log lines)
_ERROR_IGNORE_RE = re.compile(
    r"(?i)(No route matches URL|ShipStation error.*Read timed out|"
    r"No routes matched location|data:.*Error:|"
    r"/api/\S*error\S*\s+HTTP|error-analytics|"
    r"No price found|Shopify JSON endpoint failed|"
    r"HTTP error for https?://|found 0 nodes for|"
    r"background_price_checker|limiting requests.*by zone)"
)


def _has_http_check(machine, container):
    """Check if a container has an HTTP health check configured (local or remote)."""
    if machine == "local" and container in HEALTH_HTTP_CHECKS:
        return True
    return f"{machine}:{container}" in HEALTH_REMOTE_HTTP_CHECKS


def _has_native_check(machine, container):
    """Check if a container is monitored via Docker's own healthcheck status."""
    return machine == "local" and container in HEALTH_DOCKER_NATIVE_CHECKS


def _parse_native_health(status_string):
    """Parse Docker's status string for the (healthy)/(unhealthy)/(starting) marker.

    Returns one of: "healthy", "unhealthy", "starting", or None if no healthcheck info.
    """
    if not status_string:
        return None
    s = status_string.lower()
    if "(healthy)" in s:
        return "healthy"
    if "(unhealthy)" in s:
        return "unhealthy"
    if "(health: starting)" in s or "(starting)" in s:
        return "starting"
    return None


async def _get_containers_local():
    """Get container names and states from local Docker."""
    try:
        proc = await asyncio.create_subprocess_shell(
            'docker ps -a --format "{{.Names}}\t{{.State}}\t{{.Status}}"',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return []
        results = []
        for line in out.decode().strip().splitlines():
            parts = line.split('\t')
            if len(parts) >= 3:
                results.append({"name": parts[0], "state": parts[1], "status": parts[2]})
        return results
    except Exception as e:
        logger.debug("Health: local docker list error: %s", e)
        return []


async def _get_containers_remote(host):
    """Get container names and states from a remote machine via SSH."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes", host,
            'docker ps -a --format "{{.Names}}\t{{.State}}\t{{.Status}}"',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
        if proc.returncode != 0:
            return []
        results = []
        for line in out.decode().strip().splitlines():
            parts = line.split('\t')
            if len(parts) >= 3:
                results.append({"name": parts[0], "state": parts[1], "status": parts[2]})
        return results
    except Exception as e:
        logger.debug("Health: remote docker list error (%s): %s", host, e)
        return []


async def _http_health_check(url):
    """Perform an HTTP GET health check. Returns (status_code, latency_ms) or (None, None)."""
    try:
        t0 = time.monotonic()
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=HEALTH_CHECK_TIMEOUT)) as resp:
                latency = round((time.monotonic() - t0) * 1000)
                return resp.status, latency
    except Exception:
        return None, None


async def _remote_http_health_check(host, url):
    """Perform HTTP health check on a remote machine via SSH + curl. Returns (status_code, latency_ms) or (None, None)."""
    try:
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
            "-o", "BatchMode=yes", host,
            f"curl -s -o /dev/null -w '%{{http_code}}' --max-time {HEALTH_CHECK_TIMEOUT} {url}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=HEALTH_CHECK_TIMEOUT + 7)
        latency = round((time.monotonic() - t0) * 1000)
        code = int(out.decode().strip())
        return code, latency
    except Exception:
        return None, None


async def _scan_container_logs(container, machine="local", host=None):
    """Scan recent docker logs for error patterns. Returns list of error lines (max 5)."""
    try:
        if machine == "local":
            cmd = f'docker logs --tail {HEALTH_LOG_LINES} --since 5m {container} 2>&1'
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        else:
            cmd = f'docker logs --tail {HEALTH_LOG_LINES} --since 5m {container} 2>&1'
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", host, cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        errors = []
        for line in out.decode(errors="replace").splitlines():
            if _ERROR_RE.search(line) and not _ERROR_IGNORE_RE.search(line):
                errors.append(line.strip()[:200])
        return errors[-5:]
    except Exception:
        return []


def _time_ago(ts):
    """Human-readable time ago string from a unix timestamp."""
    if not ts:
        return "unknown"
    diff = int(time.time() - ts)
    if diff < 60:
        return f"{diff}s ago"
    if diff < 3600:
        return f"{diff // 60}m ago"
    return f"{diff // 3600}h {(diff % 3600) // 60}m ago"


async def _send_health_alert(app, container, machine, health, last_error, last_healthy_ts):
    """Send a Telegram health alert."""
    if not st._default_chat_id:
        return
    if health == "unhealthy":
        icon = "\U0001f534"  # red circle
        title = "Service Down"
        body = f"<b>Container:</b> {E(container)} ({E(machine)})"
        if last_error:
            body += f"\n<b>Error:</b> {E(last_error[:300])}"
        if last_healthy_ts:
            body += f"\n<b>Last healthy:</b> {_time_ago(last_healthy_ts)}"
    elif health == "degraded":
        icon = "\U0001f7e1"  # yellow circle
        title = "Service Degraded"
        body = f"<b>Container:</b> {E(container)} ({E(machine)})"
        if last_error:
            body += f"\n<b>Errors detected:</b> {E(last_error[:300])}"
    else:
        icon = "\U0001f7e2"  # green circle
        title = "Service Recovered"
        body = f"<b>Container:</b> {E(container)} ({E(machine)})"
        if last_healthy_ts:
            downtime = int(time.time() - last_healthy_ts)
            if downtime > 60:
                body += f"\n<b>Downtime:</b> ~{downtime // 60}m"

    text = f"{icon} <b>{title}</b>\n\n{body}"
    try:
        await app.bot.send_message(
            st._default_chat_id, text, parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error("Failed to send health alert: %s", e)


async def health_check_loop(app):
    """Background loop that monitors Docker container health and sends alerts."""
    # Load persisted state
    saved = load_json(HEALTH_STATUS_FILE, {})
    if isinstance(saved, dict):
        st.health_status = saved

    cycle_count = 0
    await asyncio.sleep(30)  # initial delay to let containers settle on startup

    while True:
        try:
            now = time.time()
            machines_config = load_machines()

            # Gather containers from all machines
            tasks = {"local": _get_containers_local()}
            for name, info in machines_config.items():
                host = info if isinstance(info, str) else info.get("host") or info.get("ssh", "")
                if host:
                    tasks[name] = _get_containers_remote(host)

            results = {}
            gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for mname, res in zip(tasks.keys(), gathered):
                if isinstance(res, Exception):
                    logger.debug("Health: gather error for %s: %s", mname, res)
                    results[mname] = []
                else:
                    results[mname] = res

            # Process each container
            http_tasks = []  # (key, url)
            log_tasks = []   # (key, container, machine, host)

            for machine, containers in results.items():
                host = None
                if machine != "local":
                    info = machines_config.get(machine, "")
                    host = info if isinstance(info, str) else info.get("host") or info.get("ssh", "")

                for c in containers:
                    cname = c["name"]
                    if cname in HEALTH_SKIP_CONTAINERS:
                        continue
                    key = f"{machine}:{cname}"

                    # Initialize entry if new
                    if key not in st.health_status:
                        st.health_status[key] = {
                            "machine": machine,
                            "container": cname,
                            "state": "unknown",
                            "health": "unknown",
                            "http_status": None,
                            "http_latency_ms": None,
                            "last_check": 0,
                            "last_healthy": 0,
                            "last_error": "",
                            "recent_errors": [],
                            "consecutive_failures": 0,
                        }

                    entry = st.health_status[key]
                    entry["state"] = c["state"]
                    entry["last_check"] = now

                    # Queue HTTP check if applicable
                    remote_key = f"{machine}:{cname}"
                    if machine == "local" and cname in HEALTH_HTTP_CHECKS and c["state"] == "running":
                        http_tasks.append((key, HEALTH_HTTP_CHECKS[cname], None))
                    elif remote_key in HEALTH_REMOTE_HTTP_CHECKS and c["state"] == "running":
                        http_tasks.append((key, HEALTH_REMOTE_HTTP_CHECKS[remote_key], host))

                    # Queue log scan for running app containers that have HTTP checks
                    # OR are monitored via Docker's native healthcheck.
                    has_http = (machine == "local" and cname in HEALTH_HTTP_CHECKS) or remote_key in HEALTH_REMOTE_HTTP_CHECKS
                    has_native = _has_native_check(machine, cname)
                    if c["state"] == "running" and (has_http or has_native):
                        log_tasks.append((key, cname, machine, host))

                    # Stash docker's native healthcheck status (if any) for later use.
                    if has_native:
                        entry["native_health"] = _parse_native_health(c.get("status", ""))

            # Run HTTP health checks concurrently
            if http_tasks:
                http_results = await asyncio.gather(
                    *[
                        _http_health_check(url) if host is None
                        else _remote_http_health_check(host, url)
                        for _, url, host in http_tasks
                    ],
                    return_exceptions=True
                )
                for (key, _url, _host), result in zip(http_tasks, http_results):
                    if key in st.health_status:
                        if isinstance(result, Exception):
                            st.health_status[key]["http_status"] = None
                            st.health_status[key]["http_latency_ms"] = None
                        else:
                            status_code, latency = result
                            st.health_status[key]["http_status"] = status_code
                            st.health_status[key]["http_latency_ms"] = latency

            # Run log scans concurrently
            if log_tasks:
                log_results = await asyncio.gather(
                    *[_scan_container_logs(cname, machine, host) for _, cname, machine, host in log_tasks],
                    return_exceptions=True
                )
                for (key, *_), result in zip(log_tasks, log_results):
                    if key in st.health_status and not isinstance(result, Exception):
                        st.health_status[key]["recent_errors"] = result

            # Determine health status and check for transitions
            for key, entry in st.health_status.items():
                old_health = entry.get("health", "unknown")
                state = entry.get("state", "unknown")
                is_native = _has_native_check(entry["machine"], entry["container"])
                native = entry.get("native_health") if is_native else None

                if state != "running":
                    new_health = "unhealthy"
                    entry["last_error"] = f"Container state: {state}"
                elif is_native and native == "unhealthy":
                    new_health = "unhealthy"
                    entry["last_error"] = "Docker healthcheck reports container as unhealthy"
                elif is_native and native == "starting":
                    # Treat 'starting' as healthy-ish so we don't alert during boot
                    new_health = "healthy"
                    entry["last_error"] = ""
                elif entry.get("http_status") is not None and entry["http_status"] >= 400:
                    new_health = "unhealthy"
                    entry["last_error"] = f"HTTP health check returned {entry['http_status']}"
                elif entry.get("http_status") is None and _has_http_check(entry["machine"], entry["container"]):
                    new_health = "unhealthy"
                    entry["last_error"] = "HTTP health check failed (connection refused)"
                elif entry.get("recent_errors"):
                    new_health = "degraded"
                    entry["last_error"] = entry["recent_errors"][-1] if entry["recent_errors"] else ""
                else:
                    new_health = "healthy"
                    entry["last_error"] = ""

                # Track consecutive failures
                prev_last_healthy = entry.get("last_healthy", 0)
                if new_health in ("unhealthy", "degraded"):
                    entry["consecutive_failures"] = entry.get("consecutive_failures", 0) + 1
                else:
                    entry["consecutive_failures"] = 0
                    entry["last_healthy"] = now

                # Only confirm unhealthy/degraded after consecutive threshold
                # This prevents the transition from being "used up" before alert fires
                confirmed_health = new_health
                if new_health in ("unhealthy", "degraded") and entry["consecutive_failures"] < HEALTH_CONSECUTIVE_FAILURES:
                    confirmed_health = old_health  # keep old state until confirmed

                entry["health"] = confirmed_health

                # Alert on confirmed state transitions
                alert_key = f"health:{key}"
                should_alert = False

                if confirmed_health == "unhealthy" and old_health != "unhealthy":
                    should_alert = True
                elif confirmed_health == "degraded" and old_health == "healthy":
                    should_alert = True
                elif confirmed_health == "healthy" and old_health in ("unhealthy", "degraded"):
                    should_alert = True

                if should_alert:
                    is_recovery = confirmed_health == "healthy"
                    last_alerted = st.alert_cooldowns.get(alert_key, 0)
                    # Recovery alerts always go through; down alerts respect cooldown
                    if is_recovery or (now - last_alerted >= ALERT_COOLDOWN_SECS):
                        st.alert_cooldowns[alert_key] = now
                        await _send_health_alert(
                            app, entry["container"], entry["machine"],
                            confirmed_health, entry.get("last_error", ""),
                            prev_last_healthy,
                        )

            # Remove stale entries (containers that disappeared)
            active_keys = set()
            for machine, containers in results.items():
                for c in containers:
                    active_keys.add(f"{machine}:{c['name']}")
            stale = [k for k in st.health_status if k not in active_keys]
            for k in stale:
                del st.health_status[k]

            st.health_last_full_check = now
            cycle_count += 1

            # Persist every 5 cycles (~5 minutes)
            if cycle_count % 5 == 0:
                save_json(HEALTH_STATUS_FILE, st.health_status)

        except Exception as e:
            logger.error("Health check loop error: %s", e)

        await asyncio.sleep(HEALTH_CHECK_INTERVAL)

