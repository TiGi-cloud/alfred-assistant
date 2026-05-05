"""
Lightweight metrics collector for the dashboard.

Polls Mac stats every `interval_secs` and keeps the last `max_samples` in
memory + persists to a JSON file. Each sample: {ts, cpu, mem, disk}.

  collector = MetricsCollector(interval_secs=60, max_samples=1440)  # 24h @ 1m
  await collector.start()
  collector.recent(limit=60)  # last 60 samples
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("alfred.kernel.metrics")


@dataclass
class MetricsCollector:
    interval_secs: float = 60.0
    max_samples: int = 1440  # 24h at 1-minute intervals
    state_path: Optional[Path] = None
    _samples: list[dict] = field(default_factory=list, init=False)
    _task: Optional[asyncio.Task] = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.state_path is None:
            self.state_path = Path(__file__).resolve().parent.parent / "alfred_metrics.json"
        self._load()

    # -- Persistence -------------------------------------------------------
    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                self._samples = json.loads(self.state_path.read_text())
            except Exception:
                self._samples = []

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.write_text(json.dumps(self._samples))
        except Exception:
            logger.warning("Failed to persist metrics")

    # -- Public API --------------------------------------------------------
    def recent(self, limit: int = 60) -> list[dict]:
        return list(self._samples[-limit:])

    def latest(self) -> Optional[dict]:
        return self._samples[-1] if self._samples else None

    async def start(self) -> None:
        if self._task is not None:
            return
        # Sample once immediately so the dashboard has something to show
        await self._sample_once()
        self._task = asyncio.create_task(self._loop(), name="alfred-metrics")
        logger.info("Metrics collector started (every %ss)", self.interval_secs)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    # -- Loop --------------------------------------------------------------
    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_secs)
            try:
                await self._sample_once()
            except Exception:
                logger.exception("metrics sample failed")

    async def _sample_once(self) -> None:
        sample = {"ts": time.time()}
        if sys.platform == "darwin":
            sample.update(await _macos_metrics())
        else:
            sample.update({"cpu": 0.0, "mem": 0.0, "disk": 0.0})
        self._samples.append(sample)
        if len(self._samples) > self.max_samples:
            self._samples = self._samples[-self.max_samples:]
        self._save()


async def _macos_metrics() -> dict:
    """Read CPU / memory / disk percentages on macOS via shell."""
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c",
        'cpu=$(top -l 1 -n 0 | awk \'/CPU usage/{print $3}\' | tr -d "%"); '
        'mem_free=$(vm_stat | awk \'/Pages free/{f=$3} /Pages inactive/{i=$3} END{printf "%d", (f+i)*4096/1048576}\'); '
        'mem_total=$(sysctl -n hw.memsize | awk \'{printf "%d", $1/1048576}\'); '
        'disk=$(df / | tail -1 | awk \'{print $5}\' | tr -d "%"); '
        'echo "$cpu $mem_free $mem_total $disk"',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        return {"cpu": 0.0, "mem": 0.0, "disk": 0.0}

    parts = out.decode().strip().split()
    if len(parts) != 4:
        return {"cpu": 0.0, "mem": 0.0, "disk": 0.0}
    try:
        cpu = float(parts[0])
        mem_free = float(parts[1])
        mem_total = float(parts[2])
        disk = float(parts[3])
        mem_pct = (1.0 - mem_free / mem_total) * 100.0 if mem_total else 0.0
        return {
            "cpu": round(cpu, 1),
            "mem": round(mem_pct, 1),
            "disk": round(disk, 1),
            "mem_free_mb": int(mem_free),
            "mem_total_mb": int(mem_total),
        }
    except (ValueError, ZeroDivisionError):
        return {"cpu": 0.0, "mem": 0.0, "disk": 0.0}
