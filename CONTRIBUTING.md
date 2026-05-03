# Contributing to Alfred

Thanks for taking the time to look! Alfred is a self-hosted side project, and the goal of the public repo is for other people to be able to run it on their own Macs without changes. PRs that move us toward that goal are very welcome.

## Quick start for contributors

```bash
git clone https://github.com/TiGi-cloud/alfred-assistant.git
cd alfred-assistant
./install.sh                 # creates venv, installs deps, opens setup wizard
# OR for a manual setup:
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 app.py --setup
```

## Architecture overview

```
kernel/                        ← platform-agnostic core
  messages.py                  ← Message, User, Chat, Attachment, CallbackPress
  buttons.py                   ← Button, Keyboard
  adapter.py                   ← ChatAdapter abstract base class
  runner.py                    ← Dispatcher + Context
  claude.py                    ← Claude pipeline (subprocess + stream-json)
  scheduler.py                 ← reminders + cron + threshold alerts
  machines.py                  ← SSH targets + Wake-on-LAN
  projects.py                  ← per-user named projects (cwd + env + model)
  browser.py                   ← Playwright pool (headless Chromium)
  store.py                     ← SQLite KV + persistent memory

adapters/                      ← concrete chat platform integrations
  telegram.py                  ← python-telegram-bot
  web.py                       ← browser chat at http://localhost:8765
  discord.py                   ← discord.py (optional dep)
  slack.py                     ← slack-bolt async, Socket Mode (optional dep)
  imessage.py                  ← macOS Messages.app (chat.db + AppleScript)

actions/                       ← platform-agnostic command handlers
  screen.py system.py web.py memory.py session.py scheduler.py
  machines.py projects.py menu.py notifications.py research.py
  gmail.py web_browse.py

app.py                         ← single entry point — wires everything up
setup_wizard.py                ← first-run browser configuration
```

When adding a feature:

- Pure platform-agnostic logic (Claude, scheduling, storage) → `kernel/`
- Slash command implementation → `actions/<topic>.py` exporting `register(d)`
- New chat platform → `adapters/<name>.py` implementing `kernel.ChatAdapter`

## What I welcome

- Bug fixes with reproducers.
- Small, focused PRs (one concern per PR).
- New adapters (Matrix, Discord webhooks, IRC, ...).
- New `actions/` modules — anything that can run from `Context`.
- Cross-platform improvements (Linux paths, Docker support, ...).
- Documentation, examples, screenshots.

## What I'd push back on

- Hardcoded paths, IPs, container names, or business logic. The repo was
  scrubbed of the original author's infrastructure prior to going public —
  please keep it that way. Use `USER_CONTEXT.md` (loaded at runtime) for
  personal infrastructure context.
- Backwards-compatibility shims for hypothetical users.
- Big rewrites that aren't paired with tests or a clear migration story.
- Style-only churn.

## Style

- Python 3.11+, type hints where they don't add noise.
- Two blank lines between top-level functions; one inside classes.
- Comments only when the *why* is non-obvious — don't restate the code.
- No emojis in source files unless they're part of UI text.

## Testing

There is no test suite yet (PRs adding one are welcome). At minimum, before submitting:

```bash
python3 -m py_compile $(git ls-files '*.py')   # syntax check
python3 -c "import kernel; import adapters.telegram; import adapters.web; import app"
```

If you change a command or adapter behaviour, run the bot locally against your own Telegram bot and confirm the affected flow still works.

## Filing issues

For bugs: please use the bug report template. The most useful issues include:

- The exact command / interaction that triggered the bug.
- The relevant slice of `alfred.log`.
- Your macOS version (`sw_vers`) and `python3 --version`.

For features: open an issue first if it's bigger than a one-liner — easier to discuss the shape before code.

## License

By contributing you agree your contributions are licensed under the [MIT License](./LICENSE).
