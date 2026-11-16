# Architecture

Orbit has two halves: the **app** (iPhone + Apple Watch) and the **bridge** (a small server on your own computer). They talk directly over the local network.

## Components

| Component | Where | Role |
|---|---|---|
| `orbit_bridge.py` | Mac and Windows | HTTP server: quota, approvals, history, today's stats, live refresh, optional APNs push |
| `music_mac.py` + `web/` | Mac | Controls Music.app with AppleScript and serves the web remote |
| Apple Music remote (`server.py`, `player_worker.py`, …) | Windows | Reads the Windows media session of Apple Music, serves the same web remote |
| `permission_hook.py` | both | Claude Code `PermissionRequest` hook → bridge |
| `statusline.py` | both | Claude Code status line → bridge (live usage) |
| `install.sh` / `setup_claude.py` | Mac / Windows | Installs and connects Claude Code to the bridge |

## Ports

| Port | Mac | Windows |
|---|---|---|
| 8765 | Everything (Claude + Music) | Music remote |
| 8766 | — | Claude bridge (runs in the same process as the music remote) |

Both listen on all IPv4 interfaces but reject any client that is not on a private / loopback / link-local address.

## Pairing

Each bridge has one secret key.

* Mac: `~/.claude-watch/token` (32 hex characters).
* Windows: `.state\pairing.key` (43 URL-safe characters), shared by the music remote and the Claude bridge.

The QR codes encode `http://<lan-ip>:<port>/<path>#<key>` — the key is in the URL *fragment*, so it never appears in a request line or a server log.

| QR | Path |
|---|---|
| Claude | `/orbit` |
| Music | `/app/9/` |

The app validates the link (LAN address, 20–128 character key), stores it on the device and, for Claude, forwards it to the Apple Watch over WatchConnectivity.

## Claude bridge API

Authentication: header `X-Token: <key>` (compared in constant time). Eight failures from one address within five minutes lock that address out for the rest of that window.

| Route | Purpose |
|---|---|
| `GET /state` | Usage, pending approvals, recent decisions, today's stats, forecast, host name |
| `POST /refresh` | Take a live reading now (see below) and return fresh state |
| `POST /decide` | `{id, allow}` — answer a pending approval |
| `POST /register` | Store the iPhone's APNs device token |
| `POST /request` *(loopback only)* | Used by the Claude Code hook; blocks until answered or timed out |
| `POST /usage` *(loopback only)* | Used by the status line |
| `GET /connect` *(loopback only)* | The QR pairing page |

## The quota model

Sources, in order of authority:

1. **Live reading** — `claude -p` with one tiny prompt on Haiku. Claude Code emits a `rate_limit_event` whose `unifiedWindows` carry the real `utilization` (0–1) and `resetsAt` for the 5-hour and 7-day windows. Costs about US$0.001. Taken on demand and about every 5 minutes while a client is polling.
2. **Usage endpoint** — a long-lived `claude` process answers `get_usage` control requests every 15 s. Cached server-side by Anthropic, can lag minutes.
3. **Status line** — pushed by Claude Code when a terminal session renders its status line.

`merge_usage` keeps the higher percentage inside the same window (`resets_at` within 5 minutes), replaces it for a new window, and lets a live reading overwrite everything.

**Forecast.** Samples of the 5-hour percentage from the last 90 minutes give a burn rate; if the window would fill before it resets, the app shows "about N until full".

**Today.** The bridge scans `~/.claude/projects/*/*.jsonl` for assistant messages of the current day (deduplicated by message id) and sums tokens per model and per hour. Nothing leaves the computer.

## Approvals

```text
Claude Code ─ PermissionRequest hook ─▶ POST /request ─▶ pending[id]
                                           │  (optional APNs push to the iPhone)
Watch / iPhone ─ GET /state ─▶ shows request
Watch / iPhone ─ POST /decide ─▶ event.set() ─▶ hook prints {"behavior": "allow" | "deny"}
```

If the bridge is unreachable, nobody answers within the wait (110 s) or the app has not polled recently, the hook prints nothing and Claude Code shows its normal prompt.

## Music protocol

The web remote (`index.html`) talks to the bridge with the Apple Music Remote protocol: `GET /api/state`, `POST /api/command` (`toggle`, `next`, `previous`, `volume`, `mute`, `seek`), `GET /api/cover`, `GET /api/queue`, `POST /api/queue/play`, `GET /api/lyrics`, and a WebSocket at `/events` (first frame carries the key). Authentication: `Authorization: Bearer <key>`.

**Mac.** A background thread polls Music.app through one AppleScript call (~0.6 s, only while a client is active) and serves cached state instantly; commands re-read state right away. State is pushed over a hand-written WebSocket (standard library only) when something changes, when the playback position drifts by more than 1.2 s, or every 4 s.

**Windows.** The original engine reads the Windows media session; queue and lyrics use small PowerShell helpers that drive the Apple Music window.

## Orbit app (for reference)

SwiftUI, iOS 17 / watchOS 10. Sign in with Apple, Face ID lock, `UNUserNotificationCenter` with an `APPROVAL` category (Allow requires authentication), a `WKWebView` that hosts the web remote, and WatchConnectivity for syncing connection details and the sign-in state to the watch.
