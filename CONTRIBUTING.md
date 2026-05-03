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

The repo is mid-refactor from a Telegram-only bot to a multi-adapter design:

```
kernel/                        ← platform-agnostic types
  messages.py                  ← Message, User, Chat, Attachment, CallbackPress
  buttons.py                   ← Button, Keyboard
  adapter.py                   ← ChatAdapter abstract base class
  runner.py                    ← Dispatcher + Context

adapters/                      ← concrete chat platform integrations
  telegram.py                  ← wraps python-telegram-bot
  web.py                       ← browser chat at http://localhost:8765

app.py                         ← new multi-adapter entry point
setup_wizard.py                ← first-run browser configuration

bot.py + handlers.py +
webhook.py + commands/*.py +
core.py                        ← legacy Telegram-only code (still the
                                 production path; ported into kernel/
                                 + adapters/ incrementally)
```

When adding a new feature today:

- If it's platform-specific Telegram behaviour → land it in `bot.py` /
  `handlers.py` / `commands/`.
- If it's platform-agnostic (Claude pipeline, scheduling, alerts, plugins) →
  land it in `kernel/` and have the legacy code call into it.
- New chat platforms → add an `adapters/<name>.py` implementing
  `kernel.ChatAdapter`.

## What I welcome

- Bug fixes with reproducers.
- Small, focused PRs (one concern per PR).
- New adapters (Discord, Slack, Matrix, ...).
- Porting commands from the legacy code into the kernel/dispatcher pattern.
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
