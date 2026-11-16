#!/usr/bin/env python3
"""Claude Code status line: forwards the status JSON (which includes
rate_limits.five_hour) to the watch bridge, and prints a short status line."""
import json
import os
import sys
import urllib.request

def _port():
    if os.environ.get("CLAUDE_WATCH_PORT"):
        return os.environ["CLAUDE_WATCH_PORT"]
    try:
        return open(os.path.join(os.path.expanduser("~"), ".claude-watch", "port")).read().strip() or "8765"
    except OSError:
        return "8765"


PORT = _port()

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except Exception:  # noqa: BLE001
    data = {}

try:
    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{PORT}/usage", data=raw.encode() or b"{}",
        headers={"Content-Type": "application/json"}, method="POST"), timeout=0.5).close()
except Exception:  # noqa: BLE001
    pass

parts = []
model = (data.get("model") or {}).get("display_name")
if model:
    parts.append(model)
fh = (data.get("rate_limits") or {}).get("five_hour") or {}
if fh.get("used_percentage") is not None:
    parts.append(f"5h 剩余 {100 - fh['used_percentage']:.0f}%")
print(" | ".join(parts))
