# Slack setup

Slack is the most setup-heavy of the chat platforms — ~10 minutes the first time. Once it's done it's solid.

## 1. Install the optional dependency

```bash
pip install 'slack-bolt>=1.18'
```

## 2. Create a Slack app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From scratch**
2. Name it (e.g. "Alfred"), pick the workspace you want it in

## 3. Enable Socket Mode

Left sidebar → **Socket Mode** → toggle **Enable Socket Mode** ON.

You'll be prompted to generate an **App-Level Token**. Pick the `connections:write` scope. Name it `socket-mode` and create. Copy the `xapp-…` token — that's your `SLACK_APP_TOKEN`.

## 4. Add bot scopes

Left sidebar → **OAuth & Permissions** → scroll to **Scopes** → **Bot Token Scopes** → add:

- `chat:write` — send messages
- `im:history` — read DMs
- `im:read` — see DM channels
- `im:write` — open DMs
- `files:write` — upload files
- `app_mentions:read` — see @mentions

Optional (for channel use): `channels:history`, `channels:read`, `groups:history`, `groups:read`.

## 5. Subscribe to events

Left sidebar → **Event Subscriptions** → toggle **Enable Events** ON.

Under **Subscribe to bot events**, add:

- `message.im` — direct messages to the bot
- `app_mention` — `@Alfred` in channels

(Add `message.channels` and `message.groups` if you want the bot to see channel messages it's invited to.)

Save.

## 6. Install to workspace

Top of **OAuth & Permissions** → click **Install to Workspace** → approve.

After install, copy the **Bot User OAuth Token** that appears at the top — `xoxb-…` — that's your `SLACK_BOT_TOKEN`.

## 7. Find your Slack user ID

In Slack, click your name → **Profile** → click the `...` → **Copy member ID**. Looks like `U01ABCDEFGH`.

## 8. Add to `.env`

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_IDS=U01ABCDEFGH   # comma-separated for multiple users
```

Without `SLACK_ALLOWED_USER_IDS`, **anyone in your workspace** can use the bot.

## 9. Run

```bash
python3 app.py
```

You should see:

```
00:00:00 INFO  alfred.adapters.slack — Slack adapter started (Socket Mode)
```

DM the bot in Slack — `/ping` should reply `pong 🏓`.

## What works in Slack

| Feature | Notes |
|---|---|
| Direct messages | ✅ — primary use case |
| `@mentions` in channels | ✅ — bot must be invited to the channel |
| Block Kit buttons | ✅ — Alfred renders `kernel.Keyboard` as actions blocks |
| Threading | Partial — replies fall in the channel, not threaded by default |
| File uploads | ✅ — `files_upload_v2` |
| Slash commands (registered) | ❌ — uses message text only, like Discord |
| Typing indicator | ❌ — Slack doesn't expose a public bot typing API |

## Verifying setup with a stand-alone test

Before hooking Slack into the full Alfred, you can test just the adapter:

```bash
SLACK_BOT_TOKEN=xoxb-... \
SLACK_APP_TOKEN=xapp-... \
python3 test_slack.py
```

Then DM the bot:
- `ping` → `pong 🏓`
- `whoami` → adapter / user metadata
- `buttons` → 3 inline buttons; clicks echo
- `screenshot` → screenshot uploaded

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Socket Mode is not turned on` | Step 3 — toggle in app config. |
| `not_authed` / `invalid_auth` | Bot token is wrong. Reinstall to workspace, copy the new `xoxb-…`. |
| Bot accepts no DMs | You skipped step 5 — subscribe to `message.im` and `app_mention`. |
| Events arrive twice | You're running two Alfred instances pointing at the same Slack app. Kill one. |
| `missing_scope` | Add the scope to **Bot Token Scopes**, then re-install to workspace. |

See [troubleshooting.md](../troubleshooting.md) for more.
