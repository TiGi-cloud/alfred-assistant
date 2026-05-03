"""
Tiny SQLite KV store + per-user persistent memory.

Replaces the legacy ``db.py`` + ``utils/memory.py`` combo. Everything Alfred
needs to persist across restarts that doesn't already have its own JSON
file lives here:

    db_load(key, default)   db_save(key, value)
    add_memory(user_key, text, category="fact") -> entry
    load_memories(user_key) -> list[entry]
    save_memories(user_key, entries)
    delete_memory(user_key, memory_id) -> bool
    search_memories(user_key, query) -> list[entry]
    clear_memories(user_key) -> int
    format_memories_for_prompt(user_key, max_chars=2000) -> str

The DB lives at <repo>/alfred.db unless `set_db_path()` overrides it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("alfred.kernel.store")

_DB_PATH: Path = Path(__file__).resolve().parent.parent / "alfred.db"
_conn: Optional[sqlite3.Connection] = None


def set_db_path(path: Path) -> None:
    """Override the DB location. Closes any open connection first."""
    global _DB_PATH, _conn
    _DB_PATH = Path(path)
    if _conn is not None:
        _conn.close()
        _conn = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False, timeout=5)
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        _conn.commit()
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        finally:
            _conn = None


# ---------------------------------------------------------------------------
# KV
# ---------------------------------------------------------------------------
def db_load(key: str, default: Any = None) -> Any:
    cur = _get_conn().execute("SELECT value FROM kv WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        logger.warning("Corrupt JSON for key %r — returning default", key)
        return default


def db_save(key: str, value: Any) -> None:
    payload = json.dumps(value, default=str)
    conn = _get_conn()
    with conn:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, payload),
        )


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
_MEM_KEY_PREFIX = "memory:"
MAX_MEMORIES_PER_USER = 200


def _mem_key(user_key: str) -> str:
    return f"{_MEM_KEY_PREFIX}{user_key}"


def load_memories(user_key: str) -> list[dict]:
    return db_load(_mem_key(user_key), []) or []


def save_memories(user_key: str, memories: list[dict]) -> None:
    db_save(_mem_key(user_key), memories)


def add_memory(user_key: str, text: str, *, category: str = "fact") -> dict:
    entries = load_memories(user_key)
    entry = {
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "category": category,
        "ts": time.time(),
    }
    entries.append(entry)
    if len(entries) > MAX_MEMORIES_PER_USER:
        entries = entries[-MAX_MEMORIES_PER_USER:]
    save_memories(user_key, entries)
    return entry


def delete_memory(user_key: str, memory_id: str) -> bool:
    entries = load_memories(user_key)
    before = len(entries)
    entries = [m for m in entries if m.get("id") != memory_id]
    if len(entries) < before:
        save_memories(user_key, entries)
        return True
    return False


def search_memories(user_key: str, query: str) -> list[dict]:
    entries = load_memories(user_key)
    q = query.lower()
    return [m for m in entries
            if q in m.get("text", "").lower() or q in m.get("category", "").lower()]


def clear_memories(user_key: str) -> int:
    entries = load_memories(user_key)
    count = len(entries)
    save_memories(user_key, [])
    return count


def format_memories_for_prompt(user_key: str, *, max_chars: int = 2000) -> str:
    entries = load_memories(user_key)
    if not entries:
        return ""
    by_cat: dict[str, list[str]] = {}
    for m in entries:
        cat = m.get("category", "fact")
        by_cat.setdefault(cat, []).append(m.get("text", ""))
    parts = []
    for cat, items in by_cat.items():
        parts.append(f"{cat.upper()}: " + "; ".join(items))
    result = " | ".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + "…"
    return result
