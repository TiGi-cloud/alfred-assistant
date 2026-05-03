# Discord setup

## 1. Install the optional dependency

```bash
pip install 'discord.py>=2.4'
```

## 2. Create a Discord application

1. Go to <https://discord.com/developers/applications>
2. Click **New Application** → name it (e.g. "Alfred")
3. In the left sidebar → **Bot** → **Add Bot**
4. Copy the **Bot Token** (click "Reset Token" if needed)
5. Scroll down → enable the **MESSAGE CONTENT INTENT** under "Privileged Gateway Intents". Save.

## 3. Invite the bot to a server

1. Left sidebar → **OAuth2** → **URL Generator**
2. Scopes: tick `bot` and `applications.commands`
3. Bot Permissions: tick at minimum `Send Messages`, `Attach Files`, `Embed Links`, `Read Message History`. (Add `Manage Messages` if you want `/web close` style features.)
4. Copy the generated URL, open it in a browser, pick a server you control

The bot now appears in your server (offline until you start Alfred).

## 4. Find your Discord user ID

Discord settings → **Advanced** → enable **Developer Mode**. Then right-click your username anywhere → **Copy User ID** (an 18-digit snowflake).

## 5. Add to `.env`

```
DISCORD_BOT_TOKEN=MTIz.your.token.here
DISCORD_ALLOWED_USER_IDS=123456789012345678   # comma-separated for multiple users
```

Without `DISCORD_ALLOWED_USER_IDS`, **anyone in any server the bot is in** can send commands. Don't run without it.

## 6. Run

```bash
python3 app.py
```

You should see:

```
00:00:00 INFO  alfred.adapters.discord — Discord adapter ready as Alfred#1234
```

DM the bot, or mention it in a channel — `@Alfred /ping` should reply `pong 🏓`.

## What works in Discord

| Feature | Notes |
|---|---|
| Direct messages | ✅ — primary use case |
| Channel messages | ✅ — bot replies in the same channel |
| Inline buttons | ✅ — `discord.ui.View` components |
| Photo / video / files | ✅ — uploaded as attachments |
| Photo OCR (`/ocr`) | ✅ — bot downloads the image first |
| Voice messages | ❌ (v2) — Discord voice is a different API |
| Slash commands (registered) | ❌ — the bot listens for `/foo` text, not Discord-native slash commands |

## Caveats

- Discord caps button labels at 80 chars and `custom_id` at 100 — Alfred truncates if you cross those.
- Discord caps message text at 2000 chars — the kernel chunks longer responses across multiple messages.
- The bot doesn't auto-leave servers; if you remove it via Discord's UI, the next Alfred restart skips that server cleanly.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Bot online but doesn't reply | Forgot the **Message Content Intent**. Toggle it in the dev portal and restart. |
| "Missing Permissions" errors in logs | The bot's role doesn't have the channel permissions it needs. Re-generate the OAuth URL with the right perms or grant them per-channel. |
| `discord.errors.LoginFailure` | Token is wrong or revoked. Reset in dev portal, update `.env`. |
| Buttons say "interaction failed" | Alfred handles the press but didn't `defer()` in time — usually a transient network issue. Should self-heal. |

See [troubleshooting.md](../troubleshooting.md) for more.
