"""
/web — drive a headless Chromium from chat.

  /web <url>                  — load URL, send screenshot
  /web snapshot               — markdown-ish dump of the current page
  /web click <text-or-css>    — click an element on the active page
  /web close                  — close this session's browser context

State is per-(adapter, chat) so /web click after /web <url> stays on the
same page. Requires `pip install 'playwright>=1.40' && playwright install
chromium` — the kernel.browser module surfaces a friendly install hint
when missing.
"""
from __future__ import annotations

import os

from kernel.runner import Context


def _session_key(ctx: Context) -> str:
    return f"{ctx.adapter.name}:{ctx.chat_id}"


async def cmd_web(ctx: Context) -> None:
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""

    if not args:
        await ctx.reply(
            "Usage:\n"
            "  /web <url>              — load + screenshot\n"
            "  /web snapshot           — markdown dump of current page\n"
            "  /web click <text|css>   — click an element\n"
            "  /web close              — close browser session"
        )
        return

    from kernel.browser import get_pool

    parts = args.split(maxsplit=1)
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    pool = get_pool()
    sk = _session_key(ctx)

    try:
        if sub == "snapshot":
            try:
                snap = await pool.snapshot(sk)
            except RuntimeError as e:
                await ctx.reply(str(e))
                return
            body = (
                f"📄 {snap['title']}\n"
                f"   {snap['url']}\n\n"
                f"{snap['text']}"
            )
            await ctx.reply(body[:3500])
            return

        if sub == "click":
            if not rest:
                await ctx.reply("Usage: /web click <text-or-selector>")
                return
            try:
                new_url = await pool.click(rest, session_key=sk)
            except RuntimeError as e:
                await ctx.reply(str(e))
                return
            shot = await pool.screenshot(new_url, session_key=sk)
            try:
                await ctx.adapter.send_photo(ctx.chat_id, shot, caption=new_url)
            finally:
                try:
                    os.unlink(shot)
                except OSError:
                    pass
            return

        if sub == "close":
            await pool.close_session(sk)
            await ctx.reply("🔚 closed browser session.")
            return

        # Otherwise treat it as a URL
        url = args if "://" in args else f"https://{args}"
        try:
            shot = await pool.screenshot(url, session_key=sk)
        except RuntimeError as e:
            await ctx.reply(str(e))
            return
        try:
            await ctx.adapter.send_photo(ctx.chat_id, shot, caption=url)
        finally:
            try:
                os.unlink(shot)
            except OSError:
                pass

    except Exception as e:
        await ctx.reply(f"web error: {e}")


def register(dispatcher) -> None:
    dispatcher.command("web", cmd_web)
