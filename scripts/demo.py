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

    import base64

    def data_url(p: Path) -> str:
        return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()

    note_png = ROOT / "docs" / "assets" / "screenshots" / "mock-note.png"
    if not note_png.exists():
        subprocess.check_call([sys.executable, str(ROOT / "scripts" / "_make_mock_desktop.py")])

    # ---- Turn 1: photo of a handwritten note → OCR + reasoning + reminder
    await asyncio.sleep(0.5)
    # User sends a photo (we render a small thumbnail in their bubble)
    await page.evaluate(f"""
        const log = document.getElementById('log');
        const div = document.createElement('div');
        div.className = 'msg me';
        div.innerHTML = '<div style="font-size:12px;opacity:.7;margin-bottom:4px">📎 photo</div>'
                      + '<img src="{data_url(note_png)}" '
                      +   'style="max-height:160px;width:auto;border-radius:6px;display:block">';
        log.appendChild(div);
        log.scrollTop = log.scrollHeight;
    """)
    await asyncio.sleep(0.5)
    await type_user("set me a reminder from this please", ms_per_char=28)
    await submit()
    await bot_thinking()
    await bot_replace_thinking(
        '<div style="opacity:.7;font-size:13px">📸 reading note via Vision OCR…</div>'
        '<pre style="margin:6px 0;font-size:12px">"remind me to pick up\n'
        ' dry cleaning by Friday\n'
        ' 5pm — receipt #4827"</pre>'
        '✓ Reminder set for <b>Fri 5:00 PM</b>  '
        '<span style="opacity:.5">(e3f1)</span><br>'
        'pick up dry cleaning · receipt #4827',
        pause=3.0,
    )

    # ---- Turn 2: build + run ASCII butler --------------------------------
    await type_user("build me an ASCII-art butler I can run from my Desktop", ms_per_char=24)
    await submit()
    await bot_thinking()
    butler_code = (
        '<div style="opacity:.7;font-size:13px">📂 writing <code>~/Desktop/butler.py</code></div>'
        '<pre style="margin:6px 0;font-size:11px;line-height:1.35">'
        'butler = r"""\n'
        "    ┌─────┐\n"
        "    │ ░░░ │   At your service.\n"
        "    └─┬─┬─┘\n"
        "      │ │\n"
        "    ╔═╧═╧═╗\n"
        "    ║ ▒▒▒ ║\n"
        "    ╚═════╝\n"
        '"""\n'
        'print(butler)</pre>'
        '<div style="opacity:.7;font-size:13px">▶ <code>python3 ~/Desktop/butler.py</code></div>'
        '<pre style="margin:6px 0;font-size:11px;line-height:1.35;color:#7ee787">'
        "    ┌─────┐\n"
        "    │ ░░░ │   At your service.\n"
        "    └─┬─┬─┘\n"
        "      │ │\n"
        "    ╔═╧═╧═╗\n"
        "    ║ ▒▒▒ ║\n"
        "    ╚═════╝</pre>"
        '<div style="opacity:.7;font-size:13px">✓ saved 184 chars · file ready on your Desktop</div>'
    )
    await bot_replace_thinking(butler_code, pause=3.5)

    # ---- Turn 3: research a quick answer ----------------------------------
    await type_user("in 40 words: why is Claude Code different from a regular chatbot?", ms_per_char=22)
    await submit()
    await bot_thinking()
    # Stream the research answer one chunk at a time for that "live" feel
    chunks = [
        "🔬 ", "running ",
        "15 ", "agents ", "in ", "parallel… ",
    ]
    await page.evaluate("document.getElementById('thinking').innerHTML = ''")
    for chunk in chunks:
        await page.evaluate(f"""
            const t = document.getElementById('thinking');
            t.innerHTML += {chunk!r};
            document.getElementById('log').scrollTop = 999999;
        """)
        await asyncio.sleep(0.35)
    final = (
        "It runs as a real shell agent — file edits, Bash, MCP servers, web "
        "fetch — not just text completion. So it can read your code, run "
        "commands, and produce side-effects (files, processes), where chatbots "
        "only return strings."
    )
    await page.evaluate(f"""
        const t = document.getElementById('thinking');
        t.innerHTML = '';
        t.id = '';
    """)
    # Type out the answer character-by-character so it feels alive
    cursor_id = "demo-stream"
    await page.evaluate(f"""
        const log = document.getElementById('log');
        const div = document.createElement('div');
        div.className = 'msg bot';
        div.id = {cursor_id!r};
        div.textContent = '';
        log.appendChild(div);
    """)
    for i in range(0, len(final), 3):
        piece = final[i:i + 3]
        await page.evaluate(
            f"document.getElementById({cursor_id!r}).textContent += {piece!r};"
            "document.getElementById('log').scrollTop = 999999;"
        )
        await asyncio.sleep(0.05)
    await asyncio.sleep(1.6)


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
