"""Browser automation commands — /web for navigating and interacting with web pages."""
from __future__ import annotations

import re

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from core import is_allowed, deny, user_key, check_cmd_rate
from utils.formatting import E, fmt_output
from utils.ui import build_back_close
from utils.browser import HAS_PLAYWRIGHT, get_session, close_session


async def cmd_web(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /web command with sub-commands."""
    if not is_allowed(update):
        return await deny(update)
    if not await check_cmd_rate(update, "web"):
        return

    if not HAS_PLAYWRIGHT:
        await update.message.reply_text(
            "❌ <b>Playwright not installed.</b>\n"
            "<code>pip install playwright && playwright install chromium</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    ukey = user_key(update)
    args = context.args or []

    if not args:
        await _send_web_help(update)
        return

    sub = args[0].lower()

    # /web <url> — navigate
    if sub.startswith("http") or "." in sub:
        await _navigate(update, context, ukey, " ".join(args))
        return

    # /web click <ref>
    if sub == "click" and len(args) >= 2:
        try:
            ref = int(args[1])
        except ValueError:
            await update.message.reply_text("Usage: /web click <ref_number>")
            return
        await _click(update, context, ukey, ref)
        return

    # /web type <ref> <text>
    if sub == "type" and len(args) >= 3:
        try:
            ref = int(args[1])
        except ValueError:
            await update.message.reply_text("Usage: /web type <ref_number> <text>")
            return
        text = " ".join(args[2:])
        await _type(update, context, ukey, ref, text)
        return

    # /web key <key>
    if sub == "key" and len(args) >= 2:
        await _press_key(update, context, ukey, args[1])
        return

    # /web scroll [up|down]
    if sub == "scroll":
        direction = args[1].lower() if len(args) >= 2 else "down"
        await _scroll(update, context, ukey, direction)
        return

    # /web snapshot — text snapshot of interactive elements
    if sub == "snapshot":
        await _snapshot(update, context, ukey)
        return

    # /web text — readable text content
    if sub == "text":
        await _text_content(update, context, ukey)
        return

    # /web screenshot — just the screenshot
    if sub in ("screenshot", "ss"):
        await _screenshot_only(update, context, ukey)
        return

    # /web close — close session
    if sub == "close":
        await close_session(ukey)
        await update.message.reply_text("🌐 Browser session closed.")
        return

    # Default: treat as URL
    await _navigate(update, context, ukey, " ".join(args))


async def _navigate(update, context, ukey, url):
    """Navigate to a URL and send screenshot."""
    msg = await update.message.reply_text("🌐 Loading...")
    await context.bot.send_chat_action(update.effective_chat.id, "upload_photo")

    session = await get_session(ukey)
    result_url = await session.navigate(url)

    if result_url.startswith("Navigation error"):
        await msg.edit_text(f"❌ {E(result_url)}", parse_mode=ParseMode.HTML)
        return

    path = await session.screenshot()
    title = await session.page.title() if session.page else "?"

    kb = _browser_keyboard()

    try:
        await msg.delete()
    except Exception:
        pass

    if path:
        try:
            with open(path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"🌐 <b>{E(title[:60])}</b>\n<code>{E(result_url[:80])}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
        except Exception as e:
            await update.message.reply_text(f"Screenshot failed: {e}")
    else:
        await update.message.reply_text(
            f"🌐 <b>{E(title[:60])}</b>\n<code>{E(result_url[:80])}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )


async def _click(update, context, ukey, ref):
    """Click an element and send updated screenshot."""
    session = await get_session(ukey)
    if not session.is_active:
        await update.message.reply_text("No active browser. Use /web <url> first.")
        return

    result = await session.click(ref)
    path = await session.screenshot()
    kb = _browser_keyboard()

    if path:
        try:
            with open(path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"👆 {E(result[:100])}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
        except Exception:
            await update.message.reply_text(result, reply_markup=kb)
    else:
        await update.message.reply_text(result, reply_markup=kb)


async def _type(update, context, ukey, ref, text):
    """Type into an element and send updated screenshot."""
    session = await get_session(ukey)
    if not session.is_active:
        await update.message.reply_text("No active browser. Use /web <url> first.")
        return

    result = await session.type_text(ref, text)
    path = await session.screenshot()
    kb = _browser_keyboard()

    if path:
        try:
            with open(path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"⌨️ {E(result[:100])}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
        except Exception:
            await update.message.reply_text(result, reply_markup=kb)
    else:
        await update.message.reply_text(result, reply_markup=kb)


async def _press_key(update, context, ukey, key):
    """Press a key and send screenshot."""
    session = await get_session(ukey)
    if not session.is_active:
        await update.message.reply_text("No active browser. Use /web <url> first.")
        return

    result = await session.press_key(key)
    path = await session.screenshot()
    kb = _browser_keyboard()

    if path:
        try:
            with open(path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=f"⌨️ {E(result[:100])}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                )
        except Exception:
            await update.message.reply_text(result, reply_markup=kb)
    else:
        await update.message.reply_text(result, reply_markup=kb)


async def _scroll(update, context, ukey, direction):
    """Scroll and send screenshot."""
    session = await get_session(ukey)
    if not session.is_active:
        await update.message.reply_text("No active browser. Use /web <url> first.")
        return

    result = await session.scroll(direction)
    path = await session.screenshot()
    kb = _browser_keyboard()

    if path:
        try:
            with open(path, "rb") as f:
                await update.message.reply_photo(
                    photo=f,
                    caption=result,
                    reply_markup=kb,
                )
        except Exception:
            await update.message.reply_text(result, reply_markup=kb)
    else:
        await update.message.reply_text(result, reply_markup=kb)


async def _snapshot(update, context, ukey):
    """Send text snapshot of interactive elements."""
    session = await get_session(ukey)
    if not session.is_active:
        await update.message.reply_text("No active browser. Use /web <url> first.")
        return

    text, refs = await session.snapshot()
    kb = _browser_keyboard()

    await update.message.reply_text(
        f"<pre>{E(text[:3800])}</pre>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def _text_content(update, context, ukey):
    """Send readable text content of the page."""
    session = await get_session(ukey)
    if not session.is_active:
        await update.message.reply_text("No active browser. Use /web <url> first.")
        return

    text = await session.get_text_content()
    kb = _browser_keyboard()

    await update.message.reply_text(
        fmt_output(text, 3800),
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def _screenshot_only(update, context, ukey):
    """Just send a screenshot of current page."""
    await context.bot.send_chat_action(update.effective_chat.id, "upload_photo")
    session = await get_session(ukey)
    if not session.is_active:
        await update.message.reply_text("No active browser. Use /web <url> first.")
        return

    path = await session.screenshot()
    kb = _browser_keyboard()

    if path:
        title = await session.page.title() if session.page else "?"
        with open(path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=f"🌐 {E(title[:60])}",
                parse_mode=ParseMode.HTML,
                reply_markup=kb,
            )


async def _send_web_help(update):
    """Send help text for /web command."""
    await update.message.reply_text(
        "<b>🌐 Web Browser</b>\n\n"
        "<code>/web &lt;url&gt;</code> — open a page\n"
        "<code>/web snapshot</code> — list interactive elements\n"
        "<code>/web click &lt;ref&gt;</code> — click element\n"
        "<code>/web type &lt;ref&gt; &lt;text&gt;</code> — type into field\n"
        "<code>/web key Enter|Tab|Escape</code> — press key\n"
        "<code>/web scroll [up|down]</code> — scroll page\n"
        "<code>/web text</code> — get page text content\n"
        "<code>/web screenshot</code> — capture current page\n"
        "<code>/web close</code> — close browser session",
        parse_mode=ParseMode.HTML,
    )


def _browser_keyboard() -> InlineKeyboardMarkup:
    """Quick-action buttons for browser."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📸 Screenshot", callback_data="web:screenshot"),
            InlineKeyboardButton("📋 Snapshot", callback_data="web:snapshot"),
        ],
        [
            InlineKeyboardButton("⬆️ Scroll Up", callback_data="web:scroll_up"),
            InlineKeyboardButton("⬇️ Scroll Down", callback_data="web:scroll_down"),
        ],
        [
            InlineKeyboardButton("📝 Text", callback_data="web:text"),
            InlineKeyboardButton("✖ Close", callback_data="web:close"),
        ],
    ])
