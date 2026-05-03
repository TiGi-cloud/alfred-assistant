"""Alfred /memory command — persistent memory management."""
from __future__ import annotations

import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from core import is_allowed, deny, user_key, check_cmd_rate, build_back_button
from utils.formatting import E
from utils.memory import (
    load_memories, add_memory, delete_memory, search_memories,
    clear_memories,
)

CATEGORIES = ["preference", "fact", "routine", "context", "task"]


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /memory command."""
    if not is_allowed(update):
        return await deny(update)
    if not check_cmd_rate(update, "memory"):
        return

    ukey = user_key(update)
    args = context.args or []

    if not args:
        return await _show_memories(update, ukey)

    sub = args[0].lower()

    if sub == "add" and len(args) >= 2:
        text = " ".join(args[1:])
        # Check for category prefix like "preference:likes dark mode"
        category = "fact"
        for cat in CATEGORIES:
            if text.lower().startswith(cat + ":"):
                category = cat
                text = text[len(cat) + 1:].strip()
                break
        entry = add_memory(ukey, text, category)
        await update.message.reply_text(
            f"✅ Remembered [{entry['category']}]: {E(text)}",
            parse_mode=ParseMode.HTML,
        )

    elif sub == "search" and len(args) >= 2:
        query = " ".join(args[1:])
        results = search_memories(ukey, query)
        if not results:
            await update.message.reply_text(f"No memories matching '{E(query)}'.", parse_mode=ParseMode.HTML)
        else:
            lines = []
            for m in results[:20]:
                lines.append(f"• <code>{m['id']}</code> [{m['category']}] {E(m['text'])}")
            await update.message.reply_text(
                f"🔍 Found {len(results)} memor{'y' if len(results)==1 else 'ies'}:\n" + "\n".join(lines),
                parse_mode=ParseMode.HTML,
            )

    elif sub in ("delete", "del", "rm") and len(args) >= 2:
        mid = args[1]
        if delete_memory(ukey, mid):
            await update.message.reply_text(f"🗑 Memory {mid} deleted.")
        else:
            await update.message.reply_text(f"Memory {mid} not found.")

    elif sub == "clear":
        count = clear_memories(ukey)
        await update.message.reply_text(f"🗑 Cleared {count} memories.")

    else:
        await update.message.reply_text(
            "<b>Memory</b>\n"
            "<code>/memory</code> — list all\n"
            "<code>/memory add &lt;text&gt;</code> — remember something\n"
            "<code>/memory add preference:&lt;text&gt;</code> — with category\n"
            "<code>/memory search &lt;query&gt;</code> — search\n"
            "<code>/memory delete &lt;id&gt;</code> — delete one\n"
            "<code>/memory clear</code> — delete all\n\n"
            f"Categories: {', '.join(CATEGORIES)}",
            parse_mode=ParseMode.HTML,
        )


async def _show_memories(update: Update, ukey: str):
    memories = load_memories(ukey)
    if not memories:
        await update.message.reply_text(
            "🧠 No memories yet.\nUse <code>/memory add &lt;text&gt;</code> to remember something.\n"
            "Or just tell me naturally — I'll remember it automatically.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Group by category
    by_cat: dict[str, list] = {}
    for m in memories:
        by_cat.setdefault(m.get("category", "fact"), []).append(m)

    lines = [f"🧠 <b>{len(memories)} memories</b>\n"]
    for cat, items in by_cat.items():
        lines.append(f"\n<b>{cat.upper()}</b>")
        for m in items[-10:]:  # Show last 10 per category
            ts = time.strftime("%m/%d", time.localtime(m["ts"]))
            lines.append(f"  <code>{m['id']}</code> {E(m['text'][:80])} <i>({ts})</i>")
        if len(items) > 10:
            lines.append(f"  ... and {len(items)-10} more")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=build_back_button(),
    )
