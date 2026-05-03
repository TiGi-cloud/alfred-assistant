# Alfred — Remote Mac Assistant via Telegram

A Telegram bot that gives you full remote control of your Mac through Claude. Send messages, photos, voice notes, or files — Alfred handles them on your machine and reports back.

> ⚠️ **Self-hosted only.** Alfred runs shell commands on the host with no sandbox. **Do not host this for other people** — only run your own copy on your own Mac.

> 🚧 Multi-chat support (Discord / Slack / Web UI) is on the roadmap. Today, Alfred ships with a Telegram adapter only.

## Features

### Core
- **Full Mac access** — runs any shell command, AppleScript, controls apps, reads/writes files
- **Conversation memory** — context persists across messages and bot restarts
- **Streaming responses** — live output via Claude's stream-json with automatic fallback
- **Multi-message grouping** — rapid messages combined into a single Claude prompt
- **Reply context** — reply to any bot message to include it as context
- **Long-running tasks** — no hard timeout; supports multi-hour builds and migrations
- **Auto-start on boot** — optional `launchd` service with auto-restart
- **Multi-machine** — control multiple machines, not just the local Mac
- **Persistent state** — cost tracking, model preferences, alerts, history survive restarts

### Input Types
| Input | What happens |
|-------|-------------|
| Text message | Passed to Claude, response sent back |
| Photo | OCR via macOS Vision + Claude analyzes the image |
| Voice message | Transcribed via Whisper, then processed |
| File / document | Saved locally, Claude reads and processes it |
| Location | Acknowledged, can drive location-based triggers |
| Multiple messages | Grouped within 2s window into a single prompt |
| Reply to message | Original message included as context |

### Commands
| Command | Description |
|---------|-------------|
| `/start` | Help menu with quick-action buttons |
| `/clear` | Start a new conversation |
| `/cancel` | Stop a running task |
| `/ping` | Latency check (no Claude call) |
| `/screenshot` | Send a screenshot |
| `/record [seconds]` | Screen recording video (max 60s) |
| `/watch [interval]` | Live screen stream — toggle on/off |
| `/camera` | FaceTime camera photo |
| `/snap [save\|view\|compare\|delete] <name>` | Named screenshots with comparison |
| `/status` | System info (CPU, disk, memory, IP, uptime) |
| `/cost` | Token usage and cost tracking (model-aware pricing) |
| `/clipboard [text]` | Get clipboard (text or image) / set clipboard |
| `/model opus\|sonnet\|haiku` | Switch Claude model per user |
| `/browse [path]` | Interactive file browser |
| `/machine [name]` | Switch target machine |
| `/wake <machine>` | Wake-on-LAN magic packet |
| `/shortcut [name]` | Run any Siri Shortcut by name |
| `/hey siri\|google [cmd]` | Voice-assistant bridge |
| `/export` | Export conversation as markdown |
| `/schedule` | Manage scheduled recurring tasks (cron syntax supported) |
| `/alert` | System alerts (CPU, disk, memory, process, custom) |
| `/apps` | App launcher with tappable buttons |
| `/logs [n]` | Last N lines of bot logs |
| `/undo` | Ask Claude to undo its last action |
| `/fork [save\|load\|delete] <name>` | Branch / manage conversation sessions |
| `/history [n]` | Recent command history |
| `/notifications on\|off` | Toggle macOS notification forwarding |

### Smart Features
- **Streaming responses** — Claude output streamed live to Telegram with progress
- **Multi-message grouping** — messages within 2s combined into one prompt
- **Reply-to context** — reference any bot message in a follow-up
- **File sending** — Claude can send files to chat (screenshots, exports, logs, etc.)
- **Inline keyboards** — quick-action buttons for common operations
- **Progress indicators** — animated spinner with elapsed time
- **Markdown formatting** — code blocks, bold, links in responses
- **Rate limiting** — max 3 concurrent tasks per user
- **Approval workflow** — destructive commands (`rm -rf`, `shutdown`, `reboot`) require tap-to-confirm
- **Photo OCR** — macOS Vision framework extracts text automatically
- **Multi-model** — switch between Opus / Sonnet / Haiku per user
- **Scheduled tasks** — natural language or cron syntax (via `croniter`)
- **System alerts** — monitor CPU, disk, memory, processes
- **File browser** — navigate filesystem via tappable buttons
- **Clipboard bridge** — transfer text/images between phone and Mac
- **Screen recording** — record screen and send as video
- **Live screen watch** — stream screenshots at configurable intervals
- **FaceTime camera** — capture photos from Mac's camera
- **Named snapshots** — save, view, compare, delete

