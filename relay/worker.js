// Orbit push relay — a tiny Cloudflare Worker.
//
// Why it exists: Apple only lets the app's *owner* send push notifications (it needs the owner's APNs key).
// Users' bridges therefore hand a push to this relay, and the relay forwards it to Apple.
//
// Privacy: the relay never sees what Claude wants to run. The bridge encrypts the notification text with a key
// only the user's phone knows (end-to-end, AES-256-GCM); the relay and Apple only carry ciphertext plus a
// generic fallback line. The relay stores, per registration: the APNs device token and a hash of a random secret.
//
// Endpoints
//   POST /v1/register  {device_token, env, bundle}             -> {relay_id, relay_secret}     (called by the app)
//   POST /v1/update    {device_token, env}  + relay auth         -> {ok}                         (app, token changed)
//   POST /v1/push      {enc, id}            + relay auth         -> {ok}                         (called by a bridge)
//   GET  /v1/health                                             -> {ok}
//
// Remote access (use Orbit away from home Wi-Fi) — an encrypted mailbox per pairing, in a Durable Object:
//   POST /v1/chan/<chan>/sync    bridge: upload encrypted state, receive encrypted decisions / poke flags
//   GET  /v1/chan/<chan>/state   app: latest encrypted state
//   POST /v1/chan/<chan>/decide  app: encrypted Allow/Deny
//   POST /v1/chan/<chan>/poke    app: ask the bridge for fresh data (optionally a live quota reading)
// <chan> and the bearer secret are both derived (HKDF) from the pairing token, which the relay never sees;
// everything stored is AES-GCM ciphertext. The first successful /sync claims the channel.
// relay auth = headers  X-Relay-Id: <id>  and  Authorization: Bearer <secret>

const enc = new TextEncoder();
const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const rand = (n) => { const a = new Uint8Array(n); crypto.getRandomValues(a); return hex(a); };
const sha256hex = async (s) => hex(await crypto.subtle.digest("SHA-256", enc.encode(s)));
const json = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json" } });

const TTL = 60 * 60 * 24 * 180;           // registrations expire after 180 days without use
const PUSH_PER_MINUTE = 20;
const REGISTER_PER_HOUR = 20;
let jwtCache = { t: 0, v: "" };

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

async function apnsJwt(env) {
  if (jwtCache.v && Date.now() - jwtCache.t < 40 * 60 * 1000) return jwtCache.v;
  const pem = env.APNS_KEY.replace(/-----[A-Z ]+-----/g, "").replace(/\s+/g, "");
  const der = Uint8Array.from(atob(pem), (c) => c.charCodeAt(0));
  const key = await crypto.subtle.importKey("pkcs8", der, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const head = b64u(enc.encode(JSON.stringify({ alg: "ES256", kid: env.APNS_KEY_ID })));
  const body = b64u(enc.encode(JSON.stringify({ iss: env.APNS_TEAM_ID, iat: Math.floor(Date.now() / 1000) })));
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, enc.encode(`${head}.${body}`)); // raw r||s
  jwtCache = { t: Date.now(), v: `${head}.${body}.${b64u(sig)}` };
  return jwtCache.v;
}

async function bump(env, key, ttl) {
  const n = parseInt((await env.KV.get(key)) || "0", 10) + 1;
  await env.KV.put(key, String(n), { expirationTtl: ttl });
  return n;
}

async function authed(request, env) {
  const id = request.headers.get("X-Relay-Id") || "";
  const secret = (request.headers.get("Authorization") || "").replace(/^Bearer /, "");
  if (!/^[0-9a-f]{24}$/.test(id) || !secret) return null;
  const rec = await env.KV.get(`d:${id}`, "json");
  if (!rec || !timingSafeEqual(rec.h, await sha256hex(secret))) return null;
  return { id, rec };
}

async function readJson(request, limit = 4096) {
  const text = await request.text();
  if (text.length > limit) throw new Error("too large");
  return JSON.parse(text || "{}");
}

