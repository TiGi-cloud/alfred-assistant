# Quickstart

Five minutes from `git clone` to "Alfred screenshotted my desktop in Telegram".

## 0. Prerequisites

- **macOS** (most features are Mac-specific; Linux works for the chat side but Mac tools won't be available)
- **Python 3.11+** — `python3 --version`
- **[Claude Code CLI](https://claude.com/claude-code)** installed and authenticated — `claude --version`
- **Homebrew** (recommended, for `ffmpeg` + `imagesnap`)

## 1. Clone + install

```bash
git clone https://github.com/TiGi-cloud/alfred-assistant.git
cd alfred-assistant
./install.sh
```

The installer:
1. Verifies Python 3.11+
2. Creates a `./venv` and installs `requirements.txt`
3. Installs `ffmpeg` + `imagesnap` if Homebrew is available
4. Reminds you to install the Claude CLI if missing
5. Opens the **setup wizard** at <http://localhost:8080>

## 2. Pick a chat platform in the wizard

The wizard has a card for each supported chat. Fill in **at least one** and click **Save**.

The fastest path is **Web chat** (no external account needed) — just toggle it on and click Save. Then run `python3 app.py` and visit the URL it prints.

For Telegram, see [setup/telegram.md](setup/telegram.md). Discord/Slack/iMessage have their own setup guides.

## 3. Run

```bash
python3 app.py
```

You'll see something like:

```
🎩 Web chat:   http://127.0.0.1:8765/?token=…
00:00:00 INFO  alfred.adapters.telegram — Telegram adapter started
```

## 4. Try it

In whatever chat you set up, message the bot:

| Try this | Expected |
|---|---|
| `/ping` | `pong 🏓` |
| `/screenshot` | A screenshot of your Mac arrives |
| `/status` | CPU / memory / disk / IP table |
| `take a screenshot` | Same as `/screenshot` (via Claude) |
| `what's playing in Music?` | Claude runs AppleScript and replies |
| `remember I prefer Python over JS` | Stored as long-term memory |
| `/cost` | Token usage + cost estimate for this chat |
| `/menu` | Tappable button grid of every command |

## 5. (Optional) Auto-start on boot

Sample `launchd` plist files aren't shipped — write your own targeting the Python in `./venv/bin/python3`. A working template:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key><string>com.alfred.bot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/alfred-assistant/venv/bin/python3</string>
    <string>/Users/YOU/alfred-assistant/app.py</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/YOU/alfred-assistant</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/alfred.out.log</string>
  <key>StandardErrorPath</key><string>/tmp/alfred.err.log</string>
</dict>
</plist>
```

Save to `~/Library/LaunchAgents/com.alfred.bot.plist` and load with:

```bash
launchctl load ~/Library/LaunchAgents/com.alfred.bot.plist
```

## Next

- [commands.md](commands.md) — full command reference
- [security.md](security.md) — what Alfred can and cannot see
- [troubleshooting.md](troubleshooting.md) — common issues
- [plugins.md](plugins.md) — write your own command in `actions/`
