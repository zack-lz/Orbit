# Security

Orbit is built for a **trusted home network**. Read this before using it anywhere else.

## What the bridge exposes

| Capability | Needs the key | Who can reach it |
|---|---|---|
| Read quota, today's stats, recent approvals | yes | private-network clients |
| Answer a pending Claude Code permission request | yes | private-network clients |
| Control the music player | yes | private-network clients |
| Submit a permission request / usage | no key, **loopback only** | programs on the same computer |
| Show the pairing page | no key, **loopback only** | a browser on the same computer |

The full pairing link (the QR code) **is** the key. Whoever has it can read your quota and *allow Claude Code actions* from the network. Do not share screenshots of the QR page.

## What protects it

* Private-address filter: public IPs are rejected before authentication.
* Long random key (Mac: 128-bit hex, Windows: 256-bit URL-safe), constant-time comparison.
* Lockout: 8 failed attempts from one address ⇒ blocked for 5 minutes.
* Key in the URL fragment — never in request lines or logs. WebSocket authentication happens in the first frame.
* Approval decisions are single-use and expire; the hook falls back to the normal prompt when nobody answers.
* App side: Sign in with Apple, Face ID / passcode, re-lock after 30 s, hidden app-switcher snapshot, **Allow** on the lock screen requires authentication, connections wiped on sign-out or account change.

## Known limits

* **Plain HTTP.** The LAN connection is not encrypted; anyone who can sniff your Wi-Fi can read the key. Use a WPA2/WPA3 network you trust.
* **No per-device revocation.** Keys do not expire. To revoke everyone:
  * Mac: delete `~/.claude-watch/token` and run `launchctl kickstart -k gui/$(id -u)/com.orbit.bridge`, then re-pair.
  * Windows: run `STOP.bat`, delete `.state\pairing.key`, run `START.bat`, then re-pair.
* **Keys are stored in plain text** on the computer and on the phone.
* **The hook can be used to allow actions.** Keep the bridge's network exposure small and do not enable approvals on a network you do not control.
* **Local-only does not mean safe from local malware.** Anything running as you can read the key file.

## Do not

* Forward ports 8765 / 8766 to the internet or put the bridge behind a public reverse proxy.
* Disable the firewall to "make it work" — allow Python on **private** networks only.
* Commit `token`, `pairing.key`, `apns.json`, `devices.json` or `.p8` files to a repository.

## Reporting a problem

Please report security issues privately to the repository owner rather than in a public issue, and never paste QR codes, keys or logs containing personal data.
