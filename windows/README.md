# Orbit bridge for Windows

Runs on your Windows PC and gives the Orbit iPhone / Apple Watch app: a full **Apple Music** remote (now playing, Up Next, timed lyrics, volume), your **Claude Code** quota, and permission requests on your wrist.

It bundles the [Apple Music Remote](https://github.com/zack-lz/Apple-Music-Remote) engine with the Orbit Claude bridge, in one process.

## Requirements

* Windows 10 / 11
* **Apple Music for Windows** (Microsoft Store)
* **64-bit Python 3.11** — the launcher guides you if it is missing
* For quota and approvals: **Claude Code** installed and signed in (`claude` in a terminal)
* iPhone and PC on the same Wi-Fi

## Install

1. Unzip into a **permanent** folder, for example `C:\Orbit`.
2. Double-click **`ENABLE_AUTO_START.bat`**. It creates `.venv`, installs the dependencies, connects Claude Code, starts Orbit and enables start-up after you sign in. For manual use, double-click **`START.bat`** instead.
3. Allow Python on **Private networks** when Windows Firewall asks. Keep the firewall on.
4. Open Apple Music and play a song. A browser page opens with two QR codes:

| QR | Scan it in Orbit's… |
|---|---|
| **音乐控制 Music** | Music tab → Scan to add |
| **Claude 额度与授权 Claude** | Claude tab → Scan to connect |

Choose the set whose address matches your Wi-Fi. Reserve a fixed LAN IP for the PC in your router so saved connections never go stale.

### If Python 3.11 is missing

Double-click `ENABLE_AUTO_START.bat` anyway: it opens the Python download page. Install the **Windows installer (64-bit)**, or the **Python install manager** followed by `py install 3.11` in a new PowerShell window. Then double-click `ENABLE_AUTO_START.bat` again.

## Files and scripts

| File | Purpose |
|---|---|
| `START.bat` | Prepare the environment, connect Claude Code, run in the background, open the pairing page |
| `ENABLE_AUTO_START.bat` / `REMOVE_AUTO_START.bat` | Add / remove the start-up shortcut `Orbit Bridge.lnk` |
| `STOP.bat` | Stop the background runner |
| `server.py` | Apple Music remote (port 8765); also starts the Claude bridge on port 8766 |
| `orbit_bridge.py` | Claude bridge: quota, approvals, history, stats, live refresh |
| `setup_claude.py` | Adds the Claude Code permission hook / status line (idempotent, backs up settings) |
| `permission_hook.py`, `statusline.py` | Claude Code hook and status line |
| `player_worker.py`, `*.ps1`, `timed_lyrics.py` | Windows media session, queue and lyrics |
| `index.html`, `assets/` | The web remote |

State (keys, logs) lives in `.state\`: `pairing.key`, `server.log`, `runner.log`. **Never share `pairing.key`.**

## Troubleshooting

| Symptom | Try this |
|---|---|
| Phone can't connect | Same Wi-Fi? Allow Python on **Private networks** (ports 8765 and 8766). Visit `http://localhost:8765/connect` on the PC. |
| Music works, Claude is empty | Claude Code must be installed and signed in. Re-run `START.bat` to reconnect the hook. |
| "Python 3.11 is required" | See *If Python 3.11 is missing*. |
| Nothing at start-up | Run `START.bat`, read the window, check `.state\server.log`. |
| Old copy still running | Run `STOP.bat` in the old folder first. |

## Uninstall

`STOP.bat` → `REMOVE_AUTO_START.bat` → delete the folder. To drop the Claude hook, remove the `permission_hook.py` entry from `%USERPROFILE%\.claude\settings.json` (backup: `settings.json.bak-orbit`).

## Away from home & push

Remote access and background push work through the Orbit relay (end-to-end encrypted; see the [main README](../README.md#push-notifications-and-the-relay)). They need outbound HTTPS and the `cryptography` package, which the installer / launcher installs. Disable remote access with `ORBIT_REMOTE=0`; use your own relay with `ORBIT_RELAY_URL`.

See the [main README](../README.md), [architecture](../docs/ARCHITECTURE.md) and [security notes](../docs/SECURITY.md).

Music engine and web remote © their original project, [Apple Music Remote](https://github.com/zack-lz/Apple-Music-Remote). QR decoding by [jsQR](https://github.com/cozmo/jsQR) (Apache-2.0, `assets/jsQR-LICENSE.txt`).

## License

Copyright 2026 Zixian Lin. Licensed under the [PolyForm Noncommercial License 1.0.0](../LICENSE) (a copy is included as `LICENSE`): free for personal and other noncommercial use; no commercial use or resale without permission.
