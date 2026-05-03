"""
First-run setup wizard.

Serves a friendly browser page on http://localhost:8080 that lets a
non-developer paste their Telegram bot token, list allowed users, and toggle
the local web chat. On Save, writes `.env` next to this file and exits with
a message telling the user how to start Alfred for real.

Usage:

    python3 setup_wizard.py            # opens at http://localhost:8080
    python3 setup_wizard.py --port 9000

`app.py` calls into this module automatically when no `.env` is present.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import socket
import webbrowser
from pathlib import Path
from typing import Optional

from aiohttp import web

logger = logging.getLogger("alfred.setup")

ENV_PATH = Path(__file__).parent / ".env"


# ---------------------------------------------------------------------------
# Setup HTML (single file, no external assets)
# ---------------------------------------------------------------------------
_SETUP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alfred Setup</title>
<style>
  :root {
    --bg: #0e1116;
    --bg-2: #161b22;
    --panel: #1e242c;
    --fg: #e6edf3;
    --muted: #8b949e;
    --accent: #2f81f7;
    --ok: #3fb950;
    --err: #f85149;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    min-height: 100%;
    background: var(--bg);
    color: var(--fg);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    font-size: 15px;
    line-height: 1.5;
  }
  .wrap {
    max-width: 640px;
    margin: 0 auto;
    padding: 32px 24px 64px;
  }
  h1 { font-size: 28px; margin: 0 0 4px; }
  p.lede { color: var(--muted); margin: 0 0 24px; }
  .card {
    background: var(--bg-2);
    border: 1px solid #30363d;
    border-radius: 14px;
    padding: 22px;
    margin-bottom: 18px;
  }
  .card h2 {
    margin: 0 0 6px;
    font-size: 18px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .card .help {
    font-size: 13px;
    color: var(--muted);
    margin-bottom: 14px;
  }
  .card .help a { color: var(--accent); }
  label {
    display: block;
    margin-top: 14px;
    font-size: 13px;
    color: var(--muted);
  }
  input[type="text"], input[type="password"] {
    width: 100%;
    background: var(--bg);
    color: var(--fg);
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 10px 14px;
    font: inherit;
    margin-top: 4px;
  }
  input:focus { outline: none; border-color: var(--accent); }
  .toggle {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-top: 8px;
  }
  .actions {
    display: flex;
    gap: 12px;
    align-items: center;
    margin-top: 8px;
  }
  button.primary {
    background: var(--accent);
    color: white;
    border: 0;
    border-radius: 10px;
    padding: 12px 22px;
    font-weight: 600;
    font-size: 15px;
    cursor: pointer;
  }
  button.primary:disabled { opacity: 0.5; cursor: not-allowed; }
  .status {
    font-size: 13px;
  }
  .status.ok { color: var(--ok); }
  .status.err { color: var(--err); }
  .pill {
    display: inline-block;
    padding: 2px 10px;
    border-radius: 999px;
    font-size: 11px;
    background: #21262d;
    color: var(--muted);
    margin-left: 6px;
  }
  .pill.optional { background: #21262d; }
  .pill.required { background: #2f81f7; color: white; }
</style>
</head>
<body>
<div class="wrap">
  <h1>🎩 Alfred</h1>
  <p class="lede">Set up your remote Mac assistant. This page only ever runs on your own computer; nothing is sent to the internet.</p>

  <form id="form">
    <div class="card">
      <h2>Telegram <span class="pill required">required for Telegram chat</span></h2>
      <div class="help">
        1. Open <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> on Telegram and send <code>/newbot</code>.<br>
        2. Pick a name and username for your bot.<br>
        3. Copy the token it gives you and paste it below.
      </div>
      <label>Bot token</label>
      <input type="password" name="telegram_bot_token" placeholder="123456:ABC-DEF…" autocomplete="off">

      <label>Your Telegram username (without the @) — comma-separate multiple</label>
      <input type="text" name="allowed_users" placeholder="alice, bob" autocomplete="off">

      <label>Or numeric Telegram user IDs (more reliable, optional) — comma-separated</label>
      <input type="text" name="allowed_user_ids" placeholder="123456789, 987654321" autocomplete="off">
    </div>

    <div class="card">
      <h2>Web chat <span class="pill optional">optional</span></h2>
      <div class="help">A browser-based chat at http://localhost — useful when you don't have Telegram open or want a desktop window.</div>
      <div class="toggle">
        <input type="checkbox" name="web_enabled" id="web_enabled" checked>
        <label for="web_enabled" style="margin: 0; color: var(--fg);">Enable browser chat</label>
      </div>
      <label>Port (8765 by default)</label>
      <input type="text" name="web_port" placeholder="8765" autocomplete="off">
    </div>

    <div class="card">
      <h2>Discord <span class="pill optional">optional</span></h2>
      <div class="help">
        1. Open <a href="https://discord.com/developers/applications" target="_blank" rel="noopener">Discord Developer Portal</a> and create an app.<br>
        2. Add a Bot, copy its token. Enable the <em>Message Content</em> privileged intent.<br>
        3. Invite the bot to your server with the OAuth2 URL Generator.
      </div>
      <label>Bot token</label>
      <input type="password" name="discord_bot_token" placeholder="MTIz…" autocomplete="off">

      <label>Allowed Discord user IDs — 18-digit snowflakes, comma-separated</label>
      <input type="text" name="discord_allowed_user_ids" placeholder="123456789012345678" autocomplete="off">
    </div>

    <div class="card">
      <h2>Slack <span class="pill optional">optional</span></h2>
      <div class="help">
        1. <a href="https://api.slack.com/apps" target="_blank" rel="noopener">Create a Slack app</a> → enable Socket Mode → generate an App-level token (xapp-…).<br>
        2. Add Bot Scopes (chat:write, im:history, im:read, files:write, app_mentions:read) and install to workspace → copy Bot Token (xoxb-…).<br>
        3. Subscribe to bot events: message.im, app_mention.
      </div>
      <label>Bot token (xoxb-…)</label>
      <input type="password" name="slack_bot_token" placeholder="xoxb-…" autocomplete="off">

      <label>App-level token (xapp-…)</label>
      <input type="password" name="slack_app_token" placeholder="xapp-…" autocomplete="off">

      <label>Allowed Slack user IDs (e.g. U01ABCDE), comma-separated</label>
      <input type="text" name="slack_allowed_user_ids" placeholder="U01ABCDE" autocomplete="off">
    </div>

    <div class="card">
      <h2>Advanced <span class="pill optional">optional</span></h2>
      <div class="help">Leave blank to use defaults.</div>
      <label>WEBHOOK_SECRET — bearer token for the /webhook endpoint (auto-generated if blank)</label>
      <input type="text" name="webhook_secret" placeholder="(leave blank to auto-generate)" autocomplete="off">

      <label>WEBAPP_URL — HTTPS URL of the Telegram Mini App (optional)</label>
      <input type="text" name="webapp_url" placeholder="https://example.com/app" autocomplete="off">

      <label>CLAUDE_BIN — path to the claude CLI (auto-detected if blank)</label>
      <input type="text" name="claude_bin" placeholder="(leave blank to auto-detect)" autocomplete="off">
    </div>

    <div class="actions">
      <button class="primary" type="submit">Save and finish</button>
      <span id="status" class="status"></span>
    </div>
  </form>
</div>

<script>
const form = document.getElementById("form");
const status = document.getElementById("status");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  status.textContent = "Saving…";
  status.className = "status";

  const data = Object.fromEntries(new FormData(form).entries());
  data.web_enabled = form.web_enabled.checked;

  try {
    const r = await fetch("/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    const body = await r.json();
    if (!r.ok || !body.ok) {
      status.textContent = body.error || "Save failed.";
      status.className = "status err";
      return;
    }
    status.textContent = "Saved. " + (body.note || "");
    status.className = "status ok";
    form.querySelector("button").disabled = true;
  } catch (err) {
    status.textContent = "Network error: " + err.message;
    status.className = "status err";
  }
});
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Save logic
# ---------------------------------------------------------------------------
def _quote(value: str) -> str:
    """Shell-style quoting so the .env survives spaces and special chars."""
    if not value:
        return '""'
    if any(ch in value for ch in ' \t"\\$#`\n'):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def write_env(path: Path, settings: dict) -> None:
    lines: list[str] = ["# Generated by setup_wizard.py — edit by hand any time"]

    def add(key: str, value: Optional[str], *, comment: Optional[str] = None):
        if value is None or value == "":
            return
        if comment:
            lines.append(f"# {comment}")
        lines.append(f"{key}={_quote(value)}")

    add("TELEGRAM_BOT_TOKEN", settings.get("telegram_bot_token"),
        comment="From @BotFather. Keep secret.")
    add("ALLOWED_USERS", settings.get("allowed_users"))
    add("ALLOWED_USER_IDS", settings.get("allowed_user_ids"))

    secret = settings.get("webhook_secret") or secrets.token_hex(32)
    add("WEBHOOK_SECRET", secret, comment="Bearer token for /webhook")

    add("WEBAPP_URL", settings.get("webapp_url"))
    add("CLAUDE_BIN", settings.get("claude_bin"))

    # Discord
    if settings.get("discord_bot_token"):
        lines.append("")
        lines.append("# Discord — discord.com/developers/applications")
        add("DISCORD_BOT_TOKEN", settings.get("discord_bot_token"))
        add("DISCORD_ALLOWED_USER_IDS", settings.get("discord_allowed_user_ids"))

    # Slack
    if settings.get("slack_bot_token") and settings.get("slack_app_token"):
        lines.append("")
        lines.append("# Slack — api.slack.com/apps (Socket Mode)")
        add("SLACK_BOT_TOKEN", settings.get("slack_bot_token"))
        add("SLACK_APP_TOKEN", settings.get("slack_app_token"))
        add("SLACK_ALLOWED_USER_IDS", settings.get("slack_allowed_user_ids"))

    web_enabled = bool(settings.get("web_enabled"))
    if not web_enabled:
        lines.append("WEB_DISABLED=1")
    else:
        port = (settings.get("web_port") or "").strip()
        if port:
            lines.append(f"WEB_PORT={port}")
        # Auto-generate a stable web auth token so reconnects keep working
        lines.append(f"WEB_AUTH_TOKEN={secrets.token_urlsafe(24)}")

    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------
async def _serve_index(_request: web.Request) -> web.Response:
    return web.Response(text=_SETUP_HTML, content_type="text/html")


async def _save(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)

    token = (data.get("telegram_bot_token") or "").strip()
    allowed_users = (data.get("allowed_users") or "").strip()
    allowed_ids = (data.get("allowed_user_ids") or "").strip()
    web_enabled = bool(data.get("web_enabled"))
    discord_token = (data.get("discord_bot_token") or "").strip()
    slack_bot = (data.get("slack_bot_token") or "").strip()
    slack_app = (data.get("slack_app_token") or "").strip()

    if not (token or web_enabled or discord_token or (slack_bot and slack_app)):
        return web.json_response(
            {"ok": False, "error": "Configure at least one chat (Telegram, Web, Discord, or Slack)."},
            status=400,
        )
    if token and not allowed_users and not allowed_ids:
        return web.json_response(
            {"ok": False, "error": "Telegram requires ALLOWED_USERS or ALLOWED_USER_IDS — otherwise the bot is open to anyone."},
            status=400,
        )
    if (slack_bot and not slack_app) or (slack_app and not slack_bot):
        return web.json_response(
            {"ok": False, "error": "Slack needs BOTH Bot Token (xoxb-…) AND App-level Token (xapp-…)."},
            status=400,
        )

    try:
        write_env(ENV_PATH, data)
    except OSError as e:
        return web.json_response({"ok": False, "error": f"could not write .env: {e}"}, status=500)

    note = f"Configuration written to {ENV_PATH}. Restart Alfred with: python3 app.py"
    # Schedule shutdown so the success banner shows first
    asyncio.get_running_loop().call_later(1.5, _shutdown)
    return web.json_response({"ok": True, "note": note})


def _shutdown() -> None:
    logger.info("Setup complete; stopping wizard")
    raise SystemExit(0)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def _port_available(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


async def serve(host: str = "127.0.0.1", port: int = 8080, open_browser: bool = True) -> None:
    """Run the wizard until the user clicks Save (or Ctrl-C)."""
    if not _port_available(host, port):
        raise RuntimeError(
            f"Port {port} is busy on {host}. Stop whatever is using it, "
            "or run: python3 setup_wizard.py --port 9000"
        )

    app = web.Application()
    app.add_routes([
        web.get("/", _serve_index),
        web.post("/save", _save),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    url = f"http://{host}:{port}/"
    print(f"\n  🎩 Alfred setup — open {url} in your browser\n", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        # Run until SystemExit (raised when /save completes)
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Alfred first-run setup wizard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open the browser")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        asyncio.run(serve(host=args.host, port=args.port, open_browser=not args.no_browser))
    except SystemExit:
        # /save handler invoked _shutdown; exit cleanly
        pass
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
