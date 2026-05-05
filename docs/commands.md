# Commands

Every command works on every chat platform — Telegram, Web chat, Discord, Slack, iMessage. They run identically because every adapter speaks the same `kernel.ChatAdapter` interface.

39 commands in 13 modules. Tappable button menu via `/menu`.

## Screen

| Command | Description |
|---|---|
| `/screenshot` | Take a screenshot of the Mac (via `screencapture -x`) |
| `/record [secs]` | Screen recording, 1–60 seconds, MP4 |
| `/watch [interval]` | Live screen stream — sends a screenshot every N seconds; toggle off by running again |
| `/camera` | Single photo from FaceTime camera (`imagesnap` or `ffmpeg`) |
| `/ocr` | Vision-framework OCR on the attached photo, or screenshot the screen first if no attachment |

## System

| Command | Description |
|---|---|
| `/status` | CPU + memory + disk usage with bars, IP, uptime |
| `/processes` | Top processes by CPU |
| `/apps` | Visible apps via System Events |
| `/battery` | `pmset -g batt` (laptops only) |
| `/wifi` | Current WiFi SSID |
| `/ip` | Local + public IP |
| `/uptime` | Mac uptime |
| `/volume [0-100\|mute]` | Show or set output volume |
| `/clipboard [text]` | Read or set clipboard |
| `/paste` | Read clipboard |
| `/search <query>` | Spotlight search by filename |
| `/shortcut [name]` | List or run Siri Shortcuts |
| `/focus` | Toggle Do Not Disturb (needs a Shortcut named "Toggle Do Not Disturb") |
| `/tts <text>` | Speak text aloud (`/tts -v Samantha hello`) |

## Conversation (Claude)

| Command | Description |
|---|---|
| `/clear` | Drop the current Claude conversation thread |
| `/fork save <name>` | Snapshot the current session under a name |
| `/fork load <name>` | Switch back to that snapshot |
| `/fork delete <name>` | Forget a branch |
| `/fork` | List saved branches |
| `/cost` | Token totals + estimated USD cost for this chat |

## Memory (long-term recall)

| Command | Description |
|---|---|
| `/memory` | List stored facts |
| `/memory add [category] <fact>` | Store a fact (categories: preference, fact, routine, context, task) |
| `/memory search <query>` | Filter |
| `/memory remove <id>` | Forget one |
| `/memory clear` | Wipe everything |

Memory is automatically injected into every prompt and Claude can autonomously add to it via `[REMEMBER:cat:fact]` markers in its responses.

## Reminders + scheduling

| Command | Description |
|---|---|
| `/remind in 10 min <text>` | One-shot reminder (natural language times) |
| `/remind at 7pm <text>` | … or absolute |
| `/remind 2026-05-04 09:00 <text>` | … or ISO |
| `/remind delete <id>` | Cancel |
| `/timer 5 [label]` | Quick N-minute timer |
| `/schedule "every day at 9am" <text>` | Recurring (cron or natural lang) |
| `/schedule remove <id>` | Cancel |
| `/alert cpu <%>` | Fire when CPU ≥ threshold |
| `/alert disk <%>` | Fire when disk full ≥ threshold |
| `/alert memory <%>` | Fire when memory used ≥ threshold |
| `/alert process <name>` | Fire when a named process stops |
| `/alert remove <id>` | Cancel |

5-minute cooldown between repeat alert fires.

## Multi-machine

| Command | Description |
|---|---|
| `/machine` | List machines + active selection |
| `/machine local` | Switch to this Mac |
| `/machine <name>` | Switch active SSH target |
| `/machine add <name> <host> [<MAC>]` | Add (e.g. `/machine add prod alice@prod.example.com`) |
| `/machine remove <name>` | Remove |
| `/wake <name>` | Wake-on-LAN magic packet (machine must have a MAC) |

## Projects

| Command | Description |
|---|---|
| `/project` | List projects + active |
| `/project <name>` | Switch active project (changes Claude's cwd + env) |
| `/project add <name> <cwd>` | Add (use `~` for home) |
| `/project remove <name>` | Remove |
| `/project model <name> <model>` | Set default Claude model for that project |
| `/project env <name> KEY=VALUE` | Set/remove an env var |
| `/project local` | Deactivate (use bot's repo root) |

## Web (headless browser)

| Command | Description |
|---|---|
| `/web <url>` | Load + screenshot a URL |
| `/web snapshot` | Markdown-ish dump of the current page |
| `/web click <text-or-css>` | Click an element |
| `/web close` | Close this chat's browser session |

Requires `pip install 'playwright>=1.40' && playwright install chromium`.

Claude can also open a URL itself by emitting `[BROWSE:url]` in a response — Alfred screenshots it and sends the photo back automatically.

## Other

| Command | Description |
|---|---|
| `/start`, `/menu` | Tappable button grid |
| `/help` | Full command listing in chat |
| `/ping` | Sanity check |
| `/whoami` | Adapter / user / chat metadata |
| `/open <url-or-app>` | Open URL or app on the Mac |
| `/notifications [on\|off]` | Toggle macOS notification forwarding to chat |
| `/research <topic>` | 15 parallel Claude API calls + synthesis (~$0.05–0.20) |
| `/gmail [read N]` | Read recent unread mail (Mail.app or IMAP) |
| `/gmail draft to:… subject:… body:…` | Open a compose window in Mail.app |

## Plain text — Claude

Anything that isn't a slash command goes through the Claude pipeline. Claude has full shell access via its Bash tool, so:

```
You: take a screenshot, OCR it, and tell me what apps I have open
Alfred: [streams thinking] [SEND_FILE:/tmp/shot.png]
        OCR text: ...
        Open apps: ...
```

The conversation persists across messages and across restarts.

## Persistent state files

These get auto-created next to `app.py`:

| File | What |
|---|---|
| `alfred.db` | SQLite KV store — memory + arbitrary persistence |
| `alfred_machines.json` | SSH targets + active machine pointer |
| `alfred_projects.json` | Per-user named projects |
| `alfred_scheduler.json` | Reminders, schedules, alerts |
| `alfred_notifications.json` | Per-chat notifications-on toggle |
| `alfred_metrics.json` | CPU / memory / disk samples (last 24h, dashboard sparklines) |
| `claude_sessions.json` | Active Claude session per chat |
| `claude_forks.json` | Named conversation branches |
| `claude_usage.json` | Token-usage history |

All gitignored. Back them up if you care about the state.

## Dashboard

`http://localhost:8765/dashboard?token=…` — live Mac status (CPU / memory / disk gauges, 60-min sparklines), schedules, alerts, machines, cost, command palette. Same `WEB_AUTH_TOKEN` as the chat. See [setup/dashboard.md](setup/dashboard.md) for browser walk-through and Telegram Mini App setup.