### Two-Stage Confirmation
Destructive commands surface a detailed warning explaining the risk, with clearly labeled "Yes, execute" and "Cancel" buttons.

### Optional Web Dashboard (Mini App)
A web dashboard ships in `webapp/` with system status panels and a file browser. To expose it as a Telegram Mini App you can use a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (or any other HTTPS reverse proxy) and set `WEBAPP_URL` in `.env`.

## Architecture

```
Telegram → Bot (python-telegram-bot) → claude -p --dangerously-skip-permissions → Your Mac
                                       │
                                       ├── --resume <session_id>     (conversation memory)
                                       ├── --system-prompt           (Alfred persona)
                                       ├── --output-format stream-json  (streaming + cost tracking)
                                       └── --model <user_choice>     (per-user model)

External   → http://localhost:7890/webhook   → Telegram (webhook relay + actions)
                Authorization: Bearer <WEBHOOK_SECRET>

Plugins    → plugins/*.py                    → auto-loaded as /commands

Background tasks (every 60s):
  - Schedule runner (croniter or simple matching)
  - Alert checker (CPU/disk/memory/process/custom)
  - Notification watcher (macOS Notification Center, every 30s)
```

## Stack
- **Runtime:** Python 3.11+
- **Telegram:** `python-telegram-bot`
- **AI:** Claude CLI (`claude -p`) with stream-json
- **Voice:** ffmpeg + OpenAI Whisper
- **OCR:** macOS Vision framework (via AppleScript)
- **Scheduling:** `croniter` (optional, falls back to simple matching)
- **Webhooks:** `aiohttp` with bearer-token auth
- **Camera:** `imagesnap` or ffmpeg (`avfoundation`)
- **Service:** macOS `launchd` (auto-start, keep-alive)

## Prerequisites

