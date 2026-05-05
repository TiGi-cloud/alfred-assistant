# Dashboard (Telegram Mini App)

Alfred ships a mobile-first dashboard that runs inside Telegram as a Mini App, or in any browser as a regular web page. Live CPU / memory / disk gauges, history sparklines, schedules, alerts, machines, cost — all on one tabbed screen.

The dashboard is served by the [Web adapter](web.md). You don't have to enable Web Chat to use it; the dashboard works either way.

## Quick view (browser)

When you start `python3 app.py`, the dashboard URL is printed alongside the chat URL:

```
🎩 Web chat:   http://127.0.0.1:8765/?token=…
   Dashboard:  http://127.0.0.1:8765/dashboard?token=…
```

Open the dashboard URL — done. Auth is the same `WEB_AUTH_TOKEN` as the chat.

## Telegram Mini App view

Telegram needs an HTTPS URL to embed a Mini App. So you have to expose `localhost:8765/dashboard` to the internet over HTTPS. The two cheapest paths:

### Option A — Cloudflare Tunnel (free, recommended)

1. Install: `brew install cloudflare/cloudflare/cloudflared`
2. Authenticate: `cloudflared tunnel login`
3. Create a quick tunnel: `cloudflared tunnel --url http://localhost:8765`
4. Cloudflare gives you a URL like `https://x9k2-…trycloudflare.com`
5. Add to `.env`:
   ```
   WEBAPP_URL=https://x9k2-…trycloudflare.com/dashboard?token=YOUR_WEB_AUTH_TOKEN
   ```
6. Restart Alfred. The Telegram bot's menu button now opens your dashboard.

For a stable URL (instead of a fresh one each tunnel run), set up a [named Cloudflare tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/get-started/create-local-tunnel/) — it's a one-time CLI walkthrough.

### Option B — Tailscale Funnel

If you're on Tailscale, run:

```bash
tailscale funnel --bg --https 8765 http://localhost:8765
```

Tailscale gives you a `*.ts.net` HTTPS URL with the same shape. Plug into `WEBAPP_URL` the same way.

### Option C — ngrok

```bash
ngrok http 8765
```

Free tier reuses URLs across runs. Same `WEBAPP_URL` flow.

## What the dashboard shows

| Tab | Contents |
|---|---|
| **Status** | Health gauge (0–100), three small gauges for CPU / memory / disk, 60-minute sparklines, system info (host, IP, RAM, uptime), bot info (sessions, alerts, schedules) |
| **Actions** | Tappable buttons for /screenshot, /status, /clear, etc. |
| **Terminal** | Free-form shell box (gated behind biometric / passcode if your browser supports it) |
| **Screen** | Quick screenshot button + recent captures |
| **AI** | Cost breakdown, model picker, conversation tools |
| **Auto** | Schedules, reminders, alerts list with delete actions |

The Status tab is fully wired in v2. The other tabs work where the underlying API exists; some (Docker monitoring, terminal execution, file-batch operations) return "not yet supported" until those features finish their port. See [`adapters/web.py`](../../adapters/web.py) — the `_api_*` methods make it easy to spot what's stubbed.

## Bind the menu button

Once `WEBAPP_URL` is set in `.env`, Alfred's Telegram adapter sets the bot's menu button (the ⚙️ icon next to the chat input) to open the dashboard. So in Telegram, you tap that icon → Mini App opens fullscreen → you're looking at your Mac's stats from your phone.

## Theme integration

Inside Telegram, the dashboard reads `tg.themeParams` and inherits your Telegram theme (dark / light / accent colours, header tint, etc). In a plain browser it falls back to the default dark scheme.

## Auth model

Every `/api/*` call from the dashboard carries `Authorization: Bearer <WEB_AUTH_TOKEN>`. The `/dashboard` HTML itself accepts `?token=…` in the URL (so you can paste a link). The dashboard injects the bearer into every subsequent fetch, so once the page loads, you don't have to re-authenticate per call.

If you change `WEB_AUTH_TOKEN` in `.env`, you'll need a fresh `WEBAPP_URL` with the new token, and the Telegram menu button will need to be re-set (Alfred does this automatically on next start).

## Security caveats

- The dashboard exposes everything Alfred can do over the API — including `/api/screenshot`, `/api/quick-action` (which can run any registered slash command). Treat the auth token like a password.
- Don't expose `localhost:8765` to the internet without `WEB_AUTH_TOKEN` set.
- Cloudflare Tunnel + a long, random `WEB_AUTH_TOKEN` is the realistic baseline. Don't ngrok-public-link it without auth.
- For multi-user use cases (you + spouse), set up two separate Alfred instances on two Macs. The dashboard is single-user by design.

See [security.md](../security.md) for the full threat model.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard renders but cards stay as skeleton placeholders | Check the browser console — usually means Telegram WebApp script failed because you're outside Telegram and the script tries calls the stub doesn't support. The dashboard handles this gracefully in v2. If it doesn't, file a bug. |
| `/api/status` returns 401 | URL token doesn't match `WEB_AUTH_TOKEN`. Use the URL `app.py` printed at startup. |
| Tunnel URL in `WEBAPP_URL` works in browser but Telegram says "could not load Mini App" | Telegram caches the menu button — close and reopen the bot's chat. Or `/start` to force a refresh. |
| Sparkline graphs are flat at zero | The metrics collector hasn't accumulated enough samples yet. Default poll interval is 60s; wait an hour for full resolution, or restart with a custom `MetricsCollector(interval_secs=10)` for testing. |
