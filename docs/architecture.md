# Architecture

Three layers, every dependency points downward.

```
                   ┌─────────────────────────────────┐
                   │           app.py                │   single entry point
                   └──────────────┬──────────────────┘
                                  │
                   ┌──────────────┴──────────────────┐
                   │           actions/              │   slash-command handlers
                   │   13 modules · 39 commands      │   Context → adapter calls
                   └──────────────┬──────────────────┘
                                  │
                   ┌──────────────┴──────────────────┐
                   │           kernel/               │   abstract types + services
                   │   ChatAdapter, Dispatcher,      │   no platform-specific code
                   │   ClaudeRunner, Scheduler,      │
                   │   Projects, Machines, Browser   │
                   │   store (SQLite KV + memory)    │
                   └──────────────┬──────────────────┘
                                  │
                   ┌──────────────┴──────────────────┐
                   │           adapters/             │   one per chat platform
                   │   telegram · web · discord      │   converts native ↔ kernel
                   │   slack · imessage              │
                   └─────────────────────────────────┘
```

## Layer 1: kernel/

Everything platform-agnostic. Adapters depend on this; this depends on nothing in the project.

| Module | What |
|---|---|
| `messages.py` | `Message`, `User`, `Chat`, `Attachment`, `CallbackPress`, `Location`, `MessageKind` |
| `buttons.py` | `Button`, `Keyboard`, `InlineRow` |
| `adapter.py` | Abstract `ChatAdapter` base class with 14 methods every platform implements |
| `runner.py` | `Dispatcher` (routes commands + callbacks) + `Context` (handler argument) |
| `claude.py` | `ClaudeRunner` — drives `claude -p`, streams output, handles `[SEND_FILE:]`, `[BROWSE:]`, `[REMEMBER:]` markers, persists per-chat sessions, tracks token usage |
| `scheduler.py` | `Scheduler` — reminders + cron + threshold alerts, polls every 30s, fires via adapter registry |
| `machines.py` | `MachineRegistry` — SSH targets + `send_wol` + per-user active pointer |
| `projects.py` | `ProjectRegistry` — per-user named projects (cwd + env + model); ClaudeRunner reads this |
| `browser.py` | `BrowserPool` — Playwright Chromium; one shared browser, one context per chat |
| `store.py` | SQLite KV (`db_load`/`db_save`) + persistent memory functions |
| `metrics.py` | `MetricsCollector` — async polling of Mac CPU/memory/disk every 60s, persisted to `alfred_metrics.json`; powers the dashboard's sparklines |
| `branding.py` | Single source of truth for the logo + accent colors |

Hard rule: **nothing in `kernel/` may `import telegram`, `discord`, `slack_bolt`, or any other platform SDK.** If you find yourself wanting to, the right place is `adapters/`.

## Layer 2: adapters/

One module per chat platform. Each implements `kernel.ChatAdapter`. Translates platform-native objects to/from kernel types.

| File | Library | Notes |
|---|---|---|
| `telegram.py` | `python-telegram-bot` | required core dep |
| `web.py` | `aiohttp` | three things: inline chat HTML at `/`, dashboard Mini App at `/dashboard`, JSON API at `/api/*` powering the dashboard |
| `discord.py` | `discord.py` (optional) | DMs + servers |
| `slack.py` | `slack-bolt` (optional) | Socket Mode, no public URL needed |
| `imessage.py` | stdlib | macOS-only; reads `chat.db`, sends via AppleScript |

The lazy-import pattern: third-party libs are `import`ed inside `start()`/`__init__`, not at module load. So `import adapters.discord` works without `discord.py` installed; only `start()` would raise.

Adapters don't know about Claude, scheduling, projects, etc. They only know how to:

- listen for events on their platform
- convert events → `kernel.Message` / `kernel.CallbackPress`
- accept outbound calls (`send_text`, `send_photo`, `edit_text`, …)
- authorize users (their own scheme — usernames, snowflakes, member IDs)
- download attachments

## Layer 3: actions/

Slash-command handlers. Each module exports `register(dispatcher, ...)` to wire its commands.

```python
# actions/myfeature.py
from kernel.runner import Context

async def cmd_thing(ctx: Context) -> None:
    # ctx.adapter, ctx.message, ctx.chat_id, ctx.user
    await ctx.reply("done")

def register(dispatcher) -> None:
    dispatcher.command("thing", cmd_thing)
```

