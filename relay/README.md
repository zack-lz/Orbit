# Orbit relay

A small Cloudflare Worker that gives every Orbit user **background push notifications** and **access away from home Wi-Fi**, without ever seeing readable data (see [Architecture](../docs/ARCHITECTURE.md#relay-push-and-remote-access)).

You only need this if you run your own copy of the Orbit app, or you don't want to use the shared relay.

## Deploy

Requirements: a Cloudflare account, Node.js, and an Apple **APNs key** (Apple Developer → Keys → *Apple Push Notifications service*) for your app's bundle id.

```bash
cd relay
npx wrangler login
npx wrangler kv namespace create KV        # copy the id into wrangler.toml
npx wrangler deploy
npx wrangler secret put APNS_KEY < AuthKey_XXXXXXXXXX.p8
printf 'XXXXXXXXXX' | npx wrangler secret put APNS_KEY_ID   # the key id
printf 'YYYYYYYYYY' | npx wrangler secret put APNS_TEAM_ID  # your team id
```

Set `TOPIC` in `wrangler.toml` to your app's bundle id. Then point the app (`Config.relayURL`) and bridges (`ORBIT_RELAY_URL`) at your Worker URL.

## Endpoints

| Route | Caller | Purpose |
|---|---|---|
| `POST /v1/register` | app | Register an APNs device token → `relay_id`, `relay_secret` |
| `POST /v1/update` | app | Update the token |
| `POST /v1/push` | bridge | Forward an encrypted push |
| `POST /v1/chan/<chan>/sync` | bridge | Upload encrypted state, receive encrypted decisions |
| `GET /v1/chan/<chan>/state` | app | Read the encrypted state |
| `POST /v1/chan/<chan>/decide` · `/poke` | app | Answer an approval · wake the bridge |

## Limits and cost

Registration is limited to 20 per IP per hour and pushes to 20 per minute per phone. The mailbox uses a Durable Object per pairing; the free plan covers a handful of users, a larger audience needs Workers Paid.