const validToken = (t) => typeof t === "string" && /^[0-9a-fA-F]{64,200}$/.test(t);
const validEnv = (e) => e === "development" || e === "production";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      if (url.pathname === "/v1/health") return json({ ok: true });
      const m = url.pathname.match(/^\/v1\/chan\/([0-9a-f]{32})\/(sync|state|decide|poke)$/);
      if (m) {
        const stub = env.MAILBOX.get(env.MAILBOX.idFromName(m[1]));
        return stub.fetch(new Request(`https://mailbox/${m[2]}`, { method: request.method, headers: request.headers, body: request.method === "GET" ? undefined : await request.text() }));
      }
      if (request.method !== "POST") return json({ error: "not found" }, 404);

      if (url.pathname === "/v1/register") {
        const ip = request.headers.get("CF-Connecting-IP") || "?";
        if ((await bump(env, `ip:${ip}:${Math.floor(Date.now() / 3.6e6)}`, 3700)) > REGISTER_PER_HOUR) return json({ error: "rate limited" }, 429);
        const b = await readJson(request);
        const topics = [env.TOPIC, env.TOPIC_WATCH].filter(Boolean);
        if (!validToken(b.device_token) || !validEnv(b.env) || !topics.includes(b.bundle)) return json({ error: "bad request" }, 400);
        const id = rand(12), secret = rand(24);
        const tok = b.device_token.toLowerCase();
        const prev = await env.KV.get(`t:${tok}`);              // one registration per device: replace the old one
        if (prev) await env.KV.delete(`d:${prev}`);
        await env.KV.put(`t:${tok}`, id, { expirationTtl: TTL });
        await env.KV.put(`d:${id}`, JSON.stringify({ token: b.device_token.toLowerCase(), env: b.env, topic: b.bundle, kind: b.bundle === env.TOPIC_WATCH ? "watch" : "phone", h: await sha256hex(secret) }), { expirationTtl: TTL });
        return json({ relay_id: id, relay_secret: secret });
      }

      if (url.pathname === "/v1/update") {
        const a = await authed(request, env);
        if (!a) return json({ error: "unauthorized" }, 401);
        const b = await readJson(request);
        const hasTok = b.device_token !== undefined;
        if ((hasTok && (!validToken(b.device_token) || !validEnv(b.env))) || (!hasTok && typeof b.muted !== "boolean" && typeof b.rich !== "boolean")) return json({ error: "bad request" }, 400);
        // `muted` is the user's "which devices get alerts" choice (phone / watch / both): a muted registration is skipped on push.
        const rec = { ...a.rec, ...(hasTok ? { token: b.device_token.toLowerCase(), env: b.env } : {}), ...(typeof b.muted === "boolean" ? { muted: b.muted } : {}), ...(typeof b.rich === "boolean" ? { rich: b.rich } : {}) };
        await env.KV.put(`d:${a.id}`, JSON.stringify(rec), { expirationTtl: TTL });
        return json({ ok: true });
      }

      if (url.pathname === "/v1/push") {
        const a = await authed(request, env);
        if (!a) return json({ error: "unauthorized" }, 401);
        if ((await bump(env, `p:${a.id}:${Math.floor(Date.now() / 6e4)}`, 120)) > PUSH_PER_MINUTE) return json({ error: "rate limited" }, 429);
        const b = await readJson(request, 4096);
        if (typeof b.enc !== "string" || b.enc.length > 2800 || typeof b.id !== "string" || b.id.length > 64) return json({ error: "bad request" }, 400);
        // A watch registration gets an attention ping that opens the watch app (it shows the request with details);
        // the phone gets the encrypted text + Allow / Deny. Without ciphertext there is nothing to approve blindly.
        if (a.rec.muted) return json({ ok: true, muted: true });
        const full = b.enc && (a.rec.kind !== "watch" || a.rec.rich);   // `rich`: this watch build can decrypt the text itself
        const payload = {
          aps: {
            alert: { "title-loc-key": "push.title", "loc-key": "push.body" },   // localised by the app; replaced by the decrypted text
            sound: "default",
            category: full ? "APPROVAL" : "OPEN",
            "mutable-content": full ? 1 : 0,
            "interruption-level": "time-sensitive",
          },
          id: b.id,
          ...(full ? { enc: b.enc } : {}),
        };
        const host = env.APNS_HOST || (a.rec.env === "production" ? "api.push.apple.com" : "api.sandbox.push.apple.com");
        const r = await fetch(`https://${host}/3/device/${a.rec.token}`, {
          method: "POST",
          headers: {
            authorization: `bearer ${await apnsJwt(env)}`,
            "apns-topic": a.rec.topic || env.TOPIC,
            "apns-collapse-id": b.id.slice(0, 60),
            "apns-push-type": "alert",
            "apns-priority": "10",
            "apns-expiration": String(Math.floor(Date.now() / 1000) + 120),
            "content-type": "application/json",
          },
          body: JSON.stringify(payload),
        });
        if (r.status === 200) { await env.KV.put(`d:${a.id}`, JSON.stringify(a.rec), { expirationTtl: TTL }); return json({ ok: true }); }
        const reason = ((await r.json().catch(() => ({}))) || {}).reason || "";
        if (r.status === 410 || reason === "BadDeviceToken" || reason === "Unregistered" || reason === "DeviceTokenNotForTopic") {
          await env.KV.delete(`d:${a.id}`);
          return json({ error: "device gone", reason }, 410);
        }
        return json({ error: "apns", status: r.status, reason }, 502);
      }
      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: "bad request" }, 400);
    }
  },
};

