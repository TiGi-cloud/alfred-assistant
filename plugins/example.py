"""
Example Alfred plugin.

Each plugin must define:
  - COMMAND: str       — the slash command name (without /)
  - DESCRIPTION: str   — shown in /help and plugin list
  - async def handler(update, context)   — the Telegram handler

Optional:
  - def setup(ctx: dict)  — called at load time with shared bot internals

The ctx dict contains everything you need to build real features:
  ctx["run_claude"]       — async (prompt, ukey) -> str
  ctx["send_response"]    — async (update, text)
  ctx["async_run"]        — async (cmd_list, ...) -> (rc, stdout, stderr)
  ctx["is_allowed"]       — (update) -> bool
  ctx["user_key"]         — (update) -> str
  ctx["add_history"]      — (ukey, role, text)
  ctx["E"]                — html.escape shorthand
  ctx["md_to_html"]       — markdown to Telegram HTML
  ctx["fmt_output"]       — format shell output for display
  ctx["ParseMode"]        — telegram.constants.ParseMode
  ctx["user_sessions"]    — dict: ukey -> session_id  (live, mutable)
  ctx["cost_tracker"]     — dict: ukey -> {input_tokens, output_tokens, requests}
  ctx["history"]          — dict: ukey -> list of message dicts
  ctx["user_models"]      — dict: ukey -> model name override
  ctx["SNAPSHOTS_DIR"]    — Path to snapshots directory
"""

COMMAND = "hello"
DESCRIPTION = "Say hello (example plugin)"

_ctx = {}


def setup(ctx: dict):
    """Receive shared bot context. Store it for use in handler."""
    global _ctx
    _ctx = ctx


async def handler(update, context):
    """A simple example that uses shared state if available."""
    if _ctx and not _ctx["is_allowed"](update):
        return
    ukey = _ctx["user_key"](update) if _ctx else "unknown"
    session = _ctx["user_sessions"].get(ukey, "none") if _ctx else "none"
    await update.message.reply_text(
        f"Hello from a plugin! Your session: <code>{session[:8] if session != 'none' else 'none'}</code>",
        parse_mode=_ctx["ParseMode"].HTML if _ctx else None,
    )
