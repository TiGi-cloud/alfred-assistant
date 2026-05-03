"""
adapters — concrete chat platform integrations.

Each module in this package implements `kernel.ChatAdapter` for one chat
platform: Telegram, Discord, Slack, a browser-based web UI, etc. The bot's
event loop accepts a list of started adapters and reads messages /
callbacks from all of them concurrently.

Status:
  - telegram.py — TODO: wraps the existing top-level Telegram code
  - web.py      — TODO: WebSocket-based chat UI for non-Telegram users
"""
