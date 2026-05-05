# AGENTS.md — instructions for AI assistants working in this repo

This file is for AI agents (Claude Code, Cursor, Codex, Aider, OpenAI assistants, etc.) editing this codebase. Humans should read [docs/architecture.md](docs/architecture.md) and [docs/plugins.md](docs/plugins.md) instead.

If you are an AI agent: **start here, then look at [docs/architecture.md](docs/architecture.md) for the deeper picture.** Keep responses to the user terse — that's the project's voice.

---

## What this project is

**Alfred** is a self-hosted personal assistant for macOS. The user (`TiGi-cloud`) wraps the [Claude Code CLI](https://claude.com/claude-code) and exposes it over five chat platforms: Telegram, Discord, Slack, iMessage, and a local browser chat. Same conversation memory wherever the user addresses Alfred.

The bot is named after **Alfred Pennyworth** (Batman's butler). Voice = competent butler. Brief, polite, dry. Do the work; don't narrate it. **No preamble, no "let me think about that", no apologising.** When you write replies the bot will send to chat, match this voice.

## The three layers (every dependency points downward)

```
actions/      — slash-command handlers (39 commands across 14 modules)
                each works on every adapter; uses kernel.runner.Context
adapters/     — one module per chat platform (5: telegram, web, discord, slack, imessage)
                converts native messages ↔ kernel types
kernel/       — platform-agnostic types + services (10 modules)
                messages, buttons, adapter, runner, claude, scheduler,
                machines, projects, browser, store, branding
app.py        — single entry point; wires everything
```

**Hard rule for layer purity**: nothing in `kernel/` may import `telegram`, `discord`, `slack_bolt`, etc. Platform SDKs only live in `adapters/`. Action handlers (`actions/`) call `ctx.adapter.send_text()` / `send_photo()` etc. — never `import telegram` directly.

## Where to put new things

| If you're adding… | Put it in… |
|---|---|
| A new slash command (`/foo`) | `actions/<topic>.py` exporting `register(dispatcher)` |
| A new chat platform | `adapters/<name>.py` subclassing `kernel.ChatAdapter` (14 abstract methods) |
| A new platform-agnostic capability (Claude pipeline tweak, scheduler, store) | `kernel/<module>.py` |
| A new persistent state file | `kernel/store.py` for KV; or its own JSON file with `Path` constants in the relevant module; **always add to `.gitignore`** |
| A new test | `tests/test_all.py` — a new section with `expect(...)` assertions |
| Per-platform setup docs | `docs/setup/<platform>.md` |

## Conventions you must follow

### Code

- **Python 3.11+**. Type hints where they don't add noise. `from __future__ import annotations` at top of every new file.
- **Async first**. Use `asyncio.create_subprocess_exec`, never `subprocess.run`. Use `asyncio.to_thread` only for sync code you can't avoid (sqlite, AppleScript wrappers, etc.).
- **Two blank lines between top-level functions**, one inside classes.
- **Comments only when the *why* is non-obvious.** Don't restate the code. Don't reference "the X port" or "after we did Y" — those rot.
- **No emojis in source**, except in UI text the bot will send to chat.
- **Lazy-import optional deps**. `discord.py`, `slack-bolt`, `playwright`, `anthropic` are all optional. Import them inside `start()` / first use, raise a friendly `RuntimeError` with the `pip install …` line if missing.

### Action handlers

```python
# actions/myfeature.py
from kernel.runner import Context

async def cmd_thing(ctx: Context) -> None:
    """One-line docstring. Args go in /help via the docstring style.

    Usage:
        /thing [args]
    """
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""
    # ... do the work ...
    await ctx.reply("done.")  # ctx.reply = send_text + reply_to=msg.id

def register(dispatcher) -> None:
    dispatcher.command("thing", cmd_thing)
```

Then in `actions/__init__.py`:

```python
from . import myfeature
# in register_all:
myfeature.register(dispatcher)
```

If your handler needs a kernel service (Claude runner, scheduler, machine registry, project registry), accept it via `register(d, runner=None)` and store it in a module-level `_SHARED` dict. See `actions/session.py` for the pattern.

### Adapters

Implement all 14 abstract methods on `kernel.ChatAdapter`. Lazy-import the SDK. Convert native objects → `kernel.Message` / `kernel.CallbackPress`. Authorise users via your own scheme — pass an `allowed_*` iterable in `__init__`. Look at `adapters/telegram.py` (most complete) and `adapters/imessage.py` (most exotic, polls SQLite + AppleScript send) for templates.

### Persistence

- KV store: `kernel.store.db_load(key, default)` / `db_save(key, value)` — backed by SQLite at `alfred.db`.
- JSON files: each module owns its own (e.g. `claude_sessions.json`, `alfred_scheduler.json`). Always set `state_path: Optional[Path] = None` constructor param so tests can override.
- **Anything you add to disk goes in `.gitignore`.** Verify before committing.

### Errors

- Don't `try/except: pass` to swallow errors silently. If you want to swallow, log it: `logger.exception("…")`. The user-facing layer (chat reply) gets a friendly summary.
- Friendly errors over stack traces. `RuntimeError("Could not find `claude` CLI. Install from https://claude.com/claude-code or set CLAUDE_BIN.")` not `FileNotFoundError`.

## The bot's voice (when writing chat replies)

Read this carefully — the user cares about tone:

- **Brief.** "done." beats "Sure, I'll go ahead and do that for you right away!"
- **Dry.** No exclamation marks unless something genuinely warrants alarm.
- **No preamble.** Never start with "Sure", "Of course", "Let me…".
- **No filler.** Never write "I hope that helps" or "Let me know if you need anything else".
- **Confirm before destructive ops.** "This will delete 47 files. Reply YES to proceed."
- **Match the question's size.** Small question → small answer. Don't pad.

Compare:
- ❌ "Sure! I went ahead and took a screenshot for you. Here it is! Let me know if you'd like me to do anything else."
- ✅ "📸 [photo]"

The system prompt in `kernel/claude.py:DEFAULT_SYSTEM_PROMPT` reinforces this. Don't weaken it.

## Testing

- Run **`python3 tests/test_all.py`** before any commit. Should print `XXX passed, 0 failed`.
- Run **`python3 -m ruff check --select=E9,F63,F7,F82,F401,F841,B007 kernel/ adapters/ actions/ app.py setup_wizard.py`**. Should print `All checks passed!`.
- For new features that add commands, add a section to `tests/test_all.py:test_actions` so the registered command count test catches drift.
- For new kernel services, write a section like `test_scheduler` / `test_machines` that uses a fake `ChatAdapter` to drive end-to-end behaviour.
- Live testing of chat platforms uses `test_telegram.py` / `test_slack.py` / `test_imessage.py` (manual runs only — never in CI).

CI runs the same offline suite on Python 3.11 + 3.12 against ruff. **If your change breaks CI, fix the change — don't disable the check.**

## Things that will get a PR rejected

- **Telegram-specific imports in `kernel/` or `actions/`.**
- **Hardcoded paths** (`/Users/anyone/...`, `/home/...`). Use `Path.home()` or env vars.
- **Hardcoded IPs / hostnames / personal data.** The repo was scrubbed of the original author's infrastructure; keep it generic.
- **Backwards-compatibility shims** for hypothetical users. There aren't any yet.
- **Dead code / unused imports / unused vars.** Ruff will catch most; PR review catches the rest.
- **Re-introducing the legacy structure** — `bot.py`, `handlers.py`, `commands/`, `utils/`, `core.py`, `config.py`, `db.py`, `webhook.py`, `background.py` are deliberately gone. Don't recreate them.
- **Personality theatre in chat replies** (see "voice" above).
- **Committing state files** — `alfred.db`, `*.log`, `claude_*.json`, `alfred_*.json`, `.env`, `__pycache__/`. Check `git status -s` before `git commit`.

## When you're modifying for the user

The user is on macOS. They speak quickly and may type with shortcuts ("ad" for "add", "cause" for "because"). Don't correct their typos; understand the intent and execute.

When the user asks "is X done?" or "are you sure?" — **actually verify**. Run a command, read the file, query the API. Don't say "yes" without a check; the user has been burned by AI confidence before.

When you finish a unit of work, end with:
- One sentence summary of what changed
- One sentence about what's next, or "ready when you are"

Don't use headers like "Summary" / "Next steps" — the prose IS the summary.

## Quick reference

| Task | Command |
|---|---|
| Run tests | `python3 tests/test_all.py` |
| Run lint | `python3 -m ruff check --select=E9,F63,F7,F82,F401,F841,B007 kernel/ adapters/ actions/ app.py setup_wizard.py` |
| Run app | `python3 app.py` |
| Open setup wizard | `python3 app.py --setup` |
| Find a command's implementation | `grep -rn 'dispatcher.command("foo"' actions/` |
| Find an adapter's quirk | `adapters/<name>.py` is self-contained — read top to bottom |
| Add a new platform-agnostic state | `kernel/store.py` (`db_load` / `db_save`) |

## Don't ask for clarification on these

- "Make it shorter" → tighten the prose, drop the filler.
- "Polish" → ruff clean, docstrings updated, voice consistent, `git status` clean.
- "Test it" → `python3 tests/test_all.py` and report the summary line.
- "Push" → `git status` to verify clean, then `git push`. Confirm landed via `gh run list`.

If something is genuinely ambiguous, ask one targeted question, not five. The user is busy.

## Final guidance

Read [docs/architecture.md](docs/architecture.md) for the why behind the layering. Read [docs/plugins.md](docs/plugins.md) for the full template + helpers. Read [docs/security.md](docs/security.md) before doing anything that could affect the bot's auth or shell access.

Then make the change, run the tests, write the commit, and report the result in one or two sentences. That's the loop.
