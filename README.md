# Orbit bridge (Mac)

Companion program for the **Orbit** iPhone / Apple Watch app. It runs on your Mac and lets Orbit show your
Claude Code usage, approve permission requests from your watch, and control the Mac's Music app.

## Install
1. Download **[Orbit-Bridge-mac.zip](Orbit-Bridge-mac.zip)** and unzip it.
2. Open Terminal, `cd` into the folder, run `./install.sh`.
3. A browser page with two QR codes opens. In Orbit, scan the **Claude** code on the Claude tab and the **音乐 (Music)** code on the Music tab.
4. The first time Orbit controls Music, macOS asks for permission — click OK.

Requires macOS with Python 3 (`xcode-select --install`). Uninstall: `./install.sh --uninstall`.

The pairing QR codes contain a private access key. Do not share screenshots of them.
