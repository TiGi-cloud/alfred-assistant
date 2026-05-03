#!/usr/bin/env python3
"""Mock `claude` CLI for ClaudeRunner unit tests.

Emits realistic stream-json output:
  - {"type":"system","session_id":"<sid>"}
  - {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}
  - {"type":"result","session_id":"<sid>","result":"<final>","usage":{...}}

Behaviour is controlled via env vars so tests can drive different scenarios:

  FAKE_CLAUDE_TEXT        — full assistant text to emit (default: "ok.")
  FAKE_CLAUDE_SESSION     — session id to embed (default: "sess-mock")
  FAKE_CLAUDE_FAIL        — set to a code → exits with that code, writes msg to stderr
  FAKE_CLAUDE_STDERR      — content for stderr
  FAKE_CLAUDE_RECORD_ARGS — path; if set, record argv (one per line) for inspection
"""
import json
import os
import sys
import time

if path := os.environ.get("FAKE_CLAUDE_RECORD_ARGS"):
    with open(path, "a") as f:
        f.write("\n".join(sys.argv) + "\n---END---\n")

# Drain stdin (prompt) so the parent's pipe doesn't block
try:
    _ = sys.stdin.buffer.read()
except Exception:
    pass

if (fail_code := os.environ.get("FAKE_CLAUDE_FAIL")):
    sys.stderr.write(os.environ.get("FAKE_CLAUDE_STDERR", "fake failure\n"))
    sys.exit(int(fail_code))

session_id = os.environ.get("FAKE_CLAUDE_SESSION", "sess-mock")
text = os.environ.get("FAKE_CLAUDE_TEXT", "ok.")

def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()

# 1. system init
emit({"type": "system", "session_id": session_id, "subtype": "init"})

# 2. stream the text as a few deltas
chunk = max(1, len(text) // 3)
for i in range(0, len(text), chunk):
    emit({
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": text[i:i + chunk]},
    })
    time.sleep(0.01)  # simulate streaming

# 3. final result with usage
emit({
    "type": "result",
    "session_id": session_id,
    "result": text,
    "usage": {"input_tokens": 42, "output_tokens": len(text.split())},
})
