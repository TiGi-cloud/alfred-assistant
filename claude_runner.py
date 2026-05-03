"""Alfred Claude AI runner — streaming execution, cost tracking, suggestions, response sending."""
from __future__ import annotations

import os
import re
import json
import time
import shutil
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

import bot_state as st
from config import SYSTEM_PROMPT, CLAUDE_MODEL, COST_FILE, BOT_DIR
from persistence import save_json
from core import save_sessions, load_machines, save_projects
from utils.formatting import E, md_to_html, fmt_expandable, fmt_elapsed, progress_bar, _safe_html_chunks

logger = logging.getLogger("alfred")


async def send_typing(context, chat_id):
    """Send typing indicator."""
    try:
        await context.bot.send_chat_action(chat_id, "typing")
    except Exception:
        pass


async def _send_with_retry(coro_fn, max_retries=3):
    """Call coro_fn(), retrying on FloodWait/RetryAfter up to max_retries times."""
    for attempt in range(max_retries):
        try:
            return await coro_fn()
        except Exception as e:
            err = str(e).lower()
            # Handle Telegram rate limiting (RetryAfter / flood wait)
            if "retry after" in err or "flood" in err:
                import re as _re
                m = _re.search(r'(\d+)', str(e))
                wait = int(m.group(1)) if m else 5
                wait = min(wait, 30)
                logger.warning("FloodWait: sleeping %ss (attempt %d)", wait, attempt + 1)
                await asyncio.sleep(wait)
                continue
            raise
    return None


