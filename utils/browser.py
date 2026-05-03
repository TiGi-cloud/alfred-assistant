"""Browser automation via Playwright — navigate, click, type, screenshot."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

logger = logging.getLogger("alfred.browser")

try:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# Per-user browser sessions
_sessions: dict[str, "BrowserSession"] = {}  # ukey -> session
_pw_instance = None  # Shared playwright instance

IDLE_TIMEOUT = 600  # 10 minutes
SCREENSHOT_PATH = "/tmp/alfred_browser_{ukey}.png"
SNAPSHOT_MAX_LINES = 80


class BrowserSession:
    """Manages a Playwright browser context for one user."""

    def __init__(self, ukey: str):
        self.ukey = ukey
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.last_used: float = time.time()
        self._refs: dict[int, dict] = {}  # ref_id -> {role, name, selector}
        self._ref_counter = 0

    async def start(self):
        global _pw_instance
        if not HAS_PLAYWRIGHT:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && playwright install chromium")

        if _pw_instance is None:
            _pw_instance = await async_playwright().start()

        browser = await _pw_instance.chromium.launch(headless=True)
        self.context = await browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        )
        self.page = await self.context.new_page()
        self.last_used = time.time()
        logger.info("Browser session started for %s", self.ukey)

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        except Exception as e:
            logger.warning("Error closing browser context: %s", e)
        self.context = None
        self.page = None
        self._refs.clear()
        logger.info("Browser session closed for %s", self.ukey)

    @property
    def is_active(self) -> bool:
        return self.page is not None and not self.page.is_closed()

    def touch(self):
        self.last_used = time.time()

    async def navigate(self, url: str) -> str:
        """Navigate to URL. Returns page title."""
        if not self.is_active:
            await self.start()
        self.touch()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            return f"Navigation error: {e}"
        await self.page.wait_for_timeout(1000)  # Let JS settle
        return self.page.url

    async def screenshot(self) -> str | None:
        """Take screenshot, return file path."""
        if not self.is_active:
            return None
        self.touch()
        path = SCREENSHOT_PATH.format(ukey=self.ukey.replace(":", "_"))
        await self.page.screenshot(path=path, full_page=False)
        return path

    async def snapshot(self) -> tuple[str, dict[int, dict]]:
        """Get accessibility tree with ref IDs. Returns (text, refs_dict)."""
        if not self.is_active:
            return "No active page.", {}
        self.touch()
        self._refs.clear()
        self._ref_counter = 0

        # Get interactive elements via JS
        elements = await self.page.evaluate("""() => {
            const items = [];
            const selector = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [onclick]';
            document.querySelectorAll(selector).forEach((el, i) => {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) return;
                if (el.offsetParent === null && el.tagName !== 'BODY') return;

                const tag = el.tagName.toLowerCase();
                const role = el.getAttribute('role') || tag;
                const text = (el.textContent || '').trim().substring(0, 80);
                const name = el.getAttribute('aria-label') || el.getAttribute('title')
                           || el.getAttribute('placeholder') || el.getAttribute('alt') || '';
                const type = el.getAttribute('type') || '';
                const href = el.getAttribute('href') || '';
                const value = el.value || '';

                items.push({tag, role, text, name, type, href, value,
                            x: Math.round(rect.x), y: Math.round(rect.y)});
            });
            return items;
        }""")

        lines = []
        title = await self.page.title()
        url = self.page.url
        lines.append(f"Page: {title}")
        lines.append(f"URL: {url}")
        lines.append("---")

        for el in elements:
            self._ref_counter += 1
            ref = self._ref_counter
            self._refs[ref] = el

            tag = el["tag"]
            role = el["role"]
            text = el["text"][:60]
            name = el["name"][:40]

            if tag == "input":
                input_type = el.get("type", "text")
                val = el.get("value", "")
                label = name or text
                lines.append(f"[{ref}] input({input_type}) {label!r} = {val!r}")
            elif tag == "a":
                label = text or name
                href = el.get("href", "")[:60]
                lines.append(f"[{ref}] link {label!r} → {href}")
            elif tag in ("button",) or role == "button":
                label = text or name
                lines.append(f"[{ref}] button {label!r}")
            elif tag == "select":
                label = name or text
                lines.append(f"[{ref}] select {label!r}")
            elif tag == "textarea":
                label = name or text[:30]
                lines.append(f"[{ref}] textarea {label!r}")
            else:
                label = text or name
                lines.append(f"[{ref}] {role} {label!r}")

            if len(lines) >= SNAPSHOT_MAX_LINES:
                lines.append(f"... ({len(elements) - ref} more elements)")
                break

        return "\n".join(lines), self._refs

    async def get_text_content(self) -> str:
        """Get readable text content of the page."""
        if not self.is_active:
            return "No active page."
        self.touch()
        text = await self.page.evaluate("""() => {
            const body = document.body;
            if (!body) return '';
            // Remove script/style elements
            const clone = body.cloneNode(true);
            clone.querySelectorAll('script, style, noscript').forEach(el => el.remove());
            return clone.innerText.substring(0, 4000);
        }""")
        return text.strip() or "(empty page)"

    async def click(self, ref: int) -> str:
        """Click element by ref ID."""
        if ref not in self._refs:
            return f"Invalid ref [{ref}]. Run snapshot first."
        self.touch()
        el = self._refs[ref]
        try:
            await self.page.click(f">> nth={ref - 1}", timeout=5000)
        except Exception:
            # Fallback: click by coordinates
            try:
                x, y = el.get("x", 0), el.get("y", 0)
                await self.page.mouse.click(x + 5, y + 5)
            except Exception as e:
                return f"Click failed: {e}"
        await self.page.wait_for_timeout(1000)
        return f"Clicked [{ref}]. Page: {self.page.url}"

    async def type_text(self, ref: int, text: str) -> str:
        """Type text into element by ref ID."""
        if ref not in self._refs:
            return f"Invalid ref [{ref}]. Run snapshot first."
        self.touch()
        el = self._refs[ref]
        try:
            # Find the element and type
            tag = el["tag"]
            inputs = await self.page.query_selector_all(f"{tag}")
            # Match by position
            target_x, target_y = el.get("x", 0), el.get("y", 0)
            for inp in inputs:
                box = await inp.bounding_box()
                if box and abs(box["x"] - target_x) < 5 and abs(box["y"] - target_y) < 5:
                    await inp.click()
                    await inp.fill(text)
                    return f"Typed into [{ref}]."
            # Fallback: click position and type
            await self.page.mouse.click(target_x + 5, target_y + 5)
            await self.page.keyboard.type(text)
            return f"Typed into [{ref}]."
        except Exception as e:
            return f"Type failed: {e}"

    async def press_key(self, key: str) -> str:
        """Press a keyboard key (Enter, Tab, Escape, etc.)."""
        if not self.is_active:
            return "No active page."
        self.touch()
        try:
            await self.page.keyboard.press(key)
            await self.page.wait_for_timeout(500)
            return f"Pressed {key}."
        except Exception as e:
            return f"Key press failed: {e}"

    async def scroll(self, direction: str = "down") -> str:
        """Scroll the page up or down."""
        if not self.is_active:
            return "No active page."
        self.touch()
        delta = 500 if direction == "down" else -500
        await self.page.mouse.wheel(0, delta)
        await self.page.wait_for_timeout(300)
        return f"Scrolled {direction}."


async def get_session(ukey: str) -> BrowserSession:
    """Get or create a browser session for a user."""
    if ukey in _sessions and _sessions[ukey].is_active:
        _sessions[ukey].touch()
        return _sessions[ukey]
    session = BrowserSession(ukey)
    await session.start()
    _sessions[ukey] = session
    return session


async def close_session(ukey: str):
    """Close a user's browser session."""
    if ukey in _sessions:
        await _sessions[ukey].close()
        del _sessions[ukey]


async def cleanup_idle_sessions():
    """Close sessions idle for more than IDLE_TIMEOUT seconds."""
    now = time.time()
    to_close = [
        ukey for ukey, s in _sessions.items()
        if now - s.last_used > IDLE_TIMEOUT
    ]
    for ukey in to_close:
        logger.info("Closing idle browser session for %s", ukey)
        await close_session(ukey)


async def close_all():
    """Close all browser sessions and the playwright instance."""
    global _pw_instance
    for ukey in list(_sessions.keys()):
        await close_session(ukey)
    if _pw_instance:
        await _pw_instance.stop()
        _pw_instance = None
