# Security

Read this before exposing Alfred to anyone but yourself.

## TL;DR

- Alfred runs `claude -p --dangerously-skip-permissions`. Claude executes **any shell command** on the host with no sandbox.
- **Self-hosted only.** Do not host this for other people.
- Use platform allowlists (`ALLOWED_USERS`, `DISCORD_ALLOWED_USER_IDS`, `SLACK_ALLOWED_USER_IDS`, `IMESSAGE_ALLOWED_HANDLES`).
- Web adapter binds to `127.0.0.1` only; the auth token in the URL is a soft barrier, not an authentication system.

## Threat model

**What Alfred has access to:**

- Your entire user account on the Mac (Full Disk Access if you grant it for `/ocr` + `/imessage`)
- Anything your shell can do — `rm`, `git push --force`, `osascript`, `curl`, `ssh`, …
- Claude Code's tool ecosystem — file edits, MCP servers, web fetch, etc.
- All the chat tokens you put in `.env`

**Who should be allowed to talk to Alfred:**

- You. Maybe your spouse. **Nobody else.**

**What sends commands to Alfred:**

- The chat adapter you set up. Each adapter has its own allowlist. With no allowlist set, **every adapter accepts messages from anyone who finds the bot handle.**

## Per-adapter auth

| Adapter | Auth control | Default |
|---|---|---|
| Telegram | `ALLOWED_USERS` (usernames) + `ALLOWED_USER_IDS` (numeric) | empty = open to anyone |
| Discord | `DISCORD_ALLOWED_USER_IDS` (snowflakes) | empty = open to anyone in shared servers |
| Slack | `SLACK_ALLOWED_USER_IDS` (member IDs) | empty = open to anyone in workspace |
| iMessage | `IMESSAGE_ALLOWED_HANDLES` (phone/email) | empty = open to anyone who DMs you |
| Web | `WEB_AUTH_TOKEN` (URL fragment) | auto-generated; bound to localhost |

The setup wizard refuses to save a Telegram config without an allowlist for exactly this reason. The other adapters log a loud warning at startup if their allowlist is empty — the bot still starts, but you should fix it immediately.

## Web adapter caveats

- Binds to `127.0.0.1` by default. Don't change that unless you genuinely need network access — there's no authentication beyond the URL token.
- The auth token is in the URL. Anyone who shoulder-surfs your browser can copy it. Treat it like a password.
- Don't put Alfred behind a reverse proxy that exposes it to the internet without adding real auth (TLS + OAuth proxy / Cloudflare Access / etc.).

## Dashboard caveats

The dashboard at `/dashboard` shares the same `WEB_AUTH_TOKEN` as the chat. It exposes a wider attack surface than the chat alone:

- `/api/screenshot` triggers a real `screencapture`. Anyone with the token can pull a desktop snapshot at any time.
- `/api/quick-action` runs **any registered slash command** (including `/screenshot`, `/clipboard`, `/tts`).
- `/api/files` returns a directory listing for any path on the host.
- `/api/wake` sends Wake-on-LAN packets to whatever MACs you have configured.

If you tunnel `/dashboard` to the public internet (Cloudflare Tunnel + `WEBAPP_URL` so it works as a Telegram Mini App), use a long random `WEB_AUTH_TOKEN`. Treat any leak of that token like a leaked SSH key — rotate immediately by deleting `WEB_AUTH_TOKEN` from `.env` and restarting Alfred (the wizard re-generates one).

## What lives in `.env`

The setup wizard writes `.env` with mode `0600` (readable only by your user). It contains:

- Telegram bot token, allowlist
- Discord bot token, allowlist
- Slack bot + app tokens, allowlist
- iMessage allowlist
- `WEBHOOK_SECRET`, `WEB_AUTH_TOKEN`
- Optional: `ANTHROPIC_API_KEY` for `/research`

Treat `.env` as a secret. Never commit it (the `.gitignore` blocks it). If you accidentally share one, **rotate every token in it** immediately:

- Telegram: `/revoke` to @BotFather, then `/token` again
- Discord: developer portal → Bot → Reset Token
- Slack: app config → Basic Information → Regenerate
- Anthropic: console.anthropic.com/settings/keys → Revoke

## What lives on disk

| File | Sensitive? |
|---|---|
| `alfred.db` | Memory contents — anything Claude has stored about you |
| `claude_*.json` | Conversation IDs + recent token counts — not text |
| `alfred_machines.json` | Hostnames + MAC addresses |
| `alfred_projects.json` | Project paths + env vars (could include secrets) |
| `alfred_scheduler.json` | Reminder text |

If you back up your home directory and want to scrub Alfred's state, delete those files plus `.env`. The next `python3 app.py` will recreate empty versions.

## macOS permissions you grant

The first time Alfred runs each feature, macOS prompts for the permission. You can revoke these any time in **System Settings → Privacy & Security**.

| Permission | Used for | Granted to |
|---|---|---|
| Full Disk Access | OCR (`/ocr`), iMessage (`chat.db`), some Spotlight searches | the Python interpreter (or `app.py`'s `.app` wrapper) |
| Screen Recording | `/screenshot`, `/record`, `/watch` | same |
| Camera | `/camera` | same |
| Accessibility | `/volume`, AppleScript app control, media keys | same |
| Automation | per-app, prompted on first use (Mail, Messages, …) | same |
| Notifications (read) | `/notifications on` | same (read-only access to `db2/db`) |

If you're security-conscious, install a wrapper `.app` bundle and grant permissions only to that bundle, not to the bare Python interpreter.

## Claude with shell access — what could go wrong

- A user with bot access asks: "rm -rf my Documents" → Claude does it. Claude has approval gates for some destructive patterns (matched in `kernel/claude.py` system prompt), but you shouldn't rely on them.
- A clever prompt-injection in a webpage Claude `[BROWSE]`s could try to manipulate Alfred. Web content is untrusted; treat any output that came from `[BROWSE]` as if it were typed by an adversary.
- If your `ANTHROPIC_API_KEY` leaks, someone can burn your Anthropic budget.

## What I (the project) won't do

- Phone home — Alfred doesn't talk to any server except the chat platforms you configure and the Claude API (via the Claude CLI)
- Auto-update — you control when you `git pull`
- Collect telemetry

If you find a security issue, please open a private GitHub Security Advisory or DM the maintainer rather than filing a public issue.
