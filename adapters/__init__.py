"""
adapters — concrete chat platform integrations.

Each module in this package implements `kernel.ChatAdapter` for one chat
platform. The bot's event loop accepts a list of started adapters and
reads messages / callbacks from all of them concurrently.

Modules:
  telegram.py  — wraps `python-telegram-bot` (required core dep)
  web.py       — aiohttp server: chat UI at /, dashboard at /dashboard,
                 JSON API at /api/* powering the dashboard
  discord.py   — wraps `discord.py` (optional dep, lazy-imported)
  slack.py     — wraps `slack-bolt` async over Socket Mode (optional dep)
  imessage.py  — macOS chat.db poll + AppleScript send (stdlib only)
"""