The handler receives a `Context`. It calls `ctx.adapter.send_text(...)` etc. — never directly imports `telegram`/`discord`/etc. That's how the same code drives every platform.

## Layer 0: app.py

Single entry point. ~270 lines. Builds adapters from env vars, wires them into the dispatcher + scheduler + project + machine + notification + metrics registries, starts everything, blocks on SIGINT, shuts down cleanly.

```
adapters → dispatcher → actions
                ↑
        ClaudeRunner ← projects + memory
                ↓
            Scheduler → adapters (out-of-band fire)
            MetricsCollector → WebAdapter (/api/metrics)
        BrowserPool
        NotificationWatcher
```

The WebAdapter is wired with optional refs to `claude_runner`, `scheduler`, `machines_registry`, `metrics_collector`, and `dispatcher`, so its `/api/*` endpoints can read kernel state for the dashboard. Without these refs the adapter still serves chat — the dashboard endpoints just degrade to empty / stub responses.

## Data flow: a typical message

1. User types in Telegram → `python-telegram-bot` → `MessageHandler` → `TelegramAdapter._handle_message`
2. `TelegramAdapter` converts `telegram.Update` → `kernel.Message` → puts on internal queue
3. `Dispatcher.run(adapter)` async-iters `adapter.messages()`, picks the message off the queue
4. `Dispatcher` calls `adapter.authorize(user)` first (each adapter has its own allowlist)
5. If a command (`/foo`), looks up `actions/`-registered handler; otherwise calls `default_handler` (= `app.py:default_text` → `ClaudeRunner.run`)
6. Handler does its thing — possibly calls `ctx.adapter.send_text()` / `send_photo()` / etc.
7. Adapter converts kernel call back to platform API call (`bot.send_message`, `channel.send`, etc.)
8. Reply lands in user's chat client

Same flow for every platform. The dispatcher fans out across all adapters concurrently — each adapter has its own background task reading from its own queue.

## Persistence

| File | Owner | What |
|---|---|---|
| `alfred.db` | `kernel/store.py` | SQLite KV — long-term memory + arbitrary key/value |
| `claude_sessions.json` | `ClaudeRunner` | active session per `(adapter, chat)` |
| `claude_forks.json` | `ClaudeRunner` | named branches per chat |
| `claude_usage.json` | `ClaudeRunner` | token-usage history per chat |
| `alfred_scheduler.json` | `Scheduler` | reminders + schedules + alerts |
| `alfred_machines.json` | `MachineRegistry` | SSH targets + active per user |
| `alfred_projects.json` | `ProjectRegistry` | named projects + active per user |
| `alfred_notifications.json` | `NotificationWatcher` | toggle state per chat |
| `alfred_metrics.json` | `MetricsCollector` | last 1440 CPU/memory/disk samples (24h at 1m) |

All gitignored. Live next to `app.py` by default; pass explicit `state_path=Path(...)` to override (used by tests).

## Tests

`tests/test_all.py` — 161+ checks, no live network, no API tokens. Boots a fake `claude` binary via `tests/fake_claude.py` to exercise the streaming pipeline. Live chat-platform testing is per-platform via `test_telegram.py`, `test_slack.py`, `test_imessage.py`.

CI runs the suite on Python 3.11 + 3.12 against ruff (`E9`, `F63`, `F7`, `F82`).

## Adding a new chat platform

1. Create `adapters/foo.py` subclassing `kernel.ChatAdapter`
2. Implement the 14 abstract methods (lifecycle, inbound iterators, outbound text + media, presence, auth, download)
3. Add lazy `_import_foo_sdk()` if it has a third-party dep
4. In `app.py:_build_adapters()`, look for the env var that enables it and append to the list
5. In `setup_wizard.py`, add a card for it
6. Tests: import-only checks land for free in `test_all.test_imports`

That's the whole change. The kernel doesn't care; actions don't know.

## Adding a new command

1. Create `actions/foo.py` with `async def cmd_foo(ctx)` and `def register(d): d.command("foo", cmd_foo)`
2. Add the import to `actions/__init__.py` and call its `register()` in `register_all()`
3. Optionally update `actions/web.py:cmd_help` to mention it

That's it. Works on every adapter immediately.
