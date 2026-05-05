#!/usr/bin/env python3
"""Generate README screenshots of the wizard + web chat (run locally).

Boots both servers in-process, drives a headless browser through Playwright,
populates the chat UI with a sample conversation by calling its `appendMsg`
JS function directly (so we don't depend on a Claude API call), and writes
PNGs to docs/assets/screenshots/.

Run with:

    python3 scripts/screenshots.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.web import WebAdapter  # noqa: E402
import setup_wizard  # noqa: E402

OUT = ROOT / "docs" / "assets" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)


async def screenshot_setup_wizard(pw_browser):
    """Wizard at localhost:8088."""
    site_task = asyncio.create_task(setup_wizard.serve(port=8088, open_browser=False))
    await asyncio.sleep(0.6)
    try:
        page = await pw_browser.new_page(viewport={"width": 760, "height": 1000})
        await page.goto("http://127.0.0.1:8088/", wait_until="networkidle")
        await page.evaluate("""
            // Pre-fill some fields so the wizard looks alive
            const el = (n) => document.querySelector(`[name="${n}"]`);
            el("telegram_bot_token").value = "1234567890:AAEhBP0av••••••••••";
            el("telegram_bot_token").type = "text";  // unmask for the screenshot
            el("allowed_users").value = "yourname";
            el("web_enabled").checked = true;
            el("web_port").value = "8765";
        """)
        await asyncio.sleep(0.2)
        out = OUT / "setup-wizard.png"
        await page.screenshot(path=str(out), full_page=True)
        print(f"  wrote {out}")
        await page.close()
    finally:
        site_task.cancel()
        try:
            await site_task
        except (asyncio.CancelledError, SystemExit):
            pass


async def screenshot_web_chat(pw_browser):
    """Web chat at localhost:8765 with a fake conversation injected."""
    web = WebAdapter(host="127.0.0.1", port=8089, auth_token="demo")
    await web.start()
    try:
        page = await pw_browser.new_page(viewport={"width": 760, "height": 980})
        await page.goto("http://127.0.0.1:8089/?token=demo", wait_until="domcontentloaded")
        # Wait for the chat UI to render
        await page.wait_for_selector("#log")
        # Mark connected so the status pill turns green
        await page.evaluate("""
            document.getElementById('status').textContent = 'connected';
            document.getElementById('status').className = 'status connected';
        """)
        # Inject a sample conversation by calling the same DOM helper the
        # WebSocket handler uses on incoming messages.
        await page.evaluate(r"""
            const log = document.getElementById('log');

            function add(role, html) {
                const div = document.createElement('div');
                div.className = 'msg ' + role;
                div.innerHTML = html;
                log.appendChild(div);
            }

            add('me',  'take a screenshot, OCR it, and tell me what apps I have open');
            add('bot', `<div style="display:flex;gap:10px;align-items:center;background:#0d1117;border:1px solid #30363d;border-radius:8px;padding:10px;margin:6px 0">
                         <span style="font-size:24px">📸</span>
                         <span style="color:#8b949e">screenshot.png  ·  1.2 MB  ·  3024×1964</span>
                       </div>
                       <div style="margin-top:6px">OCR text from the screenshot:</div>
                       <pre>Code — kernel/claude.py
async def run(self, ctx, prompt, …):
    …</pre>
                       Open apps: Safari · Code · Music · Terminal · Slack`);

            add('me',  'remind me at 7pm to call mom');
            add('bot', '✓ Reminder set for 19:00 today  <span style="opacity:.6">(e3f1)</span><br>call mom');

            add('me',  '/cost');
            add('bot', `<pre>📊 Usage for this chat (web):
Since:    2026-05-05 11:42
Requests: 14
Tokens:   in 25,341  ·  out 8,221
Cost:     ~$0.20  (estimate)</pre>`);

            log.scrollTop = log.scrollHeight;
        """)
        await asyncio.sleep(0.3)
        out = OUT / "web-chat.png"
        await page.screenshot(path=str(out), full_page=True)
        print(f"  wrote {out}")
        await page.close()
    finally:
        await web.stop()


async def main():
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
        try:
            print("Generating screenshots…")
            await screenshot_setup_wizard(browser)
            await screenshot_web_chat(browser)
        finally:
            await browser.close()
    finally:
        await pw.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        pass
