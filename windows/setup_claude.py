"""Connects Claude Code on this PC to the Orbit bridge (idempotent; safe to re-run).

* PermissionRequest hook  -> approval requests go to your iPhone / Apple Watch
* status line             -> live usage numbers
Existing hooks and a status line you already use are left alone.
"""
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
PORT = os.environ.get("ORBIT_CLAUDE_PORT", "8766")


def main():
    os.makedirs(os.path.join(HOME, ".claude-watch"), exist_ok=True)
    with open(os.path.join(HOME, ".claude-watch", "port"), "w") as f:
        f.write(PORT)
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    data = {}
    if os.path.exists(SETTINGS):
        try:
            data = json.load(open(SETTINGS, encoding="utf-8"))
        except ValueError:
            print("settings.json is not valid JSON; leaving it alone:", SETTINGS)
            return
        backup = SETTINGS + ".bak-orbit"
        if not os.path.exists(backup):
            shutil.copy(SETTINGS, backup)
    py = sys.executable
    hook = f'"{py}" "{os.path.join(ROOT, "permission_hook.py")}"'
    line = f'"{py}" "{os.path.join(ROOT, "statusline.py")}"'

    groups = data.setdefault("hooks", {}).setdefault("PermissionRequest", [])
    groups[:] = [g for g in groups if "permission_hook.py" not in json.dumps(g)]   # drop stale Orbit entries
    groups.append({"matcher": "*", "hooks": [{"type": "command", "command": hook, "timeout": 120}]})

    cur = data.get("statusLine")
    if not cur or "statusline.py" in json.dumps(cur):
        data["statusLine"] = {"type": "command", "command": line}
    else:
        print("You already have a Claude Code status line; live usage will use the slower fallback.")

    json.dump(data, open(SETTINGS, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print("Claude Code is connected to Orbit.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never block START.bat
        print("Claude Code setup skipped:", e)
