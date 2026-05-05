"""
/notifications — toggle macOS notification forwarding to chat.

When ON, Alfred sends every new macOS Notification Center entry to the
chat where it was enabled. macOS doesn't expose Notification Center via a
public API; we read from `~/Library/Group Containers/group.com.apple.usernoted/db2/db`
(SQLite) the same way third-party tools do. The schema changes between
macOS releases — if it stops working after an upgrade, it's likely a
schema change.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

from kernel.adapter import ChatAdapter
from kernel.runner import Context

logger = logging.getLogger("alfred.actions.notifications")


_STATE_FILE = Path(__file__).resolve().parent.parent / "alfred_notifications.json"


# Notification Center DB location (varies very slightly between macOS versions)
_DB_CANDIDATES = [
    "Library/Group Containers/group.com.apple.usernoted/db2/db",
    "Library/Application Support/com.apple.notificationcenter/db",
    "Library/Notification Center/db",
]


def _find_db() -> Optional[Path]:
    home = Path.home()
    for rel in _DB_CANDIDATES:
        p = home / rel
        if p.exists():
            return p
    return None


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def _key(ctx: Context) -> str:
    return f"{ctx.adapter.name}:{ctx.chat_id}"


# ---------------------------------------------------------------------------
# /notifications
# ---------------------------------------------------------------------------
async def cmd_notifications(ctx: Context) -> None:
    """Toggle forwarding of macOS notifications to this chat.

    Usage: /notifications [on|off]
    """
    msg = ctx.message
    args = (msg.command_args or "").strip().lower() if msg else ""
    state = _load_state()
    chat_key = _key(ctx)
    enabled = state.get(chat_key, False)

    if args in ("", "status"):
        await ctx.reply(
            f"📱 Notifications: {'ON' if enabled else 'OFF'} for this chat.\n"
            "Toggle with /notifications on or /notifications off."
        )
        return
    if args == "on":
        state[chat_key] = True
        _save_state(state)
        if not _find_db():
            await ctx.reply(
                "📱 Notifications: ON — but I can't find the macOS Notification "
                "Center database, so nothing will actually be forwarded. "
                "(Either you're not on macOS, or your version uses a path I "
                "don't know about.)"
            )
        else:
            await ctx.reply("📱 Notifications: ON — I'll forward macOS alerts here.")
        return
    if args == "off":
        state.pop(chat_key, None)
        _save_state(state)
        await ctx.reply("📱 Notifications: OFF.")
        return
    await ctx.reply("Usage: /notifications [on|off]")


# ---------------------------------------------------------------------------
# Background watcher — polls the Notification Center DB and broadcasts to
# every chat with notifications turned ON.
# ---------------------------------------------------------------------------
class NotificationWatcher:
    def __init__(self, *, poll_interval: float = 30.0) -> None:
        self._poll_interval = poll_interval
        self._adapters: dict[str, ChatAdapter] = {}
        self._task: Optional[asyncio.Task] = None
        self._last_seen: float = time.time()

    def register_adapter(self, adapter: ChatAdapter) -> None:
        self._adapters[adapter.name] = adapter

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="alfred-notifs")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._tick()
            except Exception:
                logger.exception("notification watcher tick failed")

    async def _tick(self) -> None:
        state = _load_state()
        active = [k for k, v in state.items() if v]
        if not active:
            return
        new_notifs = await asyncio.to_thread(self._fetch_new)
        if not new_notifs:
            return
        for chat_key in active:
            adapter_name, _, chat_id = chat_key.partition(":")
            adapter = self._adapters.get(adapter_name)
            if not adapter:
                continue
            for note in new_notifs[-10:]:  # cap per cycle
                text = self._format(note)
                try:
                    await adapter.send_text(chat_id, text)
                except Exception:
                    pass

    def _fetch_new(self) -> list[dict]:
        """Read the Notification Center SQLite for entries newer than last seen."""
        db = _find_db()
        if db is None:
            return []
        try:
            uri = f"file:{db}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2)
        except Exception:
            return []
        notes: list[dict] = []
        try:
            # The schema varies; try a few queries.
            cur = conn.cursor()
            queries = [
                # Modern macOS (Sonoma+)
                ("SELECT delivered_date, app_id, title, subtitle, body FROM record "
                 "WHERE delivered_date > ? ORDER BY delivered_date ASC LIMIT 25",
                 lambda r: {"ts": r[0], "app": r[1], "title": r[2] or "", "sub": r[3] or "", "body": r[4] or ""}),
                # Older variants — body in `data` blob; fall back to title only
                ("SELECT delivered_date, app_id, title FROM record "
                 "WHERE delivered_date > ? ORDER BY delivered_date ASC LIMIT 25",
                 lambda r: {"ts": r[0], "app": r[1], "title": r[2] or "", "sub": "", "body": ""}),
            ]
            apple_epoch_offset = 978307200
            ts_threshold_apple = self._last_seen - apple_epoch_offset
            for sql, parse in queries:
                try:
                    rows = cur.execute(sql, (ts_threshold_apple,)).fetchall()
                    notes = [parse(r) for r in rows]
                    if notes:
                        # Update high-water mark
                        latest = max(n["ts"] for n in notes)
                        self._last_seen = latest + apple_epoch_offset
                    return notes
                except sqlite3.DatabaseError:
                    continue
        finally:
            conn.close()
        return notes

    @staticmethod
    def _format(note: dict) -> str:
        parts = [f"📣 {note.get('title', '?')}"]
        if note.get("sub"):
            parts.append(f"   {note['sub']}")
        if note.get("body"):
            parts.append(f"   {note['body'][:300]}")
        if note.get("app"):
            parts.append(f"   from: {note['app']}")
        return "\n".join(parts)


def register(dispatcher) -> None:
    dispatcher.command("notifications", cmd_notifications)
