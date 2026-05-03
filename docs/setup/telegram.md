# Telegram setup

Most users start here. Free, instant push notifications, works on every phone.

## 1. Create a bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram and send `/newbot`
2. Follow the prompts — pick a display name and a username (must end in `bot`)
3. BotFather replies with the **HTTP API token** — looks like `1234567890:AAEhBP0av...` Keep this secret.

## 2. Find your user ID (recommended)

Usernames change. User IDs don't. Send any message to [@userinfobot](https://t.me/userinfobot) and it tells you your numeric `Id:`.

## 3. Add to `.env`

The setup wizard does this for you. If editing by hand:

```
TELEGRAM_BOT_TOKEN=1234567890:AAEhBP0av...
ALLOWED_USERS=your_username       # without the @, comma-separated for multiple
ALLOWED_USER_IDS=123456789        # optional but more reliable
```

**Setting an allowlist is mandatory.** With both empty, the bot accepts commands from anyone who finds your bot handle, and Claude has shell access. You will get attacked.

## 4. Run

```bash
python3 app.py
```

You'll see:

```
00:00:00 INFO  alfred.adapters.telegram — Telegram adapter started
```

Open the bot's chat (`@yourbotname` in Telegram search) and send `/ping` — should reply `pong 🏓` instantly.

## 5. Mini App dashboard (optional)

Telegram Mini Apps are an embedded web view inside chat. Alfred ships a built-in browser chat at `localhost:8765`, but for true mobile use you need an HTTPS URL Telegram can reach.

Easiest option: [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) pointed at `localhost:8765`. Then in `.env`:

```
WEBAPP_URL=https://yourtunnel.example.com/?token=<your WEB_AUTH_TOKEN>
```

The bot will set its menu button to open that URL inside Telegram.

## 6. Set bot commands (optional but nice)

Send these to BotFather → `/setcommands` → pick your bot:

```
status - CPU, memory, disk, IP
screenshot - Take a screenshot
clear - Start a fresh conversation
help - List all commands
remind - Set a reminder
cost - Token + cost usage
menu - Tappable menu
```

Now Telegram autocompletes them in the chat input.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Forbidden: bot was blocked by the user" | You blocked your own bot earlier. Open the bot, click the start button, send `/start`. |
| Replies cut off mid-message | Telegram caps text at 4096 chars; the kernel chunks longer responses across multiple messages. |
| Photos don't send | Permissions: macOS must let the Python interpreter capture the screen. System Settings → Privacy & Security → Screen Recording. |
| Two bots fighting | Two Alfred instances can't share a token. Kill one. |

See [troubleshooting.md](../troubleshooting.md) for more.
