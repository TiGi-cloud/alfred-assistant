# 🎩 Alfred

Self-hosted Mac assistant powered by [Claude Code](https://claude.com/claude-code). Talk to your Mac from Telegram, Discord, Slack, iMessage, or a local browser chat — same commands, same memory, anywhere.

```
You:    take a screenshot, OCR it, and tell me what apps I have open
Alfred: 📸 [photo arrives in chat]
        OCR text: …
        Open apps: Safari, Code, Music, Terminal, Slack
```

**Why Alfred:** wraps Claude Code's full agentic toolset (Bash, FileSystem, MCP servers, web fetch, …) and surfaces it through five chat platforms. Mac-native — Vision OCR, AppleScript app control, iMessage via `chat.db`, macOS Notification Center forwarding all built in.

> ⚠️ **Self-hosted only.** Alfred runs shell commands on the host with no sandbox. **Do not host this for other people.** Each person should run their own Alfred on their own Mac.

## Quick start

```bash
git clone https://github.com/TiGi-cloud/alfred-assistant.git
cd alfred-assistant
./install.sh                          # opens setup wizard at localhost:8080
```

Pick a chat platform in the wizard, click Save, then `python3 app.py`. **Five-minute walkthrough →** [docs/quickstart.md](docs/quickstart.md).

## Chat platforms

| Platform | Setup guide | Notes |
|---|---|---|
| **Telegram** | [setup/telegram.md](docs/setup/telegram.md) | Free bot token from @BotFather. Recommended starting point. |
| **Web (browser)** | [setup/web.md](docs/setup/web.md) | Built in — `http://localhost:8765`. No external account. |
| **Discord** | [setup/discord.md](docs/setup/discord.md) | Free bot. `pip install 'discord.py>=2.4'`. |
| **Slack** | [setup/slack.md](docs/setup/slack.md) | Free Slack app, Socket Mode. `pip install 'slack-bolt>=1.18'`. |
| **iMessage** | [setup/imessage.md](docs/setup/imessage.md) | macOS only. Polls `chat.db` + AppleScript send. 1:1 chats. |

Run any combination at once.

## What Alfred can do

**39 commands across every adapter** (full reference: [docs/commands.md](docs/commands.md))

| | |
|---|---|
| 📸 Screen | `/screenshot` `/record` `/watch` `/camera` `/ocr` |
| 🖥 System | `/status` `/processes` `/apps` `/battery` `/wifi` `/ip` `/uptime` |
| 🔊 Audio | `/volume` `/tts` |
| 📋 Clipboard + search | `/clipboard` `/paste` `/search` |
| 🤖 Automation | `/shortcut` `/focus` `/notifications` |
| 💬 Conversation | `/clear` `/fork` `/cost` |
| 🧠 Memory | `/memory` (stores facts across conversations) |
| ⏰ Reminders | `/remind` `/timer` `/schedule` `/alert` |
| 🌐 Multi-machine | `/machine` `/wake` |
| 📂 Projects | `/project` (per-user cwd + env + model) |
| 🔬 Deep research | `/research` (15 parallel Claude API calls) |
| 📧 Mail | `/gmail` (Mail.app or IMAP) |
| 🌍 Browser | `/web` + `[BROWSE:url]` (headless Chromium via Playwright) |
| 🎩 UI | `/start` `/menu` (tappable button grid) |

Plus: anything you say in plain text goes to Claude, which has full shell access.

## Architecture

```
kernel/        platform-agnostic types + services (Claude, scheduler, projects,
               machines, browser, store, dispatcher)
adapters/      one per chat platform (telegram, web, discord, slack, imessage)
actions/       slash-command handlers — each works on every adapter
app.py         single entry point
```

**For contributors:** [docs/architecture.md](docs/architecture.md). New chat platform = ~250 lines (subclass `kernel.ChatAdapter`). New command = ~20 lines (drop a file in `actions/`). [docs/plugins.md](docs/plugins.md) walks through it.

## How it differs from OpenClaw

[OpenClaw](https://github.com/openclaw/openclaw) is a sibling project with similar goals — also self-hosted, also multi-platform, also Claude-friendly. Use OpenClaw if you want **multi-LLM** (Claude + GPT + Gemini) and broader chat coverage including WhatsApp.

Alfred is **Claude-only**, but in exchange:

- **Wraps the Claude Code CLI directly** — inherits Claude's full tool ecosystem (Bash, file ops, MCP servers, web fetch). OpenClaw uses LLM APIs and ships its own tools.
- **Mac-native by default** — Vision OCR, AppleScript, iMessage `chat.db`, macOS notification forwarding are first-class.
- **Single binary on a single Mac** — minimum moving parts. No external services.

If you live on a Mac and want the deepest Claude Code integration, that's Alfred.

## Documentation

| Doc | Audience |
|---|---|
| [quickstart.md](docs/quickstart.md) | First 5 minutes |
| [commands.md](docs/commands.md) | Every command, with examples |
| [setup/](docs/setup/) | Per-platform getting started |
| [security.md](docs/security.md) | Auth model, attack surface, what to be careful about |
| [architecture.md](docs/architecture.md) | kernel + adapters + actions, for contributors |
| [plugins.md](docs/plugins.md) | Write your own command |
| [troubleshooting.md](docs/troubleshooting.md) | When something doesn't work |
| [faq.md](docs/faq.md) | Common questions |

## Stack

- Python 3.11+
- [Claude Code CLI](https://claude.com/claude-code) — driven via stream-json
- `python-telegram-bot` (Telegram), `aiohttp` (web), `discord.py` (Discord, optional), `slack-bolt` (Slack, optional)
- Playwright + Chromium (`/web`, optional)
- Anthropic SDK (`/research`, optional)
- macOS: `screencapture`, `osascript`, `pbpaste`/`pbcopy`, `mdfind`, Vision framework via AppleScriptObjC, ffmpeg + Whisper

## Contributing

PRs welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Don't add hardcoded paths, IPs, server names, or business logic — Alfred ships clean.

## License

MIT — see [LICENSE](./LICENSE).
