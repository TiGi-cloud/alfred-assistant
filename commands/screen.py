from __future__ import annotations

import os
import asyncio
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from core import is_allowed, deny, user_key, check_cmd_rate, build_back_button
from utils.formatting import progress_bar
from utils.helpers import async_run, take_screenshot, cleanup_temp
import bot_state as st


async def cmd_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    if not await check_cmd_rate(update, "screenshot"):
        return
    path = "/tmp/screenshot.png"
    await context.bot.send_chat_action(update.effective_chat.id, "upload_photo")
    if await take_screenshot(path):
        try:
            with open(path, 'rb') as f:
                await update.message.reply_photo(photo=f)
        finally:
            cleanup_temp(path)
    else:
        await update.message.reply_text(
            "❌ <b>Screenshot failed.</b>\n"
            "<i>Check Screen Recording permission: System Settings → Privacy &amp; Security → Screen Recording.</i>",
            parse_mode=ParseMode.HTML,
        )


async def cmd_record(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    if not await check_cmd_rate(update, "record"):
        return
    duration = 10
    if context.args:
        try:
            duration = min(int(context.args[0]), 60)
        except ValueError:
            pass

    path = "/tmp/screenrecord.mp4"
    msg = await update.message.reply_text(
        f"<b>Recording</b> {duration}s...\n{progress_bar(0)}",
        parse_mode=ParseMode.HTML,
    )

    proc = await asyncio.create_subprocess_exec(
        "screencapture", "-v", "-V", str(duration), path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    ukey = user_key(update)
    st.user_processes.setdefault(ukey, []).append(proc)
    # Update progress while recording
    for i in range(duration):
        await asyncio.sleep(1)
        pct = (i + 1) / duration * 100
        try:
            await msg.edit_text(
                f"<b>Recording</b> {i+1}/{duration}s\n{progress_bar(pct)}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    await proc.communicate()
    procs = st.user_processes.get(ukey, [])
    if proc in procs:
        procs.remove(proc)
    if not procs:
        st.user_processes.pop(ukey, None)

    if os.path.isfile(path) and os.path.getsize(path) > 0:
        try:
            await msg.delete()
        except Exception:
            pass
        await context.bot.send_chat_action(update.effective_chat.id, "upload_video")
        try:
            with open(path, 'rb') as f:
                await update.message.reply_video(video=f, caption=f"{duration}s screen recording", supports_streaming=True)
        finally:
            cleanup_temp(path)
    else:
        await msg.edit_text(
            "Recording failed. Try: <code>/record 5</code>",
            parse_mode=ParseMode.HTML,
        )


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)

    if ukey in st.watch_tasks:
        st.watch_tasks[ukey].cancel()
        del st.watch_tasks[ukey]
        await update.message.reply_text("Screen watch stopped.", reply_markup=build_back_button())
        return

    interval = 5
    if context.args:
        try:
            interval = max(2, min(int(context.args[0]), 30))
        except ValueError:
            pass

    stop_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏹ Stop Watch", callback_data=f"watch_stop:{ukey}")]
    ])
    await update.message.reply_text(
        f"Watching screen every {interval}s. Tap Stop or send /watch again to stop.",
        reply_markup=stop_kb,
    )

    async def stream_screenshots():
        try:
            while True:
                path = f"/tmp/watch_{int(time.time())}.png"
                if await take_screenshot(path):
                    with open(path, 'rb') as f:
                        await update.message.reply_photo(photo=f)
                    cleanup_temp(path)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    st.watch_tasks[ukey] = asyncio.create_task(stream_screenshots())


async def cmd_camera(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    path = "/tmp/camera_capture.jpg"
    rc, _, _ = await async_run(["which", "imagesnap"], timeout=5)
    if rc == 0:
        rc, _, err = await async_run(["imagesnap", "-w", "1", path], timeout=15)
    else:
        rc, _, err = await async_run([
            "ffmpeg", "-y", "-f", "avfoundation", "-framerate", "30",
            "-i", "0", "-frames:v", "1", path
        ], timeout=15)

    if rc == 0 and os.path.isfile(path):
        await context.bot.send_chat_action(update.effective_chat.id, "upload_photo")
        with open(path, 'rb') as f:
            await update.message.reply_photo(photo=f, caption="FaceTime camera")
        cleanup_temp(path)
    else:
        await update.message.reply_text(
            f"Camera failed. Install: <code>brew install imagesnap</code>",
            parse_mode=ParseMode.HTML,
        )