// One Durable Object per pairing channel.
export class Mailbox {
  constructor(state) {
    this.state = state;
    this.waiters = [];
  }

  wake() { const w = this.waiters; this.waiters = []; w.forEach((r) => r()); }

  async fetch(request) {
    const op = new URL(request.url).pathname.slice(1);
    const secret = (request.headers.get("Authorization") || "").replace(/^Bearer /, "");
    if (!secret) return json({ error: "unauthorized" }, 401);
    const h = await sha256hex(secret);
    let owner = await this.state.storage.get("h");
    if (!owner) {
      if (op !== "sync") return json({ error: "no such channel" }, 404);   // only the bridge may create a channel
      await this.state.storage.put("h", h);
      owner = h;
    }
    if (!timingSafeEqual(owner, h)) return json({ error: "unauthorized" }, 401);

    const text = request.method === "GET" ? "" : await request.text();
    if (text.length > 40000) return json({ error: "too large" }, 413);
    let b = {};
    try { b = text ? JSON.parse(text) : {}; } catch { return json({ error: "bad request" }, 400); }

    if (op === "state") {
      const st = await this.state.storage.get("st");
      return st ? json(st) : json({ error: "no data yet" }, 404);
    }
    if (op === "decide") {
      if (typeof b.enc !== "string" || b.enc.length > 2000) return json({ error: "bad request" }, 400);
      const q = (await this.state.storage.get("dec")) || [];
      q.push(b.enc);
      await this.state.storage.put("dec", q.slice(-20));
      this.wake();
      return json({ ok: true });
    }
    if (op === "poke") {
      await this.state.storage.put("poke", { probe: !!b.probe || !!(await this.state.storage.get("poke"))?.probe, t: Date.now() });
      this.wake();
      return json({ ok: true });
    }
    // sync (bridge)
    if (typeof b.enc === "string") {
      if (b.enc.length > 30000) return json({ error: "too large" }, 413);
      await this.state.storage.put("st", { enc: b.enc, ts: Date.now() });
    }
    const wait = Math.max(0, Math.min(25, Number(b.wait) || 0));
    const pending = async () => ((await this.state.storage.get("dec")) || []).length > 0 || !!(await this.state.storage.get("poke"));
    if (wait > 0 && !(await pending())) {
      await new Promise((resolve) => { const t = setTimeout(resolve, wait * 1000); this.waiters.push(() => { clearTimeout(t); resolve(); }); });
    }
    const decisions = (await this.state.storage.get("dec")) || [];
    const poke = await this.state.storage.get("poke");
    if (decisions.length) await this.state.storage.delete("dec");
    if (poke) await this.state.storage.delete("poke");
    return json({ decisions, poke: !!poke, probe: !!(poke && poke.probe), last_state: (await this.state.storage.get("st"))?.ts || 0 });
  }
}
