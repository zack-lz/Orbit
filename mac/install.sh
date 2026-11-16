#!/bin/bash
# Orbit bridge installer (macOS). Runs on YOUR Mac, next to your own Claude Code login.
#   ./install.sh            install + start + open the QR pairing page
#   ./install.sh --no-launchd   (testing) don't register the LaunchAgent / don't start
#   ./install.sh --uninstall    stop, remove hooks and files
set -euo pipefail
DIR="$HOME/.claude-watch"
SRC="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.orbit.bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SETTINGS="$HOME/.claude/settings.json"
LAUNCHD=1
[ "${1:-}" = "--no-launchd" ] && LAUNCHD=0

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  python3 - "$SETTINGS" <<'PY'
import json, sys, os
p = sys.argv[1]
if os.path.exists(p):
    s = json.load(open(p))
    if "claude-watch" in json.dumps(s.get("statusLine", {})):
        s.pop("statusLine", None)
    h = s.get("hooks", {})
    h["PermissionRequest"] = [g for g in h.get("PermissionRequest", []) if "claude-watch" not in json.dumps(g)]
    if not h["PermissionRequest"]:
        h.pop("PermissionRequest")
    if not h:
        s.pop("hooks", None)
    json.dump(s, open(p, "w"), indent=2)
PY
  rm -rf "$DIR"
  echo "Orbit bridge removed."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required (xcode-select --install)"; exit 1; }
mkdir -p "$DIR/apns" "$HOME/.claude" "$HOME/Library/LaunchAgents"
chmod 700 "$DIR" "$DIR/apns"
cp "$SRC/server.py" "$SRC/orbit_bridge.py" "$SRC/music_mac.py" "$SRC/statusline.py" "$SRC/permission_hook.py" "$DIR/"
rm -rf "$DIR/web" && cp -R "$SRC/web" "$DIR/web"
chmod +x "$DIR"/*.py

# private venv (for the QR code); the bridge itself is stdlib-only
if [ ! -x "$DIR/venv/bin/python" ]; then python3 -m venv "$DIR/venv"; fi
"$DIR/venv/bin/pip" install -q --disable-pip-version-check qrcode

# strong random pairing token (kept if it already exists)
if [ ! -s "$DIR/token" ]; then
  python3 -c "import secrets;print(secrets.token_hex(16),end='')" > "$DIR/token"
fi
chmod 600 "$DIR/token"

# Claude Code integration: permission requests -> phone/watch, status line -> live usage
[ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak-orbit-$(date +%Y%m%d%H%M%S)"
python3 - "$SETTINGS" "$DIR" <<'PY'
import json, os, sys
p, d = sys.argv[1], sys.argv[2]
s = json.load(open(p)) if os.path.exists(p) else {}
py = f'{d}/venv/bin/python'
s.setdefault("statusLine", {"type": "command", "command": f'"{py}" "{d}/statusline.py"'})
hooks = s.setdefault("hooks", {}).setdefault("PermissionRequest", [])
if not any("claude-watch" in json.dumps(g) for g in hooks):
    hooks.append({"matcher": "*", "hooks": [{"type": "command", "command": f'"{py}" "{d}/permission_hook.py"', "timeout": 120}]})
json.dump(s, open(p, "w"), indent=2)
PY

if [ $LAUNCHD = 1 ]; then
  cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/venv/bin/python</string><string>$DIR/server.py</string></array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$DIR/bridge.log</string>
</dict></plist>
PL
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  sleep 2
  echo "Bridge running. Opening the pairing page — scan it with Orbit (Claude tab → 扫码连接)."
  open "http://localhost:8765/connect"
else
  echo "Installed (not started)."
fi
