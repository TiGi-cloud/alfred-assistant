# iMessage setup

macOS-only. Has trade-offs you should know about before relying on it.

## How it works

There's no public iMessage API. Alfred reads `~/Library/Messages/chat.db` (the SQLite file Messages.app uses) every 1.5 seconds for new inbound messages, and sends replies by asking Messages.app via AppleScript. Both halves are unsupported by Apple.

| | What that means |
|---|---|
| ✅ Works | If your Mac is on, Messages.app is signed in to your Apple ID, and you've granted permissions |
| ⚠️ Brittle | macOS upgrades change the schema annually-ish. AppleScript send fails ~5% under load. |
| ⚠️ 1:1 only | Group chats deliberately skipped in v1 |
| ⚠️ Polling | 1–2 sec delay on incoming messages |
| ⚠️ Mac-tied | If your Mac sleeps / loses Apple ID auth, iMessage stops working |

## 1. Sign in to Messages.app

Open Messages.app. **System Settings → Apple ID** → make sure iMessage is enabled. Send yourself a test message from your phone to confirm Messages.app is receiving.

## 2. Grant Full Disk Access

Alfred needs to read `chat.db`, which is privacy-protected. Grant **Full Disk Access** to **the Python interpreter** Alfred runs with.

1. **System Settings → Privacy & Security → Full Disk Access**
2. Click `+` → press Cmd+Shift+G → paste the path to your Python:
   - System Python: `/Library/Developer/CommandLineTools/usr/bin/python3`
   - Homebrew: `/opt/homebrew/opt/python@3.12/bin/python3.12`
   - Your venv: `<repo>/venv/bin/python3.11`

   Run `which python3` after activating your venv to find the right path.
3. Toggle it ON.
4. Restart Alfred.

If you skip this, Alfred logs `authorization denied` and the iMessage adapter does nothing.

## 3. Enable in `.env`

```
IMESSAGE_ENABLED=1
IMESSAGE_ALLOWED_HANDLES=+15551234567,you@icloud.com   # phone or Apple-ID email, comma-separated
```

Without `IMESSAGE_ALLOWED_HANDLES`, **anyone who DMs your Apple ID can run shell commands**. Strongly set it.

## 4. Run

```bash
python3 app.py
```

You should see:

```
00:00:00 INFO  alfred.adapters.imessage — iMessage adapter started; polling chat.db every 1.5s
```

## 5. First send pops a permission dialog

The first time Alfred sends a message, macOS asks **"Python wants to control Messages"** — click **OK**. This goes in System Settings → Privacy & Security → Automation. If you click Deny, iMessage send is broken until you re-enable.

## 6. Test

From your iPhone (or another iMessage account), text your Mac's Apple-ID handle:

- `ping` → `pong 🏓`
- `whoami` → adapter / user metadata
- `screenshot` → photo of the Mac

## Stand-alone test harness

Before integrating, you can test just the iMessage adapter without Telegram/Web/etc.:

```bash
IMESSAGE_ALLOWED_HANDLES="+15551234567" python3 test_imessage.py
```

It boots only the iMessage adapter and runs `ping/pong/screenshot/echo`.

## Verifying chat.db access

If you suspect Full Disk Access isn't granted:

```bash
python3 -c "
from kernel.store import set_db_path
import sqlite3
from pathlib import Path
chat_db = Path.home() / 'Library/Messages/chat.db'
print('exists:', chat_db.exists())
conn = sqlite3.connect(f'file:{chat_db}?mode=ro', uri=True)
print(conn.execute('SELECT count(*) FROM message').fetchone())
"
```

If you see `authorization denied`, FDA isn't granted to the Python interpreter you ran.

## What works in iMessage

| Feature | Notes |
|---|---|
| 1:1 inbound text | ✅ |
| Inbound photos / files | ✅ — already on disk under `~/Library/Messages/Attachments` |
| Outbound text | ✅ via AppleScript |
| Outbound photos | ✅ — `(POSIX file "..." as alias)` |
| Inline buttons | ⚠️ Rendered as numbered text lines (iMessage has no buttons) |
| Group chats | ❌ |
| Read receipts / typing indicators | ❌ |
| Edit / delete sent messages | ❌ — AppleScript doesn't expose those |

## SMS via Continuity

If your Mac is paired with an iPhone that has Text Message Forwarding enabled, the iMessage adapter falls back to SMS automatically when the recipient isn't on iMessage. So you can text any phone number, not just iMessage users — as long as your iPhone is online.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot read chat.db: authorization denied` | Grant Full Disk Access to the **Python interpreter you launched with** |
| `osascript send failed` on first message | Accept the "wants to control Messages" prompt in System Settings → Privacy & Security → Automation |
| Empty text in messages | Newer macOS stores text in an `attributedBody` BLOB. The decoder is best-effort and sometimes fails — open an issue with your macOS version |
| Group chat messages ignored | By design in v1 |
| Schema-change errors after macOS upgrade | macOS rev'd `chat.db`. Open an issue. |

See [troubleshooting.md](../troubleshooting.md) for more.
