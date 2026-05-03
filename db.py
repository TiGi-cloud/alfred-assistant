"""SQLite-backed persistence for Alfred bot state.

Replaces 15+ JSON files with a single SQLite database.
Uses a key-value table so existing load/save patterns work unchanged.
"""
from __future__ import annotations

import json
import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger("alfred")

DB_PATH = Path(__file__).parent / "alfred.db"

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS kv ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL"
            ")"
        )
        _conn.commit()
    return _conn


def db_load(key: str, default=None):
    """Load a value by key. Returns parsed JSON or default."""
    if default is None:
        default = {}
    conn = _get_conn()
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError) as e:
            logger.error("Corrupt DB entry for key %s: %s", key, e)
    return default


def db_save(key: str, data):
    """Save a value by key (JSON-serialized)."""
    conn = _get_conn()
    try:
        value = json.dumps(data, indent=2)
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (key, value),
        )
        conn.commit()
    except Exception as e:
        logger.error("Failed to save key %s: %s", key, e)


def db_delete(key: str):
    """Delete a key."""
    conn = _get_conn()
    conn.execute("DELETE FROM kv WHERE key = ?", (key,))
    conn.commit()


def db_keys() -> list[str]:
    """List all keys."""
    conn = _get_conn()
    return [row[0] for row in conn.execute("SELECT key FROM kv").fetchall()]


def migrate_json_to_db(json_files: dict[str, Path]):
    """One-time migration: load each JSON file into the DB, then rename the file.

    json_files: mapping of db_key -> Path to JSON file
    """
    migrated = 0
    for key, path in json_files.items():
        if not path.exists():
            continue
        # Skip if already in DB
        if db_load(key, None) is not None:
            continue
        try:
            data = json.loads(path.read_text())
            db_save(key, data)
            # Rename original to .migrated so it's not loaded again
            backup = path.with_suffix(path.suffix + ".migrated")
            path.rename(backup)
            migrated += 1
            logger.info("Migrated %s -> db key '%s'", path.name, key)
        except Exception as e:
            logger.error("Failed to migrate %s: %s", path.name, e)
    if migrated:
        logger.info("Migrated %d JSON files to SQLite", migrated)


def close():
    """Close the database connection."""
    global _conn
    if _conn:
        _conn.close()
        _conn = None
