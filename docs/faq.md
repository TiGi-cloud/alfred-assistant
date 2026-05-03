# FAQ

## Why another self-hosted Claude bot?

Alfred is **Mac-first** and runs **through the Claude Code CLI**, not the Anthropic API.

The Claude Code CLI ships with Claude's full agentic toolset — Bash, file ops, MCP servers, web fetch, etc. So when you message Alfred "summarise the last 10 git commits in my dotfiles repo", Claude Code does the `git log` + reading + summarising itself; Alfred just relays the text/attachments to chat.

The trade-off: you're locked to Claude. No GPT, no Gemini. If you want multi-LLM, look at [OpenClaw](https://github.com/openclaw/openclaw) instead — it talks to LLM APIs directly.

## How is this different from OpenClaw?

| | Alfred | OpenClaw |
|---|---|---|
| LLM | Claude only (via the Claude Code CLI) | Claude / GPT / Gemini / DeepSeek |
| Tools | inherits Claude Code's full toolset | implements its own |
| Mac-native | first-class — Vision OCR, AppleScript, iMessage chat.db | works on Mac but not optimised |
| WhatsApp | not supported (TOS / cost concerns) | supported |
| Maturity | new | months of public iteration |

Both are MIT-licensed, both are self-hosted, both have ~the same chat-platform list. **Use OpenClaw if you want multi-LLM. Use Alfred if you want the deepest Claude Code integration on a Mac.**

## Does Alfred work on Linux?

Partly. The chat-platform side and the Claude pipeline work fine. Mac-specific features (`/screenshot`, `/ocr`, `/camera`, `/apps`, `/clipboard`, `/wifi`, AppleScript-driven things, iMessage) all return "macOS only" on other platforms. There's no fundamental reason Linux equivalents couldn't be added — PRs welcome.

## Does it work on Windows?

The setup wizard, web chat, and Telegram/Discord/Slack adapters work. Most of the system commands shell out to macOS-specific binaries and won't work. iMessage definitely won't.

## Can multiple people use the same Alfred?

Each user shows up to Alfred as a different `user.id`, so memories, projects, fork branches, schedules, and active machines are scoped per-user. But: every user shares the same Mac, the same shell access, the same files. If they're not all you-or-someone-you'd-let-`rm -rf`-your-laptop, **don't share access**.

The setup wizard and `app.py` warn loudly if any adapter has no allowlist.

## Can I host it for friends?

**Don't.** Alfred runs `claude -p --dangerously-skip-permissions` — Claude has unrestricted shell access to whatever box Alfred runs on. A friend with bot access can `rm -rf ~/`, `cat ~/.ssh/id_rsa`, …

If you really want to: it would have to be a single-user instance per friend on their own machine, which is what `install.sh` does.

## Why doesn't Alfred sandbox Claude?

The whole point is that Claude can do whatever you'd do at the terminal. Sandboxing Claude breaks ~80% of the use cases (file edits, builds, deploys, AppleScript). The protection layer is **the chat allowlist**, not a code sandbox.

If you want a sandboxed version, use Claude Code directly with permissions enabled — it'll prompt for each shell action.

## How much does it cost to run?

Costs are pay-as-you-go to Anthropic. Order-of-magnitude:

- A typical chat turn: a few cents
- `/research`: ~$0.05–0.20 per call (15 parallel Haiku + 1 Sonnet synthesis)
- Cache hits dramatically reduce cost — `/cost` shows them

`/cost` per chat shows tokens + estimated USD using the rough Anthropic pricing baked into `actions/session.py`. Anthropic's [pricing page](https://www.anthropic.com/pricing) is the source of truth.

## Where does my data go?

- **Chat platform**: messages flow through Telegram/Discord/Slack/Apple servers like any other DM. Their privacy policies apply.
- **Claude**: prompts go to Anthropic per their API terms. Set `ANTHROPIC_DISABLE_TELEMETRY=1` in `.env` if you want it off.
- **Local**: everything else (memories, projects, sessions, costs) stays on your Mac in `alfred.db` and the `*.json` state files. Nothing else phones home.

## Can I switch from `bot.py`/legacy to `app.py`?

You're on `app.py` already — the legacy code was deleted. If you're upgrading from a pre-v1 clone, the SQLite schema is identical, so your existing memories migrate automatically. Conversations don't migrate (Claude session IDs are different across versions).

## Will Alfred remember me across reboots?

Yes — sessions, memories, schedules, alerts, projects, machines, costs all persist to disk. The only thing that doesn't persist is the live "thinking…" state of an in-flight Claude turn (it's in memory).

## How do I add a new chat platform?

See [architecture.md → Adding a new chat platform](architecture.md#adding-a-new-chat-platform). Short version: subclass `kernel.ChatAdapter`, implement 14 methods, register in `app.py`. ~250-400 lines per platform based on the existing 5.

## How do I write a custom command?

See [plugins.md](plugins.md). A trivial `/hello` is 4 lines + 1 line in `actions/__init__.py`.

## How do I delete everything?

```bash
rm .env alfred.db* alfred_*.json claude_*.json
launchctl unload ~/Library/LaunchAgents/com.alfred.bot.plist 2>/dev/null
rm ~/Library/LaunchAgents/com.alfred.bot.plist 2>/dev/null
```

Revoke macOS permissions in **System Settings → Privacy & Security**. Revoke chat tokens in their respective developer portals.

## Where do I report bugs?

[GitHub Issues](https://github.com/TiGi-cloud/alfred-assistant/issues). The bug report template asks for the right info. Include `python3 app.py 2>&1 | tail -50` if anything's exploding.

## Where can I read more?

- [quickstart.md](quickstart.md) — first 5 minutes
- [commands.md](commands.md) — every command, with examples
- [architecture.md](architecture.md) — kernel + adapters + actions, for contributors
- [security.md](security.md) — auth model, what to be careful about
- [plugins.md](plugins.md) — write your own command
- [troubleshooting.md](troubleshooting.md) — when something doesn't work
- [setup/](setup/) — per-platform getting-started guides