def _resolve_claude_bin():
    """Find the `claude` CLI binary. Override with CLAUDE_BIN env var."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit and os.path.exists(explicit):
        return explicit
    # Try PATH first
    found = shutil.which("claude")
    if found:
        return found
    # Common install locations
    for candidate in (
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.claude/local/claude"),
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    ):
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError(
        "Could not find `claude` CLI. Install it from https://claude.com/claude-code "
        "or set CLAUDE_BIN env var to its absolute path."
    )


def _build_claude_cmd(ukey, prompt, output_format="stream-json"):
    cmd = [
        _resolve_claude_bin(), "-p", "--dangerously-skip-permissions",
        "--system-prompt", SYSTEM_PROMPT, "--output-format", output_format,
        "--verbose",
    ]
    # Per-project model override
    proj_name = st.active_project.get(ukey)
    proj_data = st.projects.get(ukey, {}).get(proj_name, {}) if proj_name else {}
    model = proj_data.get("model") or st.user_models.get(ukey) or CLAUDE_MODEL
    if model:
        cmd.extend(["--model", model])
    if ukey in st.user_sessions:
        cmd.extend(["--resume", st.user_sessions[ukey]])

    machine = st.user_machines.get(ukey)
    if machine and machine != "local":
        machines = load_machines()
        if machine in machines:
            info = machines[machine]
            host = info if isinstance(info, str) else info.get("host", "?")
            prompt = (
                f"[REMOTE MACHINE: {machine} ({host})] "
                f"Run on remote via SSH: ssh {host} '<command>'. Request: {prompt}"
            )

    # Inject persistent memory
    from utils.memory import format_memories_for_prompt
    mem_str = format_memories_for_prompt(ukey)
    if mem_str:
        prompt = f"[USER MEMORY: {mem_str}]\n\n{prompt}"

    # Inject system context snapshot periodically
    count = st._msg_count_since_snapshot.get(ukey, 0) + 1
    if count >= st.SNAPSHOT_INTERVAL:
        snapshot = _get_system_snapshot()
        if snapshot:
            prompt = f"[SYSTEM CONTEXT: {snapshot}]\n\n{prompt}"
        st._msg_count_since_snapshot[ukey] = 0
    else:
        st._msg_count_since_snapshot[ukey] = count

    # Inject global env keys (not values) so Claude knows what's available
    if st.global_env:
        genv_keys = ", ".join(st.global_env.keys())
        prompt = f"[GLOBAL ENV AVAILABLE: {genv_keys}]\n\n{prompt}"

    # Inject project context if active
    if proj_name and proj_data:
        parts = [f"PROJECT: {proj_name}"]
        if proj_data.get("cwd"):
            parts.append(f"CWD: {proj_data['cwd']}")
        git = proj_data.get("git", {})
        if git.get("org"):
            parts.append(f"GIT: {git['org']}/{git.get('repo', proj_name)}")
        deploy = proj_data.get("deploy", {})
        if deploy.get("target"):
            deploy_str = deploy["target"]
            if deploy.get("deploy_cmd"):
                deploy_str += f" ({deploy['deploy_cmd']})"
            parts.append(f"DEPLOY: {deploy_str}")
        prompt = f"[{' | '.join(parts)}]\n\n{prompt}"

    return cmd, prompt


def _get_system_snapshot() -> str:
    """Quick synchronous system snapshot for context injection."""
    import subprocess
    try:
        result = subprocess.run(
            ["bash", "-c",
             'echo "$(date +%H:%M) | '
             'CPU: $(top -l 1 -n 0 2>/dev/null | grep "CPU usage" | awk \'{print $3}\') | '
             'Disk: $(df -h / | tail -1 | awk \'{print $5 " used, " $4 " free"}\') | '
             'Mem: $(vm_stat | awk \'/Pages free/{f=$3}/Pages active/{a=$3}END{printf \"%.0fMB free\", f*4096/1048576}\')"'
             ],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _track_cost(ukey, usage):
    if not usage:
        return
    if ukey not in st.cost_tracker:
        st.cost_tracker[ukey] = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    st.cost_tracker[ukey]["input_tokens"] += inp
    st.cost_tracker[ukey]["output_tokens"] += out
    st.cost_tracker[ukey]["requests"] += 1
    save_json(COST_FILE, st.cost_tracker)

    # Track time-windowed usage
    now = time.time()
    entry = {"ts": now, "in": inp, "out": out}
    st.usage_hourly.setdefault(ukey, []).append(entry)
    st.usage_weekly.setdefault(ukey, []).append(entry)
    # Prune old entries
    hour_ago = now - 3600
    week_ago = now - 7 * 86400
    st.usage_hourly[ukey] = [e for e in st.usage_hourly[ukey] if e["ts"] > hour_ago]
    st.usage_weekly[ukey] = [e for e in st.usage_weekly[ukey] if e["ts"] > week_ago]


async def run_claude(prompt, ukey, thinking_msg=None, context=None, chat_id=None):
    # Send typing indicator immediately
    if chat_id and context:
        await send_typing(context, chat_id)

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    # Global env vars first, then per-project overrides (only strings allowed in env)
    if st.global_env:
        for k, v in st.global_env.items():
            env[k] = v if isinstance(v, str) else json.dumps(v)
    proj_name = st.active_project.get(ukey)
    proj_data = st.projects.get(ukey, {}).get(proj_name, {}) if proj_name else {}
    if proj_data.get("env"):
        for k, v in proj_data["env"].items():
            env[k] = v if isinstance(v, str) else json.dumps(v)
    cwd = proj_data.get("cwd") or str(BOT_DIR)
    if not os.path.isdir(cwd):
        cwd = str(BOT_DIR)

    cmd, prompt_text = _build_claude_cmd(ukey, prompt, "stream-json")

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        cwd=cwd,
        # Raise StreamReader line buffer from default 64 KB to 64 MB.
        # Claude's stream-json `system` init line enumerates every tool
        # (incl. all MCP tools) and easily exceeds 64 KB, causing
        # readline() to raise "Separator is found, but chunk is longer than limit".
        limit=64 * 1024 * 1024,
    )
    # Feed prompt via stdin to avoid OS arg-length / CLI chunking limits
    proc.stdin.write(prompt_text.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    st.user_processes.setdefault(ukey, []).append(proc)

    # Drain stderr in background to prevent pipe buffer deadlock
    stderr_chunks: list = []
    async def _drain_stderr():
        try:
            data = await proc.stderr.read()
            if data:
                stderr_chunks.append(data.decode(errors='replace'))
        except Exception:
            pass
    stderr_drain_task = asyncio.create_task(_drain_stderr())

    accumulated = ""
    session_id = ""
    usage = {}
    result_errors = []
    last_edit = time.time()
    start_time = time.time()
    dots = ["\u280b", "\u2819", "\u2839", "\u2838", "\u283c", "\u2834", "\u2826", "\u2827", "\u2807", "\u280f"]
    dot_idx = 0
    stream_worked = False

    # Cancel button on progress messages
    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2716 Cancel", callback_data=f"cancel:{ukey}")]
    ])

    try:
        while True:
            raw_line = await proc.stdout.readline()

            if not raw_line:
                break

            line = raw_line.decode(errors='replace').strip()
            if not line:
                continue

            try:
                chunk = json.loads(line)
                stream_worked = True
                msg_type = chunk.get("type", "")
                logger.debug("Claude chunk: type=%s subtype=%s is_error=%s keys=%s", msg_type, chunk.get("subtype"), chunk.get("is_error"), list(chunk.keys()))

                if msg_type == "result":
                    session_id = chunk.get("session_id", session_id)
                    usage = chunk.get("usage", usage)
                    result = chunk.get("result", "")
                    result_errors = chunk.get("errors", [])
                    if result:
                        accumulated = result
                elif msg_type == "assistant":
                    msg = chunk.get("message", {})
                    content = msg.get("content", [])
                    for block in (content if isinstance(content, list) else []):
                        if isinstance(block, dict) and block.get("type") == "text":
                            accumulated = block.get("text", accumulated)
                elif msg_type == "content_block_delta":
                    delta = chunk.get("delta", {})
                    if delta.get("type") == "text_delta":
                        accumulated += delta.get("text", "")
                        if len(accumulated) > 200_000:
                            accumulated = accumulated[-200_000:]  # cap to prevent OOM
                elif msg_type == "system":
                    session_id = chunk.get("session_id", session_id)

            except json.JSONDecodeError:
                accumulated += line + "\n"

            # Update thinking message with streamed content
            now = time.time()
            if thinking_msg and now - last_edit > 3:
                elapsed = int(now - start_time)
                elapsed_str = fmt_elapsed(elapsed)
                if accumulated:
                    preview = accumulated[-3000:] if len(accumulated) > 3000 else accumulated
                    display = f"{dots[dot_idx % len(dots)]} <b>Streaming...</b> ({elapsed_str})\n\n{E(preview)}"
                    if len(display) > 4096:
                        display = display[-4096:]
                else:
                    bar = progress_bar((elapsed % 30) / 30 * 100, 8)
                    display = f"{dots[dot_idx % len(dots)]} <b>Working...</b> ({elapsed_str})\n{bar}"

                try:
                    await thinking_msg.edit_text(display, parse_mode=ParseMode.HTML, reply_markup=cancel_kb)
                except Exception:
                    try:
                        await thinking_msg.edit_text(f"Working... ({elapsed_str})", reply_markup=cancel_kb)
                    except Exception:
                        pass

                if chat_id and context:
                    await send_typing(context, chat_id)

                last_edit = now
                dot_idx += 1

        await proc.wait()

    except Exception as e:
        try:
            proc.kill()
        except Exception:
            pass
        return f"Error running Claude: {e}"
    finally:
        # Remove this specific process from the list
        procs = st.user_processes.get(ukey, [])
        if proc in procs:
            procs.remove(proc)
        if not procs:
            st.user_processes.pop(ukey, None)
        stderr_drain_task.cancel()
        try:
            await stderr_drain_task
        except asyncio.CancelledError:
            pass

    # Fallback to json mode if stream-json didn't work
    logger.info("Claude stream: worked=%s accumulated_len=%s returncode=%s stderr=%s", stream_worked, len(accumulated), proc.returncode, "".join(stderr_chunks)[:500])
    if not stream_worked and not accumulated:
        return await _run_claude_json(prompt, ukey, thinking_msg, context, chat_id)

    if proc.returncode != 0 and not accumulated:
        all_errors = "".join(stderr_chunks) + " ".join(result_errors)
        # If session is stale/missing, clear it and retry once
        if ukey in st.user_sessions and "No conversation found" in all_errors:
            logger.warning("Stale session for %s, clearing and retrying", ukey)
            st.user_sessions.pop(ukey, None)
            save_sessions()
            # Also clear stale session from active project
            _pn = st.active_project.get(ukey)
            if _pn and ukey in st.projects and _pn in st.projects[ukey]:
                st.projects[ukey][_pn]["session_id"] = ""
                save_projects()
            return await run_claude(prompt, ukey, thinking_msg, context, chat_id)
        stderr_data = "".join(stderr_chunks)
        error_detail = "; ".join(result_errors) if result_errors else stderr_data
        return f"Claude error (exit {proc.returncode}):\n{error_detail}"

    if session_id:
        st.user_sessions[ukey] = session_id
        save_sessions()
        # Sync session back to active project
        _pn = st.active_project.get(ukey)
        if _pn and ukey in st.projects and _pn in st.projects[ukey]:
            st.projects[ukey][_pn]["session_id"] = session_id
            st.projects[ukey][_pn]["last_used"] = time.time()
            st.projects[ukey][_pn]["msg_count_today"] = st.projects[ukey][_pn].get("msg_count_today", 0) + 1
            save_projects()

    _track_cost(ukey, usage)
    return accumulated


async def _run_claude_json(prompt, ukey, thinking_msg=None, context=None, chat_id=None):
    """Fallback: use --output-format json (non-streaming)."""
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)

    # Global env vars first, then per-project overrides (only strings allowed in env)
    if st.global_env:
        for k, v in st.global_env.items():
            env[k] = v if isinstance(v, str) else json.dumps(v)
    proj_name = st.active_project.get(ukey)
    proj_data = st.projects.get(ukey, {}).get(proj_name, {}) if proj_name else {}
    if proj_data.get("env"):
        for k, v in proj_data["env"].items():
            env[k] = v if isinstance(v, str) else json.dumps(v)
    cwd = proj_data.get("cwd") or str(BOT_DIR)
    if not os.path.isdir(cwd):
        cwd = str(BOT_DIR)

    cmd, prompt_text = _build_claude_cmd(ukey, prompt, "json")

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        cwd=cwd,
        # Match stream-json path: default 64 KB StreamReader limit is too
        # small for Claude's init line once MCP tool schemas are loaded.
        limit=64 * 1024 * 1024,
    )
    proc.stdin.write(prompt_text.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    st.user_processes.setdefault(ukey, []).append(proc)

    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2716 Cancel", callback_data=f"cancel:{ukey}")]
    ])

    async def update_progress():
        start = time.time()
        while True:
            await asyncio.sleep(5)
            elapsed = int(time.time() - start)
            elapsed_str = fmt_elapsed(elapsed)
            bar = progress_bar((elapsed % 30) / 30 * 100, 8)
            try:
                if thinking_msg:
                    await thinking_msg.edit_text(
                        f"\u280b <b>Working...</b> ({elapsed_str})\n{bar}",
                        parse_mode=ParseMode.HTML,
                        reply_markup=cancel_kb,
                    )
                if chat_id and context:
                    await send_typing(context, chat_id)
            except Exception:
                pass

    progress_task = asyncio.create_task(update_progress())
    try:
        stdout, stderr = await proc.communicate()
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except asyncio.CancelledError:
            pass
        procs = st.user_processes.get(ukey, [])
        if proc in procs:
            procs.remove(proc)
        if not procs:
            st.user_processes.pop(ukey, None)

    raw = stdout.decode().strip()
    stderr_text = stderr.decode().strip()
    logger.error("Claude JSON fallback: exit=%s stdout=%s stderr=%s cmd=%s", proc.returncode, raw[:500], stderr_text[:500], cmd)
    if proc.returncode != 0:
        return f"Claude error (exit {proc.returncode}):\n{stderr_text}"

    try:
        data = json.loads(raw)
        sid = data.get("session_id", "")
        if sid:
            st.user_sessions[ukey] = sid
            save_sessions()
            # Sync session back to active project
            _pn = st.active_project.get(ukey)
            if _pn and ukey in st.projects and _pn in st.projects[ukey]:
                st.projects[ukey][_pn]["session_id"] = sid
                st.projects[ukey][_pn]["last_used"] = time.time()
                st.projects[ukey][_pn]["msg_count_today"] = st.projects[ukey][_pn].get("msg_count_today", 0) + 1
                save_projects()
        _track_cost(ukey, data.get("usage", {}))
        return data.get("result", raw)
    except (json.JSONDecodeError, KeyError):
        return raw


def _generate_suggestions(response: str) -> list[str]:
    """Generate 2-3 contextual follow-up suggestions based on response content."""
    suggestions = []
    resp_lower = response.lower()

    # Process-related
    if any(w in resp_lower for w in ("process", "pid", "running", "cpu usage", "memory usage")):
        suggestions.append("Show top processes by memory")
        if "pid" in resp_lower:
            suggestions.append("Kill that process")

    # File-related
    if any(w in resp_lower for w in ("file", "created", "saved to", "written")):
        suggestions.append("Show me the file")
    if "directory" in resp_lower or "folder" in resp_lower:
        suggestions.append("List the contents")

    # Error/fix related
    if any(w in resp_lower for w in ("error", "failed", "issue", "problem")):
        suggestions.append("How do I fix this?")
        suggestions.append("Show the logs")

    # Docker/container related
    if any(w in resp_lower for w in ("container", "docker", "image")):
        suggestions.append("Show container logs")
        suggestions.append("Restart the container")

    # Disk/space related
    if any(w in resp_lower for w in ("disk", "storage", "space", "gb free")):
        suggestions.append("What's taking the most space?")

    # Network related
    if any(w in resp_lower for w in ("ip address", "network", "connection", "wifi")):
        suggestions.append("Run a speed test")

    # Screenshot related
    if "screenshot" in resp_lower:
        suggestions.append("Take another screenshot")

    # Git related
    if any(w in resp_lower for w in ("commit", "branch", "git", "merge")):
        suggestions.append("Show git status")
        suggestions.append("Show recent commits")

    # Generic useful follow-ups
    if not suggestions:
        suggestions.append("Tell me more")
        suggestions.append("Take a screenshot")

    return suggestions[:3]


def _build_suggestion_buttons(response: str) -> InlineKeyboardMarkup | None:
    """Build inline keyboard with suggestion buttons."""
    suggestions = _generate_suggestions(response)
    if not suggestions:
        return None
    rows = []
    for s in suggestions:
        cb_data = f"suggest:{s[:60]}"
        rows.append([InlineKeyboardButton(f"💡 {s}", callback_data=cb_data)])
    rows.append([InlineKeyboardButton("← Back", callback_data="menu:main")])
    return InlineKeyboardMarkup(rows)


async def send_response(update_or_msg, response: str, bot=None, chat_id=None):
    file_pattern = r'\[SEND_FILE:(.*?)\]'
    files_to_send = re.findall(file_pattern, response)
    browse_pattern = r'\[BROWSE:(.*?)\]'
    urls_to_browse = re.findall(browse_pattern, response)
    # Extract and save memories: [REMEMBER:category:text]
    remember_pattern = r'\[REMEMBER:(\w+):(.*?)\]'
    memories_to_save = re.findall(remember_pattern, response)
    clean_response = re.sub(file_pattern, '', response)
    clean_response = re.sub(browse_pattern, '', clean_response)
    clean_response = re.sub(remember_pattern, '', clean_response).strip()

    # Save memories if any
    if memories_to_save:
        _mem_ukey = "default:0"
        if hasattr(update_or_msg, 'effective_user') and hasattr(update_or_msg, 'effective_chat'):
            _mem_ukey = f"{update_or_msg.effective_user.id}:{update_or_msg.effective_chat.id}"
        elif hasattr(update_or_msg, 'chat') and hasattr(update_or_msg, 'from_user'):
            _mem_ukey = f"{update_or_msg.from_user.id}:{update_or_msg.chat.id}"
        from utils.memory import add_memory
        for cat, text in memories_to_save:
            add_memory(_mem_ukey, text.strip(), cat.strip())

    if hasattr(update_or_msg, 'message') and update_or_msg.message:
        _msg = update_or_msg.message
        reply = lambda text, **kw: _send_with_retry(lambda: _msg.reply_text(text, **kw))
        reply_photo = lambda **kw: _send_with_retry(lambda: _msg.reply_photo(**kw))
        reply_video = lambda **kw: _send_with_retry(lambda: _msg.reply_video(**kw))
        reply_audio = lambda **kw: _send_with_retry(lambda: _msg.reply_audio(**kw))
        reply_doc = lambda **kw: _send_with_retry(lambda: _msg.reply_document(**kw))
    elif bot and chat_id:
        reply = lambda text, **kw: _send_with_retry(lambda: bot.send_message(chat_id, text, **kw))
        reply_photo = lambda **kw: _send_with_retry(lambda: bot.send_photo(chat_id, **kw))
        reply_video = lambda **kw: _send_with_retry(lambda: bot.send_video(chat_id, **kw))
        reply_audio = lambda **kw: _send_with_retry(lambda: bot.send_audio(chat_id, **kw))
        reply_doc = lambda **kw: _send_with_retry(lambda: bot.send_document(chat_id, **kw))
    else:
        return

    if clean_response:
        formatted = md_to_html(clean_response)
        formatted = fmt_expandable(formatted, threshold=3000)

        for mode in (ParseMode.HTML, None):
            try:
                text_to_send = formatted if mode == ParseMode.HTML else clean_response
                if len(text_to_send) <= 4096:
                    await reply(text_to_send, parse_mode=mode)
                else:
                    chunks = _safe_html_chunks(text_to_send, 3900) if mode == ParseMode.HTML else [text_to_send[i:i+3900] for i in range(0, len(text_to_send), 3900)]
                    total = len(chunks)
                    for i, chunk in enumerate(chunks):
                        if i > 0:
                            pfx = f"<i>({i+1}/{total})</i>\n" if mode == ParseMode.HTML else f"({i+1}/{total})\n"
                            chunk = pfx + chunk
                        if len(chunk) > 4096:
                            chunk = chunk[:4096]
                        await reply(chunk, parse_mode=mode)
                break
            except Exception:
                if mode is None:
                    try:
                        await reply(clean_response[:4096])
                    except Exception:
                        pass
                continue

    for fpath in files_to_send:
        fpath = fpath.strip()
        if not os.path.isfile(fpath):
            await reply(f"File not found: {fpath}")
            continue
        ext = os.path.splitext(fpath)[1].lower()
        try:
            with open(fpath, 'rb') as f:
                if ext in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
                    await reply_photo(photo=f)
                elif ext in ('.mp4', '.mov', '.avi'):
                    await reply_video(video=f)
                elif ext in ('.mp3', '.ogg', '.wav', '.m4a'):
                    await reply_audio(audio=f)
                else:
                    await reply_doc(document=f)
        except Exception as e:
            await reply(f"Failed to send {fpath}: {e}")

    # Browser automation: navigate to URLs and send screenshots
    for url in urls_to_browse:
        url = url.strip()
        try:
            from utils.browser import get_session, HAS_PLAYWRIGHT
            if not HAS_PLAYWRIGHT:
                await reply("Browser not available (playwright not installed).")
                continue
            # Derive ukey from update if possible
            _ukey = "default:0"
            if hasattr(update_or_msg, 'effective_user') and hasattr(update_or_msg, 'effective_chat'):
                _ukey = f"{update_or_msg.effective_user.id}:{update_or_msg.effective_chat.id}"
            elif hasattr(update_or_msg, 'chat') and hasattr(update_or_msg, 'from_user'):
                _ukey = f"{update_or_msg.from_user.id}:{update_or_msg.chat.id}"
            session = await get_session(_ukey)
            result_url = await session.navigate(url)
            if result_url.startswith("Navigation error"):
                await reply(f"🌐 Failed to open {url}: {result_url}")
                continue
            screenshot_path = await session.screenshot()
            if screenshot_path and os.path.isfile(screenshot_path):
                from commands.web import _browser_keyboard
                title = await session.page.title() if session.page else "?"
                from utils.formatting import E as _E
                with open(screenshot_path, 'rb') as f:
                    await reply_photo(
                        photo=f,
                        caption=f"🌐 {_E(title[:60])}\n{_E(result_url[:80])}",
                        parse_mode=ParseMode.HTML,
                        reply_markup=_browser_keyboard(),
                    )
        except Exception as e:
            logger.error("Browser [BROWSE:%s] error: %s", url, e)
            await reply(f"🌐 Failed to open {url}: {e}")


# ---------------------------------------------------------------------------
