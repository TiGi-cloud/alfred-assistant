"""
Web adapter — exposes Alfred as a browser-based chat at http://localhost:<port>.

Implements `kernel.ChatAdapter` over a single aiohttp server that serves a
chat UI HTML page and a `/ws` WebSocket. Each connected browser becomes a
chat session whose `chat_id` is the session's UUID.

Security:
  - The adapter binds to 127.0.0.1 by default — never exposed to the network.
  - If `auth_token` is supplied, every WebSocket connection must include
    `?token=<auth_token>` in the URL. Otherwise the server returns 401.

Capabilities today:
  - Text in / text out
  - Inline buttons (callback presses come back as `CallbackPress`)
  - Photos sent from the bot are rendered inline in the chat
  - Voice / video / documents are surfaced as download links
  - Auto-reconnect on the browser side

Limitations (v1):
  - No file upload from browser (v2)
  - No edit-message (`edit_text` rewrites the same message client-side)
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import AsyncIterator, Optional

from aiohttp import WSMsgType, web

from kernel import (
    Attachment,
    CallbackPress,
    Chat,
    ChatAdapter,
    Keyboard,
    Message,
    MessageKind,
    SentMessage,
    User,
)
from kernel.adapter import PathLike
from kernel.branding import logo_data_url

logger = logging.getLogger("alfred.adapters.web")


# ---------------------------------------------------------------------------
# Inline chat UI (single-file HTML)
# ---------------------------------------------------------------------------
_CHAT_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alfred</title>
<link rel="icon" type="image/png" href="__FAVICON__">
<style>
  :root {
    --bg: #0e1116;
    --bg-2: #161b22;
    --fg: #e6edf3;
    --muted: #8b949e;
    --accent: #2f81f7;
    --me: #1f6feb;
    --bot: #21262d;
    --danger: #f85149;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    height: 100%;
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 15px;
    line-height: 1.45;
  }
  header {
    padding: 12px 18px;
    border-bottom: 1px solid #30363d;
    background: var(--bg-2);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  header h1 {
    font-size: 16px; margin: 0; font-weight: 600;
    display: flex; align-items: center; gap: 8px;
  }
  header .logo { border-radius: 6px; }
  header .status {
    font-size: 12px;
    color: var(--muted);
  }
  header .status.connected { color: #3fb950; }
  header .status.disconnected { color: var(--danger); }
  main {
    display: flex;
    flex-direction: column;
    height: calc(100vh - 50px);
  }
  #log {
    flex: 1;
    overflow-y: auto;
    padding: 16px 18px;
  }
  .msg {
    max-width: 80%;
    margin: 6px 0;
    padding: 8px 12px;
    border-radius: 12px;
    word-wrap: break-word;
    white-space: pre-wrap;
  }
  .msg.me { background: var(--me); margin-left: auto; }
  .msg.bot { background: var(--bot); }
  .msg img { max-width: 100%; border-radius: 8px; display: block; margin-top: 6px; }
  .msg pre {
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px;
    overflow-x: auto;
    margin: 4px 0;
  }
  .msg a.file {
    display: inline-block;
    margin-top: 4px;
    color: var(--accent);
    text-decoration: none;
  }
  .keyboard {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 8px;
  }
  .keyboard button {
    background: #30363d;
    color: var(--fg);
    border: 1px solid #444c56;
    border-radius: 8px;
    padding: 6px 10px;
    font-size: 13px;
    cursor: pointer;
  }
  .keyboard button:hover { background: #444c56; }
  form {
    display: flex;
    gap: 8px;
    padding: 12px 18px;
    border-top: 1px solid #30363d;
    background: var(--bg-2);
  }
  #input {
    flex: 1;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 10px 14px;
    font: inherit;
    resize: none;
  }
  #input:focus { outline: none; border-color: var(--accent); }
  button.send {
    background: var(--accent);
    color: white;
    border: 0;
    border-radius: 10px;
    padding: 0 18px;
    font-weight: 600;
    cursor: pointer;
  }
  button.send:disabled { opacity: 0.5; cursor: not-allowed; }
</style>
</head>
<body>
<header>
  <h1><img class="logo" src="__LOGO__" alt="" width="28" height="28">Alfred</h1>
  <span id="status" class="status">connecting…</span>
</header>
<main>
  <div id="log" aria-live="polite"></div>
  <form id="form">
    <textarea id="input" rows="1" placeholder="Talk to Alfred…" autofocus></textarea>
    <button class="send" type="submit">Send</button>
  </form>
</main>
<script>
(() => {
  const log = document.getElementById("log");
  const form = document.getElementById("form");
  const input = document.getElementById("input");
  const status = document.getElementById("status");

  // Read token from URL query (?token=xxx) so reconnects keep working
  const urlParams = new URLSearchParams(location.search);
  const token = urlParams.get("token") || "";

  let ws = null;
  let reconnectMs = 1000;

  function setStatus(text, cls) {
    status.textContent = text;
    status.className = "status" + (cls ? " " + cls : "");
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function appendMsg(role, html) {
    const div = document.createElement("div");
    div.className = "msg " + role;
    div.innerHTML = html;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
    return div;
  }

  function renderKeyboard(kb) {
    if (!kb || !kb.length) return "";
    let html = '<div class="keyboard">';
    for (const row of kb) {
      for (const btn of row) {
        if (btn.url) {
          html += `<a href="${escapeHtml(btn.url)}" target="_blank" rel="noopener"><button type="button">${escapeHtml(btn.label)}</button></a>`;
        } else {
          html += `<button type="button" data-cb="${escapeHtml(btn.data || "")}">${escapeHtml(btn.label)}</button>`;
        }
      }
    }
    html += "</div>";
    return html;
  }

  function renderTextMessage(msg) {
    let body = escapeHtml(msg.text || "");
    if (msg.parse_mode === "html") body = msg.text;
    let html = body;
    if (msg.keyboard) html += renderKeyboard(msg.keyboard);
    return html;
  }

  function renderPhoto(msg) {
    const cap = msg.caption ? `<div>${escapeHtml(msg.caption)}</div>` : "";
    return cap + `<img src="${msg.url}" alt="photo">` +
      (msg.keyboard ? renderKeyboard(msg.keyboard) : "");
  }

  function renderFile(msg, label) {
    const cap = msg.caption ? `<div>${escapeHtml(msg.caption)}</div>` : "";
    const linkText = msg.filename || label || "file";
    return cap + `<a class="file" href="${msg.url}" download="${escapeHtml(linkText)}">📎 ${escapeHtml(linkText)}</a>`;
  }

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
    }
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/ws${token ? "?token=" + encodeURIComponent(token) : ""}`;
    ws = new WebSocket(url);
    setStatus("connecting…");

    ws.addEventListener("open", () => {
      setStatus("connected", "connected");
      reconnectMs = 1000;
    });

    ws.addEventListener("close", () => {
      setStatus("disconnected — reconnecting…", "disconnected");
      setTimeout(connect, reconnectMs);
      reconnectMs = Math.min(reconnectMs * 2, 15000);
    });

    ws.addEventListener("error", () => { /* close handler will handle reconnect */ });

    ws.addEventListener("message", (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "text")  appendMsg("bot", renderTextMessage(m));
      else if (m.type === "edit") {
        // Find a previous message div by id and replace its body
        const target = document.querySelector(`[data-msg-id="${m.id}"]`);
        if (target) target.innerHTML = renderTextMessage(m);
        else appendMsg("bot", renderTextMessage(m));
      }
      else if (m.type === "photo") {
        const div = appendMsg("bot", renderPhoto(m));
        if (m.id) div.setAttribute("data-msg-id", m.id);
      }
      else if (m.type === "video")    appendMsg("bot", renderFile(m, "video"));
      else if (m.type === "voice")    appendMsg("bot", renderFile(m, "voice note"));
      else if (m.type === "document") appendMsg("bot", renderFile(m, "document"));
      else if (m.type === "typing")   { /* ignore for now */ }
    });
  }

  // Inline keyboard click handler (delegated)
  log.addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-cb]");
    if (!btn) return;
    const data = btn.getAttribute("data-cb");
    if (!data) return;
    send({ type: "callback", data, label: btn.textContent });
    appendMsg("me", "▶ " + escapeHtml(btn.textContent));
  });

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    send({ type: "text", text });
    appendMsg("me", escapeHtml(text));
    input.value = "";
    input.focus();
  });

  // Submit on Enter (Shift+Enter for newline)
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  connect();
})();
</script>
</body>
</html>
"""


