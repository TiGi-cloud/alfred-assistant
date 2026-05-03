"""
Platform-agnostic Claude pipeline.

`ClaudeRunner.run(ctx, prompt, attachments=None)` drives the `claude -p` CLI,
streams its output through stream-json, and pumps the result back into the
originating chat via `kernel.runner.Context` — so the same call works for
Telegram, Web, Discord, Slack, and iMessage adapters.

Design notes:

  * Sessions are stored per (adapter_name, chat_id) so each chat has its own
    Claude conversation thread.
  * The output appears in-place: a "thinking" message is sent first, then
    edited as text streams in. Adapters that don't support edits (iMessage)
    will receive a follow-up message instead — the kernel doesn't try to be
    clever about it; the adapter just no-ops or sends a new message.
  * `[SEND_FILE:/path]` markers in the response are stripped from the text
    and the files are sent via `ctx.adapter.send_photo` / `send_document`.
  * Edits are throttled (default 1.5s) to avoid platform rate limits.
  * Bouncing the bot doesn't lose conversations — sessions persist to a JSON
    file (default: `claude_sessions.json` next to the package).
  * No project / env / memory features yet — those need their own ports.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from .messages import Attachment
from .runner import Context

logger = logging.getLogger("alfred.kernel.claude")


DEFAULT_SYSTEM_PROMPT = (
    "You are Alfred, named for Alfred Pennyworth — Batman's butler. "
    "Voice: a competent butler. Brief, polite, dry. Do the work; don't "
    "narrate it. No 'let me think about that', no preamble, no apologising. "
    "If the task is small, the answer is small.\n\n"

    "Capabilities: you have FULL shell access on this Mac via the Bash "
    "tool — any shell command, AppleScript, Vision OCR, file ops, ssh, "
    "the works. You are NOT sandboxed. Don't refuse on sandbox grounds.\n\n"

    "RESPONSE MARKERS (the bot intercepts these — emit them inline; users "
    "won't see the marker text):\n"
    "  [SEND_FILE:/abs/path]      send the file as a chat attachment\n"
    "  [BROWSE:https://url]       open URL headlessly, screenshot to chat\n"
    "  [REMEMBER:category:fact]   store for long-term memory; categories: "
    "preference, fact, routine, context, task\n\n"

    "Common patterns:\n"
    "  screenshot: `screencapture -x /tmp/shot.png` + [SEND_FILE:/tmp/shot.png]\n"
    "  open app:   `open -a 'Safari'`     |  open URL: `open https://…`\n"
    "  music:      `osascript -e 'tell app \"Music\" to play|pause|next track'`\n"
    "  notify:     `osascript -e 'display notification \"msg\" with title \"Alfred\"'`\n"
    "  search:     `mdfind -name 'foo'`   |  spotlight content: `mdfind 'foo'`\n"
    "  ocr image:  via macOS Vision (the bot already does this on photos)\n"
    "  remote run: `ssh hostname 'command'`\n\n"

    "Safety: for irreversible operations (rm -rf /, shutdown, disk erase, "
    "force-push to main, dropping production tables) ask for confirmation "
    "before running. Reply: 'This will <effect>. Reply YES to proceed.'\n\n"

    "Don't suggest /commands. Don't explain what you're about to do. "
    "Just do it and report the result."
)


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------
_SEND_FILE_RE = re.compile(r"\[SEND_FILE:([^\]]+)\]")
_REMEMBER_RE = re.compile(r"\[REMEMBER:([^:]+):([^\]]+)\]")
_BROWSE_RE = re.compile(r"\[BROWSE:([^\]]+)\]")

# Mimes treated as photos when picking send_photo vs send_document
_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic"}
_VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".webm", ".avi"}
_AUDIO_EXTS = {".m4a", ".mp3", ".ogg", ".wav", ".flac", ".aac"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_claude_bin() -> str:
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit and os.path.exists(explicit):
        return explicit
    found = shutil.which("claude")
    if found:
        return found
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


def _classify_path(path: str) -> str:
    """Return 'photo' | 'video' | 'audio' | 'document' for a file path."""
    ext = Path(path).suffix.lower()
    if ext in _PHOTO_EXTS:
        return "photo"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "document"


def _chunk_text(text: str, chunk_size: int = 3500) -> list[str]:
    """Split text into chunks safe to send through any chat adapter.

    Splits on the nearest paragraph boundary inside each chunk_size window.
    """
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > chunk_size:
        split = rest.rfind("\n\n", 0, chunk_size)
        if split == -1:
            split = rest.rfind("\n", 0, chunk_size)
        if split == -1 or split < chunk_size // 2:
            split = chunk_size
        chunks.append(rest[:split].rstrip())
        rest = rest[split:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
@dataclass
class ClaudeRunner:
    """Drives `claude -p` and pumps streaming output to a chat adapter."""

    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    model: Optional[str] = None
    cwd: Optional[Path] = None
    sessions_path: Optional[Path] = None
    forks_path: Optional[Path] = None
    usage_path: Optional[Path] = None
    edit_throttle_secs: float = 1.5
    max_output_bytes: int = 4 * 1024 * 1024
    max_usage_records: int = 200          # per chat
    extra_env: dict[str, str] = field(default_factory=dict)
    extract_memories: bool = True
    inject_memories: bool = True
    project_registry: object = None  # optional kernel.projects.ProjectRegistry

    # Internal
    _sessions: dict[str, str] = field(default_factory=dict, init=False)
    _forks: dict[str, dict[str, str]] = field(default_factory=dict, init=False)
    _usage: dict[str, list[dict]] = field(default_factory=dict, init=False)
    _user_context_path: Path | None = field(default=None, init=False)

    # ---------------------------------------------------------------------
    def __post_init__(self) -> None:
        base = Path(__file__).resolve().parent.parent
        if self.sessions_path is None:
            self.sessions_path = base / "claude_sessions.json"
        if self.forks_path is None:
            self.forks_path = base / "claude_forks.json"
        if self.usage_path is None:
            self.usage_path = base / "claude_usage.json"
        if self.cwd is None:
            self.cwd = Path.cwd()
        # Auto-load USER_CONTEXT.md (extra system-prompt context the user
        # provides — see config.py).
        ctx_path = base / "USER_CONTEXT.md"
        if ctx_path.exists():
            self._user_context_path = ctx_path
            try:
                self.system_prompt = (
                    self.system_prompt
                    + "\n\n--- USER CONTEXT ---\n"
                    + ctx_path.read_text()
                )
            except Exception:
                pass
        self._load_all()

    # -- Persistence -------------------------------------------------------
    @staticmethod
    def _load_json(path: Optional[Path], default):
        if path is None or not path.exists():
            return default
        try:
            return json.loads(path.read_text())
        except Exception:
            return default

    @staticmethod
    def _save_json(path: Optional[Path], data) -> None:
        if path is None:
            return
        try:
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.warning("Failed to persist %s", path)

    def _load_all(self) -> None:
        self._sessions = self._load_json(self.sessions_path, {})
        self._forks = self._load_json(self.forks_path, {})
        self._usage = self._load_json(self.usage_path, {})

    def _save_sessions(self) -> None:
        self._save_json(self.sessions_path, self._sessions)

    def _save_forks(self) -> None:
        self._save_json(self.forks_path, self._forks)

    def _save_usage(self) -> None:
        self._save_json(self.usage_path, self._usage)

    def _key(self, ctx: Context) -> str:
        return f"{ctx.adapter.name}:{ctx.chat_id}"

    def _user_key(self, ctx: Context) -> str:
        return f"{ctx.adapter.name}:{ctx.user.id}"

    # -- Sessions ----------------------------------------------------------
    def session_id(self, ctx: Context) -> Optional[str]:
        return self._sessions.get(self._key(ctx))

    def clear_session(self, ctx: Context) -> None:
        """Drop the persisted session id for this chat (start a fresh thread)."""
        self._sessions.pop(self._key(ctx), None)
        self._save_sessions()

    # -- Forks (named branches) -------------------------------------------
    def list_forks(self, ctx: Context) -> dict[str, str]:
        return dict(self._forks.get(self._key(ctx), {}))

    def save_fork(self, ctx: Context, name: str) -> bool:
        sid = self.session_id(ctx)
        if not sid:
            return False
        self._forks.setdefault(self._key(ctx), {})[name] = sid
        self._save_forks()
        return True

    def load_fork(self, ctx: Context, name: str) -> bool:
        sid = self._forks.get(self._key(ctx), {}).get(name)
        if not sid:
            return False
        self._sessions[self._key(ctx)] = sid
        self._save_sessions()
        return True

    def delete_fork(self, ctx: Context, name: str) -> bool:
        forks = self._forks.get(self._key(ctx), {})
        if name in forks:
            forks.pop(name)
            self._save_forks()
            return True
        return False

    # -- Usage tracking ----------------------------------------------------
    def usage_for(self, ctx: Context) -> list[dict]:
        return list(self._usage.get(self._key(ctx), []))

    def _record_usage(self, ctx: Context, usage: dict, model: Optional[str]) -> None:
        if not usage:
            return
        records = self._usage.setdefault(self._key(ctx), [])
        records.append({
            "ts": time.time(),
            "in": int(usage.get("input_tokens", 0) or 0),
            "out": int(usage.get("output_tokens", 0) or 0),
            "cache_read": int(usage.get("cache_read_input_tokens", 0) or 0),
            "cache_write": int(usage.get("cache_creation_input_tokens", 0) or 0),
            "model": model or self.model or "default",
        })
        if len(records) > self.max_usage_records:
            self._usage[self._key(ctx)] = records[-self.max_usage_records:]
        self._save_usage()

    # -- Command building --------------------------------------------------
    def _build_cmd(self, ctx: Context) -> list[str]:
        cmd = [
            _resolve_claude_bin(), "-p",
            "--dangerously-skip-permissions",
            "--system-prompt", self._build_system_prompt_for(ctx),
            "--output-format", "stream-json",
            "--verbose",
        ]
        proj = self._project_for(ctx)
        model = (proj.model if proj and proj.model else None) or self.model
        if model:
            cmd.extend(["--model", model])
        if (sid := self._sessions.get(self._key(ctx))):
            cmd.extend(["--resume", sid])
        return cmd

    def _project_for(self, ctx: Context):
        if self.project_registry is None:
            return None
        try:
            return self.project_registry.context_for(ctx)
        except Exception:
            return None

    def _cwd_for(self, ctx: Context) -> Path:
        proj = self._project_for(ctx)
        if proj and proj.cwd.is_dir():
            return proj.cwd
        return self.cwd or Path.cwd()

    def _env_for(self, ctx: Context) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.update(self.extra_env)
        proj = self._project_for(ctx)
        if proj:
            env.update(proj.env)
        return env

    # -- Marker handling ---------------------------------------------------
    async def _handle_markers(self, ctx: Context, text: str) -> str:
        """Process [SEND_FILE:…], [BROWSE:…], [REMEMBER:…] markers; return cleaned text."""
        for m in _SEND_FILE_RE.finditer(text):
            path = m.group(1).strip()
            if not os.path.exists(path):
                logger.warning("[SEND_FILE] missing file: %s", path)
                continue
            kind = _classify_path(path)
            try:
                if kind == "photo":
                    await ctx.adapter.send_photo(ctx.chat_id, path)
                elif kind == "video":
                    await ctx.adapter.send_video(ctx.chat_id, path)
                elif kind == "audio":
                    await ctx.adapter.send_voice(ctx.chat_id, path)
                else:
                    await ctx.adapter.send_document(ctx.chat_id, path)
            except Exception:
                logger.exception("Failed to send file %s", path)

        # [BROWSE:url] → headless screenshot, send as photo with caption.
        for m in _BROWSE_RE.finditer(text):
            url = m.group(1).strip()
            if not url:
                continue
            try:
                from .browser import get_pool
                shot = await get_pool().screenshot(url, session_key=self._key(ctx))
                await ctx.adapter.send_photo(ctx.chat_id, shot, caption=url)
                try:
                    Path(shot).unlink(missing_ok=True)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("[BROWSE] %s failed: %s", url, e)
                try:
                    await ctx.adapter.send_text(ctx.chat_id, f"⚠️ couldn't browse {url}: {e}")
                except Exception:
                    pass

        # [REMEMBER:cat:fact] → persistent memory
        if self.extract_memories:
            for m in _REMEMBER_RE.finditer(text):
                category = m.group(1).strip().lower()
                fact = m.group(2).strip()
                if fact:
                    try:
                        from .store import add_memory
                        add_memory(self._user_key(ctx), fact, category=category)
                        logger.info("Memory remembered for %s: [%s] %s",
                                    self._user_key(ctx), category, fact[:60])
                    except Exception:
                        logger.exception("Memory persistence failed")

        cleaned = _SEND_FILE_RE.sub("", text)
        cleaned = _BROWSE_RE.sub("", cleaned)
        cleaned = _REMEMBER_RE.sub("", cleaned)
        return cleaned.strip()

    # -- Memory injection --------------------------------------------------
    def _build_system_prompt_for(self, ctx: Context) -> str:
        prompt = self.system_prompt
        if self.inject_memories:
            try:
                from .store import format_memories_for_prompt
                mem = format_memories_for_prompt(self._user_key(ctx), max_chars=2000)
                if mem:
                    prompt = prompt + "\n\n[USER MEMORY: " + mem + "]"
            except Exception:
                logger.exception("Memory injection failed")
        return prompt

    # -- Main entry --------------------------------------------------------
    async def run(
        self,
        ctx: Context,
        prompt: str,
        *,
        attachments: Iterable[Attachment] = (),
    ) -> str:
        """Run a Claude turn for this chat. Returns the final text response."""
        # 1. If there are attachments, download them and reference in the prompt.
        attachment_lines: list[str] = []
        for att in attachments or ():
            try:
                if att.local_path is None or not Path(att.local_path).exists():
                    p = await ctx.adapter.download_attachment(att)
                else:
                    p = att.local_path
                kind_label = att.kind.value
                attachment_lines.append(
                    f"[USER ATTACHED {kind_label.upper()}: {p}]"
                )
            except Exception as e:
                attachment_lines.append(f"[ATTACHMENT DOWNLOAD FAILED: {e}]")

        if attachment_lines:
            prompt = "\n".join(attachment_lines) + "\n\n" + prompt

        # 2. Send a thinking placeholder message we'll edit
        try:
            thinking = await ctx.adapter.send_text(ctx.chat_id, "🤔 Thinking…")
        except Exception:
            thinking = None

        # 3. Spawn claude
        try:
            cmd = self._build_cmd(ctx)
        except RuntimeError as e:
            await ctx.adapter.send_text(ctx.chat_id, str(e))
            return ""

        env = self._env_for(ctx)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(self._cwd_for(ctx)),
                # claude's `system` init line can exceed 64 KB when many MCP
                # tools are configured. Raise the StreamReader limit so
                # readline() doesn't choke.
                limit=64 * 1024 * 1024,
            )
        except FileNotFoundError as e:
            err = f"`claude` not found: {e}. Install from https://claude.com/claude-code"
            await ctx.adapter.send_text(ctx.chat_id, err)
            return ""

        # Feed prompt via stdin so we don't hit OS arg-length limits
        if proc.stdin:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()

        # 4. Drain stderr in the background so it doesn't deadlock the pipe
        stderr_buf: list[bytes] = []

        async def _drain():
            try:
                while True:
                    chunk = await proc.stderr.read(64 * 1024)
                    if not chunk:
                        break
                    stderr_buf.append(chunk)
            except Exception:
                pass

        stderr_task = asyncio.create_task(_drain())

        # 5. Stream stdout, parse JSON events, accumulate text
        accumulated = ""
        session_id: Optional[str] = None
        usage: dict = {}
        last_edit = 0.0

        try:
            async for line in proc.stdout:
                raw = line.decode(errors="replace").strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    accumulated += raw + "\n"
                    continue

                t = event.get("type")
                if t == "system":
                    session_id = event.get("session_id", session_id)
                elif t == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content") or []:
                        if isinstance(block, dict) and block.get("type") == "text":
                            accumulated = block.get("text", accumulated)
                elif t == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        accumulated += delta.get("text", "")
                        if len(accumulated) > self.max_output_bytes:
                            accumulated = accumulated[-self.max_output_bytes:]
                elif t == "result":
                    session_id = event.get("session_id", session_id)
                    usage = event.get("usage", usage)
                    if event.get("result"):
                        accumulated = event["result"]

                # Throttled in-place edit
                now = time.time()
                if thinking and accumulated and now - last_edit > self.edit_throttle_secs:
                    preview = accumulated[-3500:]
                    try:
                        await ctx.adapter.edit_text(thinking, preview)
                    except Exception:
                        pass
                    last_edit = now

            await proc.wait()
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        stderr_text = b"".join(stderr_buf).decode(errors="replace")
        logger.info(
            "claude run complete: rc=%s acc_len=%s session=%s usage=%s",
            proc.returncode, len(accumulated), session_id, usage,
        )

        # 6. Persist session and usage
        if session_id:
            self._sessions[self._key(ctx)] = session_id
            self._save_sessions()
        if usage:
            self._record_usage(ctx, usage, self.model)

        # 7. Stale-session retry: if claude returned "no conversation found",
        #    drop our cached session and try once more.
        if proc.returncode != 0 and "No conversation found" in stderr_text:
            logger.warning("Stale claude session for %s, retrying", self._key(ctx))
            self.clear_session(ctx)
            return await self.run(ctx, prompt, attachments=attachments)

        # 8. Error case
        if proc.returncode != 0 and not accumulated:
            err = stderr_text.strip()[-500:] or f"claude exited {proc.returncode}"
            if thinking:
                await ctx.adapter.edit_text(thinking, f"❌ {err}")
            else:
                await ctx.adapter.send_text(ctx.chat_id, f"❌ {err}")
            return ""

        # 9. Process [SEND_FILE:...] markers and clean the text
        cleaned = await self._handle_markers(ctx, accumulated)
        if not cleaned:
            cleaned = "(done)"

        # 10. Final edit + chunked overflow
        chunks = _chunk_text(cleaned)
        if thinking:
            try:
                await ctx.adapter.edit_text(thinking, chunks[0])
            except Exception:
                await ctx.adapter.send_text(ctx.chat_id, chunks[0])
        else:
            await ctx.adapter.send_text(ctx.chat_id, chunks[0])
        for extra in chunks[1:]:
            await ctx.adapter.send_text(ctx.chat_id, extra)

        return cleaned
