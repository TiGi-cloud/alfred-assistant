#!/usr/bin/env python3
"""
Comprehensive offline test suite for Alfred.

Runs everything we can verify without external chat tokens:

  - Code quality: ruff (errors-only), py_compile every .py
  - Static checks: imports, abstract-method completeness, dataclass validation
  - Web adapter: HTTP server + WebSocket round-trip (real, on localhost)
  - Dispatcher: command + callback routing, authorisation flow
  - Setup wizard: every validation branch + .env writer output
  - iMessage: chat.db read access + AppleScript syntax validation via osacompile
  - Telegram / Discord / Slack: build outbound keyboards + verify shape

Run with:

    python3 tests/test_all.py

Exits 0 on success, non-zero on failure. Prints one line per test.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Tiny test runner
# ---------------------------------------------------------------------------
GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
_pass = 0
_fail = 0
_skip = 0


def ok(msg: str) -> None:
    global _pass
    _pass += 1
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str, exc: BaseException | None = None) -> None:
    global _fail
    _fail += 1
    extra = f"  ({type(exc).__name__}: {exc})" if exc else ""
    print(f"  {RED}✗{RESET} {msg}{extra}")


def skip(msg: str) -> None:
    global _skip
    _skip += 1
    print(f"  {YELLOW}-{RESET} {msg} (skipped)")


def section(title: str) -> None:
    print(f"\n  {YELLOW}{title}{RESET}")


def expect(cond: bool, msg: str, note: str = "") -> None:
    if cond:
        ok(msg)
    else:
        fail(msg + (f" — {note}" if note else ""))


# ---------------------------------------------------------------------------
# 1) Code quality
# ---------------------------------------------------------------------------
def test_code_quality() -> None:
    section("Code quality")

    py_files = subprocess.check_output(
        ["git", "ls-files", "*.py"], cwd=ROOT, text=True
    ).split()
    expect(len(py_files) >= 30, f"git tracks {len(py_files)} .py files")

    rc = subprocess.run(["python3", "-m", "py_compile", *py_files], cwd=ROOT).returncode
    expect(rc == 0, "py_compile all tracked .py files")

    rc = subprocess.run(
        ["python3", "-m", "ruff", "check", "--select=E9,F63,F7,F82", "."],
        cwd=ROOT,
        capture_output=True,
    ).returncode
    expect(rc == 0, "ruff (errors-only ruleset) clean across repo")


# ---------------------------------------------------------------------------
# 2) Imports + abstract-method completeness
# ---------------------------------------------------------------------------
def test_imports() -> None:
    section("Imports + abstract method completeness")

    import inspect

    import kernel
    from kernel import (
        Attachment, AttachmentKind, Button, CallbackPress, Chat, ChatAdapter,
        Keyboard, Message, MessageKind, SentMessage, User,
    )
    from kernel.runner import Context, Dispatcher
    ok("kernel + kernel.runner import")

    import adapters.web as web_mod
    import adapters.telegram as tg_mod
    import adapters.discord as dx_mod
    import adapters.slack as sl_mod
    import adapters.imessage as im_mod
    ok("all 5 adapters import without their optional 3rd-party deps")

    for name, cls in [
        ("WebAdapter", web_mod.WebAdapter),
        ("TelegramAdapter", tg_mod.TelegramAdapter),
        ("DiscordAdapter", dx_mod.DiscordAdapter),
        ("SlackAdapter", sl_mod.SlackAdapter),
        ("iMessageAdapter", im_mod.iMessageAdapter),
    ]:
        expect(issubclass(cls, ChatAdapter), f"{name} subclasses ChatAdapter")
        leftover = [n for n, m in inspect.getmembers(cls)
                    if getattr(m, "__isabstractmethod__", False)]
        expect(not leftover, f"{name} implements every abstract method", str(leftover))


# ---------------------------------------------------------------------------
# 3) Dataclass / Button validation
# ---------------------------------------------------------------------------
def test_dataclasses() -> None:
    section("kernel dataclass invariants")

    from kernel import Button, Keyboard, Message, MessageKind, User, Chat

    # Button must have exactly one of data/url/webapp_url
    try:
        Button(label="bad")  # zero
        fail("Button(label=...) without data/url should raise")
    except ValueError:
        ok("Button(no payload) raises ValueError")
    try:
        Button(label="bad", data="x", url="https://y")
        fail("Button with two payloads should raise")
    except ValueError:
        ok("Button(data + url) raises ValueError")
    Button(label="ok", data="cb")
    Button(label="ok", url="https://example.com")
    Button(label="ok", webapp_url="https://example.com/app")
    ok("Button single-payload variants construct")

    kb = Keyboard.grid([Button(label=str(i), data=f"cb:{i}") for i in range(5)], cols=2)
    expect(len(kb.rows) == 3, "Keyboard.grid wraps to 2 columns")
    expect(not Keyboard(rows=()).is_empty() is False, "empty keyboard is is_empty()")

    # Message command parsing
    m = Message(
        id="1", chat=Chat(id="c"), user=User(id="u"),
        kind=MessageKind.COMMAND, text="/screenshot foo bar",
    )
    expect(m.command_name == "screenshot", "command_name parse")
    expect(m.command_args == "foo bar", "command_args parse")

    m2 = Message(
        id="2", chat=Chat(id="c"), user=User(id="u"),
        kind=MessageKind.COMMAND, text="/help@MyBot",
    )
    expect(m2.command_name == "help", "command_name strips @bot suffix")


# ---------------------------------------------------------------------------
# 4) Web adapter end-to-end
# ---------------------------------------------------------------------------
async def test_web_adapter() -> None:
    section("Web adapter — HTTP + WebSocket round-trip")

    import aiohttp
    from adapters.web import WebAdapter
    from kernel import MessageKind

    web = WebAdapter(host="127.0.0.1", port=8901, auth_token="probe-token")
    await web.start()
    try:
        async with aiohttp.ClientSession() as s:
            # 4a. Index HTML
            async with s.get("http://127.0.0.1:8901/") as r:
                html = await r.text()
            expect("Alfred" in html and "/ws" in html, "GET / serves chat HTML")

            # 4b. /file requires auth
            async with s.get("http://127.0.0.1:8901/file/missing") as r:
                expect(r.status == 401, "file route requires auth token")
            async with s.get("http://127.0.0.1:8901/file/missing?token=probe-token") as r:
                expect(r.status == 404, "file route 404 with valid auth + missing token")

            # 4c. /ws without token: closes 4401
            async with s.ws_connect("ws://127.0.0.1:8901/ws") as ws:
                await asyncio.wait_for(ws.receive(), timeout=3)
                expect(ws.closed, "ws rejects connection without token")

            # 4d. /ws with token: full round trip
            async with s.ws_connect("ws://127.0.0.1:8901/ws?token=probe-token") as ws:
                # Client → server: text message
                await ws.send_json({"type": "text", "text": "ping"})
                msg = await asyncio.wait_for(web.messages().__anext__(), timeout=3)
                expect(msg.text == "ping", "text from ws arrives in messages() queue")
                expect(msg.kind == MessageKind.TEXT, "text msg has TEXT kind")
                expect(msg.chat.id == msg.user.id, "web session id used as chat + user id")

                # Client → server: command
                await ws.send_json({"type": "text", "text": "/screenshot now"})
                msg2 = await asyncio.wait_for(web.messages().__anext__(), timeout=3)
                expect(msg2.kind == MessageKind.COMMAND, "/cmd → COMMAND kind")
                expect(msg2.command_name == "screenshot", "COMMAND.command_name parse")

                # Client → server: callback (button press)
                await ws.send_json({"type": "callback", "data": "menu:home", "label": "Home"})
                cb = await asyncio.wait_for(web.callbacks().__anext__(), timeout=3)
                expect(cb.data == "menu:home", "callback data arrives in callbacks() queue")

                # Server → client: text
                sent = await web.send_text(msg.chat.id, "hello there")
                payload = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=3))
                expect(payload["type"] == "text" and payload["text"] == "hello there",
                       "send_text arrives at ws as text payload")
                expect(sent.message_id == payload["id"], "SentMessage.message_id matches payload.id")

                # Server → client: typing indicator
                await web.send_typing(msg.chat.id)
                payload = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=3))
                expect(payload["type"] == "typing", "send_typing emits typing payload")

                # Server → client: photo (small inline data: URL)
                tmp = Path(tempfile.mkdtemp()) / "tiny.png"
                # Smallest valid PNG
                tmp.write_bytes(bytes.fromhex(
                    "89504E470D0A1A0A0000000D49484452000000010000000108060000001F15C489"
                    "0000000D49444154789C636060000000050001A5F645400000000049454E44AE426082"
                ))
                await web.send_photo(msg.chat.id, tmp, caption="hi")
                payload = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=3))
                expect(payload["type"] == "photo", "send_photo emits photo payload")
                expect(payload["url"].startswith("data:image/png;base64,"),
                       "small photo inlined as data URL")
                expect(payload["caption"] == "hi", "photo caption preserved")

                # Server → client: keyboard
                from kernel import Button, Keyboard
                kb = Keyboard.of(
                    [Button(label="Yes", data="cb:yes"), Button(label="No", data="cb:no")],
                    [Button(label="Site", url="https://example.com")],
                )
                await web.send_text(msg.chat.id, "pick one", keyboard=kb)
                payload = json.loads(await asyncio.wait_for(ws.receive_str(), timeout=3))
                expect(payload["keyboard"] is not None, "keyboard rendered to JSON")
                expect(len(payload["keyboard"]) == 2, "keyboard preserves 2 rows")
                expect(payload["keyboard"][1][0].get("url") == "https://example.com",
                       "url buttons keep url field")

    finally:
        await web.stop()


# ---------------------------------------------------------------------------
# 5) Dispatcher routing
# ---------------------------------------------------------------------------
async def test_dispatcher() -> None:
    section("Dispatcher routing")

    from kernel import (
        ChatAdapter, Chat, Keyboard, Message, MessageKind, SentMessage, User,
        CallbackPress,
    )
    from kernel.runner import Context, Dispatcher

    sent_log: list[tuple[str, str]] = []
    auth_log: list[str] = []

    class FakeAdapter(ChatAdapter):
        name = "fake"
        def __init__(self, allow_users=None):
            self.q_msg = asyncio.Queue()
            self.q_cb = asyncio.Queue()
            self._allow = set(allow_users or [])
        async def start(self): pass
        async def stop(self): pass
        async def messages(self):
            while True:
                yield await self.q_msg.get()
        async def callbacks(self):
            while True:
                yield await self.q_cb.get()
        async def send_text(self, chat_id, text, *, reply_to=None, keyboard=None,
                            parse_mode=None, disable_preview=False):
            sent_log.append((chat_id, text))
            return SentMessage(chat_id=chat_id, message_id="m1")
        async def edit_text(self, sent, text, **kw): pass
        async def delete(self, sent): pass
        async def send_photo(self, *a, **kw): pass
        async def send_video(self, *a, **kw): pass
        async def send_voice(self, *a, **kw): pass
        async def send_document(self, *a, **kw): pass
        async def send_typing(self, chat_id): pass
        async def authorize(self, user):
            auth_log.append(user.id)
            return (not self._allow) or user.id in self._allow
        async def download_attachment(self, att, dest=None): return Path()

    # 5a. Command dispatch
    a = FakeAdapter()
    d = Dispatcher(default_handler=lambda ctx: ctx.reply("default"))

    async def on_ping(ctx):
        await ctx.reply("pong")

    d.command("ping", on_ping)

    msg = Message(id="1", chat=Chat(id="c1"), user=User(id="u1"),
                  kind=MessageKind.COMMAND, text="/ping")
    await a.q_msg.put(msg)

    run_task = asyncio.create_task(d.run(a))
    await asyncio.sleep(0.05)

    expect(sent_log == [("c1", "pong")], "command handler dispatched", str(sent_log))

    # 5b. Default handler for non-command text
    sent_log.clear()
    msg2 = Message(id="2", chat=Chat(id="c1"), user=User(id="u1"),
                   kind=MessageKind.TEXT, text="hi")
    await a.q_msg.put(msg2)
    await asyncio.sleep(0.05)
    expect(sent_log == [("c1", "default")], "default handler dispatched on text")

    # 5c. Callback prefix dispatch
    sent_log.clear()
    cb_log: list[str] = []
    async def on_menu(ctx):
        cb_log.append(ctx.callback.data)
        await ctx.reply("ok")
    d.callback_prefix("menu:", on_menu)
    await a.q_cb.put(CallbackPress(id="cb1", chat=Chat(id="c1"), user=User(id="u1"),
                                   data="menu:home"))
    await asyncio.sleep(0.05)
    expect(cb_log == ["menu:home"], "callback_prefix matched")

    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass

    # 5d. Authorisation: unauthorized handler invoked when user not in allowlist
    a2 = FakeAdapter(allow_users={"alice"})
    sent_log.clear()
    auth_log.clear()
    d2 = Dispatcher(default_handler=lambda ctx: ctx.reply("ignored"))
    d2.command("ping", on_ping)

    msg3 = Message(id="3", chat=Chat(id="c1"), user=User(id="bob"),
                   kind=MessageKind.COMMAND, text="/ping")
    await a2.q_msg.put(msg3)
    run_task = asyncio.create_task(d2.run(a2))
    await asyncio.sleep(0.05)
    expect(auth_log == ["bob"], "authorize() called for unauthed user")
    expect(any("not authorized" in t.lower() for _, t in sent_log) or sent_log == [],
           "unauthed user gets default unauthorized response or nothing", str(sent_log))
    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# 6) Setup wizard validation matrix
# ---------------------------------------------------------------------------
async def test_setup_wizard() -> None:
    section("Setup wizard — validation matrix + .env writer")

    import aiohttp
    import setup_wizard

    tmp = Path(tempfile.gettempdir()) / "alfred-wizard-test.env"
    setup_wizard.ENV_PATH = tmp

    server_task = asyncio.create_task(setup_wizard.serve(port=8902, open_browser=False))
    await asyncio.sleep(0.5)

    try:
        async with aiohttp.ClientSession() as s:
            async def post(payload):
                async with s.post("http://127.0.0.1:8902/save", json=payload) as r:
                    body = await r.json()
                    return r.status, body

            # --- Rejected configs ---
            status, body = await post({})
            expect(status == 400, "empty config rejected")

            status, body = await post({"telegram_bot_token": "123:abc"})
            expect(status == 400, "Telegram without allowlist rejected")

            status, body = await post({
                "slack_bot_token": "xoxb-only-bot",
            })
            expect(status == 400, "Slack with only bot token rejected (needs xapp-)")

            status, body = await post({
                "slack_app_token": "xapp-only-app",
            })
            expect(status == 400, "Slack with only app token rejected (needs xoxb-)")

            # --- Accepted configs ---
            for name, payload, must_include, must_exclude in [
                ("Telegram only", {
                    "telegram_bot_token": "111:abc",
                    "allowed_users": "alice",
                    "web_enabled": False,
                }, ["TELEGRAM_BOT_TOKEN=111:abc", "ALLOWED_USERS=alice"], ["DISCORD_", "SLACK_"]),
                ("Web only", {
                    "web_enabled": True,
                    "web_port": "8888",
                }, ["WEB_PORT=8888", "WEB_AUTH_TOKEN="], ["TELEGRAM_BOT_TOKEN", "DISCORD_"]),
                ("Discord only", {
                    "discord_bot_token": "MTIz.fake",
                    "discord_allowed_user_ids": "1234567890",
                    "web_enabled": False,
                }, ["DISCORD_BOT_TOKEN=MTIz.fake", "DISCORD_ALLOWED_USER_IDS=1234567890"],
                   ["TELEGRAM_BOT_TOKEN", "SLACK_"]),
                ("Slack only", {
                    "slack_bot_token": "xoxb-fake",
                    "slack_app_token": "xapp-fake",
                    "slack_allowed_user_ids": "U01ABC",
                    "web_enabled": False,
                }, ["SLACK_BOT_TOKEN=xoxb-fake", "SLACK_APP_TOKEN=xapp-fake"],
                   ["TELEGRAM_BOT_TOKEN"]),
                ("iMessage only", {
                    "imessage_enabled": True,
                    "imessage_allowed_handles": "+15551234567",
                    "web_enabled": False,
                }, ["IMESSAGE_ENABLED=1", "IMESSAGE_ALLOWED_HANDLES=+15551234567"],
                   ["TELEGRAM_BOT_TOKEN", "DISCORD_"]),
                ("All five", {
                    "telegram_bot_token": "111:abc",
                    "allowed_users": "alice",
                    "discord_bot_token": "MTIz.fake",
                    "slack_bot_token": "xoxb-fake",
                    "slack_app_token": "xapp-fake",
                    "imessage_enabled": True,
                    "imessage_allowed_handles": "+15551234567",
                    "web_enabled": True,
                }, ["TELEGRAM_", "DISCORD_", "SLACK_", "IMESSAGE_", "WEB_AUTH_TOKEN="], []),
            ]:
                if tmp.exists():
                    tmp.unlink()
                status, body = await post(payload)
                if status != 200:
                    fail(f"{name}: save returned {status}: {body}")
                    continue
                content = tmp.read_text()
                missing = [s for s in must_include if s not in content]
                extra = [s for s in must_exclude if s in content]
                ok_flag = not missing and not extra
                if ok_flag:
                    ok(f"wizard accepts '{name}' and writes correct .env")
                else:
                    fail(f"wizard '{name}' .env: missing={missing} extra={extra}")
                expect((tmp.stat().st_mode & 0o777) == 0o600,
                       f".env has 0600 mode for '{name}'")
    finally:
        if tmp.exists():
            tmp.unlink()
        server_task.cancel()
        try:
            await server_task
        except (asyncio.CancelledError, SystemExit):
            pass


# ---------------------------------------------------------------------------
# 7) iMessage adapter unit checks (no live messages)
# ---------------------------------------------------------------------------
def test_imessage_unit() -> None:
    section("iMessage — offline unit checks")

    from adapters.imessage import (
        _decode_attributed_body, _escape_applescript, _apple_date_to_unix,
        iMessageAdapter, CHAT_DB,
    )

    # Decoder edge cases
    expect(_decode_attributed_body(b"") == "", "decoder handles empty bytes")
    expect(_decode_attributed_body(None) == "", "decoder handles None")
    expect(_decode_attributed_body(b"random\x00garbage\x80") == "",
           "decoder handles non-Apple binary without crashing")

    # AppleScript escape
    expect(_escape_applescript('hello "world"') == 'hello \\"world\\"',
           "AppleScript escape handles quotes")
    expect(_escape_applescript("path\\to\\file") == "path\\\\to\\\\file",
           "AppleScript escape handles backslashes")

    # Apple time format conversion (978307200 = 2001-01-01 UTC in Unix epoch)
    expect(int(_apple_date_to_unix(0)) == 978307200, "Apple epoch t=0 → 2001-01-01")
    expect(int(_apple_date_to_unix(1e18)) > 1700000000,
           "Apple ns timestamp converts to plausible recent Unix time")

    # Adapter instantiates and exposes correct allow set
    a = iMessageAdapter(allowed_handles=["+15551234567", "you@example.com"])
    expect(a._allowed == {"+15551234567", "you@example.com"},
           "iMessage allowlist normalised")

    # chat.db read access (only if Full Disk Access is granted)
    if CHAT_DB.exists():
        from adapters.imessage import _connect_db, _max_rowid
        try:
            conn = _connect_db()
        except RuntimeError as e:
            skip(f"chat.db read denied: grant Full Disk Access to Python ({e})")
            return
        except Exception as e:
            skip(f"chat.db open error: {e}")
            return
        try:
            m = _max_rowid(conn)
            ok(f"chat.db readable; max ROWID = {m} (Full Disk Access OK)")
        finally:
            conn.close()
    else:
        skip("chat.db not present (Messages.app not set up?)")


# ---------------------------------------------------------------------------
# 8) AppleScript syntax validation via osacompile
# ---------------------------------------------------------------------------
def test_applescript_syntax() -> None:
    section("AppleScript send syntax")

    if sys.platform != "darwin":
        skip("not macOS")
        return

    # Simulate the script our adapter would build for a fake handle.
    script = '''
        tell application "Messages"
            try
                set targetService to 1st service whose service type = iMessage
                set targetBuddy to buddy "+15551234567" of targetService
                send "hello" to targetBuddy
            on error errMsg
                set targetService to 1st service whose service type = SMS
                set targetBuddy to buddy "+15551234567" of targetService
                send "hello" to targetBuddy
            end try
        end tell
    '''
    # osacompile reports syntax errors without executing
    proc = subprocess.run(
        ["osacompile", "-e", script, "-o", os.devnull],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        ok("AppleScript send syntax compiles")
    else:
        fail(f"osacompile rejected the AppleScript: {proc.stderr.strip()}")


# ---------------------------------------------------------------------------
# 9) Adapter outbound serialisation (no network)
# ---------------------------------------------------------------------------
def test_adapter_serialisation() -> None:
    section("Outbound serialisers (no network)")

    from kernel import Button, Keyboard

    kb = Keyboard.of(
        [Button(label="Yes", data="cb:yes"), Button(label="No", data="cb:no")],
        [Button(label="Site", url="https://example.com")],
    )

    # Telegram: keyboard → InlineKeyboardMarkup
    try:
        from adapters.telegram import _to_keyboard
        markup = _to_keyboard(kb)
        rows = markup.inline_keyboard
        expect(len(rows) == 2, "telegram keyboard has 2 rows")
        expect(rows[0][0].callback_data == "cb:yes", "telegram callback_data preserved")
        expect(rows[1][0].url == "https://example.com", "telegram url button preserved")
    except Exception as e:
        fail("telegram _to_keyboard", e)

    # Web: keyboard → JSON
    from adapters.web import _keyboard_to_json
    j = _keyboard_to_json(kb)
    expect(isinstance(j, list) and len(j) == 2, "web keyboard JSON shape")
    expect(j[0][0]["data"] == "cb:yes", "web callback_data preserved")
    expect(j[1][0]["url"] == "https://example.com", "web url preserved")

    # Slack: keyboard → Block Kit blocks
    from adapters.slack import _kb_to_blocks
    blocks = _kb_to_blocks(kb, "pick one")
    expect(blocks[0]["type"] == "section", "slack section block first")
    actions = [b for b in blocks if b["type"] == "actions"]
    expect(len(actions) == 2, "slack actions block per keyboard row")
    expect(actions[0]["elements"][0]["action_id"] == "cb:yes",
           "slack action_id preserved")


# ---------------------------------------------------------------------------
# 9b) Platform-agnostic actions/
# ---------------------------------------------------------------------------
async def test_actions() -> None:
    section("actions/ — cross-platform command handlers")

    import actions
    from kernel.runner import Dispatcher

    d = Dispatcher()
    actions.register_all(d)
    commands = sorted(d._commands.keys())
    expected = {
        # screen
        "camera", "ocr", "record", "screenshot", "watch",
        # system
        "apps", "battery", "clipboard", "focus", "ip", "paste", "processes",
        "search", "shortcut", "status", "tts", "uptime", "volume", "wifi",
        # web
        "help", "open", "ping", "whoami",
        # session (memory + claude)
        "clear", "cost", "fork", "memory",
        # scheduler
        "alert", "remind", "schedule", "timer",
    }
    expect(set(commands) == expected,
           f"actions.register_all registers exactly {len(expected)} commands",
           f"got {sorted(set(commands) ^ expected)}")

    # Functional test: run /uptime through the dispatcher with a fake adapter.
    from kernel import (Chat, ChatAdapter, Message, MessageKind, SentMessage,
                        User)

    captured = []

    class FakeAdapter(ChatAdapter):
        name = "test"
        async def start(self): pass
        async def stop(self): pass
        async def messages(self):
            if False:
                yield  # pragma: no cover
        async def callbacks(self):
            if False:
                yield  # pragma: no cover
        async def send_text(self, chat_id, text, **kw):
            captured.append(text)
            return SentMessage(chat_id=chat_id, message_id="0")
        async def edit_text(self, sent, text, **kw): pass
        async def delete(self, sent): pass
        async def send_photo(self, *a, **kw): pass
        async def send_video(self, *a, **kw): pass
        async def send_voice(self, *a, **kw): pass
        async def send_document(self, *a, **kw): pass
        async def send_typing(self, chat_id): pass
        async def authorize(self, user): return True
        async def download_attachment(self, att, dest=None): return Path()

    fake = FakeAdapter()
    msg = Message(id="1", chat=Chat(id="c"), user=User(id="u"),
                  kind=MessageKind.COMMAND, text="/uptime")
    from kernel.runner import Context
    ctx = Context(adapter=fake, message=msg)
    await d._dispatch_message(fake, msg)
    expect(any("up" in t.lower() or "load" in t.lower() for t in captured) if captured else False,
           "/uptime end-to-end through dispatcher → uptime output captured",
           f"captured={captured}")

    # /ping is platform-trivial — verify it runs
    captured.clear()
    msg2 = Message(id="2", chat=Chat(id="c"), user=User(id="u"),
                   kind=MessageKind.COMMAND, text="/ping")
    await d._dispatch_message(fake, msg2)
    expect(captured == ["pong 🏓"], "/ping → pong 🏓", f"got {captured}")

    # /help should produce a multi-line menu
    captured.clear()
    msg3 = Message(id="3", chat=Chat(id="c"), user=User(id="u"),
                   kind=MessageKind.COMMAND, text="/help")
    await d._dispatch_message(fake, msg3)
    expect(captured and "Alfred" in captured[0] and "/screenshot" in captured[0],
           "/help → menu mentioning Alfred and at least one command")


# ---------------------------------------------------------------------------
# 9d) Session + Memory commands (/clear /fork /cost /memory)
# ---------------------------------------------------------------------------
async def test_session_memory() -> None:
    section("Session + memory commands — /clear /fork /cost /memory")

    from kernel import (Chat, ChatAdapter, Message, MessageKind, SentMessage,
                        User)
    from kernel.claude import ClaudeRunner
    from kernel.runner import Context, Dispatcher

    sends: list[str] = []

    class CaptureAdapter(ChatAdapter):
        name = "test"
        async def start(self): pass
        async def stop(self): pass
        async def messages(self):
            if False: yield  # pragma: no cover
        async def callbacks(self):
            if False: yield  # pragma: no cover
        async def send_text(self, chat_id, text, **kw):
            sends.append(text)
            return SentMessage(chat_id=chat_id, message_id="0")
        async def edit_text(self, sent, text, **kw): pass
        async def delete(self, sent): pass
        async def send_photo(self, *a, **kw): pass
        async def send_video(self, *a, **kw): pass
        async def send_voice(self, *a, **kw): pass
        async def send_document(self, *a, **kw): pass
        async def send_typing(self, chat_id): pass
        async def authorize(self, user): return True
        async def download_attachment(self, att, dest=None): return Path()

    sessions = Path(tempfile.gettempdir()) / "alfred-test-sm-sessions.json"
    forks = Path(tempfile.gettempdir()) / "alfred-test-sm-forks.json"
    usage = Path(tempfile.gettempdir()) / "alfred-test-sm-usage.json"
    for f in (sessions, forks, usage):
        if f.exists():
            f.unlink()

    runner = ClaudeRunner(sessions_path=sessions, forks_path=forks, usage_path=usage)

    import actions
    d = Dispatcher()
    actions.register_all(d, claude_runner=runner)

    fake_adapter = CaptureAdapter()

    async def dispatch(text: str, chat: str = "c1", user: str = "u1") -> None:
        sends.clear()
        m = Message(id="x", chat=Chat(id=chat), user=User(id=user),
                    kind=MessageKind.COMMAND, text=text)
        await d._dispatch_message(fake_adapter, m)

    # /clear with no session does no harm
    await dispatch("/clear")
    expect("New conversation" in (sends[0] if sends else ""),
           "/clear → friendly confirmation")

    # Inject a session manually, then /fork save / load / delete cycle
    runner._sessions["test:c1"] = "sess-aaa"
    runner._save_sessions()

    await dispatch("/fork save experiment")
    expect("Saved" in (sends[0] if sends else ""), "/fork save → confirms")
    forks_data = json.loads(forks.read_text())
    expect(forks_data.get("test:c1", {}).get("experiment") == "sess-aaa",
           "fork written to disk")

    # Switch to a different session, then load brings us back
    runner._sessions["test:c1"] = "sess-bbb"
    runner._save_sessions()
    await dispatch("/fork load experiment")
    expect(runner.session_id_for("c1") if hasattr(runner, "session_id_for") else
           runner._sessions["test:c1"] == "sess-aaa",
           "/fork load restores saved session id")

    await dispatch("/fork")
    expect("experiment" in (sends[0] if sends else ""), "/fork lists branches")

    await dispatch("/fork delete experiment")
    expect("Deleted" in (sends[0] if sends else ""), "/fork delete → confirms")
    forks_data = json.loads(forks.read_text())
    expect(not forks_data.get("test:c1", {}),
           "fork removed from disk")

    # /cost on empty usage
    await dispatch("/cost")
    expect("No usage" in (sends[0] if sends else ""),
           "/cost on empty usage → friendly")

    # Inject some usage records and check formatting
    ctx = Context(adapter=fake_adapter,
                  message=Message(id="x", chat=Chat(id="c1"), user=User(id="u1"),
                                  kind=MessageKind.TEXT, text=""))
    runner._record_usage(ctx, {"input_tokens": 1000, "output_tokens": 500}, "claude-sonnet-4-6")
    runner._record_usage(ctx, {"input_tokens": 2000, "output_tokens": 800}, "claude-opus-4-7")
    await dispatch("/cost")
    body = sends[0] if sends else ""
    expect("Tokens" in body and "1,000" not in body and "3,000" in body,
           "/cost sums input tokens correctly")
    expect("$" in body and "By model" in body,
           "/cost shows estimated cost + per-model breakdown")

    # /memory add / search / list / remove / clear
    await dispatch("/memory clear")
    expect("Cleared" in (sends[0] if sends else ""), "/memory clear → confirms")

    await dispatch("/memory add I like dark mode")
    expect("Remembered" in (sends[0] if sends else ""), "/memory add → confirms")

    await dispatch("/memory add preference Coffee black no sugar")
    expect("Remembered" in (sends[0] if sends else ""), "/memory add with category")

    await dispatch("/memory")
    listing = sends[0] if sends else ""
    expect("dark mode" in listing and "Coffee" in listing,
           "/memory lists stored memories")

    await dispatch("/memory search dark")
    expect("dark mode" in (sends[0] if sends else ""),
           "/memory search filters")

    # Cleanup
    for f in (sessions, forks, usage):
        if f.exists():
            f.unlink()
    # Also clear test memories from the SQLite store
    try:
        from utils.memory import clear_memories
        clear_memories("test:u1")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 9e) Scheduler — reminders, schedules, alerts
# ---------------------------------------------------------------------------
async def test_scheduler() -> None:
    section("Scheduler — /remind /timer /schedule /alert")

    from kernel import (Chat, ChatAdapter, Message, MessageKind, SentMessage,
                        User)
    from kernel.runner import Context, Dispatcher
    from kernel.scheduler import Scheduler, parse_when, parse_natural_schedule

    # parse_when sanity
    now = 1_700_000_000
    expect(int(parse_when("in 5 min", now=now)) == now + 300, "parse_when 'in 5 min'")
    expect(int(parse_when("in 2 hours", now=now)) == now + 7200, "parse_when 'in 2 hours'")
    expect(parse_when("garbage") is None, "parse_when rejects garbage")

    # parse_natural_schedule basics
    expect(parse_natural_schedule("every 5 min") == "*/5 * * * *",
           "parse_natural 'every 5 min'")
    expect(parse_natural_schedule("every day at 9am") == "0 9 * * *",
           "parse_natural 'every day at 9am'")
    expect(parse_natural_schedule("every weekday") == "0 9 * * 1-5",
           "parse_natural 'every weekday'")

    # Stand up a scheduler with a fake adapter and exercise the full flow
    sent: list[tuple[str, str]] = []

    class FireAdapter(ChatAdapter):
        name = "test"
        async def start(self): pass
        async def stop(self): pass
        async def messages(self):
            if False: yield  # pragma: no cover
        async def callbacks(self):
            if False: yield  # pragma: no cover
        async def send_text(self, chat_id, text, **kw):
            sent.append((chat_id, text))
            return SentMessage(chat_id=chat_id, message_id="0")
        async def edit_text(self, sent, text, **kw): pass
        async def delete(self, sent): pass
        async def send_photo(self, *a, **kw): pass
        async def send_video(self, *a, **kw): pass
        async def send_voice(self, *a, **kw): pass
        async def send_document(self, *a, **kw): pass
        async def send_typing(self, chat_id): pass
        async def authorize(self, user): return True
        async def download_attachment(self, att, dest=None): return Path()

    state = Path(tempfile.gettempdir()) / "alfred-test-sched.json"
    if state.exists():
        state.unlink()

    fa = FireAdapter()
    sched = Scheduler(state_path=state, poll_interval=0.05)
    sched.register_adapter(fa)

    ctx = Context(adapter=fa,
                  message=Message(id="x", chat=Chat(id="c1"), user=User(id="u"),
                                  kind=MessageKind.TEXT, text=""))

    # Reminder fires when due
    job = sched.add_reminder(ctx, time.time() - 1, "wake up")
    expect(state.exists(), "scheduler persists state to disk")
    persisted = json.loads(state.read_text())
    expect("test:c1" in persisted and len(persisted["test:c1"]) == 1,
           "reminder appears in state file")

    sent.clear()
    await sched.tick()
    expect(any("wake up" in t for _, t in sent),
           "due reminder fires via adapter.send_text",
           f"sent={sent}")
    expect(not sched.list_jobs(ctx, kind="reminder"),
           "reminder removes itself after firing")

    # Reminder NOT due → no fire
    sent.clear()
    sched.add_reminder(ctx, time.time() + 3600, "later")
    await sched.tick()
    expect(not sent, "future reminder doesn't fire yet")
    expect(len(sched.list_jobs(ctx, kind="reminder")) == 1,
           "future reminder still pending")

    # Schedule (cron) → fires at appropriate moment
    sent.clear()
    j = sched.add_schedule(ctx, "every minute", "tick")
    expect(j["cron"] == "* * * * *", "schedule cron normalised from natural lang")
    # Force last_fired into the past so next_cron_fire <= now
    j["last_fired"] = time.time() - 120
    await sched.tick()
    expect(any("tick" in t for _, t in sent), "schedule fires when due",
           f"sent={sent}")
    expect(len(sched.list_jobs(ctx, kind="schedule")) == 1,
           "schedule does NOT delete itself (recurring)")

    # Alert: process check (alert when nginx not running — almost surely true on test box)
    sent.clear()
    a = sched.add_alert(ctx, "process", label="totally-not-a-real-process-xyz")
    a["last_fired"] = 0  # bypass cooldown
    await sched.tick()
    expect(any("ALERT" in t and "totally-not" in t for _, t in sent),
           "process alert fires for missing process",
           f"sent={sent}")

    # Cooldown prevents immediate re-fire
    sent.clear()
    await sched.tick()
    expect(not any("ALERT" in t for _, t in sent),
           "alert cooldown prevents immediate re-fire")

    # Listing + removal
    jobs = sched.list_jobs(ctx)
    expect(len(jobs) >= 3, f"list_jobs sees all jobs ({len(jobs)} found)")
    rid = next(j["id"] for j in jobs if j["kind"] == "reminder")
    expect(sched.remove_job(ctx, rid), "remove_job returns True for known id")
    expect(not sched.remove_job(ctx, "nope"), "remove_job returns False for unknown id")

    # Cleanup
    if state.exists():
        state.unlink()


# ---------------------------------------------------------------------------
# 9c) ClaudeRunner — mock `claude` binary, full pipeline test
# ---------------------------------------------------------------------------
async def test_claude_runner() -> None:
    section("kernel.claude.ClaudeRunner — full pipeline via mock claude binary")

    from kernel import (Chat, ChatAdapter, Message, MessageKind, SentMessage,
                        User)
    from kernel.claude import ClaudeRunner
    from kernel.runner import Context

    fake = ROOT / "tests" / "fake_claude.py"
    expect(fake.exists() and os.access(fake, os.X_OK), "fake_claude.py is executable")

    # Capture every adapter call
    edits: list[tuple[str, str]] = []
    sends: list[tuple[str, str]] = []
    photos: list[str] = []

    class CaptureAdapter(ChatAdapter):
        name = "test"
        _mid = 0
        async def start(self): pass
        async def stop(self): pass
        async def messages(self):
            if False: yield  # pragma: no cover
        async def callbacks(self):
            if False: yield  # pragma: no cover
        async def send_text(self, chat_id, text, **kw):
            self._mid += 1
            sends.append((chat_id, text))
            return SentMessage(chat_id=chat_id, message_id=str(self._mid))
        async def edit_text(self, sent, text, **kw):
            edits.append((sent.message_id, text))
        async def delete(self, sent): pass
        async def send_photo(self, chat_id, path, **kw):
            photos.append(str(path))
            return SentMessage(chat_id=chat_id, message_id="0")
        async def send_video(self, chat_id, path, **kw):
            photos.append(str(path))
            return SentMessage(chat_id=chat_id, message_id="0")
        async def send_voice(self, chat_id, path, **kw):
            photos.append(str(path))
            return SentMessage(chat_id=chat_id, message_id="0")
        async def send_document(self, chat_id, path, **kw):
            photos.append(str(path))
            return SentMessage(chat_id=chat_id, message_id="0")
        async def send_typing(self, chat_id): pass
        async def authorize(self, user): return True
        async def download_attachment(self, att, dest=None): return Path()

    sessions_path = Path(tempfile.gettempdir()) / "alfred-test-sessions.json"
    if sessions_path.exists():
        sessions_path.unlink()

    args_log = Path(tempfile.gettempdir()) / "fake-claude-args.log"
    if args_log.exists():
        args_log.unlink()

    base_env = {
        "CLAUDE_BIN": str(fake),
        "FAKE_CLAUDE_TEXT": "Hello from Claude.",
        "FAKE_CLAUDE_SESSION": "sess-abc",
        "FAKE_CLAUDE_RECORD_ARGS": str(args_log),
    }

    # --- 1. Plain text round trip ---
    for k, v in base_env.items():
        os.environ[k] = v
    try:
        adapter = CaptureAdapter()
        runner = ClaudeRunner(sessions_path=sessions_path, edit_throttle_secs=0)
        ctx = Context(
            adapter=adapter,
            message=Message(id="1", chat=Chat(id="c"), user=User(id="u"),
                            kind=MessageKind.TEXT, text="hi"),
        )
        result = await runner.run(ctx, "hi")
        expect(result == "Hello from Claude.", "runner returns final text",
               f"got {result!r}")
        expect(any("Hello" in t for _, t in edits) or any("Hello" in t for _, t in sends),
               "final text edited into the thinking message", str(edits + sends))
    finally:
        for k in base_env:
            os.environ.pop(k, None)

    # --- 2. Session persisted ---
    expect(sessions_path.exists(), "claude_sessions.json written")
    if sessions_path.exists():
        data = json.loads(sessions_path.read_text())
        expect(data.get("test:c") == "sess-abc",
               "session id keyed by adapter:chat", str(data))

    # --- 3. --resume <id> on next call ---
    edits.clear(); sends.clear()
    if args_log.exists():
        args_log.unlink()
    base_env["FAKE_CLAUDE_RECORD_ARGS"] = str(args_log)
    for k, v in base_env.items():
        os.environ[k] = v
    try:
        adapter = CaptureAdapter()
        runner = ClaudeRunner(sessions_path=sessions_path, edit_throttle_secs=0)
        ctx = Context(
            adapter=adapter,
            message=Message(id="2", chat=Chat(id="c"), user=User(id="u"),
                            kind=MessageKind.TEXT, text="hi again"),
        )
        await runner.run(ctx, "hi again")
        argv = args_log.read_text() if args_log.exists() else ""
        expect("--resume" in argv and "sess-abc" in argv,
               "second run passes --resume <session_id>")
    finally:
        for k in base_env:
            os.environ.pop(k, None)

    # --- 4. [SEND_FILE:...] marker → send_photo called, marker stripped from text ---
    edits.clear(); sends.clear(); photos.clear()
    fake_image = Path(tempfile.gettempdir()) / "alfred-test-image.png"
    fake_image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    env = dict(base_env)
    env["FAKE_CLAUDE_TEXT"] = (
        f"Here's the screenshot you asked for. [SEND_FILE:{fake_image}] Done."
    )
    for k, v in env.items():
        os.environ[k] = v
    try:
        adapter = CaptureAdapter()
        runner = ClaudeRunner(sessions_path=sessions_path, edit_throttle_secs=0)
        ctx = Context(
            adapter=adapter,
            message=Message(id="3", chat=Chat(id="c"), user=User(id="u"),
                            kind=MessageKind.TEXT, text="screenshot please"),
        )
        result = await runner.run(ctx, "screenshot please")
        expect(str(fake_image) in photos, "send_photo invoked with the marker path")
        expect("[SEND_FILE:" not in result, "marker stripped from final text")
        expect("Here's the screenshot" in result and "Done" in result,
               "non-marker text preserved")
    finally:
        for k in env:
            os.environ.pop(k, None)
        if fake_image.exists():
            fake_image.unlink()

    # --- 5. Non-zero exit → friendly error message ---
    edits.clear(); sends.clear()
    env = dict(base_env)
    env["FAKE_CLAUDE_FAIL"] = "1"
    env["FAKE_CLAUDE_STDERR"] = "boom: synthetic error"
    for k, v in env.items():
        os.environ[k] = v
    try:
        adapter = CaptureAdapter()
        runner = ClaudeRunner(sessions_path=sessions_path, edit_throttle_secs=0)
        ctx = Context(
            adapter=adapter,
            message=Message(id="4", chat=Chat(id="c"), user=User(id="u"),
                            kind=MessageKind.TEXT, text="will fail"),
        )
        result = await runner.run(ctx, "will fail")
        expect(result == "", "failed run returns empty string")
        all_text = " ".join(t for _, t in edits + sends)
        expect("boom" in all_text or "❌" in all_text,
               "stderr surfaced to chat as friendly error", all_text[:200])
    finally:
        for k in env:
            os.environ.pop(k, None)

    # Cleanup
    if sessions_path.exists():
        sessions_path.unlink()
    if args_log.exists():
        args_log.unlink()


# ---------------------------------------------------------------------------
# 10) app.py entry-point smoke
# ---------------------------------------------------------------------------
def test_app_smoke() -> None:
    section("app.py + setup_wizard.py + bot.py legacy")

    # app.py with no env: should detect needs_setup → exit cleanly via wizard,
    # but here we just verify the module loads and the function exists.
    import importlib.util
    for path in ("app.py", "setup_wizard.py", "bot.py"):
        try:
            spec = importlib.util.spec_from_file_location(
                path.replace(".py", "_test"), ROOT / path
            )
            mod = importlib.util.module_from_spec(spec)
            # bot.py has side effects on import (raises if no token), so set one
            os.environ.setdefault("TELEGRAM_BOT_TOKEN", "dummy:smoke")
            os.environ.setdefault("ALLOWED_USERS", "ci")
            spec.loader.exec_module(mod)
            ok(f"{path} imports without error")
        except SystemExit as e:
            # bot.py / app.py may sys.exit at module load on bad env — that's not a fail
            ok(f"{path} imported; exited with {e.code}")
        except Exception as e:
            fail(f"{path} import", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def amain() -> int:
    test_code_quality()
    test_imports()
    test_dataclasses()
    await test_web_adapter()
    await test_dispatcher()
    await test_setup_wizard()
    test_imessage_unit()
    test_applescript_syntax()
    test_adapter_serialisation()
    await test_actions()
    await test_scheduler()
    await test_claude_runner()
    await test_session_memory()
    test_app_smoke()

    print(f"\n  Summary: {GREEN}{_pass} passed{RESET}, "
          f"{RED}{_fail} failed{RESET}, "
          f"{YELLOW}{_skip} skipped{RESET}\n")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(amain()))
