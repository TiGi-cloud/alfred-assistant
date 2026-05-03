"""
Screen capture handlers: /screenshot, /record, /watch, /camera, /ocr.

All Mac-native; works across every chat adapter that supports send_photo /
send_video.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from kernel.runner import Context

# Per-chat watch tasks so /watch can be toggled
_watch_tasks: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _run(*cmd: str, timeout: float = 30) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _temp_path(suffix: str) -> str:
    fd, path = tempfile.mkstemp(prefix="alfred-", suffix=suffix)
    os.close(fd)
    return path


# ---------------------------------------------------------------------------
# /screenshot
# ---------------------------------------------------------------------------
async def cmd_screenshot(ctx: Context) -> None:
    if sys.platform != "darwin":
        await ctx.reply("/screenshot: macOS only.")
        return
    path = _temp_path(".png")
    rc, _, err = await _run("screencapture", "-x", path, timeout=10)
    if rc != 0 or not os.path.exists(path):
        await ctx.reply(f"screencapture failed: {err.strip() or 'unknown'}")
        return
    await ctx.adapter.send_photo(ctx.chat_id, path, caption="📸")
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# /record [seconds]
# ---------------------------------------------------------------------------
async def cmd_record(ctx: Context) -> None:
    """Record the screen. Usage: /record [seconds] (default 10, max 60)."""
    if sys.platform != "darwin":
        await ctx.reply("/record: macOS only.")
        return
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""
    try:
        seconds = max(1, min(60, int(args))) if args else 10
    except ValueError:
        await ctx.reply("Usage: /record [seconds]  (1-60)")
        return

    path = _temp_path(".mov")
    await ctx.reply(f"⏺ Recording {seconds}s…")
    rc, _, err = await _run("screencapture", "-v", "-V", str(seconds), path, timeout=seconds + 15)
    if rc != 0 or not os.path.exists(path) or os.path.getsize(path) < 1024:
        await ctx.reply(f"Recording failed: {err.strip() or 'no output'}")
        return
    await ctx.adapter.send_video(ctx.chat_id, path, caption=f"📹 {seconds}s recording")
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# /watch [interval]
# ---------------------------------------------------------------------------
async def cmd_watch(ctx: Context) -> None:
    """Live screen stream — sends a screenshot every N seconds. Toggle on/off."""
    if sys.platform != "darwin":
        await ctx.reply("/watch: macOS only.")
        return
    chat_id = ctx.chat_id

    if chat_id in _watch_tasks and not _watch_tasks[chat_id].done():
        _watch_tasks[chat_id].cancel()
        _watch_tasks.pop(chat_id, None)
        await ctx.reply("👁 Watch stopped.")
        return

    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""
    try:
        interval = max(2, min(60, int(args))) if args else 5
    except ValueError:
        await ctx.reply("Usage: /watch [seconds-between-frames]  (2-60)")
        return

    await ctx.reply(f"👁 Watching every {interval}s — send /watch again to stop.")

    async def _loop():
        try:
            while True:
                path = _temp_path(".png")
                rc, _, _ = await _run("screencapture", "-x", path, timeout=10)
                if rc == 0 and os.path.exists(path):
                    try:
                        await ctx.adapter.send_photo(chat_id, path)
                    except Exception:
                        pass
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    _watch_tasks[chat_id] = asyncio.create_task(_loop(), name=f"watch-{chat_id}")


# ---------------------------------------------------------------------------
# /camera — FaceTime camera photo
# ---------------------------------------------------------------------------
async def cmd_camera(ctx: Context) -> None:
    if sys.platform != "darwin":
        await ctx.reply("/camera: macOS only.")
        return
    path = _temp_path(".jpg")
    if shutil.which("imagesnap"):
        rc, _, err = await _run("imagesnap", "-w", "1", path, timeout=15)
    else:
        # Fallback to ffmpeg avfoundation
        if not shutil.which("ffmpeg"):
            await ctx.reply("Install: `brew install imagesnap` (or ffmpeg)")
            return
        rc, _, err = await _run(
            "ffmpeg", "-y", "-f", "avfoundation", "-framerate", "30",
            "-i", "0", "-frames:v", "1", path,
            timeout=15,
        )
    if rc != 0 or not os.path.exists(path):
        await ctx.reply(f"camera failed: {err.strip()[:200] or 'unknown'}")
        return
    await ctx.adapter.send_photo(ctx.chat_id, path, caption="📷")
    try:
        os.remove(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# /ocr — Vision framework OCR on the most-recent attached image
# ---------------------------------------------------------------------------
_OCR_APPLESCRIPT = '''
use framework "Foundation"
use framework "AppKit"
use framework "Vision"
use scripting additions
on run argv
    set imgPath to (item 1 of argv) as text
    set img to current application's NSImage's alloc()'s initWithContentsOfFile:imgPath
    if img is missing value then return ""
    set reqHandler to current application's VNImageRequestHandler's alloc()'s initWithData:(img's TIFFRepresentation()) options:(current application's NSDictionary's dictionary())
    set req to current application's VNRecognizeTextRequest's alloc()'s init()
    req's setRecognitionLevel:(current application's VNRequestTextRecognitionLevelAccurate)
    reqHandler's performRequests:(current application's NSArray's arrayWithObject:req) |error|:(missing value)
    set output to ""
    repeat with obs in (req's results())
        set output to output & ((obs's topCandidates:1)'s first item's |string|() as text) & linefeed
    end repeat
    return output
end run
'''


async def _vision_ocr(image_path: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-l", "AppleScript", "-e", _OCR_APPLESCRIPT, image_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode(errors="replace").strip() if proc.returncode == 0 else ""


async def cmd_ocr(ctx: Context) -> None:
    """OCR the photo this command replies to (or /ocr alone takes a screenshot first)."""
    if sys.platform != "darwin":
        await ctx.reply("/ocr: macOS only.")
        return

    msg = ctx.message
    image_path: str | None = None

    # 1. If the message has an attached photo, download it
    if msg and msg.attachments:
        for att in msg.attachments:
            if att.kind.value == "photo":
                if att.local_path and Path(att.local_path).exists():
                    image_path = str(att.local_path)
                else:
                    try:
                        p = await ctx.adapter.download_attachment(att)
                        image_path = str(p)
                    except Exception as e:
                        await ctx.reply(f"Couldn't download attachment: {e}")
                        return
                break

    # 2. No attachment → screenshot the current screen
    if not image_path:
        image_path = _temp_path(".png")
        rc, _, _ = await _run("screencapture", "-x", image_path, timeout=10)
        if rc != 0:
            await ctx.reply("screenshot failed.")
            return

    text = await _vision_ocr(image_path)
    if not text:
        await ctx.reply("No text detected (or Vision OCR failed).")
    else:
        await ctx.reply(text[:3500] + ("\n\n…(truncated)" if len(text) > 3500 else ""))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(dispatcher) -> None:
    dispatcher.command("screenshot", cmd_screenshot)
    dispatcher.command("record", cmd_record)
    dispatcher.command("watch", cmd_watch)
    dispatcher.command("camera", cmd_camera)
    dispatcher.command("ocr", cmd_ocr)
