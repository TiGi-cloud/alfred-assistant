"""Alfred persistent memory — stores and recalls facts about the user across sessions.

Memory is stored in SQLite via the existing db.py KV store.
Each user has a list of memory entries: {id, text, category, ts}.
Categories: preference, fact, routine, context, task.
"""
from __future__ import annotations

import time
import uuid
import logging
from db import db_load, db_save

logger = logging.getLogger("alfred")

DB_KEY_PREFIX = "memory:"  # memory:<ukey>


def _db_key(ukey: str) -> str:
    return f"{DB_KEY_PREFIX}{ukey}"


def load_memories(ukey: str) -> list[dict]:
    """Load all memories for a user."""
    return db_load(_db_key(ukey), [])


def save_memories(ukey: str, memories: list[dict]):
    """Persist all memories for a user."""
    db_save(_db_key(ukey), memories)


def add_memory(ukey: str, text: str, category: str = "fact") -> dict:
    """Add a new memory entry. Returns the created entry."""
    memories = load_memories(ukey)
    entry = {
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "category": category,
        "ts": time.time(),
    }
    memories.append(entry)
    # Cap at 200 memories per user
    if len(memories) > 200:
        memories = memories[-200:]
    save_memories(ukey, memories)
    logger.info("Memory added for %s: [%s] %s", ukey, category, text[:80])
    return entry


def delete_memory(ukey: str, memory_id: str) -> bool:
    """Delete a memory by ID. Returns True if found and deleted."""
    memories = load_memories(ukey)
    before = len(memories)
    memories = [m for m in memories if m["id"] != memory_id]
    if len(memories) < before:
        save_memories(ukey, memories)
        return True
    return False


def search_memories(ukey: str, query: str) -> list[dict]:
    """Search memories by keyword (case-insensitive)."""
    memories = load_memories(ukey)
    q = query.lower()
    return [m for m in memories if q in m["text"].lower() or q in m.get("category", "").lower()]


def clear_memories(ukey: str) -> int:
    """Clear all memories. Returns count deleted."""
    memories = load_memories(ukey)
    count = len(memories)
    save_memories(ukey, [])
    return count


def format_memories_for_prompt(ukey: str, max_chars: int = 2000) -> str:
    """Format memories as a compact string to inject into the system prompt."""
    memories = load_memories(ukey)
    if not memories:
        return ""

    # Group by category
    by_cat: dict[str, list[str]] = {}
    for m in memories:
        cat = m.get("category", "fact")
        by_cat.setdefault(cat, []).append(m["text"])

    parts = []
    for cat, items in by_cat.items():
        lines = "; ".join(items)
        parts.append(f"{cat.upper()}: {lines}")

    result = " | ".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars] + "..."
    return result
