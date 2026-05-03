# Writing your own command

Alfred's `actions/` package is where slash commands live. Every module is platform-agnostic — anything you write here works on Telegram, Web, Discord, Slack, and iMessage automatically.

## Hello world

Create `actions/myfeature.py`:

```python
from kernel.runner import Context

async def cmd_hello(ctx: Context) -> None:
    await ctx.reply(f"hello {ctx.user.display_name or '?'} from {ctx.adapter.name}")

def register(dispatcher) -> None:
    dispatcher.command("hello", cmd_hello)
```

Then add to `actions/__init__.py`:

```python
from . import ..., myfeature

def register_all(dispatcher, **kw):
    ...
    myfeature.register(dispatcher)
```

`python3 app.py`. Send `/hello` from any chat. That's it.

## What's on `Context`

```python
ctx.adapter            # the ChatAdapter that delivered this message
ctx.message            # kernel.Message (or None for callback handlers)
ctx.callback           # kernel.CallbackPress (or None for message handlers)
ctx.chat_id            # str — platform-specific chat id
ctx.user               # kernel.User — id, username, display_name, is_bot

await ctx.reply(text, *, keyboard=None, parse_mode=None)
                       # convenience: send_text + reply_to=msg.id
```

## Sending media

Every `ChatAdapter` exposes:

```python
await ctx.adapter.send_photo(ctx.chat_id, "/path/or/Pathlike", caption="…")
await ctx.adapter.send_video(ctx.chat_id, path, caption="…")
await ctx.adapter.send_voice(ctx.chat_id, path)
await ctx.adapter.send_document(ctx.chat_id, path, filename="…", caption="…")
await ctx.adapter.send_typing(ctx.chat_id)
```

These work the same on every platform. Telegram and Discord upload natively; the Web adapter inlines small photos as `data:` URLs and serves larger files via a per-token route.

## Inline buttons

```python
from kernel import Button, Keyboard

kb = Keyboard.of(
    [Button("Yes", data="cb:yes"), Button("No", data="cb:no")],
    [Button("Open docs", url="https://example.com/docs")],
)
await ctx.reply("pick one:", keyboard=kb)
```

The dispatcher routes the press to your callback handler:

```python
async def on_press(ctx):
    cb = ctx.callback
    await ctx.adapter.send_text(ctx.chat_id, f"you picked {cb.data}")

def register(dispatcher):
    ...
    dispatcher.callback_prefix("cb:", on_press)
```

`dispatcher.callback_prefix(prefix, handler)` is the usual pattern — you encode any data you need after the prefix.

## Reading attachments

When the user sends a photo/voice/document, the message arrives with `attachments`:

```python
async def cmd_describe(ctx):
    msg = ctx.message
    if not msg or not msg.attachments:
        await ctx.reply("send me a photo first")
        return
    att = msg.attachments[0]
    local = att.local_path or await ctx.adapter.download_attachment(att)
    # `local` is a real path on disk you can read
    ...
```

iMessage attachments arrive with `local_path` already set (Messages.app stores them on disk). Telegram/Discord/Slack require `download_attachment` to actually fetch.

## Long-running tasks

Background tasks should be cancellable on shutdown. Pattern:

```python
import asyncio

_tasks: dict[str, asyncio.Task] = {}

async def cmd_watch(ctx):
    chat_id = ctx.chat_id
    if chat_id in _tasks and not _tasks[chat_id].done():
        _tasks[chat_id].cancel()
        _tasks.pop(chat_id)
        await ctx.reply("stopped")
        return

    async def loop():
        try:
            while True:
                await asyncio.sleep(5)
                await ctx.adapter.send_text(chat_id, "tick")
        except asyncio.CancelledError:
            pass

    _tasks[chat_id] = asyncio.create_task(loop(), name=f"watch-{chat_id}")
    await ctx.reply("started — run /watch again to stop")
```

Look at `actions/screen.py:cmd_watch` for the production version.

## Persistent state

The simplest way: use `kernel.store`:

```python
from kernel.store import db_load, db_save

async def cmd_set(ctx):
    text = ctx.message.command_args
    db_save(f"myfeature:{ctx.user.id}", {"value": text})

async def cmd_get(ctx):
    data = db_load(f"myfeature:{ctx.user.id}", default={"value": "(unset)"})
    await ctx.reply(data["value"])
```

Or for a JSON file alongside the bot:

```python
from pathlib import Path
import json

STATE = Path(__file__).parent.parent / "myfeature_state.json"
```

Add the state file to `.gitignore` so it doesn't get committed.

## Talking to Claude from your handler

```python
def register(dispatcher, claude_runner=None):
    _RUNNER["x"] = claude_runner
    dispatcher.command("askit", cmd_askit)

async def cmd_askit(ctx):
    runner = _RUNNER["x"]
    if not runner:
        return
    await runner.run(ctx, "summarise the last 10 git commits")
```

`actions/__init__.py` shows how `register_all` forwards `claude_runner` to handlers that need it.

## Don't do these

- **Don't `import telegram`** (or `discord`, `slack_bolt`, …) in `actions/`. The whole point is that handlers are adapter-agnostic. If you need a platform-specific thing, drop it into `adapters/<name>.py` instead.
- **Don't block** the event loop with sync I/O. Use `asyncio.subprocess` instead of `subprocess`, `asyncio.to_thread` for sync code you can't avoid, and `aiohttp` for HTTP.
- **Don't leak credentials** into responses. If your handler uses an API key, don't echo it back.

## Running tests

`tests/test_all.py` exercises every action. Add a small section there for any non-trivial handler:

```python
async def test_my_feature():
    section("My feature")
    # build a fake adapter, dispatch a Message, assert what got sent
```

Look at `test_session_memory` for the full pattern.

## Where to look in the codebase

- `actions/web.py` — the simplest examples (`/ping`, `/whoami`)
- `actions/system.py` — most varied — shell-out, parsing, edge cases
- `actions/menu.py` — inline-button menus with sub-menus
- `actions/scheduler.py` — uses a shared kernel service via `_SHARED` dict
- `actions/research.py` — calls the Anthropic SDK directly, parallel `asyncio.gather`
- `actions/web_browse.py` — uses the kernel browser pool
