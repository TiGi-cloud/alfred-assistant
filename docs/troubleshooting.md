# Troubleshooting

If something doesn't work, this is the first stop. Each section is a symptom; if yours isn't here, open an issue with the output of `python3 app.py 2>&1 | tail -50`.

## Setup wizard

### "Port 8080 is busy"

Something else is already listening on that port (often another web server or another Alfred instance).

```bash
python3 setup_wizard.py --port 9000
```

### Wizard saves but `python3 app.py` says "no adapters"

You toggled Web off, Telegram has empty token, no Discord/Slack — nothing to start. Re-run the wizard and enable at least one chat.

## Claude pipeline

### "`claude` not found"

Install the [Claude Code CLI](https://claude.com/claude-code) and make sure `claude --version` works in the terminal you launched Alfred from.

If you have it installed in a non-standard path, set `CLAUDE_BIN` in `.env`:

```
CLAUDE_BIN=/opt/homebrew/bin/claude
```

### Replies come back empty / "(no response)"

Look at `app.py`'s output — claude often writes the real error to stderr. Common causes:

- Hit your Anthropic rate limit / monthly cap
- The system prompt grew too large (e.g. `USER_CONTEXT.md` is huge); claude rejects very large prompts
- Authentication expired — run `claude` interactively to refresh

### Stale session error: "No conversation found"

Self-healing — the runner clears the cached session id and retries automatically. If it keeps happening for a specific chat, run `/clear` on that chat.

### `[BROWSE:url]` markers don't render

Playwright isn't installed.

```bash
pip install 'playwright>=1.40'
playwright install chromium
```

## Telegram

### "Unauthorized" / messages don't go through

Check `ALLOWED_USERS` and `ALLOWED_USER_IDS` in `.env`. Either should contain your Telegram username (without `@`) or numeric ID. The bot logs incoming attempts at INFO level — watch the terminal while you message it.

### Bot says nothing

Two bots can't share a token. If `bot.py` (legacy) and `app.py` are both running with the same token, only one wins. Kill the other.

### Mini App / "Dashboard" button missing

Set `WEBAPP_URL` in `.env` to an HTTPS URL pointing at `/dashboard` on your tunnelled localhost (Cloudflare Tunnel / Tailscale Funnel / ngrok). Full walkthrough in [setup/dashboard.md](setup/dashboard.md). Restart Alfred and the menu button will be set automatically on next start.

## Slack

### "Socket Mode is not turned on"

Go to <https://api.slack.com/apps> → your app → **Socket Mode** → toggle ON, save. The App-Level token (`xapp-…`) must have the `connections:write` scope.

### Bot accepts no messages

Make sure you've **subscribed to bot events**: `message.im` and `app_mention` at minimum. App config → **Event Subscriptions**.

### `slack-bolt` not installed

```bash
pip install 'slack-bolt>=1.18'
```

## Discord

### Bot is "online" but doesn't respond

You forgot the **Message Content** privileged intent. Discord Developer Portal → your app → **Bot** → enable "MESSAGE CONTENT INTENT" → save → restart Alfred.

### Bot can't join a server

Generate an OAuth URL with the **bot** + **applications.commands** scopes and the right permissions (Send Messages, Attach Files, Read Message History). Visit the URL and add to your server.

## iMessage

### "Cannot read chat.db: authorization denied"

Grant **Full Disk Access** to the Python interpreter (not Alfred — Python). System Settings → Privacy & Security → Full Disk Access → `+` → add `/Library/Developer/CommandLineTools/usr/bin/python3` (or whichever interpreter you're using; check with `which python3`).

### Sends fail silently

The first send pops "Python wants to control Messages" — accept it. If you missed it, you can re-enable in System Settings → Privacy & Security → Automation.

### Group chats don't work

By design — only 1:1 chats are supported in v1. Schema for group chats is messier and AppleScript group sending is unreliable.

### Schema changed after macOS upgrade

Apple changes the `chat.db` schema occasionally. The adapter falls back to plain `text` column if the `attributedBody` decoder fails — so messages still come through, just without rich content. Open an issue with your macOS version.

## Web chat

### Browser shows "disconnected — reconnecting…" forever

Your `WEB_AUTH_TOKEN` doesn't match the URL token. Either:

- Use the URL `app.py` printed at startup (which has the right token)
- Or check `.env` and visit `http://localhost:<port>/?token=<value>`

### Photos don't render

Photos ≤ 4 MB are inlined as `data:` URLs. Larger files are served from `/file/<token>` and require the auth token in the request. Check the browser console for 401s — usually means you opened the page without `?token=…`.

## Dashboard

### `/dashboard` returns 401

Open with `?token=<WEB_AUTH_TOKEN>` in the URL. The token is in `.env` and is also printed at startup. The dashboard then injects it into every `/api/*` call as a Bearer header.

### Cards stay as skeleton placeholders forever

Usually a JavaScript error broke the load chain. Open browser dev-tools → Console. Common cause: when running outside Telegram, the official `webapp.js` shim sometimes throws on unsupported APIs. The dashboard handles this in v2; if you still hit it, file a bug with the console output.

### CPU / memory / disk show `NaN%`

The metrics collector hasn't returned a sample yet. The first sample lands ~1 second after startup; subsequent samples every 60s. Refresh after a few seconds. Or restart Alfred — `MetricsCollector.start()` samples once before the loop.

### Sparkline graphs are flat at zero

The collector hasn't accumulated enough history. Default poll is 60s; a full 60-minute graph needs an hour of uptime. The history persists to `alfred_metrics.json` so it accumulates across restarts.

### Telegram Mini App "could not load"

Telegram caches the menu button URL. Close the bot's chat → reopen, or send `/start`. If still broken, verify `WEBAPP_URL` in `.env` resolves over HTTPS in your phone's browser first.

## Permissions

### "Operation not permitted" for screencapture / clipboard / etc.

You haven't granted the relevant macOS permission to the Python interpreter. The first attempt should pop a system prompt; if you accidentally clicked Deny, revoke and re-add in System Settings → Privacy & Security → Screen Recording / Accessibility / Full Disk Access.

## Background tasks

### Reminders/schedules don't fire

The scheduler poll interval defaults to 30s. If you set a `/remind in 1 sec`, it'll fire on the next 30-second tick. For testing immediate fires, you can construct a `Scheduler(poll_interval=1)` (we do that in tests).

### Notifications don't forward

Three causes in order of likelihood:

1. You haven't granted Full Disk Access to Python (need it to read `~/Library/Group Containers/group.com.apple.usernoted/db2/db`)
2. Your macOS uses a different DB path; we try a few candidates
3. The schema changed in a macOS upgrade; the watcher falls through to "no notifications" silently

Run with `LOG_LEVEL=DEBUG` to see what the watcher sees.

## Tests

### `python3 tests/test_all.py` fails on a fresh clone

Run `python3 -m pip install -r requirements.txt` first (and `pip install ruff`). The CI workflow does this; locally you have to.

### "py_compile" fails citing files that don't exist

Stale `git ls-files` after a delete. Run `git add -A && git commit -m wip` (or `git stash`) so the file list refreshes.

## Performance

### Telegram replies feel slow

The Claude pipeline streams output by editing the same message every 1.5 seconds (default `edit_throttle_secs`). Lower it for snappier perceived response, but Telegram's rate limit kicks in around 1 edit/second per chat — going below ~1.0 risks `Flood control` errors.

### `/research` costs are higher than expected

The default is 15 parallel Haiku calls + 1 Sonnet synthesis. Set `RESEARCH_AGENT_MODEL` and `RESEARCH_SYNTH_MODEL` in `.env` if you want to tune it. Or override `_DEFAULT_NUM_AGENTS` in `actions/research.py`.
