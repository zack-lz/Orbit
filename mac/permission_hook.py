#!/usr/bin/env python3
"""Claude Code PermissionRequest hook: forwards the request to the watch bridge
and waits for Allow/Deny from the Apple Watch.

If the bridge is not running or nobody answers in time, it prints nothing and
exits 0, so Claude Code shows its normal permission prompt on the Mac.
"""
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
WAIT = int(os.environ.get("CLAUDE_WATCH_WAIT", "110"))


def main():
    try:
        hook = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return
    hook["_wait"] = WAIT
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/request",
        data=json.dumps(hook).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=WAIT + 5) as r:
            decision = json.load(r).get("decision")
    except Exception:  # noqa: BLE001  bridge down -> normal prompt
        return
    if not decision:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
