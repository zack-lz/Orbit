# Orbit bridge for Mac

Runs on your Mac and gives the Orbit iPhone / Apple Watch app: your Claude Code quota, permission requests on your wrist, and a remote for the Mac's **Music** app.

## Requirements

* macOS 12 or newer
* Python 3 (`xcode-select --install` if `python3 --version` fails)
* For quota and approvals: Claude Code installed and signed in (`claude auth login`)
* For music: nothing — the Music app is built in

## Install

```bash
cd ~/Downloads/Orbit-Bridge     # the folder you unzipped
./install.sh
```

The script:

1. copies the bridge to `~/.claude-watch/`
2. creates a private Python environment there and installs the `qrcode` package
3. generates a random 128-bit access key (kept if it already exists)
4. connects Claude Code (backs up `~/.claude/settings.json` first): the permission hook, and a status line if you have none
5. installs and starts a user LaunchAgent, `com.orbit.bridge`
6. opens `http://localhost:8765/connect` with two QR codes

| QR | Scan it in Orbit's… |
|---|---|
| **Claude** | Claude tab → Scan to connect |
| **Music** | Music tab → Scan to add |

The first time Orbit controls Music, macOS asks to allow Python to control "Music" — choose **OK** (or enable it under *System Settings → Privacy & Security → Automation*).

## Everyday use

* The bridge starts at login and restarts if it quits.
* Re-open the pairing page any time: `http://localhost:8765/connect` (only from this Mac).
* Logs: `~/.claude-watch/bridge.log`.

## What works in Music

Play/pause, previous/next, seek, volume, mute (volume to 0 and back), cover art, the next 30 tracks of the current playlist (tap to play), and plain-text lyrics. The Music app does not expose timed lyrics to scripts.

## Uninstall

```bash
./install.sh --uninstall
```

Stops the service and removes the hooks it added, `~/.claude-watch/` and the LaunchAgent.

## Files

| File | Purpose |
|---|---|
| `orbit_bridge.py` / `server.py` | The bridge (`server.py` is a thin launcher) |
| `music_mac.py` | Music.app control, artwork, queue, lyrics |
| `web/` | The music remote's web interface |
| `permission_hook.py`, `statusline.py` | Claude Code hook and status line |
| `install.sh` | Installer / uninstaller |

See the [main README](../README.md), [architecture](../docs/ARCHITECTURE.md) and [security notes](../docs/SECURITY.md).
