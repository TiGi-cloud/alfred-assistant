"""Persistence helpers for Alfred bot state.

Uses SQLite (via db.py) as primary storage.
load_json/save_json now map file paths to DB keys for backward compatibility.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from db import db_load, db_save

logger = logging.getLogger("alfred")


def _path_to_key(path: Path) -> str:
    """Convert a file path to a DB key (e.g. 'sessions.json' -> 'sessions')."""
    return path.stem


def load_json(path: Path, default=None):
    """Load data from SQLite DB keyed by the file stem.

    Falls back to reading the JSON file if the DB key doesn't exist yet
    (handles pre-migration state).
    """
    if default is None:
        default = {}
    key = _path_to_key(path)
    data = db_load(key, None)
    if data is not None:
        return data
    # Fallback: read from JSON file (pre-migration)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            # Auto-migrate to DB
            db_save(key, data)
            return data
        except Exception as e:
            corrupt = path.with_suffix(path.suffix + ".corrupt")
            logger.error("Corrupt JSON in %s (%s) — backed up to %s", path, e, corrupt)
            try:
                path.rename(corrupt)
            except Exception:
                pass
    return default


def save_json(path: Path, data):
    """Save data to SQLite DB keyed by the file stem."""
    key = _path_to_key(path)
    db_save(key, data)
