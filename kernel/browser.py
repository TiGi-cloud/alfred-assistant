"""
Lightweight async browser pool around Playwright.

Provides one shared headless Chromium instance + one BrowserContext per
chat session, so /web snapshot followed by /web click "Submit" works
because the page state is preserved.

Optional dependency: `playwright>=1.40` plus a one-time `playwright install
chromium`. Functions here lazy-import so the rest of Alfred works without
playwright installed; calls to `screenshot()` / `snapshot()` raise a
RuntimeError with install instructions if it's missing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("alfred.kernel.browser")


def _import_playwright():
    try:
        from playwright.async_api import async_playwright  # type: ignore[import]
        return async_playwright
    except ImportError as e:
        raise RuntimeError(
            "Browser features require Playwright. Install with:\n"
            "  pip install 'playwright>=1.40'\n"
            "  python -m playwright install chromium"
        ) from e


class BrowserPool:
    """Owns one headless Chromium process and yields per-session contexts."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._contexts: dict[str, Any] = {}      # session_key -> BrowserContext
        self._pages: dict[str, Any] = {}          # session_key -> Page
        self._last_url: dict[str, str] = {}       # session_key -> url

    async def _ensure_browser(self) -> None:
        async with self._lock:
            if self._browser is not None:
                return
            async_playwright = _import_playwright()
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)

    async def _ensure_page(self, session_key: str):
        await self._ensure_browser()
        page = self._pages.get(session_key)
        if page is None:
            ctx = await self._browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Alfred Browser; +https://github.com/TiGi-cloud/alfred-assistant)",
            )
            page = await ctx.new_page()
            self._contexts[session_key] = ctx
            self._pages[session_key] = page
        return page

    async def screenshot(
        self,
        url: str,
        *,
        session_key: str = "default",
        full_page: bool = False,
        wait_after_load_ms: int = 800,
    ) -> Path:
        """Open `url` in this session's page and capture a PNG to disk."""
        page = await self._ensure_page(session_key)
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(wait_after_load_ms / 1000)
        self._last_url[session_key] = url
        fd, path = tempfile.mkstemp(prefix="alfred-browse-", suffix=".png")
        os.close(fd)
        await page.screenshot(path=path, full_page=full_page)
        return Path(path)

    async def snapshot(self, session_key: str = "default") -> dict:
        """Return the current page's title, url, and a markdown-ish text dump."""
        page = self._pages.get(session_key)
        if page is None:
            raise RuntimeError("No active page — call screenshot() first.")
        title = await page.title()
        url = page.url
        # innerText is the closest thing browsers expose to "what the user sees"
        text = await page.evaluate("() => document.body.innerText")
        text = (text or "")[:8000]
        return {"title": title, "url": url, "text": text}

    async def click(self, selector_or_text: str, *, session_key: str = "default") -> str:
        """Click an element. Accepts a CSS selector or visible text."""
        page = self._pages.get(session_key)
        if page is None:
            raise RuntimeError("No active page — call screenshot() first.")
        # Try as selector first; fall back to "by text"
        try:
            await page.click(selector_or_text, timeout=4000)
        except Exception:
            try:
                await page.get_by_text(selector_or_text, exact=False).first.click(timeout=4000)
            except Exception as e:
                raise RuntimeError(f"Could not find or click {selector_or_text!r}: {e}") from e
        return page.url

    async def close_session(self, session_key: str) -> None:
        ctx = self._contexts.pop(session_key, None)
        self._pages.pop(session_key, None)
        self._last_url.pop(session_key, None)
        if ctx is not None:
            try:
                await ctx.close()
            except Exception:
                pass

    async def shutdown(self) -> None:
        for k in list(self._contexts.keys()):
            await self.close_session(k)
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None


# Process-wide singleton (lazy)
_pool: Optional[BrowserPool] = None


def get_pool() -> BrowserPool:
    global _pool
    if _pool is None:
        _pool = BrowserPool()
    return _pool


async def shutdown_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.shutdown()
        _pool = None
