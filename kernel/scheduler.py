"""
Background scheduler for Alfred.

A `Scheduler` instance owns three kinds of jobs:

  * **reminder** — one-shot, fires at a specific timestamp, deletes itself
  * **schedule** — recurring, driven by a cron expression
  * **alert**    — condition-based; fires when a metric crosses a threshold

The scheduler runs a single async polling loop. Every `poll_interval` seconds
it scans all jobs and fires the ones that are due. Firing means calling
`send_text` on whichever adapter the job was created from — so a reminder
created in your Telegram chat fires back to that same Telegram chat.

State is persisted to a JSON file (default: `alfred_scheduler.json`) keyed by
"<adapter>:<chat_id>" so jobs survive restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .adapter import ChatAdapter
from .runner import Context

logger = logging.getLogger("alfred.kernel.scheduler")


# ---------------------------------------------------------------------------
# Cron / natural-language helpers (reusable across kernel + legacy)
# ---------------------------------------------------------------------------
def parse_natural_schedule(expr: str) -> str:
    """Convert natural-language schedule to a cron expression.

    Returns the input unchanged if no pattern matches — so the result can
    always be passed to croniter, which raises ValueError for nonsense.
    """
    s = expr.lower().strip()
    if s in ("every minute", "minutely"):
        return "* * * * *"
    m = re.match(r"every (\d+) min(?:utes?)?$", s)
    if m:
        return f"*/{m.group(1)} * * * *"
    m = re.match(r"every (\d+) hours?$", s)
    if m:
        return f"0 */{m.group(1)} * * *"
    if s in ("every hour", "hourly"):
        return "0 * * * *"
    if s in ("every day", "daily"):
        return "0 9 * * *"
    if s in ("every weekday", "weekdays"):
        return "0 9 * * 1-5"
    if s in ("every weekend", "weekends"):
        return "0 9 * * 0,6"
    if s in ("every morning", "mornings"):
        return "0 8 * * *"
    if s in ("every night", "nightly", "every evening"):
        return "0 21 * * *"
    if s == "midnight":
        return "0 0 * * *"
    if s == "noon":
        return "0 12 * * *"
    m = re.match(r"(?:daily|every day) at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if m:
        h = int(m.group(1)); mn = int(m.group(2) or 0)
        suffix = m.group(3) or ""
        if suffix == "pm" and h < 12:
            h += 12
        elif suffix == "am" and h == 12:
            h = 0
        return f"{mn} {h} * * *"
    m = re.match(r"at (\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if m:
        h = int(m.group(1)); mn = int(m.group(2) or 0)
        suffix = m.group(3) or ""
        if suffix == "pm" and h < 12:
            h += 12
        elif suffix == "am" and h == 12:
            h = 0
        return f"{mn} {h} * * *"
    return expr


_REL_RE = re.compile(
    r"^in\s+(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.IGNORECASE,
)
_AT_RE = re.compile(
    r"^at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$",
    re.IGNORECASE,
)


def parse_when(when: str, *, now: Optional[float] = None) -> Optional[float]:
    """Parse 'in 5 min', 'at 3pm', or 'YYYY-MM-DD HH:MM' to a Unix timestamp."""
    s = when.strip()
    if not s:
        return None
    base = now or time.time()

    if (m := _REL_RE.match(s)):
        n = int(m.group(1))
        unit = m.group(2).lower()
        mult = 1
        if unit.startswith("s"):
            mult = 1
        elif unit.startswith("m"):
            mult = 60
        elif unit.startswith("h"):
            mult = 3600
        elif unit.startswith("d"):
            mult = 86400
        return base + n * mult

    if (m := _AT_RE.match(s)):
        h = int(m.group(1)); mn = int(m.group(2) or 0)
        suffix = (m.group(3) or "").lower()
        if suffix == "pm" and h < 12:
            h += 12
        elif suffix == "am" and h == 12:
            h = 0
        # Today at H:M, or tomorrow if past
        local = time.localtime(base)
        target = time.mktime((
            local.tm_year, local.tm_mon, local.tm_mday, h, mn, 0,
            0, 0, local.tm_isdst,
        ))
        if target <= base:
            target += 86400
        return target

    # ISO-ish format
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            t = time.mktime(time.strptime(s, fmt))
            return t
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Cron next-fire calculation
# ---------------------------------------------------------------------------
def _next_cron_fire(cron_expr: str, base: Optional[float] = None) -> Optional[float]:
    base = base or time.time()
    try:
        from croniter import croniter  # type: ignore[import]
        return float(croniter(cron_expr, base).get_next(float))
    except ImportError:
        # croniter is optional. Fall back to simple "every N minutes" matching.
        m = re.match(r"\*/(\d+) \* \* \* \*$", cron_expr.strip())
        if m:
            mins = int(m.group(1))
            return base + mins * 60
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Alerts: metric checkers
# ---------------------------------------------------------------------------
async def _check_metric(metric: str) -> Optional[float]:
    """Read a single metric value (0-100% for cpu/disk/memory, else None)."""
    if sys.platform != "darwin":
        return None
    try:
        if metric == "cpu":
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                'top -l 1 -n 0 | grep "CPU usage" | awk \'{print $3}\' | tr -d "%"',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return float(out.decode().strip().split()[0])
        if metric == "disk":
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                "df / | tail -1 | awk '{print $5}' | tr -d '%'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return float(out.decode().strip())
        if metric == "memory":
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c",
                'free=$(vm_stat | awk \'/Pages free/{f=$3} /Pages inactive/{i=$3} END{printf "%d", (f+i)*4096/1048576}\'); '
                'total=$(sysctl -n hw.memsize | awk \'{printf "%d", $1/1048576}\'); '
                'echo "scale=1; ($total - $free) * 100 / $total" | bc',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            return float(out.decode().strip())
    except Exception:
        return None
    return None


async def _check_process(name: str) -> bool:
    """Return True if a process matching `name` is running."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-i", name,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        return False
    return proc.returncode == 0


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
@dataclass
class Scheduler:
    state_path: Optional[Path] = None
    poll_interval: float = 30.0
    alert_cooldown_secs: float = 300.0

    _jobs: dict[str, list[dict]] = field(default_factory=dict, init=False)
    _adapters: dict[str, ChatAdapter] = field(default_factory=dict, init=False)
    _task: Optional[asyncio.Task] = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.state_path is None:
            self.state_path = Path(__file__).resolve().parent.parent / "alfred_scheduler.json"
        self._load()

    # -- Persistence -------------------------------------------------------
    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                self._jobs = json.loads(self.state_path.read_text())
            except Exception:
                self._jobs = {}

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.write_text(json.dumps(self._jobs, indent=2))
        except Exception:
            logger.warning("Failed to persist scheduler state")

    @staticmethod
    def _key(ctx: Context) -> str:
        return f"{ctx.adapter.name}:{ctx.chat_id}"

    # -- Adapter registry --------------------------------------------------
    def register_adapter(self, adapter: ChatAdapter) -> None:
        self._adapters[adapter.name] = adapter

    # -- Job management ----------------------------------------------------
    def _new_id(self) -> str:
        return uuid.uuid4().hex[:6]

    def add_reminder(self, ctx: Context, fires_at: float, text: str) -> dict:
        job = {
            "id": self._new_id(),
            "kind": "reminder",
            "fires_at": fires_at,
            "text": text,
            "created_at": time.time(),
            "user_id": ctx.user.id,
        }
        self._jobs.setdefault(self._key(ctx), []).append(job)
        self._save()
        return job

    def add_schedule(self, ctx: Context, cron_or_natural: str, text: str) -> dict:
        cron = parse_natural_schedule(cron_or_natural)
        if _next_cron_fire(cron) is None:
            raise ValueError(f"Could not parse schedule: {cron_or_natural!r} → {cron!r}")
        job = {
            "id": self._new_id(),
            "kind": "schedule",
            "cron": cron,
            "natural": cron_or_natural,
            "text": text,
            "last_fired": 0,
            "created_at": time.time(),
            "user_id": ctx.user.id,
        }
        self._jobs.setdefault(self._key(ctx), []).append(job)
        self._save()
        return job

    def add_alert(
        self,
        ctx: Context,
        metric: str,
        threshold: Optional[float] = None,
        *,
        label: Optional[str] = None,
    ) -> dict:
        metric = metric.lower()
        if metric not in ("cpu", "disk", "memory", "process"):
            raise ValueError("metric must be one of: cpu, disk, memory, process")
        if metric in ("cpu", "disk", "memory") and threshold is None:
            raise ValueError(f"{metric} alert requires a threshold (e.g. 90 = 90%)")
        if metric == "process" and not label:
            raise ValueError("process alert requires a process name as label")
        job = {
            "id": self._new_id(),
            "kind": "alert",
            "metric": metric,
            "threshold": threshold,
            "label": label or metric,
            "last_fired": 0,
            "created_at": time.time(),
            "user_id": ctx.user.id,
        }
        self._jobs.setdefault(self._key(ctx), []).append(job)
        self._save()
        return job

    def list_jobs(self, ctx: Context, kind: Optional[str] = None) -> list[dict]:
        jobs = self._jobs.get(self._key(ctx), [])
        if kind:
            jobs = [j for j in jobs if j.get("kind") == kind]
        return list(jobs)

    def remove_job(self, ctx: Context, job_id: str) -> bool:
        jobs = self._jobs.get(self._key(ctx), [])
        for j in jobs:
            if j["id"] == job_id:
                jobs.remove(j)
                self._save()
                return True
        return False

    def clear_jobs(self, ctx: Context, kind: Optional[str] = None) -> int:
        key = self._key(ctx)
        before = self._jobs.get(key, [])
        if kind:
            self._jobs[key] = [j for j in before if j.get("kind") != kind]
        else:
            self._jobs[key] = []
        removed = len(before) - len(self._jobs.get(key, []))
        if removed:
            self._save()
        return removed

    # -- Run loop ---------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="alfred-scheduler")
        logger.info("Scheduler started (poll every %ss)", self.poll_interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.tick()
            except Exception:
                logger.exception("scheduler tick failed")

    async def tick(self, *, now: Optional[float] = None) -> None:
        """One scan over all jobs. Fires whatever is due."""
        now = now or time.time()
        for chat_key in list(self._jobs.keys()):
            for job in list(self._jobs.get(chat_key, [])):
                try:
                    await self._maybe_fire(chat_key, job, now=now)
                except Exception:
                    logger.exception("job %s failed", job.get("id"))

    async def _maybe_fire(self, chat_key: str, job: dict, *, now: float) -> None:
        kind = job.get("kind")
        if kind == "reminder":
            if job.get("fires_at", 0) <= now:
                await self._fire(chat_key, job, prefix="⏰ ", text=job.get("text", ""))
                self._jobs[chat_key].remove(job)
                self._save()
        elif kind == "schedule":
            cron = job.get("cron", "")
            last = job.get("last_fired") or job.get("created_at", 0)
            next_at = _next_cron_fire(cron, base=last)
            if next_at and next_at <= now:
                await self._fire(chat_key, job, prefix="🗓 ", text=job.get("text", ""))
                job["last_fired"] = now
                self._save()
        elif kind == "alert":
            if (now - job.get("last_fired", 0)) < self.alert_cooldown_secs:
                return
            triggered, detail = await self._evaluate_alert(job)
            if triggered:
                msg = f"🚨 ALERT: {job.get('label', '?')} — {detail}"
                await self._fire(chat_key, job, prefix="", text=msg)
                job["last_fired"] = now
                self._save()

    async def _evaluate_alert(self, job: dict) -> tuple[bool, str]:
        metric = job.get("metric")
        threshold = job.get("threshold")
        if metric in ("cpu", "disk", "memory"):
            value = await _check_metric(metric)
            if value is None:
                return False, ""
            if value >= float(threshold):
                return True, f"{metric}={value:.1f}% ≥ {threshold}%"
            return False, ""
        if metric == "process":
            name = job.get("label", "")
            running = await _check_process(name)
            if not running:
                return True, f"process '{name}' not running"
            return False, ""
        return False, ""

    async def _fire(self, chat_key: str, job: dict, *, prefix: str, text: str) -> None:
        adapter_name, _, chat_id = chat_key.partition(":")
        adapter = self._adapters.get(adapter_name)
        if adapter is None:
            logger.warning("scheduler: no live adapter %s for %s", adapter_name, chat_key)
            return
        try:
            await adapter.send_text(chat_id, prefix + text)
        except Exception:
            logger.exception("scheduler send to %s failed", chat_key)
