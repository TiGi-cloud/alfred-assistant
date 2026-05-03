"""AI-related command handlers — /clear, /clearhistory, /export, /model,
/undo, /fork, /history, /research."""
from __future__ import annotations

import os
import re
import json
import asyncio
import time
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

import anthropic as _anthropic

import bot_state as st
from core import (
    is_allowed, deny, user_key, check_cmd_rate,
    add_history, save_sessions, save_projects,
    build_back_button,
)
from claude_runner import run_claude, send_response
from persistence import load_json, save_json
from utils.formatting import E, fmt_spoiler
from utils.helpers import async_run, cleanup_temp
from config import (
    HISTORY_FILE, FORKS_FILE, MODELS_FILE, CLAUDE_MODEL,
)

logger = logging.getLogger("alfred")


# ---------------------------------------------------------------------------
# /clear
# ---------------------------------------------------------------------------
async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    had_session = ukey in st.user_sessions
    st.user_sessions.pop(ukey, None)
    save_sessions()
    # If a project is active, clear its session_id but keep the project config
    active_proj = st.active_project.get(ukey)
    proj_note = ""
    if active_proj and ukey in st.projects and active_proj in st.projects[ukey]:
        st.projects[ukey][active_proj]["session_id"] = ""
        st.projects[ukey][active_proj]["pending"] = ""
        save_projects()
        proj_note = f"\n• Project <code>{E(active_proj)}</code> session cleared (config kept)"
    await update.message.reply_text(
        "✅ <b>Conversation cleared.</b>\n"
        f"• Active session {'deleted' if had_session else 'was already empty'}\n"
        f"• Next message starts a fresh conversation{proj_note}\n"
        "• Cost stats preserved (<code>/cost</code> to view)",
        parse_mode=ParseMode.HTML,
        reply_markup=build_back_button(),
    )


