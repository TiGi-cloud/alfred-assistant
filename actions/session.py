"""
Session-management commands: /clear, /fork, /cost.

The ClaudeRunner instance is supplied via `register(dispatcher, runner)`
so handlers can talk to the same runner the dispatcher's default-text
handler uses.
"""
from __future__ import annotations

import time
from typing import Optional

from kernel.runner import Context

# Approximate Claude pricing per 1M tokens (USD). These shift over time;
# treat /cost output as a guide, not authoritative.
# Source: https://www.anthropic.com/pricing — adjust as needed.
_PRICES_PER_MTOK = {
    # model substring → (input, output)
    "opus":   (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku":  (0.80, 4.0),
}


def _price_for(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for substr, prices in _PRICES_PER_MTOK.items():
        if substr in m:
            return prices
    # Default to sonnet pricing if unknown
    return _PRICES_PER_MTOK["sonnet"]


def _runner_from_ctx(ctx: Context):
    """Pull the singleton runner stored on the dispatcher."""
    runner = getattr(ctx.adapter, "_alfred_runner", None) or _SHARED.get("runner")
    return runner


_SHARED: dict = {}


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------
async def cmd_clear(ctx: Context) -> None:
    """Drop the current Claude conversation thread for this chat."""
    runner = _runner_from_ctx(ctx)
    if runner is None:
        await ctx.reply("(no Claude runner attached — /clear is a no-op.)")
        return
    runner.clear_session(ctx)
    await ctx.reply("✨ New conversation started.")


# ---------------------------------------------------------------------------
# /fork
# ---------------------------------------------------------------------------
async def cmd_fork(ctx: Context) -> None:
    """Manage named conversation branches.

    /fork                 — list saved branches
    /fork save <name>     — save current session as <name>
    /fork load <name>     — switch back to <name>
    /fork delete <name>   — delete the named branch
    """
    runner = _runner_from_ctx(ctx)
    if runner is None:
        await ctx.reply("(no Claude runner attached — /fork is a no-op.)")
        return

    msg = ctx.message
    args = (msg.command_args or "").split() if msg else []

    if not args:
        forks = runner.list_forks(ctx)
        if not forks:
            await ctx.reply(
                "No saved branches.\n"
                "Use /fork save <name> to save the current conversation."
            )
            return
        body = "\n".join(f"  • {n} → {sid[:8]}…" for n, sid in forks.items())
        await ctx.reply(f"Saved branches ({len(forks)}):\n{body}")
        return

    sub = args[0].lower()
    name = " ".join(args[1:]).strip()

    if sub == "save":
        if not name:
            await ctx.reply("Usage: /fork save <name>")
            return
        if runner.save_fork(ctx, name):
            await ctx.reply(f"💾 Saved current conversation as '{name}'.")
        else:
            await ctx.reply("Nothing to save — start a conversation first.")
    elif sub == "load":
        if not name:
            await ctx.reply("Usage: /fork load <name>")
            return
        if runner.load_fork(ctx, name):
            await ctx.reply(f"⏎ Loaded branch '{name}'.")
        else:
            await ctx.reply(f"No branch named '{name}'.")
    elif sub in ("delete", "remove", "rm"):
        if not name:
            await ctx.reply("Usage: /fork delete <name>")
            return
        if runner.delete_fork(ctx, name):
            await ctx.reply(f"🗑 Deleted branch '{name}'.")
        else:
            await ctx.reply(f"No branch named '{name}'.")
    else:
        await ctx.reply("Usage: /fork [save|load|delete] <name>")


# ---------------------------------------------------------------------------
# /cost
# ---------------------------------------------------------------------------
async def cmd_cost(ctx: Context) -> None:
    """Show token usage + estimated cost for this chat."""
    runner = _runner_from_ctx(ctx)
    if runner is None:
        await ctx.reply("(no Claude runner attached.)")
        return

    records = runner.usage_for(ctx)
    if not records:
        await ctx.reply("No usage recorded yet for this chat.")
        return

    total_in = sum(r.get("in", 0) for r in records)
    total_out = sum(r.get("out", 0) for r in records)
    total_cache_r = sum(r.get("cache_read", 0) for r in records)
    total_cache_w = sum(r.get("cache_write", 0) for r in records)
    requests = len(records)
    first_ts = records[0].get("ts")
    since = time.strftime("%Y-%m-%d %H:%M", time.localtime(first_ts)) if first_ts else "?"

    by_model: dict[str, list[dict]] = {}
    for r in records:
        by_model.setdefault(r.get("model", "default"), []).append(r)

    total_cost = 0.0
    for model, rs in by_model.items():
        in_tok = sum(r.get("in", 0) for r in rs)
        out_tok = sum(r.get("out", 0) for r in rs)
        in_price, out_price = _price_for(model)
        total_cost += (in_tok / 1_000_000) * in_price + (out_tok / 1_000_000) * out_price

    body = [
        f"📊 Usage for this chat ({ctx.adapter.name}):",
        f"  Since:    {since}",
        f"  Requests: {requests}",
        f"  Tokens:   in {total_in:,}  ·  out {total_out:,}",
    ]
    if total_cache_r or total_cache_w:
        body.append(f"  Cache:    read {total_cache_r:,}  ·  write {total_cache_w:,}")
    body.append(f"  Cost:     ~${total_cost:.4f}  (estimate, see Anthropic pricing)")
    if len(by_model) > 1:
        body.append("")
        body.append("By model:")
        for model, rs in by_model.items():
            in_tok = sum(r.get("in", 0) for r in rs)
            out_tok = sum(r.get("out", 0) for r in rs)
            body.append(f"  {model}: {in_tok + out_tok:,} tokens · {len(rs)} runs")
    await ctx.reply("\n".join(body))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(dispatcher, runner=None) -> None:
    if runner is not None:
        _SHARED["runner"] = runner
    dispatcher.command("clear", cmd_clear)
    dispatcher.command("fork", cmd_fork)
    dispatcher.command("cost", cmd_cost)
