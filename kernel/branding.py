"""
Centralised Alfred branding — logo + colours.

The logo lives once in `docs/assets/`. Anywhere we need to embed it (web
chat, setup wizard, Mini App) calls `logo_data_url()` so we don't end up
with stale base64 inline in source.
"""
from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "docs" / "assets"

LOGOS = {
    "favicon": ASSETS / "favicon.png",       # 64×64
    "128":     ASSETS / "logo-128.png",
    "256":     ASSETS / "logo-256.png",
    "512":     ASSETS / "logo-512.png",
    "full":    ASSETS / "logo.png",
}

# Single source of brand-truth for any UI surface that wants Alfred colours
PRIMARY_BG = "#0e1116"
ACCENT     = "#2f81f7"


@lru_cache(maxsize=8)
def logo_data_url(size: str = "favicon") -> str:
    """Return `data:image/png;base64,...` for the named logo size.

    Returns an empty string (renders nothing) if the file is missing —
    so the bot stays usable even if someone deletes docs/assets.
    """
    p = LOGOS.get(size, LOGOS["favicon"])
    if not p.exists():
        return ""
    data = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def logo_path(size: str = "full") -> Path | None:
    """Return the on-disk path for a logo size, or None if missing."""
    p = LOGOS.get(size, LOGOS["full"])
    return p if p.exists() else None
