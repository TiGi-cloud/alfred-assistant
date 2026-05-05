# Web chat setup

The fastest path to "Alfred working" — no external account needed.

The web adapter is an aiohttp server bound to `127.0.0.1` that serves three things:

- `/` — the chat UI (this page)
- `/dashboard` — the Mini App dashboard with live gauges + sparklines (see [dashboard.md](dashboard.md))
- `/api/*` — JSON endpoints powering the dashboard

The setup wizard auto-generates an auth token and embeds it in the URLs.

## 1. Enable in the wizard

Run `./install.sh` (or `python3 setup_wizard.py`) → check **"Enable browser chat"** → click Save.

The default port is `8765`. Change it in the wizard if you have a conflict.

## 2. Run

```bash
python3 app.py
```

It prints:

```
🎩 Web chat:   http://127.0.0.1:8765/?token=abc123…
```

Open that URL in Safari/Chrome/Firefox. You'll see a dark-themed chat interface.

## 3. Use it

Type a message and hit Enter (Shift-Enter for a new line). The Web adapter speaks to the same Claude pipeline as Telegram — slash commands, attachments, inline buttons all work.

## What it supports

| | Status |
|---|---|
| Text in / out | ✅ |
| Inline buttons | ✅ — render as native HTML buttons; clicks fire as `kernel.CallbackPress` |
| Photos sent from bot | ✅ — small (≤ 4 MB) inlined as `data:` URLs; larger served from `/file/<token>` |
| Videos / voice / documents | ✅ — surfaced as a download link |
| File upload from browser | ❌ (v2) |
| Voice input | ❌ (v2) |
| Mobile responsive | ✅ — works on phones if you can reach `localhost`, but you usually can't |

## Mobile use

`localhost` is only reachable from the same Mac. To use the web chat from your phone you have three options:

1. **Same Wi-Fi**: run `python3 app.py` with `WEB_HOST=0.0.0.0` in `.env` and visit `http://<your-mac-ip>:8765/?token=…` from your phone. ⚠️ Bad idea unless your network is fully trusted.
2. **Cloudflare Tunnel** (recommended): `cloudflared tunnel --url http://localhost:8765` → public HTTPS URL → bookmark on your phone. Token still required.
3. **Use Telegram instead** — that's what it's for.

## Auto-reconnect

The browser tries to reconnect on disconnect (1s → 2s → 4s → … capped at 15s). The "🟢 connected / 🔴 disconnected" indicator in the header tells you what state it's in.

## Resetting

If the browser misbehaves, just refresh the page — each refresh creates a new chat session (new `chat_id`), so your conversation thread restarts. To preserve the conversation across refreshes, use Telegram or another chat platform.

## Security

The web adapter binds to `127.0.0.1` by default. Don't change that unless you've added real authentication. The URL token is a soft barrier — anyone who can read your URL bar (e.g. screen-sharing on a call) gets in.

If you need network access, put it behind:
- A VPN that only your devices can reach
- Cloudflare Access / Tailscale Funnel / oauth2-proxy

See [security.md](../security.md).
