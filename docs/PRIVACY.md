# Orbit — Privacy Policy

*Effective 16 November 2026.*

Orbit is a companion app for iPhone and Apple Watch. It shows your Claude Code quota, lets you answer Claude Code permission requests, and remote-controls the music on your own computer. It talks to a small program (the **Orbit bridge**) that you run on **your own Mac or Windows PC**.

**Short version: Orbit has no accounts, no analytics, no advertising and no tracking. We do not collect your name, email, usage, prompts, commands or music data.**

## What stays on your devices

* **Sign in with Apple** — Apple gives the app a user identifier and (if you choose to share it) your email. They are kept only in the iOS Keychain on your device and are used to show the sign-in screen. They are never sent to us.
* **Connections** — the address and pairing key of your computer, and the music remotes you added, are stored on your device (and copied to your Apple Watch). Signing out erases them.
* **Face ID / passcode** — used locally to unlock the app. Orbit never receives biometric data.
* **Camera** — used only to scan the pairing QR code. Images are processed on the device and not stored or uploaded.
* **Local network** — used to talk to the Orbit bridge on your home network.

## What goes through the Orbit relay

For background notifications and for use away from your home Wi-Fi, Orbit uses a small relay (a Cloudflare Worker, [source code](https://github.com/zack-lz/Orbit/tree/main/relay)).

* **End-to-end encrypted.** What Claude wants to run, your quota and your answers are encrypted (AES-256-GCM) on your computer or phone with a key derived from your pairing key, which the relay never sees. The relay and Apple only see ciphertext.
* **What the relay stores:** your Apple push-notification device token(s), a hash of a random secret per device, and the latest *encrypted* state. Registrations expire after 180 days without use; the encrypted mailbox is overwritten continuously.
* **Metadata:** like any web service, Cloudflare processes the IP address of requests and the relay can see when a device or bridge connects and the size of messages. We do not use this for any other purpose.

## Third parties

* **Apple** — Sign in with Apple and the Apple Push Notification service.
* **Cloudflare** — hosts the relay.

Orbit does not communicate with Anthropic. Claude Code on *your* computer does what it always does; Orbit only reads its local output and answers its permission hooks.

## Your choices

* Sign out in Settings to erase everything stored on the device.
* Run your own relay (instructions in the repository) or disable remote access on the bridge (`ORBIT_REMOTE=0`).
* Revoke the Sign in with Apple link in iOS Settings → your name → Sign in with Apple.

## Children

Orbit is not directed at children under 13 and does not knowingly collect data from them.

## Changes and contact

If this policy changes the new version will be published at this address with an updated date. Questions: open an issue at <https://github.com/zack-lz/Orbit/issues>.