# ---------------------------------------------------------------------------
# /clearhistory
# ---------------------------------------------------------------------------
async def cmd_clearhistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear conversation history log for the current user."""
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    st.history.pop(ukey, None)
    save_json(HISTORY_FILE, st.history)
    await update.message.reply_text(
        "✅ <b>History log cleared.</b>\n"
        "• Active session preserved (conversation continues)\n"
        "• Past messages removed from <code>/history</code> log\n"
        "• Use <code>/clear</code> to also reset the session",
        parse_mode=ParseMode.HTML,
        reply_markup=build_back_button(),
    )


# ---------------------------------------------------------------------------
# /export
# ---------------------------------------------------------------------------
async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    session_id = st.user_sessions.get(ukey)
    if not session_id:
        await update.message.reply_text("No active conversation to export.")
        return
    export_path = f"/tmp/alfred_export_{session_id[:8]}.md"
    try:
        rc, _, _ = await async_run(["claude", "export", session_id, "-o", export_path])
        if os.path.isfile(export_path) and os.path.getsize(export_path) > 0:
            with open(export_path, 'rb') as f:
                await update.message.reply_document(document=f, filename=f"conversation_{session_id[:8]}.md")
        else:
            await update.message.reply_text(
                f"Export unavailable. Session: {fmt_spoiler(session_id)}",
                parse_mode=ParseMode.HTML,
            )
    finally:
        cleanup_temp(export_path)


# ---------------------------------------------------------------------------
# /model
# ---------------------------------------------------------------------------
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    aliases = {
        "opus": "claude-opus-4-6",
        "sonnet": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5-20251001",
    }
    if context.args:
        model = context.args[0]
        model = aliases.get(model, model)
        st.user_models[ukey] = model
        save_json(MODELS_FILE, st.user_models)
        await update.message.reply_text(f"Model: <code>{E(model)}</code>", parse_mode=ParseMode.HTML)
    else:
        current = st.user_models.get(ukey) or CLAUDE_MODEL or "default"
        _aliases = {"claude-opus-4-6": "opus", "claude-sonnet-4-6": "sonnet", "claude-haiku-4-5-20251001": "haiku"}
        _cur_short = _aliases.get(current, current.split("-")[1] if "-" in current else current)
        def _mlabel(name: str, hint: str) -> str:
            return f"✓ {name}" if _cur_short == name.lower() else name
        buttons = [[
            InlineKeyboardButton(_mlabel("Opus", "opus"),     callback_data="setmodel:opus"),
            InlineKeyboardButton(_mlabel("Sonnet", "sonnet"), callback_data="setmodel:sonnet"),
            InlineKeyboardButton(_mlabel("Haiku", "haiku"),   callback_data="setmodel:haiku"),
        ], [InlineKeyboardButton("\u2190 Back", callback_data="menu:main")]]
        await update.message.reply_text(
            f"Current model: <code>{E(current)}</code>\n"
            f"<i>Opus — most capable  ·  Sonnet — balanced  ·  Haiku — fastest</i>\n\nSelect:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )


# ---------------------------------------------------------------------------
# /undo
# ---------------------------------------------------------------------------
async def cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    if ukey not in st.user_sessions:
        await update.message.reply_text("No active session. Nothing to undo.")
        return

    thinking_msg = await update.message.reply_text(
        "\u280b <b>Undoing last action...</b>", parse_mode=ParseMode.HTML,
    )
    st.user_request_count[ukey] = st.user_request_count.get(ukey, 0) + 1
    try:
        response = await run_claude(
            "Undo the last action you performed. Revert any file changes, "
            "kill any processes you started, and restore previous state. Tell me what you undid.",
            ukey, thinking_msg=thinking_msg, context=context, chat_id=update.effective_chat.id,
        )
    except Exception as e:
        response = f"Error: {e}"
    finally:
        st.user_request_count[ukey] = max(0, st.user_request_count.get(ukey, 1) - 1)
    try:
        await thinking_msg.delete()
    except Exception:
        pass
    add_history(ukey, "user", "/undo")
    add_history(ukey, "alfred", response[:200])
    await send_response(update, response)


# ---------------------------------------------------------------------------
# /fork
# ---------------------------------------------------------------------------
async def cmd_fork(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)

    if not context.args:
        user_forks = st.forks.get(ukey, {})
        if user_forks:
            current = st.user_sessions.get(ukey, "none")
            items = "\n".join(f"  <code>{E(n)}</code> \u2192 {fmt_spoiler(s[:12])}" for n, s in user_forks.items())
            msg = (
                f"<b>Current:</b> {fmt_spoiler(current[:12]) if current != 'none' else 'none'}\n\n"
                f"<b>Branches:</b>\n{items}\n\n"
                "<code>/fork save|load|delete &lt;name&gt;</code>"
            )
        else:
            msg = (
                "<b>Conversation branching</b>\n\n"
                "<code>/fork save &lt;name&gt;</code> -- save branch\n"
                "<code>/fork load &lt;name&gt;</code> -- switch\n"
                "<code>/fork delete &lt;name&gt;</code>"
            )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=build_back_button())
        return

    action = context.args[0].lower()
    name = context.args[1] if len(context.args) > 1 else ""

    if action == "save":
        if not name:
            return await update.message.reply_text("Usage: <code>/fork save name</code>", parse_mode=ParseMode.HTML)
        session_id = st.user_sessions.get(ukey)
        if not session_id:
            return await update.message.reply_text("No active session to save.")
        if ukey not in st.forks:
            st.forks[ukey] = {}
        st.forks[ukey][name] = session_id
        save_json(FORKS_FILE, st.forks)
        total_branches = len(st.forks[ukey])
        await update.message.reply_text(
            f"✅ <b>Branch saved:</b> <code>{E(name)}</code>\n"
            f"• Conversation snapshot preserved\n"
            f"• Switch back anytime: <code>/fork load {E(name)}</code>\n"
            f"• Total branches: {total_branches} (view with <code>/fork</code>)",
            parse_mode=ParseMode.HTML,
        )
    elif action == "load":
        if not name or name not in st.forks.get(ukey, {}):
            return await update.message.reply_text(f"No branch: <code>{E(name)}</code>", parse_mode=ParseMode.HTML)
        st.user_sessions[ukey] = st.forks[ukey][name]
        save_sessions()
        other_branches = [b for b in st.forks.get(ukey, {}) if b != name]
        other_str = f"\n• Other saved branches: {', '.join(f'<code>{E(b)}</code>' for b in other_branches[:5])}" if other_branches else ""
        await update.message.reply_text(
            f"✅ <b>Switched to branch:</b> <code>{E(name)}</code>\n"
            f"• Previous state preserved (not lost){other_str}\n"
            f"• Type <code>/fork</code> to see all branches",
            parse_mode=ParseMode.HTML,
        )
    elif action == "delete":
        if name not in st.forks.get(ukey, {}):
            return await update.message.reply_text(f"No branch: <code>{E(name)}</code>", parse_mode=ParseMode.HTML)
        safe_name = name[:46]  # fork_del_confirm: = 17 chars; 17+46=63 < 64 byte limit
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"fork_del_confirm:{safe_name}"),
            InlineKeyboardButton("❌ Cancel", callback_data="fork_del_cancel"),
        ]])
        branch_size = len(st.user_sessions.get(ukey, ""))
        await update.message.reply_text(
            f"Delete branch <code>{E(name)}</code>?\n"
            f"<i>This removes the saved conversation context ({branch_size} chars). Cannot be undone.</i>",
            parse_mode=ParseMode.HTML, reply_markup=kb
        )
    else:
        session_id = st.user_sessions.get(ukey)
        if session_id:
            if ukey not in st.forks:
                st.forks[ukey] = {}
            st.forks[ukey][action] = session_id
            save_json(FORKS_FILE, st.forks)
            await update.message.reply_text(f"Saved branch: <code>{E(action)}</code>", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /history
# ---------------------------------------------------------------------------
async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)
    user_history = st.history.get(ukey, [])
    n = 20
    if context.args:
        try:
            n = min(int(context.args[0]), 50)
        except ValueError:
            pass
    if not user_history:
        return await update.message.reply_text("No history yet.", reply_markup=build_back_button())

    entries = user_history[-n:]
    lines = []
    for e in entries:
        role = "You" if e["role"] == "user" else "Alfred"
        lines.append(f"<code>{e['time']}</code> <b>{role}:</b> {E(e['text'][:80])}")
    msg = "<b>Recent history:</b>\n" + "\n".join(lines)

    if len(msg) > 3500:
        msg = f"<blockquote expandable>{msg}</blockquote>"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=build_back_button())


# ---------------------------------------------------------------------------
# /research
# ---------------------------------------------------------------------------
async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run 15 parallel Claude agents on a topic and synthesize a report."""
    if not is_allowed(update):
        return await deny(update)
    if not await check_cmd_rate(update, "research"):
        return

    topic = " ".join(context.args).strip() if context.args else ""
    if not topic:
        await update.message.reply_text(
            "<b>/research</b> <i>&lt;topic&gt;</i>\n\n"
            "Runs 15 parallel AI agents to research a topic, then synthesizes a comprehensive report.\n\n"
            "Example:\n<code>/research The economic impact of large language models</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        await update.message.reply_text("❌ ANTHROPIC_API_KEY not set in environment.")
        return

    status_msg = await update.message.reply_text(
        f"🔬 <b>Deep Research</b>\n<code>{E(topic)}</code>\n\n🔍 Decomposing into 15 questions...",
        parse_mode=ParseMode.HTML,
    )

    aclient = _anthropic.AsyncAnthropic(api_key=api_key)

    # Determine models based on user's /model setting
    ukey = user_key(update)
    user_model = st.user_models.get(ukey, "").lower()
    if "opus" in user_model:
        agent_model, synth_model = "claude-sonnet-4-6", "claude-opus-4-6"
    elif "haiku" in user_model:
        agent_model, synth_model = "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"
    else:
        agent_model, synth_model = "claude-haiku-4-5-20251001", "claude-sonnet-4-6"

    # -- Decompose --------------------------------------------------------
    try:
        decomp = await aclient.messages.create(
            model=agent_model,
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": (
                    f"Decompose this research topic into exactly 15 distinct, non-overlapping "
                    f"sub-questions for parallel research. Return ONLY a JSON array of 15 strings.\n\n"
                    f"Topic: {topic}"
                ),
            }],
        )
        raw = decomp.content[0].text.strip()
        m = re.search(r'\[.*\]', raw, re.DOTALL)
        questions: list[str] = json.loads(m.group()) if m else []
        questions = [str(q) for q in questions[:15]]
        while len(questions) < 15:
            questions.append(f"Additional aspect {len(questions) + 1}: {topic}")
    except Exception as exc:
        await status_msg.edit_text(f"❌ Decomposition failed: {E(str(exc))}", parse_mode=ParseMode.HTML)
        return

    # -- Parallel agents --------------------------------------------------
    results: list[str] = [""] * 15
    done:    list[bool] = [False] * 15
    failed:  list[bool] = [False] * 15
    _last_edit: list[float] = [time.time()]

    def _status_text(note: str = "") -> str:
        n_done = sum(done)
        filled = int((n_done / 15) * 12)
        bar = "█" * filled + "░" * (12 - filled)
        # Show 3 rows of 5 dots, each labelled 1-15
        rows = []
        for row in range(3):
            row_dots = []
            for col in range(5):
                i = row * 5 + col
                if done[i] and not failed[i]:
                    row_dots.append("✓")
                elif failed[i]:
                    row_dots.append("✗")
                else:
                    row_dots.append(str(i + 1))
            rows.append(" ".join(row_dots))
        grid = "\n".join(rows)
        line = f"{bar} {n_done}/15"
        if note:
            line += f"  {note}"
        return (
            f"🔬 <b>Deep Research</b>\n<code>{E(topic)}</code>\n\n"
            f"{line}\n<code>{grid}</code>"
        )

    async def _push_status(note: str = "", force: bool = False):
        now = time.time()
        if not force and now - _last_edit[0] < 1.5:
            return
        _last_edit[0] = now
        try:
            await status_msg.edit_text(_status_text(note), parse_mode=ParseMode.HTML)
        except Exception:
            pass

    await _push_status(force=True)

    async def _run_agent(i: int, question: str):
        try:
            resp = await aclient.messages.create(
                model=agent_model,
                max_tokens=900,
                messages=[{
                    "role": "user",
                    "content": (
                        f'You are a research agent. Topic of the study: "{topic}"\n\n'
                        f"Your sub-question: {question}\n\n"
                        f"Write a focused 2-4 paragraph response with specific facts and analysis."
                    ),
                }],
            )
            results[i] = resp.content[0].text.strip()
            done[i] = True
        except Exception as exc:
            results[i] = f"[Error: {exc}]"
            done[i] = True
            failed[i] = True
        await _push_status()

    await asyncio.gather(*[_run_agent(i, questions[i]) for i in range(15)])
    await _push_status(force=True)

    # -- Synthesis --------------------------------------------------------
    await status_msg.edit_text(
        f"🔬 <b>Deep Research</b>\n<code>{E(topic)}</code>\n\n✅ 15/15 — synthesizing report...",
        parse_mode=ParseMode.HTML,
    )

    good = [(questions[i], results[i]) for i in range(15) if not failed[i] and results[i]]
    sections = "\n\n".join(f"Q{i+1}: {q}\n{r}" for i, (q, r) in enumerate(good))

    try:
        synth = await aclient.messages.create(
            model=synth_model,
            max_tokens=3500,
            messages=[{
                "role": "user",
                "content": (
                    f'Write a comprehensive research report on "{topic}" synthesizing these '
                    f'{len(good)} research findings. Structure: Executive Summary, Key Findings '
                    f'(thematic), Analysis & Implications, Conclusions.\n\n{sections}'
                ),
            }],
        )
        report = synth.content[0].text.strip()
    except Exception as exc:
        report = f"Synthesis failed: {exc}\n\n--- Raw Findings ---\n\n{sections}"

    n_good = len(good)
    n_fail = 15 - n_good
    fail_note = f"  ({n_fail} failed)" if n_fail else ""
    await status_msg.edit_text(
        f"🔬 <b>Deep Research: {E(topic)}</b>\n✅ Complete — {n_good}/15 agents succeeded{fail_note}.",
        parse_mode=ParseMode.HTML,
    )

    # Send report in numbered chunks
    chunk_size = 3800
    chunks = [report[i:i + chunk_size] for i in range(0, len(report), chunk_size)]
    for idx, chunk in enumerate(chunks):
        header = f"<b>📋 Research Report</b>" if idx == 0 else f"<b>📋 Research Report (cont. {idx + 1}/{len(chunks)})</b>"
        try:
            await update.message.reply_text(f"{header}\n\n{chunk}", parse_mode=ParseMode.HTML)
        except Exception:
            await update.message.reply_text(f"{chunk}")
