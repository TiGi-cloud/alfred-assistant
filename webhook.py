"""Alfred webhook server — REST API and Mini App backend."""
from __future__ import annotations

import os
import re
import json
import time
import socket
import hashlib
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from aiohttp import web

from telegram.constants import ParseMode

import bot_state as st
from config import (
    WEBHOOK_SECRET, WEBHOOK_PORT, WEBAPP_DIR,
    SCHEDULES_FILE, GEOFENCES_FILE, WEBAPP_PIN_FILE, ALERTS_FILE,
    DANGEROUS_PATTERNS, METRICS_MAX,
)
from persistence import load_json, save_json
from utils.formatting import E, fmt_elapsed
from utils.helpers import async_run, parse_natural_schedule

from claude_runner import run_claude as _run_claude, send_response as _send_response, _build_claude_cmd, _track_cost
from core import get_system_status as _get_system_status, save_sessions as _save_sessions, load_machines as _load_machines, user_key as _user_key_fn

logger = logging.getLogger("alfred")


async def start_webhook_server(app):
    def check_auth(request):
        if not WEBHOOK_SECRET:
            return True
        auth = request.headers.get("Authorization", "")
        # Method 1: Bearer token (backward compatible)
        if auth == f"Bearer {WEBHOOK_SECRET}":
            return True
        # Method 2: HMAC signature (X-Signature header)
        sig = request.headers.get("X-Signature", "")
        if sig:
            import hmac as _hmac
            ts = request.headers.get("X-Timestamp", "")
            # Reject if timestamp is > 5 min old (replay protection)
            try:
                if abs(time.time() - float(ts)) > 300:
                    return False
            except (ValueError, TypeError):
                return False
            body = request.headers.get("X-Body-Hash", request.path)
            expected = _hmac.new(
                WEBHOOK_SECRET.encode(), f"{ts}:{body}".encode(), hashlib.sha256
            ).hexdigest()
            if _hmac.compare_digest(sig, expected):
                return True
        # Method 3: JWT token
        if auth.startswith("Bearer ") and "." in auth[7:]:
            token = auth[7:]
            try:
                import base64
                # Simple JWT verification (HS256 only, no external deps)
                parts = token.split(".")
                if len(parts) != 3:
                    return False
                payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64))
                # Check expiry
                if payload.get("exp", 0) < time.time():
                    return False
                # Verify signature
                import hmac as _hmac
                sig_input = f"{parts[0]}.{parts[1]}".encode()
                expected_sig = base64.urlsafe_b64encode(
                    _hmac.new(WEBHOOK_SECRET.encode(), sig_input, hashlib.sha256).digest()
                ).rstrip(b"=").decode()
                if _hmac.compare_digest(parts[2], expected_sig):
                    return True
            except Exception:
                pass
        return False

    async def handle_webhook(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        try:
            message = data.get("message", data.get("text", str(data)))
            chat_id = data.get("chat_id", st._default_chat_id)
            action = data.get("action")

            if not chat_id:
                return web.json_response({"error": "No chat_id. Send a message to the bot first."}, status=400)

            if action:
                ukey = data.get("user_key", "webhook")
                response = await _run_claude(action, ukey)
                await _send_response(None, response, bot=app.bot, chat_id=chat_id)
                return web.json_response({"ok": True, "result": "Action executed"})
            else:
                await app.bot.send_message(chat_id, f"\U0001f517 <b>Webhook:</b>\n{E(message[:4000])}", parse_mode=ParseMode.HTML)
                return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # Mini App API endpoints
    async def api_status(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        info = await _get_system_status()
        uptime = int(time.time() - st._start_time)
        return web.json_response({
            "status": info,
            "uptime_seconds": uptime,
            "uptime": fmt_elapsed(uptime),
            "sessions": len(st.user_sessions),
            "active_tasks": sum(st.user_request_count.values()),
            "alerts": sum(len(v) for v in st.alerts.values()),
            "plugins": list(st.plugins.keys()),
            "schedules": len(load_json(SCHEDULES_FILE, [])),
        })

    async def api_files(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        path = request.query.get("path", str(Path.home() / "Desktop"))
        if not os.path.isdir(path):
            return web.json_response({"error": "Not a directory"}, status=400)
        show_hidden = request.query.get("hidden", "false").lower() == "true"
        entries = []
        for entry in sorted(os.listdir(path))[:50]:
            if not show_hidden and entry.startswith("."):
                continue
            full = os.path.join(path, entry)
            try:
                size = os.path.getsize(full) if os.path.isfile(full) else 0
            except OSError:
                size = 0
            entries.append({
                "name": entry,
                "is_dir": os.path.isdir(full),
                "size": size,
            })
        return web.json_response({"path": path, "entries": entries})

    async def api_cost(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        # Return usage stats (tokens, requests) without cost calculations
        usage = {}
        for ukey, stats in st.cost_tracker.items():
            usage[ukey] = {
                "requests": stats.get("requests", 0),
                "input_tokens": stats.get("input_tokens", 0),
                "output_tokens": stats.get("output_tokens", 0),
            }
        return web.json_response(usage)

    async def api_usage_limits(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        import time as _time
        import datetime as _dtmod
        _dt = _dtmod.datetime
        now = _time.time()
        hour_ago = now - 3600
        week_ago = now - 7 * 86400

        # Aggregate across all users (local bot tracking)
        hourly_reqs = 0
        hourly_tokens = 0
        weekly_reqs = 0
        weekly_tokens = 0
        for entries in st.usage_hourly.values():
            for e in entries:
                if e["ts"] > hour_ago:
                    hourly_reqs += 1
                    hourly_tokens += e["in"] + e["out"]
        for entries in st.usage_weekly.values():
            for e in entries:
                if e["ts"] > week_ago:
                    weekly_reqs += 1
                    weekly_tokens += e["in"] + e["out"]

        hourly_limit = 50
        weekly_limit = 500
        hourly_pct = min(int(hourly_reqs / hourly_limit * 100), 100)
        weekly_pct = min(int(weekly_reqs / weekly_limit * 100), 100)
        mins_left = 60 - (int(now) % 3600) // 60
        dt = _dt.now()
        days_to_mon = (7 - dt.weekday()) % 7 or 7
        reset_day = dt.replace(hour=11, minute=0, second=0) + _dtmod.timedelta(days=days_to_mon)

        # ---- Claude subscription data from stats-cache.json ----
        subscription = {}
        try:
            import pathlib
            stats_path = pathlib.Path.home() / ".claude" / "stats-cache.json"
            creds_path = pathlib.Path.home() / ".claude" / ".credentials.json"
            if stats_path.exists():
                import json as _json
                stats = _json.loads(stats_path.read_text())
                # Model usage totals
                model_usage = stats.get("modelUsage", {})
                models = []
                total_in = 0
                total_out = 0
                total_cache_read = 0
                total_cache_write = 0
                for model_name, mu in model_usage.items():
                    inp = mu.get("inputTokens", 0)
                    out = mu.get("outputTokens", 0)
                    cr = mu.get("cacheReadInputTokens", 0)
                    cw = mu.get("cacheCreationInputTokens", 0)
                    total_in += inp
                    total_out += out
                    total_cache_read += cr
                    total_cache_write += cw
                    models.append({
                        "name": model_name,
                        "input_tokens": inp,
                        "output_tokens": out,
                        "cache_read": cr,
                        "cache_write": cw,
                    })
                # Daily activity (last 14 days)
                daily = []
                for entry in stats.get("dailyActivity", [])[-14:]:
                    daily.append({
                        "date": entry.get("date", ""),
                        "messages": entry.get("messageCount", 0),
                        "sessions": entry.get("sessionCount", 0),
                        "tools": entry.get("toolCallCount", 0),
                    })
                # Daily model tokens (last 14 days)
                daily_tokens = []
                for entry in stats.get("dailyModelTokens", [])[-14:]:
                    tokens_total = sum(entry.get("tokensByModel", {}).values())
                    daily_tokens.append({
                        "date": entry.get("date", ""),
                        "tokens": tokens_total,
                    })
                subscription = {
                    "total_sessions": stats.get("totalSessions", 0),
                    "total_messages": stats.get("totalMessages", 0),
                    "first_session": stats.get("firstSessionDate", ""),
                    "last_computed": stats.get("lastComputedDate", ""),
                    "total_input_tokens": total_in,
                    "total_output_tokens": total_out,
                    "total_cache_read": total_cache_read,
                    "total_cache_write": total_cache_write,
                    "models": models,
                    "daily_activity": daily,
                    "daily_tokens": daily_tokens,
                }
                # Longest session
                ls = stats.get("longestSession", {})
                if ls:
                    dur_ms = ls.get("duration", 0)
                    hrs = dur_ms // 3600000
                    mins = (dur_ms % 3600000) // 60000
                    subscription["longest_session"] = f"{hrs}h {mins}m ({ls.get('messageCount', 0)} msgs)"
            # Subscription type from credentials
            if creds_path.exists():
                import json as _json
                creds = _json.loads(creds_path.read_text())
                oauth = creds.get("claudeAiOauth", {})
                subscription["plan"] = oauth.get("subscriptionType") or "Max"
                subscription["rate_tier"] = oauth.get("rateLimitTier") or "—"
        except Exception:
            pass

        return web.json_response({
            "hourly_pct": hourly_pct,
            "weekly_pct": weekly_pct,
            "hourly_reqs": hourly_reqs,
            "hourly_tokens": hourly_tokens,
            "weekly_reqs": weekly_reqs,
            "weekly_tokens": weekly_tokens,
            "hour_reset": f"Resets in {mins_left} min",
            "week_reset": f"Resets {reset_day.strftime('%a %I:%M %p')}",
            "subscription": subscription,
        })

    _live_usage_cache = {"ts": 0, "data": None}

    async def api_live_usage(request):
        import time as _time
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        cached = _live_usage_cache
        age = _time.time() - cached["ts"]
        if cached["data"] is not None and age < 300:
            return web.json_response({**cached["data"], "cached_seconds": int(age)})
        import json as _json, pathlib
        script = pathlib.Path(__file__).parent / "get_claude_usage.py"
        if not script.exists():
            return web.json_response({"error": "get_claude_usage.py not found"})
        try:
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/python3", str(script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=40)
            out = stdout.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                return web.json_response({"error": f"Script failed: {stderr.decode()[:200]}"})
            data = _json.loads(out)
            if "error" in data and not any(k.endswith("_pct") for k in data):
                return web.json_response({"error": data.get("error", "Parse failed"), "raw": data.get("raw", "")})
            _live_usage_cache["ts"] = _time.time()
            _live_usage_cache["data"] = data
            return web.json_response(data)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return web.json_response({"error": "Timed out (40s)"})
        except Exception as e:
            return web.json_response({"error": str(e)})

    async def api_history(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            limit = min(int(request.query.get("limit", "30")), 500)
        except (ValueError, OverflowError):
            limit = 30
        all_entries = []
        for ukey, entries in st.history.items():
            # Use only the chat_id portion to avoid exposing user IDs
            user_label = ukey.split(":", 1)[0][:6] + "…" if ":" in ukey else ukey[:6] + "…"
            for e in entries:
                all_entries.append({**e, "user": user_label})
        all_entries.sort(key=lambda x: x.get("ts", x.get("time", "")), reverse=True)
        return web.json_response(all_entries[:limit])

    async def api_schedules(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response(load_json(SCHEDULES_FILE, []))

    async def api_schedules_delete(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            idx = int(data.get("index", -1))
            scheds = load_json(SCHEDULES_FILE, [])
            if 0 <= idx < len(scheds):
                removed = scheds.pop(idx)
                save_json(SCHEDULES_FILE, scheds)
                return web.json_response({"ok": True, "removed": removed})
            return web.json_response({"error": "Invalid index"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_alerts(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response(st.alerts)

    async def api_alerts_delete(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            ukey = data.get("user", "")
            idx = int(data.get("index", -1))
            if ukey in st.alerts and 0 <= idx < len(st.alerts[ukey]):
                removed = st.alerts[ukey].pop(idx)
                save_json(ALERTS_FILE, st.alerts)
                return web.json_response({"ok": True, "removed": removed})
            return web.json_response({"error": "Invalid user/index"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_machines(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        machines = _load_machines()
        # Only expose machine targets, not user IDs
        active_targets = list(set(st.user_machines.values()))
        return web.json_response({"machines": machines, "active_targets": active_targets})

    async def _get_local_stats():
        """Get stats for the local macOS machine."""
        import shutil
        result = {}
        try:
            # CPU usage via top (macOS)
            proc = await asyncio.create_subprocess_shell(
                "top -l 1 -n 0 | grep 'CPU usage'",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            line = out.decode().strip()
            # "CPU usage: 5.26% user, 10.52% sys, 84.21% idle"
            if 'idle' in line:
                idle = float(line.split('idle')[0].split(',')[-1].strip().rstrip('%'))
                result['cpu'] = round(100 - idle, 1)
        except Exception:
            result['cpu'] = None
        try:
            # Memory via vm_stat + sysctl
            proc = await asyncio.create_subprocess_shell(
                "vm_stat",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            vm = out.decode()
            page_size = 16384  # default
            for l in vm.splitlines():
                if 'page size' in l:
                    page_size = int(l.split()[-2])
            pages_active = pages_wired = pages_compressed = 0
            pages_free = pages_inactive = pages_speculative = 0
            for l in vm.splitlines():
                parts = l.split(':')
                if len(parts) == 2:
                    val = int(parts[1].strip().rstrip('.'))
                    k = parts[0].strip().lower()
                    if 'pages active' in k: pages_active = val
                    elif 'pages wired' in k: pages_wired = val
                    elif 'pages occupied by compressor' in k: pages_compressed = val
                    elif 'pages free' in k: pages_free = val
                    elif 'pages inactive' in k: pages_inactive = val
                    elif 'pages speculative' in k: pages_speculative = val
            used_mb = (pages_active + pages_wired + pages_compressed) * page_size / 1048576
            proc2 = await asyncio.create_subprocess_shell(
                "sysctl -n hw.memsize",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out2, _ = await asyncio.wait_for(proc2.communicate(), timeout=5)
            total_mb = int(out2.decode().strip()) / 1048576
            result['ram_used'] = round(used_mb)
            result['ram_total'] = round(total_mb)
        except Exception:
            result['ram_used'] = result['ram_total'] = None
        try:
            disk = shutil.disk_usage('/')
            result['disk_used'] = round(disk.used / (1024**3), 1)
            result['disk_total'] = round(disk.total / (1024**3), 1)
        except Exception:
            result['disk_used'] = result['disk_total'] = None
        try:
            load = os.getloadavg()
            result['load'] = [round(x, 2) for x in load]
        except Exception:
            result['load'] = None
        try:
            proc = await asyncio.create_subprocess_shell(
                "uptime | sed 's/.*up //' | sed 's/,[^,]*load.*//' | xargs",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            result['uptime'] = out.decode().strip()
        except Exception:
            result['uptime'] = None
        result['os'] = 'macOS'
        result['online'] = True
        return result

    async def _get_remote_stats(host):
        """Get stats for a remote Linux machine via SSH."""
        cmd = (
            "echo OS:$(uname -s);"
            "echo CPU:$(grep 'cpu ' /proc/stat | awk '{u=$2+$4; t=$2+$4+$5; if(t>0) printf \"%.1f\", u/t*100; else print \"0\"}');"
            "echo MEM:$(free -m | awk '/Mem:/{print $3\",\"$2}');"
            "echo DISK:$(df -h / | awk 'NR==2{print $3\",\"$2}');"
            "echo LOAD:$(cat /proc/loadavg | awk '{print $1\",\"$2\",\"$3}');"
            "echo UPTIME:$(uptime -p 2>/dev/null | sed 's/up //' || uptime | sed 's/.*up //' | sed 's/,[^,]*load.*//' | xargs)"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", host, cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
            if proc.returncode != 0:
                return {'online': False}
            result = {'online': True}
            for line in out.decode().strip().splitlines():
                if ':' not in line:
                    continue
                key, val = line.split(':', 1)
                key, val = key.strip(), val.strip()
                if key == 'OS':
                    result['os'] = val
                elif key == 'CPU':
                    try: result['cpu'] = float(val)
                    except: result['cpu'] = None
                elif key == 'MEM':
                    parts = val.split(',')
                    if len(parts) == 2:
                        try:
                            result['ram_used'] = int(parts[0])
                            result['ram_total'] = int(parts[1])
                        except: pass
                elif key == 'DISK':
                    parts = val.split(',')
                    if len(parts) == 2:
                        result['disk_used_str'] = parts[0]
                        result['disk_total_str'] = parts[1]
                elif key == 'LOAD':
                    parts = val.split(',')
                    try: result['load'] = [float(x) for x in parts]
                    except: result['load'] = None
                elif key == 'UPTIME':
                    result['uptime'] = val
            return result
        except asyncio.TimeoutError:
            return {'online': False, 'error': 'timeout'}
        except Exception as e:
            return {'online': False, 'error': str(e)}

    async def api_machine_stats(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        machines = _load_machines()
        tasks = {'local': _get_local_stats()}
        for name, info in machines.items():
            host = info if isinstance(info, str) else info.get("host", "")
            if host:
                tasks[name] = _get_remote_stats(host)
        results = {}
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, res in zip(tasks.keys(), gathered):
            if isinstance(res, Exception):
                results[name] = {'online': False, 'error': str(res)}
            else:
                results[name] = res
        return web.json_response({"stats": results})

    async def _get_local_docker():
        """Get docker containers running locally."""
        try:
            proc = await asyncio.create_subprocess_shell(
                'docker ps --format "{{.ID}}\\t{{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}\\t{{.State}}"',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            if proc.returncode != 0:
                return {"error": "docker not running", "containers": []}
            containers = []
            for line in out.decode().strip().splitlines():
                if not line.strip():
                    continue
                parts = line.split('\t')
                if len(parts) >= 6:
                    containers.append({
                        "id": parts[0][:12],
                        "name": parts[1],
                        "image": parts[2],
                        "status": parts[3],
                        "ports": parts[4],
                        "state": parts[5],
                    })
            return {"containers": containers}
        except Exception as e:
            return {"error": str(e), "containers": []}

    async def _get_remote_docker(host):
        """Get docker containers on a remote machine via SSH."""
        cmd = 'docker ps --format "{{.ID}}\\t{{.Names}}\\t{{.Image}}\\t{{.Status}}\\t{{.Ports}}\\t{{.State}}"'
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                "-o", "BatchMode=yes", host, cmd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=12)
            if proc.returncode != 0:
                return {"error": "docker not available", "containers": []}
            containers = []
            for line in out.decode().strip().splitlines():
                if not line.strip():
                    continue
                parts = line.split('\t')
                if len(parts) >= 6:
                    containers.append({
                        "id": parts[0][:12],
                        "name": parts[1],
                        "image": parts[2],
                        "status": parts[3],
                        "ports": parts[4],
                        "state": parts[5],
                    })
            return {"containers": containers}
        except asyncio.TimeoutError:
            return {"error": "timeout", "containers": []}
        except Exception as e:
            return {"error": str(e), "containers": []}

    async def api_docker_status(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        machines = _load_machines()
        tasks = {'local': _get_local_docker()}
        for name, info in machines.items():
            host = info if isinstance(info, str) else info.get("host", "")
            if host:
                tasks[name] = _get_remote_docker(host)
        results = {}
        gathered = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, res in zip(tasks.keys(), gathered):
            if isinstance(res, Exception):
                results[name] = {"error": str(res), "containers": []}
            else:
                results[name] = res
        return web.json_response({"docker": results})

    async def api_screenshot(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        import base64
        path = "/tmp/webapp_screenshot.png"
        proc = await asyncio.create_subprocess_exec(
            "/usr/sbin/screencapture", "-x", path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()
        if os.path.isfile(path):
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            os.remove(path)
            return web.json_response({"image": b64})
        return web.json_response({"error": "Screenshot failed"}, status=500)

    async def api_execute(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            cmd = data.get("command", "")
            if not cmd:
                return web.json_response({"error": "No command"}, status=400)
            # Safety: block dangerous commands
            for pattern in DANGEROUS_PATTERNS:
                if re.search(pattern, cmd, re.IGNORECASE):
                    return web.json_response({"error": "Blocked: destructive command"}, status=403)
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return web.json_response({"error": "Command timed out (30s)"}, status=408)
            return web.json_response({
                "stdout": stdout.decode(errors="replace")[:10000],
                "stderr": stderr.decode(errors="replace")[:5000],
                "returncode": proc.returncode,
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_execute_start(request):
        """Start a long-running command and return an ID for polling."""
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            cmd = data.get("command", "")
            if not cmd:
                return web.json_response({"error": "No command"}, status=400)
            for pattern in DANGEROUS_PATTERNS:
                if re.search(pattern, cmd, re.IGNORECASE):
                    return web.json_response({"error": "Blocked: destructive command"}, status=403)

            st._term_id_counter += 1
            tid = str(st._term_id_counter)

            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PATH": f"/usr/local/bin:/usr/bin:/bin:/sbin:/usr/sbin:{os.environ.get('PATH', '')}"},
            )
            entry = {"proc": proc, "output": "", "done": False, "returncode": None}
            st.terminal_processes[tid] = entry

            async def _reader():
                try:
                    while True:
                        line = await proc.stdout.readline()
                        if not line:
                            break
                        entry["output"] += line.decode(errors="replace")
                        # Keep buffer limited
                        if len(entry["output"]) > 50000:
                            entry["output"] = entry["output"][-40000:]
                    stderr_data = await proc.stderr.read()
                    if stderr_data:
                        entry["output"] += stderr_data.decode(errors="replace")
                    await proc.wait()
                    entry["returncode"] = proc.returncode
                except Exception:
                    pass
                finally:
                    entry["done"] = True
                    entry["finished_at"] = time.time()

            rt = asyncio.create_task(_reader())
            st.reminder_tasks.add(rt)  # reuse strong-ref set for fire-and-forget tasks
            rt.add_done_callback(st.reminder_tasks.discard)
            # Prune completed entries older than 10 minutes
            cutoff = time.time() - 600
            stale = [k for k, v in st.terminal_processes.items() if v.get("done") and v.get("finished_at", 0) < cutoff]
            for k in stale:
                st.terminal_processes.pop(k, None)
            return web.json_response({"id": tid})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_execute_poll(request):
        """Poll output of a running command."""
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        tid = request.query.get("id", "")
        try:
            offset = int(request.query.get("offset", "0"))
        except (ValueError, TypeError):
            offset = 0
        entry = st.terminal_processes.get(tid)
        if not entry:
            return web.json_response({"error": "Unknown process"}, status=404)
        chunk = entry["output"][offset:]
        limited = chunk[:20000]
        return web.json_response({
            "output": limited,
            "offset": offset + len(limited),
            "done": entry["done"],
            "returncode": entry["returncode"],
        })

    async def api_execute_cancel(request):
        """Cancel a running command."""
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            tid = data.get("id", "")
            entry = st.terminal_processes.get(tid)
            if not entry:
                return web.json_response({"error": "Unknown process"}, status=404)
            if not entry["done"]:
                try:
                    entry["proc"].kill()
                except ProcessLookupError:
                    pass
                entry["output"] += "\n[Cancelled by user]"
                entry["done"] = True
                entry["returncode"] = -9
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_quick_action(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            action = data.get("action", "")
            actions = {
                "screenshot": "screencapture -x /tmp/screenshot.png && echo 'Screenshot saved'",
                "clipboard": "pbpaste 2>/dev/null || echo '(empty)'",
                "ip": "ipconfig getifaddr en0 2>/dev/null || echo 'N/A'",
                "uptime": "uptime",
                "disk": "df -h / | tail -1",
                "top": "ps -Ao pid,%cpu,%mem,comm -r | head -6",
                "wifi": "iface=$(networksetup -listallhardwareports | awk '/Wi-Fi/{getline; print $2}'); networksetup -getairportnetwork \"$iface\" 2>/dev/null || echo 'N/A'",
            }
            cmd = actions.get(action)
            if not cmd:
                return web.json_response({"error": f"Unknown action: {action}"}, status=400)
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return web.json_response({"error": "Quick action timed out"}, status=408)
            return web.json_response({
                "output": stdout.decode(errors="replace").strip()[:5000],
                "error": stderr.decode(errors="replace").strip()[:1000],
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_wake(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            name = data.get("machine", "")
            machines = _load_machines()
            if name not in machines:
                return web.json_response({"error": f"Unknown machine: {name}"}, status=400)
            info = machines[name]
            if isinstance(info, str):
                return web.json_response({"error": f"Machine {name} has no MAC address configured"}, status=400)
            mac = info.get("mac")
            if not mac:
                return web.json_response({"error": f"No MAC for {name}"}, status=400)
            # Send WOL magic packet
            try:
                mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
            except ValueError:
                return web.json_response({"error": f"Invalid MAC address format: {mac}"}, status=400)
            if len(mac_bytes) != 6:
                return web.json_response({"error": "MAC address must be 6 bytes"}, status=400)
            packet = b'\xff' * 6 + mac_bytes * 16
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(packet, ('255.255.255.255', 9))
            finally:
                s.close()
            return web.json_response({"ok": True, "message": f"WOL sent to {name}"})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def serve_webapp(request):
        index = WEBAPP_DIR / "index.html"
        if index.exists():
            html_content = index.read_text()
            # Inject WEBHOOK_SECRET so the web app can authenticate API calls
            if WEBHOOK_SECRET:
                injection = f'<script>window.__ALFRED_SECRET__ = {json.dumps(WEBHOOK_SECRET)};</script>'
                html_content = html_content.replace('</head>', f'{injection}\n</head>', 1)
            return web.Response(text=html_content, content_type="text/html")
        return web.Response(text="Mini App not found. Create webapp/index.html", status=404)

    async def api_chat(request):
        """Send a plain message to the bot's default (or specified) chat."""
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
        text = data.get("text", "").strip()
        if not text:
            return web.json_response({"error": "No text provided"}, status=400)
        chat_id = data.get("chat_id", st._default_chat_id)
        if not chat_id:
            return web.json_response({"error": "No chat_id. Send a message to the bot first."}, status=400)
        try:
            await app.bot.send_message(chat_id, E(text[:4000]), parse_mode=ParseMode.HTML)
            return web.json_response({"ok": True})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_health(request):
        """Public health check — no auth required."""
        uptime = int(time.time() - st._start_time)
        return web.json_response({
            "status": "ok",
            "uptime_seconds": uptime,
            "uptime": fmt_elapsed(uptime),
        })

    # --- NEW: Metrics history API (sparklines) ---
    async def api_metrics(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            limit = min(int(request.query.get("limit", "60")), METRICS_MAX)
        except (ValueError, OverflowError):
            limit = 60
        return web.json_response(st.metrics_history[-limit:])

    # --- NEW: Health score API ---
    async def api_health_score(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        info = await _get_system_status()
        try:
            cpu = float(info.get("CPU_PCT", "0").split()[0])
        except ValueError:
            cpu = 0
        try:
            mem_free = int(info.get("MEM_FREE_MB", "0"))
            mem_total = int(info.get("MEM_TOTAL_MB", "1"))
            mem_pct = (mem_total - mem_free) / mem_total * 100 if mem_total else 0
        except ValueError:
            mem_pct = 0
        try:
            disk_pct = int(info.get("DISK_PCT", "0"))
        except ValueError:
            disk_pct = 0

        # Health score: weighted average (lower usage = higher score)
        cpu_score = max(0, 100 - cpu)
        mem_score = max(0, 100 - mem_pct)
        disk_score = max(0, 100 - disk_pct)
        health = int(cpu_score * 0.4 + mem_score * 0.35 + disk_score * 0.25)

        level = "critical" if health < 30 else "warning" if health < 60 else "good" if health < 85 else "excellent"
        return web.json_response({
            "score": health, "level": level,
            "cpu": round(cpu, 1), "mem": round(mem_pct, 1), "disk": disk_pct,
            "details": {
                "cpu_score": round(cpu_score, 1),
                "mem_score": round(mem_score, 1),
                "disk_score": round(disk_score, 1),
            }
        })

    # --- Docker Health Monitor API ---
    async def api_health_monitor(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        machine_filter = request.query.get("machine", "")

        services = []
        for key, info in st.health_status.items():
            if machine_filter and info.get("machine") != machine_filter:
                continue
            services.append({
                "key": key,
                "machine": info.get("machine", ""),
                "container": info.get("container", ""),
                "state": info.get("state", "unknown"),
                "health": info.get("health", "unknown"),
                "http_status": info.get("http_status"),
                "http_latency_ms": info.get("http_latency_ms"),
                "last_check": info.get("last_check", 0),
                "last_healthy": info.get("last_healthy", 0),
                "last_error": info.get("last_error", ""),
                "recent_errors": info.get("recent_errors", [])[-5:],
                "consecutive_failures": info.get("consecutive_failures", 0),
            })

        order = {"unhealthy": 0, "degraded": 1, "unknown": 2, "healthy": 3}
        services.sort(key=lambda s: (order.get(s["health"], 9), s["machine"], s["container"]))

        summary = {"healthy": 0, "unhealthy": 0, "degraded": 0, "unknown": 0}
        for s in services:
            h = s["health"]
            summary[h] = summary.get(h, 0) + 1

        return web.json_response({
            "services": services,
            "summary": summary,
            "last_check": st.health_last_full_check,
        })

    async def api_container_logs(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        container = request.query.get("container", "")
        machine = request.query.get("machine", "local")
        lines = min(int(request.query.get("lines", "50")), 200)
        if not container or not re.match(r'^[a-zA-Z0-9_.-]+$', container):
            return web.json_response({"error": "Invalid container name"}, status=400)

        cmd = f'docker logs --tail {lines} {container} 2>&1'
        try:
            if machine == "local":
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            else:
                machines = _load_machines()
                info = machines.get(machine, "")
                host = info if isinstance(info, str) else info.get("host") or info.get("ssh", "")
                if not host:
                    return web.json_response({"error": "Unknown machine"}, status=400)
                proc = await asyncio.create_subprocess_exec(
                    "ssh", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
                    "-o", "BatchMode=yes", host, cmd,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), timeout=15)
            output = out.decode(errors="replace")
            if not output and err:
                output = err.decode(errors="replace")
            return web.json_response({
                "logs": output[:20000],
                "container": container,
                "machine": machine,
            })
        except asyncio.TimeoutError:
            return web.json_response({"error": "Timeout fetching logs"}, status=408)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # --- NEW: Commands list API (command palette) ---
    async def api_commands(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        cmds = [
            {"cmd": "/screenshot", "desc": "Take a screenshot", "category": "screen"},
            {"cmd": "/record", "desc": "Record screen [seconds]", "category": "screen"},
            {"cmd": "/watch", "desc": "Live screen stream (toggle)", "category": "screen"},
            {"cmd": "/camera", "desc": "FaceTime camera photo", "category": "screen"},
            {"cmd": "/snap", "desc": "Named snapshots (save/view/compare)", "category": "screen"},
            {"cmd": "/status", "desc": "System info (CPU, RAM, disk)", "category": "system"},
            {"cmd": "/clipboard", "desc": "Get/set clipboard", "category": "system"},
            {"cmd": "/apps", "desc": "App launcher", "category": "system"},
            {"cmd": "/logs", "desc": "View bot logs", "category": "system"},
            {"cmd": "/battery", "desc": "Battery status", "category": "system"},
            {"cmd": "/processes", "desc": "Running processes", "category": "system"},
            {"cmd": "/volume", "desc": "Get/set volume", "category": "system"},
            {"cmd": "/browse", "desc": "Interactive file browser", "category": "files"},
            {"cmd": "/export", "desc": "Export conversation as file", "category": "files"},
            {"cmd": "/search", "desc": "Search files by name", "category": "files"},
            {"cmd": "/clear", "desc": "Start new AI conversation", "category": "ai"},
            {"cmd": "/model", "desc": "Switch Claude model", "category": "ai"},
            {"cmd": "/cost", "desc": "Claude usage stats", "category": "ai"},
            {"cmd": "/undo", "desc": "Undo last action", "category": "ai"},
            {"cmd": "/fork", "desc": "Branch conversations", "category": "ai"},
            {"cmd": "/history", "desc": "Command history", "category": "ai"},
            {"cmd": "/research", "desc": "Deep research (15 agents)", "category": "ai"},
            {"cmd": "/machine", "desc": "Switch target machine", "category": "remote"},
            {"cmd": "/wake", "desc": "Wake-on-LAN", "category": "remote"},
            {"cmd": "/shortcut", "desc": "Run Siri Shortcut", "category": "remote"},
            {"cmd": "/hey", "desc": "Voice assistant bridge", "category": "remote"},
            {"cmd": "/schedule", "desc": "Scheduled tasks (cron)", "category": "auto"},
            {"cmd": "/alert", "desc": "System alerts", "category": "auto"},
            {"cmd": "/notifications", "desc": "macOS notification forwarding", "category": "auto"},
            {"cmd": "/remind", "desc": "Set a reminder", "category": "auto"},
            {"cmd": "/timer", "desc": "Set a countdown timer", "category": "auto"},
            {"cmd": "/terminal", "desc": "Run shell command", "category": "tools"},
            {"cmd": "/ip", "desc": "Show IP address", "category": "tools"},
            {"cmd": "/wifi", "desc": "Wi-Fi info", "category": "tools"},
            {"cmd": "/tts", "desc": "Text to speech", "category": "tools"},
            {"cmd": "/open", "desc": "Open file/URL on Mac", "category": "tools"},
            {"cmd": "/ping", "desc": "Check bot is alive", "category": "tools"},
            {"cmd": "/cancel", "desc": "Cancel running task", "category": "tools"},
            {"cmd": "/settings", "desc": "View/change settings", "category": "tools"},
        ]
        # Add plugin commands
        for name, desc in st.plugins.items():
            cmds.append({"cmd": f"/{name}", "desc": desc, "category": "plugin"})
        return web.json_response(cmds)

    # --- NEW: Chat with Claude from webapp (streaming via polling) ---
    _chat_sessions: dict[str, dict] = {}  # id -> {proc, output, done, session_id}
    _chat_counter = [0]

    async def api_chat_start(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            message = data.get("message", "").strip()
            if not message:
                return web.json_response({"error": "No message"}, status=400)

            _chat_counter[0] += 1
            chat_id = str(_chat_counter[0])
            ukey = data.get("user_key", "webapp")

            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            # Inject global + project env vars (same as run_claude)
            if st.global_env:
                for k, v in st.global_env.items():
                    if isinstance(v, str):
                        env[k] = v
            proj_name = st.active_project.get(ukey)
            proj_data = st.projects.get(ukey, {}).get(proj_name, {}) if proj_name else {}
            if proj_data.get("env"):
                env.update(proj_data["env"])
            cwd = proj_data.get("cwd") or None

            cmd = _build_claude_cmd(ukey, message, "stream-json")

            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env, cwd=cwd,
                # 64 MB line buffer — Claude's stream-json init line is
                # larger than asyncio's default 64 KB once MCP tools load.
                limit=64 * 1024 * 1024,
            )
            entry = {"proc": proc, "output": "", "done": False, "session_id": ""}
            _chat_sessions[chat_id] = entry

            async def _reader():
                try:
                    while True:
                        raw_line = await proc.stdout.readline()
                        if not raw_line:
                            break
                        line = raw_line.decode(errors='replace').strip()
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                            msg_type = chunk.get("type", "")
                            if msg_type == "result":
                                entry["session_id"] = chunk.get("session_id", "")
                                result = chunk.get("result", "")
                                if result:
                                    entry["output"] = result
                                usage = chunk.get("usage", {})
                                if usage:
                                    _track_cost(ukey, usage)
                            elif msg_type == "content_block_delta":
                                delta = chunk.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    entry["output"] += delta.get("text", "")
                            elif msg_type == "assistant":
                                msg = chunk.get("message", {})
                                for block in (msg.get("content", []) if isinstance(msg.get("content"), list) else []):
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        entry["output"] = block.get("text", entry["output"])
                            elif msg_type == "system":
                                entry["session_id"] = chunk.get("session_id", entry["session_id"])
                        except json.JSONDecodeError:
                            entry["output"] += line + "\n"
                    await proc.wait()
                    if entry["session_id"]:
                        st.user_sessions[ukey] = entry["session_id"]
                        _save_sessions()
                except Exception:
                    pass
                finally:
                    entry["done"] = True

            rt = asyncio.create_task(_reader())
            st.reminder_tasks.add(rt)
            rt.add_done_callback(st.reminder_tasks.discard)

            return web.json_response({"id": chat_id})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_chat_poll(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        chat_id = request.query.get("id", "")
        try:
            offset = int(request.query.get("offset", "0"))
        except (ValueError, TypeError):
            offset = 0
        entry = _chat_sessions.get(chat_id)
        if not entry:
            return web.json_response({"error": "Unknown session"}, status=404)
        chunk = entry["output"][offset:]
        limited = chunk[:20000]
        return web.json_response({
            "text": limited,
            "offset": offset + len(limited),
            "done": entry["done"],
        })

    # --- NEW: File upload API ---
    async def api_upload(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            reader = await request.multipart()
            dest_dir = str(Path.home() / "Desktop")
            files_saved = []
            async for part in reader:
                if part.name == "path":
                    dest_dir = (await part.text()).strip() or dest_dir
                elif part.name == "file" or part.filename:
                    filename = part.filename or f"upload_{int(time.time())}"
                    # Sanitize filename
                    filename = os.path.basename(filename)
                    dest = os.path.join(dest_dir, filename)
                    os.makedirs(dest_dir, exist_ok=True)
                    with open(dest, "wb") as f:
                        while True:
                            chunk = await part.read_chunk(8192)
                            if not chunk:
                                break
                            f.write(chunk)
                    files_saved.append({"name": filename, "path": dest, "size": os.path.getsize(dest)})
            return web.json_response({"ok": True, "files": files_saved})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # --- NEW: File download API ---
    async def api_download(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        fpath = request.query.get("path", "")
        if not fpath or not os.path.isfile(fpath):
            return web.json_response({"error": "File not found"}, status=404)
        return web.FileResponse(fpath)

    # --- NEW: Batch file operations API ---
    async def api_files_batch(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            action = data.get("action", "")
            paths = data.get("paths", [])
            if not paths:
                return web.json_response({"error": "No paths"}, status=400)

            # Safety check
            for p in paths:
                for pattern in DANGEROUS_PATTERNS:
                    if re.search(pattern, p, re.IGNORECASE):
                        return web.json_response({"error": f"Blocked path: {p}"}, status=403)

            if action == "delete":
                deleted = []
                for p in paths:
                    if os.path.isfile(p):
                        os.remove(p)
                        deleted.append(p)
                    elif os.path.isdir(p):
                        import shutil
                        shutil.rmtree(p)
                        deleted.append(p)
                return web.json_response({"ok": True, "deleted": deleted})

            elif action == "zip":
                import zipfile
                dest = data.get("dest", f"/tmp/batch_{int(time.time())}.zip")
                with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for p in paths:
                        if os.path.isfile(p):
                            zf.write(p, os.path.basename(p))
                        elif os.path.isdir(p):
                            for root, dirs, files in os.walk(p):
                                for f in files:
                                    fpath = os.path.join(root, f)
                                    arcname = os.path.relpath(fpath, os.path.dirname(p))
                                    zf.write(fpath, arcname)
                return web.json_response({"ok": True, "zip": dest, "size": os.path.getsize(dest)})

            elif action == "move":
                dest_dir = data.get("dest", "")
                if not dest_dir:
                    return web.json_response({"error": "No dest"}, status=400)
                import shutil
                os.makedirs(dest_dir, exist_ok=True)
                moved = []
                for p in paths:
                    name = os.path.basename(p)
                    new_path = os.path.join(dest_dir, name)
                    shutil.move(p, new_path)
                    moved.append(new_path)
                return web.json_response({"ok": True, "moved": moved})

            elif action == "copy":
                dest_dir = data.get("dest", "")
                if not dest_dir:
                    return web.json_response({"error": "No dest"}, status=400)
                import shutil
                os.makedirs(dest_dir, exist_ok=True)
                copied = []
                for p in paths:
                    name = os.path.basename(p)
                    new_path = os.path.join(dest_dir, name)
                    if os.path.isfile(p):
                        shutil.copy2(p, new_path)
                    elif os.path.isdir(p):
                        shutil.copytree(p, new_path)
                    copied.append(new_path)
                return web.json_response({"ok": True, "copied": copied})

            else:
                return web.json_response({"error": f"Unknown action: {action}"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # --- NEW: Media controls API ---
    async def api_media(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        # Get now playing info — check Spotify first (separate script to avoid
        # compile errors when Spotify is not installed), then browsers.
        spotify_script = '''
        tell application "System Events"
            set spotifyRunning to (name of processes) contains "Spotify"
        end tell
        if spotifyRunning then
            tell application "Spotify"
                if player state is playing then
                    return "playing|Spotify|" & name of current track & "|" & artist of current track & "|" & album of current track
                else if player state is paused then
                    return "paused|Spotify|" & name of current track & "|" & artist of current track & "|" & album of current track
                else
                    return "stopped|Spotify|||"
                end if
            end tell
        end if
        return ""
        '''
        browser_script = '''
        set output to ""
        tell application "System Events"
            set chromeRunning to (name of processes) contains "Google Chrome"
            set safariRunning to (name of processes) contains "Safari"
        end tell
        if chromeRunning then
            try
                tell application "Google Chrome"
                    repeat with w in windows
                        repeat with t in tabs of w
                            if URL of t contains "youtube.com" or URL of t contains "music.youtube.com" then
                                set tabTitle to title of t
                                if tabTitle ends with " - YouTube" then
                                    set tabTitle to text 1 thru -11 of tabTitle
                                end if
                                if tabTitle ends with " - YouTube Music" then
                                    set tabTitle to text 1 thru -17 of tabTitle
                                end if
                                set output to "playing|YouTube (Chrome)|" & tabTitle & "||"
                                exit repeat
                            end if
                        end repeat
                        if output is not "" then exit repeat
                    end repeat
                end tell
            end try
        end if
        if output is "" and safariRunning then
            try
                tell application "Safari"
                    repeat with w in windows
                        repeat with t in tabs of w
                            if URL of t contains "youtube.com" or URL of t contains "music.youtube.com" then
                                set tabTitle to name of t
                                if tabTitle ends with " - YouTube" then
                                    set tabTitle to text 1 thru -11 of tabTitle
                                end if
                                if tabTitle ends with " - YouTube Music" then
                                    set tabTitle to text 1 thru -17 of tabTitle
                                end if
                                set output to "playing|YouTube (Safari)|" & tabTitle & "||"
                                exit repeat
                            end if
                        end repeat
                        if output is not "" then exit repeat
                    end repeat
                end tell
            end try
        end if
        if output is "" then set output to "none||||"
        return output
        '''
        # Try Spotify first (may fail if Spotify not installed — that's OK)
        rc, out, _ = await async_run(["osascript", "-e", spotify_script], timeout=10)
        spotify_out = out.strip() if rc == 0 else ""
        if spotify_out and not spotify_out.startswith("stopped") and spotify_out != "":
            parts = spotify_out.split("|")
        else:
            # Fall back to browser detection
            rc, out, _ = await async_run(["osascript", "-e", browser_script], timeout=10)
            parts = out.strip().split("|") if rc == 0 else []
        # Get system volume
        rc2, vol_out, _ = await async_run(
            ["osascript", "-e", "output volume of (get volume settings)"], timeout=5
        )
        try:
            volume = int(vol_out.strip()) if rc2 == 0 else 50
        except ValueError:
            volume = 50
        result = {
            "state": parts[0] if len(parts) > 0 else "none",
            "app": parts[1] if len(parts) > 1 else "",
            "track": parts[2] if len(parts) > 2 else "",
            "artist": parts[3] if len(parts) > 3 else "",
            "album": parts[4] if len(parts) > 4 else "",
            "volume": volume,
        }
        return web.json_response(result)

    async def api_media_control(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            action = data.get("action", "")
            app_name = data.get("app", "System")

            # Volume controls — always use system volume
            if action == "volume":
                vol = max(0, min(100, int(data.get("value", 50))))
                rc, _, err = await async_run(
                    ["osascript", "-e", f"set volume output volume {vol}"], timeout=5
                )
                return web.json_response({"ok": rc == 0, "error": err if rc != 0 else ""})
            if action == "vol_up":
                rc, _, err = await async_run(
                    ["osascript", "-e",
                     "set v to output volume of (get volume settings)\n"
                     "set volume output volume (v + 10)"], timeout=5
                )
                return web.json_response({"ok": rc == 0, "error": err if rc != 0 else ""})
            if action == "vol_down":
                rc, _, err = await async_run(
                    ["osascript", "-e",
                     "set v to output volume of (get volume settings)\n"
                     "set volume output volume (v - 10)"], timeout=5
                )
                return web.json_response({"ok": rc == 0, "error": err if rc != 0 else ""})

            logger.info(f"[media] action={action} app_name={app_name}")
            # Auto-detect what's playing when app is 'System'
            if app_name == "System":
                detect_script = (
                    'tell application "System Events" to set procs to name of processes\n'
                    'if procs contains "Google Chrome" then\n'
                    '  tell application "Google Chrome"\n'
                    '    repeat with w in windows\n'
                    '      repeat with t in tabs of w\n'
                    '        if URL of t contains "youtube.com" then return "YouTube (Chrome)"\n'
                    '      end repeat\n'
                    '    end repeat\n'
                    '  end tell\n'
                    'end if\n'
                    'if procs contains "Safari" then\n'
                    '  tell application "Safari"\n'
                    '    repeat with w in windows\n'
                    '      repeat with t in tabs of w\n'
                    '        if URL of t contains "youtube.com" then return "YouTube (Safari)"\n'
                    '      end repeat\n'
                    '    end repeat\n'
                    '  end tell\n'
                    'end if\n'
                    'if procs contains "Spotify" then return "Spotify"\n'
                    'return "System"'
                )
                detect_rc, detect_out, _ = await async_run(["osascript", "-e", detect_script], timeout=10)
                logger.info(f"[media] detect rc={detect_rc} out={detect_out[:200]} err={_[:200] if _ else ''}")
                if detect_rc == 0 and detect_out.strip():
                    app_name = detect_out.strip()
                logger.info(f"[media] auto-detected app_name={app_name}")

            # Use Spotify-specific AppleScript only for Spotify, otherwise browser JS or system keys
            if app_name == "Spotify":
                actions = {
                    "play": 'tell application "Spotify" to play',
                    "pause": 'tell application "Spotify" to pause',
                    "toggle": 'tell application "Spotify" to playpause',
                    "next": 'tell application "Spotify" to next track',
                    "prev": 'tell application "Spotify" to previous track',
                }
                script = actions.get(action)
                if not script:
                    return web.json_response({"error": f"Unknown action: {action}"}, status=400)
                rc, _, err = await async_run(["osascript", "-e", script], timeout=10)
            elif "YouTube" in app_name or "Chrome" in app_name or "Safari" in app_name:
                # Direct browser JS control — works in background, no focus needed
                # Requires Chrome: View > Developer > Allow JavaScript from Apple Events
                if action not in ("toggle", "play", "pause", "next", "prev"):
                    return web.json_response({"error": f"Unknown action: {action}"}, status=400)
                js_commands = {
                    "toggle": "var v=document.querySelector('video');if(v){if(v.paused)v.play();else v.pause();}",
                    "play": "var v=document.querySelector('video');if(v)v.play();",
                    "pause": "var v=document.querySelector('video');if(v)v.pause();",
                    "next": "document.querySelector('.ytp-next-button')?.click();",
                    "prev": "var v=document.querySelector('video');if(v){if(v.currentTime>5){v.currentTime=0}else{history.back()}}",
                }
                js = js_commands[action]
                browser = "Google Chrome" if "Chrome" in app_name else "Safari"
                if browser == "Google Chrome":
                    script = f'''
                    tell application "Google Chrome"
                        repeat with w in windows
                            repeat with t in tabs of w
                                if URL of t contains "youtube.com" then
                                    execute t javascript "{js}"
                                    return "ok"
                                end if
                            end repeat
                        end repeat
                    end tell
                    return "no youtube tab"
                    '''
                else:
                    script = f'''
                    tell application "Safari"
                        repeat with w in windows
                            repeat with t in tabs of w
                                if URL of t contains "youtube.com" then
                                    do JavaScript "{js}" in t
                                    return "ok"
                                end if
                            end repeat
                        end repeat
                    end tell
                    return "no youtube tab"
                    '''
                rc, _, err = await async_run(["osascript", "-e", script], timeout=10)
            else:
                # System media keys via Python Quartz — fallback for other apps
                if action not in ("toggle", "play", "pause", "next", "prev"):
                    return web.json_response({"error": f"Unknown action: {action}"}, status=400)
                import pathlib
                media_keys_script = str(pathlib.Path(__file__).parent / "media_keys.py")
                rc, _, err = await async_run(["python3", media_keys_script, action], timeout=10)
            logger.info(f"[media] result rc={rc} err={err[:200] if err else ''}")
            return web.json_response({"ok": rc == 0, "error": err if rc != 0 else ""})
        except Exception as e:
            logger.error(f"[media] exception: {e}")
            return web.json_response({"error": str(e)}, status=500)

    # --- NEW: Schedule create API ---
    async def api_schedule_create(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            cron = data.get("cron", "").strip()
            task = data.get("task", "").strip()
            if not cron or not task:
                return web.json_response({"error": "Need cron and task"}, status=400)
            # Try natural language parsing
            parsed_cron = parse_natural_schedule(cron)
            schedules = load_json(SCHEDULES_FILE, [])
            entry = {
                "cron": parsed_cron,
                "task": task,
                "chat_id": st._default_chat_id,
                "user_key": "webapp",
                "created": datetime.now().isoformat(),
            }
            schedules.append(entry)
            save_json(SCHEDULES_FILE, schedules)
            return web.json_response({"ok": True, "schedule": entry, "index": len(schedules) - 1})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # --- NEW: Clipboard sync toggle API ---
    async def api_clipboard_sync(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            enabled = data.get("enabled", False)
            st.clipboard_sync_enabled["webapp"] = enabled
            return web.json_response({"ok": True, "enabled": enabled})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # --- NEW: Geofence management API ---
    async def api_geofences(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return web.json_response({
            "geofences": load_json(GEOFENCES_FILE, []),
            "last_location": st._last_location,
        })

    async def api_geofence_create(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            fence = {
                "name": data.get("name", "").strip(),
                "lat": float(data.get("lat", 0)),
                "lon": float(data.get("lon", 0)),
                "radius_m": int(data.get("radius_m", 100)),
                "action": data.get("action", "").strip(),
                "trigger": data.get("trigger", "enter"),  # enter, exit, both
            }
            if not fence["name"] or not fence["action"]:
                return web.json_response({"error": "Need name and action"}, status=400)
            fences = load_json(GEOFENCES_FILE, [])
            fences.append(fence)
            save_json(GEOFENCES_FILE, fences)
            return web.json_response({"ok": True, "fence": fence})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_geofence_delete(request):
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            idx = int(data.get("index", -1))
            fences = load_json(GEOFENCES_FILE, [])
            if 0 <= idx < len(fences):
                removed = fences.pop(idx)
                save_json(GEOFENCES_FILE, fences)
                return web.json_response({"ok": True, "removed": removed})
            return web.json_response({"error": "Invalid index"}, status=400)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    # --- NEW: Webapp PIN auth ---
    async def api_pin_verify(request):
        """Verify PIN for webapp access. Returns a session token."""
        try:
            data = await request.json()
            pin = data.get("pin", "")
            stored = load_json(WEBAPP_PIN_FILE, {})
            if not stored.get("pin"):
                # No PIN set — allow access
                return web.json_response({"ok": True, "token": "no-pin"})
            if hashlib.sha256(pin.encode()).hexdigest() == stored["pin"]:
                # Generate session token
                token = hashlib.sha256(f"{pin}{time.time()}{os.urandom(16).hex()}".encode()).hexdigest()[:32]
                stored.setdefault("sessions", [])
                stored["sessions"].append({"token": token, "created": time.time()})
                # Keep only last 10 sessions
                stored["sessions"] = stored["sessions"][-10:]
                save_json(WEBAPP_PIN_FILE, stored)
                return web.json_response({"ok": True, "token": token})
            return web.json_response({"ok": False, "error": "Wrong PIN"}, status=401)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_pin_set(request):
        """Set or change webapp PIN."""
        if not check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            data = await request.json()
            new_pin = data.get("pin", "")
            stored = load_json(WEBAPP_PIN_FILE, {})
            if new_pin:
                stored["pin"] = hashlib.sha256(new_pin.encode()).hexdigest()
            else:
                stored.pop("pin", None)
            stored["sessions"] = []  # Invalidate all sessions
            save_json(WEBAPP_PIN_FILE, stored)
            return web.json_response({"ok": True, "has_pin": bool(new_pin)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)

    async def api_pin_status(request):
        """Check if PIN is configured."""
        stored = load_json(WEBAPP_PIN_FILE, {})
        return web.json_response({"has_pin": bool(stored.get("pin"))})

    webapp = web.Application()
    # Existing routes
    webapp.router.add_get("/api/health", api_health)
    webapp.router.add_post("/api/chat", api_chat)
    webapp.router.add_post("/webhook", handle_webhook)
    webapp.router.add_post("/notify", handle_webhook)
    webapp.router.add_get("/api/status", api_status)
    webapp.router.add_get("/api/files", api_files)
    webapp.router.add_get("/api/cost", api_cost)
    webapp.router.add_get("/api/usage_limits", api_usage_limits)
    webapp.router.add_get("/api/live_usage", api_live_usage)
    webapp.router.add_get("/api/history", api_history)
    webapp.router.add_get("/api/schedules", api_schedules)
    webapp.router.add_post("/api/schedules/delete", api_schedules_delete)
    webapp.router.add_get("/api/alerts", api_alerts)
    webapp.router.add_post("/api/alerts/delete", api_alerts_delete)
    webapp.router.add_get("/api/machines", api_machines)
    webapp.router.add_get("/api/machine-stats", api_machine_stats)
    webapp.router.add_get("/api/docker-status", api_docker_status)
    webapp.router.add_get("/api/health-monitor", api_health_monitor)
    webapp.router.add_get("/api/container-logs", api_container_logs)
    webapp.router.add_get("/api/screenshot", api_screenshot)
    webapp.router.add_post("/api/execute", api_execute)
    webapp.router.add_post("/api/execute-start", api_execute_start)
    webapp.router.add_get("/api/execute-poll", api_execute_poll)
    webapp.router.add_post("/api/execute-cancel", api_execute_cancel)
    webapp.router.add_post("/api/quick-action", api_quick_action)
    webapp.router.add_post("/api/wake", api_wake)
    webapp.router.add_get("/app", serve_webapp)
    # New routes
    webapp.router.add_get("/api/metrics", api_metrics)
    webapp.router.add_get("/api/health-score", api_health_score)
    webapp.router.add_get("/api/commands", api_commands)
    webapp.router.add_post("/api/chat-start", api_chat_start)
    webapp.router.add_get("/api/chat-poll", api_chat_poll)
    webapp.router.add_post("/api/upload", api_upload)
    webapp.router.add_get("/api/download", api_download)
    webapp.router.add_post("/api/files/batch", api_files_batch)
    webapp.router.add_get("/api/media", api_media)
    webapp.router.add_post("/api/media/control", api_media_control)
    webapp.router.add_post("/api/schedules/create", api_schedule_create)
    webapp.router.add_post("/api/clipboard-sync", api_clipboard_sync)
    webapp.router.add_get("/api/geofences", api_geofences)
    webapp.router.add_post("/api/geofences/create", api_geofence_create)
    webapp.router.add_post("/api/geofences/delete", api_geofence_delete)
    webapp.router.add_post("/api/pin/verify", api_pin_verify)
    webapp.router.add_post("/api/pin/set", api_pin_set)
    webapp.router.add_get("/api/pin/status", api_pin_status)

    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", WEBHOOK_PORT)
    try:
        await site.start()
        logger.info("Webhook server on http://127.0.0.1:%d", WEBHOOK_PORT)
    except Exception as e:
        logger.warning("Webhook server failed: %s", e)

