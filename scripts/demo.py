#!/usr/bin/env python3
"""
Generate the README demo GIF — a 25-second web-chat conversation showing
the bot answering three questions back-to-back, including a real macOS
screenshot.

How it works:
  1. Boot the WebAdapter on a private port
  2. Use Playwright (with video recording on) to load the chat page
  3. Drive a fake conversation by injecting `appendMsg` calls + simulated
     typing into the chat input — frame-by-frame so the recording looks
     like a real session, not a static page
  4. Save webm, transcode to gif via ffmpeg, drop into docs/assets/

Run: `python3 scripts/demo.py`
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.web import WebAdapter  # noqa: E402

OUT = ROOT / "docs" / "assets" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)
DEMO_GIF = OUT / "demo.gif"


async def _drive_conversation(page, web_adapter):
    """Inject a believable conversation into the chat UI."""
    # Mark the WS connected so the header doesn't say "disconnected"
    await page.evaluate("""
        const s = document.getElementById('status');
        s.textContent = 'connected';
        s.className = 'status connected';
    """)

    async def type_user(text: str, *, ms_per_char: int = 35):
        """Simulate human typing by appending one character at a time."""
        await page.evaluate("document.getElementById('input').focus()")
        for ch in text:
            await page.keyboard.type(ch, delay=ms_per_char)
        await asyncio.sleep(0.4)

    async def submit():
        """Press Enter to submit the typed text — fires the existing form
        handler which renders the user message + clears the input."""
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.6)

    async def bot_says(html: str, pause: float = 1.2):
        await page.evaluate(f"""
            const log = document.getElementById('log');
            const div = document.createElement('div');
            div.className = 'msg bot';
            div.innerHTML = {html!r};
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
        """)
        await asyncio.sleep(pause)

    async def bot_thinking():
        """Show a streaming-style indicator that we'll replace next."""
        await page.evaluate("""
            const log = document.getElementById('log');
            const div = document.createElement('div');
            div.className = 'msg bot';
            div.id = 'thinking';
            div.innerHTML = '<span style="opacity:.7">🤔 Thinking…</span>';
            log.appendChild(div);
            log.scrollTop = log.scrollHeight;
        """)
        await asyncio.sleep(0.9)

    async def bot_replace_thinking(html: str, pause: float = 1.4):
        await page.evaluate(f"""
            const t = document.getElementById('thinking');
            if (t) {{ t.innerHTML = {html!r}; t.id = ''; }}
        """)
        await asyncio.sleep(pause)

    # ---- Turn 1: take a screenshot ----------------------------------------
    await asyncio.sleep(0.6)
    await type_user("take a screenshot of my desktop")
    await submit()
    await bot_thinking()

    # Use a privacy-safe MOCK desktop so the public README doesn't reveal
    # the host machine's open windows. Generated once by
    # scripts/_make_mock_desktop.py — regenerate it if the demo flow changes.
    mock = ROOT / "docs" / "assets" / "screenshots" / "mock-desktop.png"
    if not mock.exists():
        # Regenerate on demand if missing
        subprocess.check_call([sys.executable, str(ROOT / "scripts" / "_make_mock_desktop.py")])
    import base64
    img_bytes = mock.read_bytes()
    img_url = "data:image/png;base64," + base64.b64encode(img_bytes).decode()

    await bot_replace_thinking(
        f'<div>📸 here you go.</div>'
        f'<img src="{img_url}" alt="screenshot" '
        f'style="max-height:240px;width:auto;border-radius:6px;margin-top:6px">',
        pause=2.5,
    )

    # ---- Turn 2: open apps -----------------------------------------------
    await type_user("what apps are open?")
    await submit()
    await bot_thinking()
    await bot_replace_thinking(
        "Safari · Visual Studio Code · Music · Terminal · Slack · Telegram",
        pause=2.0,
    )

    # ---- Turn 3: cost -----------------------------------------------------
    await type_user("/cost")
    await submit()
    await bot_says(
        '<pre>📊 Usage for this chat (web):'
        '\nSince:    2026-05-05 12:18'
        '\nRequests: 14'
        '\nTokens:   in 25,341  ·  out 8,221'
        '\nCost:     ~$0.20  (estimate)</pre>',
        pause=2.5,
    )


async def record_demo() -> Path:
    from playwright.async_api import async_playwright

    web = WebAdapter(host="127.0.0.1", port=8930, auth_token="demo")
    await web.start()
    print("  web adapter listening on :8930")

    pw = await async_playwright().start()
    video_dir = Path(tempfile.mkdtemp(prefix="alfred-demo-video-"))
    print(f"  recording to {video_dir}")
    try:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 760, "height": 720},
            record_video_dir=str(video_dir),
            record_video_size={"width": 760, "height": 720},
        )
        page = await context.new_page()
        await page.goto("http://127.0.0.1:8930/?token=demo",
                        wait_until="domcontentloaded")
        await page.wait_for_selector("#log")
        await _drive_conversation(page, web)
        # Hold final frame for a moment so the GIF doesn't loop too tightly
        await asyncio.sleep(1.0)
        await context.close()  # flushes the video
        await browser.close()
    finally:
        await pw.stop()
        await web.stop()

    # Find the produced webm
    videos = list(video_dir.glob("*.webm"))
    if not videos:
        raise RuntimeError("No video produced")
    webm = videos[0]
    print(f"  webm: {webm} ({webm.stat().st_size:,} bytes)")
    return webm


def webm_to_gif(webm: Path, out: Path) -> None:
    """Two-pass ffmpeg → palette → gif for clean colours at small size."""
    palette = webm.parent / "palette.png"
    common = ["-y", "-i", str(webm), "-vf"]
    fps = "12"
    scale = "640:-1"
    # Pass 1: build palette
    subprocess.check_call([
        "ffmpeg", *common,
        f"fps={fps},scale={scale}:flags=lanczos,palettegen=stats_mode=diff",
        str(palette),
    ], stderr=subprocess.DEVNULL)
    # Pass 2: encode using palette
    subprocess.check_call([
        "ffmpeg", "-y", "-i", str(webm), "-i", str(palette),
        "-filter_complex",
        f"fps={fps},scale={scale}:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=5",
        "-loop", "0",
        str(out),
    ], stderr=subprocess.DEVNULL)
    palette.unlink(missing_ok=True)


async def amain() -> None:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found — install with `brew install ffmpeg`", file=sys.stderr)
        sys.exit(1)
    print(f"Generating {DEMO_GIF.name}…")
    t0 = time.time()
    webm = await record_demo()
    webm_to_gif(webm, DEMO_GIF)
    # Clean up the temp video dir
    shutil.rmtree(webm.parent, ignore_errors=True)
    print(f"  ✓ wrote {DEMO_GIF}  ({DEMO_GIF.stat().st_size / 1024:.0f} KB, {time.time()-t0:.1f}s)")


if __name__ == "__main__":
    asyncio.run(amain())