1. **macOS** (Apple Silicon or Intel). Most features rely on macOS-only tools.
2. **Python 3.11+**
3. **[Claude Code CLI](https://claude.com/claude-code)** installed and authenticated. Verify:
   ```bash
   claude --version
   ```
4. **Homebrew** (recommended) for installing `ffmpeg`, `imagesnap`.
5. **A Telegram account** to create a bot via [@BotFather](https://t.me/BotFather).

## Setup

### Easiest path (browser wizard, recommended for non-developers)

```bash
# 1. Clone the repo
git clone https://github.com/TiGi-cloud/alfred-assistant.git
cd alfred-assistant

# 2. Run the installer — it sets up dependencies and opens a browser wizard
./install.sh
```

The wizard at <http://localhost:8080> walks you through:

1. Pasting a Telegram bot token from [@BotFather](https://t.me/BotFather)
2. Listing the Telegram usernames allowed to talk to your bot
3. Toggling the local browser chat at <http://localhost:8765>
4. Optional: Webhook secret, Mini App URL, custom paths

It writes a `.env` file (mode 0600), then exits with restart instructions.

### Manual path

```bash
# 1. Clone & install dependencies
git clone https://github.com/TiGi-cloud/alfred-assistant.git
cd alfred-assistant
pip3 install -r requirements.txt
brew install ffmpeg imagesnap        # imagesnap optional, used by /camera

# 2. Create a Telegram bot via @BotFather and copy the token

# 3. Copy and edit the env template
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, ALLOWED_USERS, etc.

# 4. Run
python3 app.py             # multi-adapter (Telegram + browser chat)
# or
python3 bot.py             # legacy entry point — Telegram only, full feature set
```

### Two entry points (during the multi-chat refactor)

- **`app.py`** — new multi-adapter entry point. Runs Telegram + a local browser
  chat side-by-side via the `kernel.ChatAdapter` interface. A handful of demo
  commands are wired (`/ping`, `/whoami`, `/screenshot`); more get ported with
  every release.
- **`bot.py`** — legacy entry point. The original full-feature Telegram bot.
  Use this if you need the complete command set today; switch to `app.py` once
  the migration completes.

### Auto-start on boot (optional)

Sample `launchd` plist files for the bot and an optional Cloudflare Tunnel are *not* shipped — write your own targeting the Python interpreter you used in step 2 and load with:

```bash
launchctl load ~/Library/LaunchAgents/com.alfred.telegrambot.plist
```

## macOS Permissions

In **System Settings → Privacy & Security**, grant the Python interpreter (or a wrapper `.app` bundle) the following:

- **Screen Recording** — for `/screenshot`, `/record`, `/watch`
- **Camera** — for `/camera`
- **Accessibility** — for UI automation, Siri control, media-key bridging
- **Full Disk Access** — for reading/writing arbitrary paths
- **Automation** — auto-prompted per app on first use

## User Context (teach Alfred about your setup)

Alfred ships without any infrastructure-specific knowledge baked in. To teach it about your servers, projects, or shortcuts, create a `USER_CONTEXT.md` file in the bot directory. Its contents are appended to the system prompt at runtime. Example:

```markdown
# My infrastructure

- mac-mini (local): home machine, projects in ~/Desktop/
- prod server: ssh prod (user: alice, /home/alice/)
  - docker projects in /home/alice/<name>
- GitHub orgs: my-team, my-personal

# Shortcuts
- "rebuild api" → ssh prod 'cd /home/alice/api && docker compose up -d --build'
- "check logs on prod" → ssh prod 'docker logs api'
```

Alfred reads this file on startup. Restart the bot after editing.

## Multi-Machine

```
/machine add myserver 192.168.1.10
/machine add myserver 192.168.1.10 AA:BB:CC:DD:EE:FF   # with MAC for WOL
/machine add production user@prod.example.com
/machine myserver        # switch
/machine local           # switch back
/wake myserver           # send Wake-on-LAN packet
```

Machines are stored in `machines.json`.

## Webhook Integration

Trigger Alfred from any script:

```bash
# Simple notification
curl -X POST http://localhost:7890/webhook \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret" \
  -d '{"message": "Build completed successfully!"}'

# Trigger a Claude action
curl -X POST http://localhost:7890/webhook \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-secret" \
  -d '{"action": "check disk space and send a summary"}'
```

## Plugin System

Create `plugins/mycommand.py`:

```python
COMMAND = "ping"
DESCRIPTION = "Check if Alfred is alive"

async def handler(update, context):
    await update.message.reply_text("Pong!")
```

Plugins are auto-loaded on startup. See `plugins/example.py` for a full template.

## Scheduled Tasks

```
/schedule "every hour"  "check disk space and alert if below 10%"
/schedule "every day"   "summarize today's calendar events"
/schedule "*/5 * * * *" "check if nginx is running"    # cron syntax (requires croniter)
/schedule remove 1
```

## System Alerts

```
/alert cpu 90          # alert when CPU > 90%
/alert disk 85         # alert when disk usage > 85%
/alert memory 80       # alert when memory > 80%
/alert process nginx   # alert when nginx stops
/alert custom "curl -s http://myapp.local/health | grep -v OK"
/alert remove 1
```

## Named Snapshots

```
/snap save desktop     # save current screen as "desktop"
/snap view desktop     # view the saved snapshot
/snap compare desktop  # compare saved vs current side-by-side
/snap delete desktop
/snap                  # list all snapshots
```

## Conversation Branching

```
/fork save experiment  # save current conversation as "experiment"
/fork load experiment  # switch to the "experiment" branch
/fork delete experiment
/fork                  # list all branches
```

## Example Usage

```
You: take a screenshot and send it
Alfred: [sends screenshot photo]

You: what docker containers are running on prod?
Alfred: [SSHes to prod] nginx, postgres, redis all running…

You: [sends photo of error on screen]
Alfred: OCR detected: "segfault in libcurl". Try: brew reinstall curl…

You: [voice message: "what's my IP address"]
Alfred: Your local IP is 192.168.1.42

You: /hey siri turn off living room lights
Alfred: Sent to Siri: turn off living room lights

You: /watch 3
Alfred: Watching screen every 3s… [sends screenshots]

You: rm -rf /tmp/old-builds
Alfred: ⚠️ Potentially destructive command detected. [Approve] [Deny]

You: /alert cpu 85
Alfred: Alert added: CPU > 85%
[Later] Alfred: 🚨 CPU > 85%: 92%
```

## Security Notes

- Alfred runs `claude -p --dangerously-skip-permissions`, which means Claude can execute **any** shell command on the host.
- Always set `ALLOWED_USERS` (or `ALLOWED_USER_IDS`) before running publicly. With both empty, the bot accepts messages from **anyone** who finds the bot handle.
- Treat your `.env` as a secret. Never commit it.
- Destructive commands have a tap-to-confirm gate, but you should still trust the user list you grant access to.

## License

MIT — see [LICENSE](./LICENSE).

## Contributing

Issues and PRs welcome. See [CONTRIBUTING.md](./CONTRIBUTING.md) once it lands.
