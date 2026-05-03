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

from .messages import Attachment, AttachmentKind
from .runner import Context

logger = logging.getLogger("alfred.kernel.claude")


DEFAULT_SYSTEM_PROMPT = (
    "You are Alfred, a personal assistant running on a Mac. You have FULL "
    "shell access via the Bash tool — run any command, AppleScript, etc. "
    "Never say you can't do something due to sandboxing — you are NOT "
    "sandboxed. Be concise — your response goes to a chat client.\n\n"
    "RESPONSE MARKERS (the bot intercepts these):\n"
    "  [SEND_FILE:/abs/path] — send the file as an attachment\n"
    "  [REMEMBER:category:fact] — extract for long-term memory\n\n"
    "When the user asks for a screenshot, take it with `screencapture -x "
    "/tmp/shot.png` and emit `[SEND_FILE:/tmp/shot.png]`.\n"
    "When the user asks 'open X' open it with `open -a 'X'` or `open "
    "<url>` for URLs.\n"
    "Don't suggest /commands — just do the thing the user asked for."
)


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------
_SEND_FILE_RE = re.compile(r"\[SEND_FILE:([^\]]+)\]")
_REMEMBER_RE = re.compile(r"\[REMEMBER:([^:]+):([^\]]+)\]")

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
    edit_throttle_secs: float = 1.5
    max_output_bytes: int = 4 * 1024 * 1024
    extra_env: dict[str, str] = field(default_factory=dict)

    # Internal
    _sessions: dict[str, str] = field(default_factory=dict, init=False)
    _user_context_path: Path | None = field(default=None, init=False)

    # ---------------------------------------------------------------------
    def __post_init__(self) -> None:
        if self.sessions_path is None:
            self.sessions_path = Path(__file__).resolve().parent.parent / "claude_sessions.json"
        if self.cwd is None:
            self.cwd = Path.cwd()
        # Auto-load USER_CONTEXT.md (extra system-prompt context the user
        # provides — see config.py).
        ctx_path = Path(__file__).resolve().parent.parent / "USER_CONTEXT.md"
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
        self._load_sessions()

    # -- Sessions ----------------------------------------------------------
    def _load_sessions(self) -> None:
        if self.sessions_path and self.sessions_path.exists():
            try:
                self._sessions = json.loads(self.sessions_path.read_text())
            except Exception:
                self._sessions = {}

    def _save_sessions(self) -> None:
        if self.sessions_path is None:
            return
        try:
            self.sessions_path.write_text(json.dumps(self._sessions, indent=2))
        except Exception:
            logger.warning("Failed to persist sessions to %s", self.sessions_path)

    def _key(self, ctx: Context) -> str:
        return f"{ctx.adapter.name}:{ctx.chat_id}"

    def clear_session(self, ctx: Context) -> None:
        """Drop the persisted session id for this chat (start a fresh thread)."""
        self._sessions.pop(self._key(ctx), None)
        self._save_sessions()

    # -- Command building --------------------------------------------------
    def _build_cmd(self, ctx: Context) -> list[str]:
        cmd = [
            _resolve_claude_bin(), "-p",
            "--dangerously-skip-permissions",
            "--system-prompt", self.system_prompt,
            "--output-format", "stream-json",
            "--verbose",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if (sid := self._sessions.get(self._key(ctx))):
            cmd.extend(["--resume", sid])
        return cmd

    # -- Marker handling ---------------------------------------------------
    async def _handle_markers(self, ctx: Context, text: str) -> str:
        """Process [SEND_FILE:...] markers, send the files, return the cleaned text."""
        sent_files: list[str] = []
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
                sent_files.append(path)
            except Exception:
                logger.exception("Failed to send file %s", path)

        cleaned = _SEND_FILE_RE.sub("", text)
        # Strip [REMEMBER:...] from the user-visible text (memory module
        # would extract these — TODO when memory is ported).
        cleaned = _REMEMBER_RE.sub("", cleaned)
        return cleaned.strip()

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

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.update(self.extra_env)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(self.cwd),
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

        # 6. Persist session
        if session_id:
            self._sessions[self._key(ctx)] = session_id
            self._save_sessions()

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