def _render_chat_html() -> str:
    """Bake the logo into the chat HTML once at module load."""
    return (
        _CHAT_HTML_TEMPLATE
        .replace("__FAVICON__", logo_data_url("favicon"))
        .replace("__LOGO__", logo_data_url("favicon"))
    )


_CHAT_HTML = _render_chat_html()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _keyboard_to_json(kb: Optional[Keyboard]) -> Optional[list[list[dict]]]:
    if kb is None or kb.is_empty():
        return None
    return [
        [
            {
                "label": b.label,
                **({"data": b.data} if b.data else {}),
                **({"url": b.url} if b.url else {}),
                **({"webapp_url": b.webapp_url} if b.webapp_url else {}),
            }
            for b in row
        ]
        for row in kb.rows
    ]


def _data_url(path: Path) -> str:
    """Inline-encode a small file as a data: URL. Used for photos so we don't
    need a public file-serving endpoint. For larger files we fall back to a
    served URL."""
    mime, _ = mimetypes.guess_type(str(path))
    mime = mime or "application/octet-stream"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


# ---------------------------------------------------------------------------
# WebAdapter
# ---------------------------------------------------------------------------
class WebAdapter(ChatAdapter):
    """Browser-based chat adapter served on http://<host>:<port>."""

    name = "web"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        auth_token: Optional[str] = None,
        photo_inline_limit_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        self._host = host
        self._port = port
        self._auth_token = auth_token
        self._photo_inline_limit = photo_inline_limit_bytes
        self._messages: asyncio.Queue[Message] = asyncio.Queue()
        self._callbacks: asyncio.Queue[CallbackPress] = asyncio.Queue()
        self._sessions: dict[str, web.WebSocketResponse] = {}
        self._files: dict[str, Path] = {}  # token -> served file path
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._started = False

    @property
    def url(self) -> str:
        suffix = f"/?token={self._auth_token}" if self._auth_token else "/"
        return f"http://{self._host}:{self._port}{suffix}"

    # -- Lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        if self._started:
            return
        app = web.Application()
        app.add_routes([
            web.get("/", self._serve_index),
            web.get("/ws", self._serve_ws),
            web.get("/file/{token}", self._serve_file),
        ])
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        self._started = True
        logger.info("Web adapter listening at %s", self.url)

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            for ws in list(self._sessions.values()):
                await ws.close()
            if self._site:
                await self._site.stop()
            if self._runner:
                await self._runner.cleanup()
        finally:
            self._started = False
            logger.info("Web adapter stopped")

    # -- HTTP routes --------------------------------------------------------
    async def _serve_index(self, request: web.Request) -> web.Response:
        return web.Response(text=_CHAT_HTML, content_type="text/html")

    async def _serve_file(self, request: web.Request) -> web.StreamResponse:
        if self._auth_token and request.query.get("token") != self._auth_token:
            return web.Response(status=401, text="unauthorized")
        token = request.match_info["token"]
        path = self._files.get(token)
        if not path or not path.exists():
            return web.Response(status=404, text="not found")
        return web.FileResponse(path)

    async def _serve_ws(self, request: web.Request) -> web.WebSocketResponse:
        if self._auth_token and request.query.get("token") != self._auth_token:
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.close(code=4401, message=b"unauthorized")
            return ws

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        session_id = uuid.uuid4().hex
        self._sessions[session_id] = ws
        logger.info("Web session opened: %s", session_id)

        try:
            async for raw in ws:
                if raw.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(raw.data)
                except json.JSONDecodeError:
                    continue
                kind = data.get("type")
                if kind == "text":
                    text = data.get("text", "").strip()
                    if not text:
                        continue
                    msg = Message(
                        id=uuid.uuid4().hex,
                        chat=Chat(id=session_id, type="direct", title="Web"),
                        user=User(id=session_id, display_name="Web"),
                        kind=MessageKind.COMMAND if text.startswith("/") else MessageKind.TEXT,
                        text=text,
                    )
                    await self._messages.put(msg)
                elif kind == "callback":
                    cb = CallbackPress(
                        id=uuid.uuid4().hex,
                        chat=Chat(id=session_id, type="direct", title="Web"),
                        user=User(id=session_id, display_name="Web"),
                        data=data.get("data", ""),
                    )
                    await self._callbacks.put(cb)
        finally:
            self._sessions.pop(session_id, None)
            logger.info("Web session closed: %s", session_id)
        return ws

    # -- Inbound streams ----------------------------------------------------
    async def messages(self) -> AsyncIterator[Message]:
        while True:
            yield await self._messages.get()

    async def callbacks(self) -> AsyncIterator[CallbackPress]:
        while True:
            yield await self._callbacks.get()

    # -- Outbound: text -----------------------------------------------------
    async def _send_to_session(self, chat_id: str, payload: dict) -> None:
        ws = self._sessions.get(chat_id)
        if ws is None or ws.closed:
            logger.warning("Web send to unknown / closed session: %s", chat_id)
            return
        try:
            await ws.send_json(payload)
        except ConnectionResetError:
            self._sessions.pop(chat_id, None)

    async def send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: Optional[str] = None,
        keyboard: Optional[Keyboard] = None,
        parse_mode: Optional[str] = None,
        disable_preview: bool = False,
    ) -> SentMessage:
        msg_id = uuid.uuid4().hex
        await self._send_to_session(chat_id, {
            "type": "text",
            "id": msg_id,
            "text": text,
            "parse_mode": parse_mode,
            "keyboard": _keyboard_to_json(keyboard),
            "reply_to": reply_to,
        })
        return SentMessage(chat_id=chat_id, message_id=msg_id)

    async def edit_text(
        self,
        sent: SentMessage,
        text: str,
        *,
        keyboard: Optional[Keyboard] = None,
        parse_mode: Optional[str] = None,
    ) -> None:
        await self._send_to_session(sent.chat_id, {
            "type": "edit",
            "id": sent.message_id,
            "text": text,
            "parse_mode": parse_mode,
            "keyboard": _keyboard_to_json(keyboard),
        })

    async def delete(self, sent: SentMessage) -> None:
        await self._send_to_session(sent.chat_id, {
            "type": "delete",
            "id": sent.message_id,
        })

    # -- Outbound: media ----------------------------------------------------
    def _serve_path(self, path: Path) -> str:
        token = uuid.uuid4().hex
        self._files[token] = path
        suffix = f"&token={self._auth_token}" if self._auth_token else ""
        return f"/file/{token}?_={int(time.time())}{suffix}"

    async def send_photo(
        self,
        chat_id: str,
        photo: PathLike,
        *,
        caption: Optional[str] = None,
        keyboard: Optional[Keyboard] = None,
    ) -> SentMessage:
        msg_id = uuid.uuid4().hex
        path = Path(photo)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        url = (
            _data_url(path)
            if 0 < size <= self._photo_inline_limit
            else self._serve_path(path)
        )
        await self._send_to_session(chat_id, {
            "type": "photo",
            "id": msg_id,
            "url": url,
            "caption": caption,
            "keyboard": _keyboard_to_json(keyboard),
        })
        return SentMessage(chat_id=chat_id, message_id=msg_id)

    async def send_video(
        self,
        chat_id: str,
        video: PathLike,
        *,
        caption: Optional[str] = None,
    ) -> SentMessage:
        msg_id = uuid.uuid4().hex
        path = Path(video)
        await self._send_to_session(chat_id, {
            "type": "video",
            "id": msg_id,
            "url": self._serve_path(path),
            "filename": path.name,
            "caption": caption,
        })
        return SentMessage(chat_id=chat_id, message_id=msg_id)

    async def send_voice(self, chat_id: str, voice: PathLike) -> SentMessage:
        msg_id = uuid.uuid4().hex
        path = Path(voice)
        await self._send_to_session(chat_id, {
            "type": "voice",
            "id": msg_id,
            "url": self._serve_path(path),
            "filename": path.name,
        })
        return SentMessage(chat_id=chat_id, message_id=msg_id)

    async def send_document(
        self,
        chat_id: str,
        path: PathLike,
        *,
        filename: Optional[str] = None,
        caption: Optional[str] = None,
    ) -> SentMessage:
        msg_id = uuid.uuid4().hex
        p = Path(path)
        await self._send_to_session(chat_id, {
            "type": "document",
            "id": msg_id,
            "url": self._serve_path(p),
            "filename": filename or p.name,
            "caption": caption,
        })
        return SentMessage(chat_id=chat_id, message_id=msg_id)

    # -- Outbound: presence -------------------------------------------------
    async def send_typing(self, chat_id: str) -> None:
        await self._send_to_session(chat_id, {"type": "typing"})

    # -- Auth + downloads ---------------------------------------------------
    async def authorize(self, user: User) -> bool:
        # Web sessions are gated by the URL token at WebSocket-connect time;
        # if they got this far they're already authenticated.
        return True

    async def download_attachment(
        self,
        attachment: Attachment,
        dest: Optional[Path] = None,
    ) -> Path:
        # The web adapter doesn't support inbound attachments yet — files
        # uploaded from the browser will be a v2 feature.
        raise NotImplementedError("Web adapter does not yet accept inbound attachments")
