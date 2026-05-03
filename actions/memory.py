"""
/memory — long-term facts Claude remembers across conversations.

Memories are stored per (adapter, user) so they follow you within a chat
platform. The Claude runner injects them into every prompt's system context
and extracts new ones from `[REMEMBER:category:fact]` markers in responses.

Usage:

    /memory                       list everything I remember
    /memory add <fact>            store a new memory (category: fact)
    /memory add pref <fact>       …with explicit category
    /memory search <query>        keyword search
    /memory remove <id>           delete by short ID
    /memory clear                 wipe all memories for this user
"""
from __future__ import annotations

import time

from kernel.runner import Context

from kernel.store import (  # noqa: E402
    add_memory, clear_memories, delete_memory, load_memories, search_memories,
)


CATEGORIES = ("preference", "fact", "routine", "context", "task")


def _user_key(ctx: Context) -> str:
    """Same key the kernel.ClaudeRunner uses, so memories stay in sync."""
    return f"{ctx.adapter.name}:{ctx.user.id}"


def _fmt(memories: list[dict], limit: int = 25) -> str:
    if not memories:
        return "(none yet)"
    out = []
    for m in memories[-limit:]:
        when = time.strftime("%m-%d %H:%M", time.localtime(m.get("ts", 0)))
        cat = m.get("category", "fact")
        body = m.get("text", "")[:120]
        mid = m.get("id", "?")
        out.append(f"  [{cat:8}] {body}   ({mid} · {when})")
    if len(memories) > limit:
        out.insert(0, f"  … {len(memories) - limit} older memories not shown")
    return "\n".join(out)


async def cmd_memory(ctx: Context) -> None:
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""
    parts = args.split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    ukey = _user_key(ctx)

    if not sub:
        memories = load_memories(ukey)
        await ctx.reply(f"🧠 Memory ({len(memories)} entries):\n" + _fmt(memories))
        return

    if sub == "add":
        if not rest:
            await ctx.reply("Usage: /memory add [category] <fact>")
            return
        # Optional explicit category as the first word
        cat_words = rest.split(maxsplit=1)
        if cat_words[0].lower() in CATEGORIES and len(cat_words) == 2:
            category = cat_words[0].lower()
            fact = cat_words[1]
        else:
            category = "fact"
            fact = rest
        entry = add_memory(ukey, fact, category=category)
        await ctx.reply(f"💾 Remembered [{category}]: {fact[:100]}  ({entry['id']})")
        return

    if sub == "search":
        if not rest:
            await ctx.reply("Usage: /memory search <query>")
            return
        results = search_memories(ukey, rest)
        if not results:
            await ctx.reply(f"No memories matching '{rest}'.")
            return
        await ctx.reply(f"🔍 {len(results)} match(es):\n" + _fmt(results))
        return

    if sub in ("remove", "delete", "rm"):
        if not rest:
            await ctx.reply("Usage: /memory remove <id>")
            return
        if delete_memory(ukey, rest):
            await ctx.reply(f"🗑 Deleted memory {rest}.")
        else:
            await ctx.reply(f"No memory with id '{rest}'.")
        return

    if sub == "clear":
        n = clear_memories(ukey)
        await ctx.reply(f"🧹 Cleared {n} memories.")
        return

    await ctx.reply(
        "Usage:\n"
        "  /memory\n"
        "  /memory add [category] <fact>\n"
        "  /memory search <query>\n"
        "  /memory remove <id>\n"
        "  /memory clear"
    )


def register(dispatcher) -> None:
    dispatcher.command("memory", cmd_memory)
