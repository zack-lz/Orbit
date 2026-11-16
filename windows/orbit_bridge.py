#!/usr/bin/env python3
"""Claude Watch bridge: runs on the Mac.

- Claude Code's status line script POSTs /usage (localhost only)  -> latest 5h / 7d usage
- Claude Code's PermissionRequest hook POSTs /request (localhost only) and blocks
  until the watch answers (or the wait times out, then the normal prompt shows on the Mac)
- The Apple Watch app GETs /state and POSTs /decide, authenticated with a token

Only the Python standard library is used.
"""
import base64
import hashlib
import json
import os
import select
import socket
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PLATFORM = os.environ.get("ORBIT_PLATFORM") or sys.platform   # override only for testing
IS_MAC = PLATFORM == "darwin"
IS_WIN = PLATFORM == "win32"
if IS_MAC:
    import music_mac  # noqa: E402  (Windows music is served by the bundled Apple Music remote instead)

HOME = os.path.expanduser("~/.claude-watch")
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
TOKEN_FILE = os.path.join(HOME, "token")
PORT = int(os.environ.get("CLAUDE_WATCH_PORT", "8765"))
WAIT_SECONDS = int(os.environ.get("CLAUDE_WATCH_WAIT", "110"))
LOCAL_ADDRS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def configure(port=None, token=None, home=None):
    """Let a host program (the Windows Apple Music remote) run this bridge in-process."""
    global PORT, TOKEN, HOME, TOKEN_FILE, APNS_CONF, DEVICES_FILE
    if home:
        HOME = str(home)
        os.makedirs(HOME, exist_ok=True)
        TOKEN_FILE = os.path.join(HOME, "token")
        APNS_CONF = os.path.join(HOME, "apns.json")
        DEVICES_FILE = os.path.join(HOME, "devices.json")
    if port:
        PORT = int(port)
    if token:
        TOKEN = token


def load_token():
    env = os.environ.get("CLAUDE_WATCH_TOKEN")
    if env:
        return env.strip()
    os.makedirs(HOME, exist_ok=True)
    if not os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "w") as f:
            f.write(secrets.token_hex(16))  # 32 hex chars
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    with open(TOKEN_FILE) as f:
        return f.read().strip()


TOKEN = ""   # set by configure() or loaded in main()
WATCH_ACTIVE_SECONDS = int(os.environ.get("CLAUDE_WATCH_ACTIVE", "20"))
lock = threading.Lock()
_failures = {}  # client ip -> recent failed-auth timestamps (5 failures-in-5-min style lockout)
last_watch_seen = 0.0  # last time the watch app polled /state
usage = {}  # latest usage snapshot
pending = {}  # id -> {"info": {...}, "event": Event, "decision": None|dict}


def summarize(hook):
    """Turn a PermissionRequest hook payload into a short, watch-sized text."""
    tool = hook.get("tool_name", "?")
    ti = hook.get("tool_input") or {}
    if tool == "Bash":
        text = ti.get("command", "")
    elif tool in ("Edit", "Write", "Read", "MultiEdit", "NotebookEdit"):
        text = ti.get("file_path") or ti.get("notebook_path") or ""
    elif tool in ("WebFetch",):
        text = ti.get("url", "")
    elif tool in ("WebSearch",):
        text = ti.get("query", "")
    else:
        text = json.dumps(ti, ensure_ascii=False)
    if ti.get("description"):
        text = f"{ti['description']}\n{text}"
    return tool, text[:400]


APNS_CONF = os.path.join(HOME, "apns.json")      # {"key_id","team_id","topic","env","key_file"}
DEVICES_FILE = os.path.join(HOME, "devices.json")  # push tokens registered by the Orbit iPhone app
_jwt_cache = {"t": 0, "v": ""}


def load_devices():
    try:
        return json.load(open(DEVICES_FILE))
    except Exception:  # noqa: BLE001
        return []


def register_device(tok):
    devs = [d for d in load_devices() if d != tok]
    devs.append(tok)
    with open(DEVICES_FILE, "w") as f:
        json.dump(devs[-5:], f)
    os.chmod(DEVICES_FILE, 0o600)


def _der_to_raw(der):
    """ECDSA DER signature -> 64-byte r||s as JWT ES256 requires."""
    assert der[0] == 0x30
    i = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    parts = []
    for _ in range(2):
        assert der[i] == 0x02
        ln = der[i + 1]
        parts.append(der[i + 2:i + 2 + ln].lstrip(b"\x00").rjust(32, b"\x00"))
        i += 2 + ln
    return parts[0] + parts[1]


def apns_jwt(conf):
    import base64
    import subprocess
    if time.time() - _jwt_cache["t"] < 2400 and _jwt_cache["v"]:
        return _jwt_cache["v"]
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    head = b64(json.dumps({"alg": "ES256", "kid": conf["key_id"]}).encode())
    body = b64(json.dumps({"iss": conf["team_id"], "iat": int(time.time())}).encode())
    signing = f"{head}.{body}".encode()
    der = subprocess.run(["openssl", "dgst", "-sha256", "-sign", os.path.expanduser(conf["key_file"])],
                         input=signing, capture_output=True, check=True).stdout
    _jwt_cache.update(t=time.time(), v=f"{head}.{body}.{b64(_der_to_raw(der))}")
    return _jwt_cache["v"]


def send_apns(hook, rid):
    """Push a time-sensitive Approve/Deny notification to the registered iPhone (mirrored to the watch)."""
    import subprocess
    try:
        conf = json.load(open(APNS_CONF))
    except Exception:  # noqa: BLE001
        return False
    devs = load_devices()
    if not devs:
        return False
    tool, text = summarize(hook)
    payload = json.dumps({
        "aps": {
            "alert": {"title": f"Claude 请求授权：{tool}", "body": text[:150]},
            "sound": "default",
            "category": "APPROVAL",
            "interruption-level": "time-sensitive",
        },
        "id": rid,
    }, ensure_ascii=False)
    host = "api.push.apple.com" if conf.get("env") == "production" else "api.sandbox.push.apple.com"
    ok = False
    try:
        jwt = apns_jwt(conf)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"apns jwt failed: {e}\n")
        return False
    for tok in devs:
        r = subprocess.run([
            "curl", "-s", "--http2", "-m", "10", "-o", "/dev/stderr", "-w", "%{http_code}",
            "-X", "POST", f"https://{host}/3/device/{tok}",
            "-H", f"authorization: bearer {jwt}", "-H", f"apns-topic: {conf['topic']}",
            "-H", "apns-push-type: alert", "-H", "apns-priority: 10",
            "-H", f"apns-expiration: {int(time.time()) + 120}", "-d", payload],
            capture_output=True, text=True)
        if r.stdout.strip() == "200":
            ok = True
        else:
            sys.stderr.write(f"apns push failed {r.stdout} {r.stderr}\n")
    return ok


def find_claude():
    """`claude` on PATH, else the copies bundled with / installed next to the Claude apps."""
    import glob
    import shutil
    p = shutil.which("claude")
    if p:
        return p
    home = os.path.expanduser("~")
    patterns = []
    if IS_MAC:
        patterns = [home + "/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude",
                    home + "/.claude/local/claude", home + "/.local/bin/claude", "/opt/homebrew/bin/claude",
                    "/usr/local/bin/claude"]
    elif IS_WIN:
        appdata = os.environ.get("APPDATA", home + "\\AppData\\Roaming")
        local = os.environ.get("LOCALAPPDATA", home + "\\AppData\\Local")
        patterns = [home + "\\.local\\bin\\claude.exe", appdata + "\\npm\\claude.cmd", appdata + "\\npm\\claude",
                    appdata + "\\Claude\\claude-code\\*\\claude.exe", appdata + "\\Claude\\claude-code\\*\\*\\claude.exe",
                    local + "\\Programs\\claude\\claude.exe", local + "\\AnthropicClaude\\*\\claude.exe"]
    else:
        patterns = [home + "/.local/bin/claude", "/usr/local/bin/claude"]
    found = [f for pat in patterns for f in glob.glob(pat) if os.path.isfile(f)]
    found.sort(key=os.path.getmtime)
    return found[-1] if found else None


def _hide_window():
    """Keep Windows from flashing a console for every helper process."""
    return {"creationflags": 0x08000000} if IS_WIN else {}


def _pct(win):
    if not isinstance(win, dict):
        return None, None
    used = win.get("used_percentage", win.get("utilization"))  # 0-100
    reset = win.get("resets_at")
    if isinstance(reset, str):
        try:
            from datetime import datetime
            reset = datetime.fromisoformat(reset.replace("Z", "+00:00")).timestamp()
        except ValueError:
            reset = None
    return used, reset


def merge_usage(five, seven, source, extra=None, force=False):
    """Combine usage from the status line (live, from API response headers) and get_usage (server cached).
    Utilization never decreases inside one rate-limit window, so within the same window keep the higher
    value; a new window (different resets_at) replaces the old one."""
    if five[0] is None and seven[0] is None:
        return
    now = time.time()
    with lock:
        for key, (pct, reset) in (("five_hour", five), ("seven_day", seven)):
            if pct is None:
                continue
            old_pct, old_reset = usage.get(key + "_pct"), usage.get(key + "_resets_at")
            same_window = (old_pct is not None and old_reset and reset and abs(old_reset - reset) < 300
                           and old_reset > now - 60)
            if same_window and not force:
                pct = max(pct, old_pct)
                reset = old_reset
            if same_window and force:
                reset = reset or old_reset
            usage[key + "_pct"] = pct
            usage[key + "_resets_at"] = reset or old_reset
        usage.update(extra or {})
        usage.update({"updated_at": now, "source": source, "status": "ok"})
    note_sample()


_probe_lock = threading.Lock()
_last_probe = {"t": 0.0, "ok": False}


def live_probe():
    """Make one tiny real Claude Code call (haiku, 1 turn, ~0.001 USD). Its rate_limit_event carries the
    live 5h/7d utilization taken from the API response headers, unlike the cached get_usage endpoint."""
    import subprocess
    with _probe_lock:
        if time.time() - _last_probe["t"] < 4:          # collapse bursts of refresh presses
            return _last_probe["ok"]
        exe = find_claude()
        ok = False
        if exe:
            try:
                msg = json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})
                out = subprocess.run(
                    [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                     "--model", "claude-haiku-5-5", "--max-turns", "1", "--no-session-persistence", "--tools", ""],
                    input=msg + "\n", capture_output=True, text=True, timeout=45, cwd=HOME, **_hide_window()).stdout
                for line in out.splitlines():
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    if d.get("type") != "rate_limit_event":
                        continue
                    w = (d.get("rate_limit_info") or {}).get("unifiedWindows") or {}
                    def conv(x):
                        if not isinstance(x, dict) or x.get("utilization") is None:
                            return (None, None)
                        return (round(x["utilization"] * 100, 1), x.get("resetsAt"))
                    merge_usage(conv(w.get("five_hour")), conv(w.get("seven_day")), "live", force=True)
                    ok = True
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"live probe failed: {e}\n")
        _last_probe.update(t=time.time(), ok=ok)
        return ok


class UsagePoller:
    """One long-lived `claude` process answering get_usage requests (no per-poll startup cost)."""

    def __init__(self):
        self.proc = None
        self.q = None
        self.n = 0

    def _start(self):
        import queue
        import subprocess
        exe = find_claude()
        if not exe:
            raise RuntimeError("claude not found")
        self.proc = subprocess.Popen([exe, "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     cwd=HOME, text=True, bufsize=1, **_hide_window())
        self.q = queue.Queue()
        threading.Thread(target=lambda: [self.q.put(l) for l in self.proc.stdout], daemon=True).start()
        self._send({"type": "control_request", "request_id": "init", "request": {"subtype": "initialize"}})

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def stop(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        self.proc = None

    def poll(self):
        import queue
        if self.proc is None or self.proc.poll() is not None:
            self.stop()
            self._start()
        self.n += 1
        rid = f"u{self.n}"
        self._send({"type": "control_request", "request_id": rid,
                    "request": {"subtype": "get_usage", "skip_behaviors": True}})
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                line = self.q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            resp = msg.get("response") or {}
            if msg.get("type") == "control_response" and resp.get("request_id") == rid:
                body = resp.get("response") or {}
                rl = body.get("rate_limits")
                if not rl:
                    with lock:
                        usage["status"] = "not logged in" if not body.get("rate_limits_available") else "no data"
                    return usage["status"]
                merge_usage(_pct(rl.get("five_hour")), _pct(rl.get("seven_day")), "api",
                            {"plan": body.get("subscription_type")})
                return "ok"
        self.stop()
        return "timeout"


_fallback = UsagePoller()


def poll_once_fallback():
    try:
        _fallback.poll()
    except Exception:  # noqa: BLE001
        _fallback.stop()


def usage_loop():
    poller = UsagePoller()
    while True:
        try:
            r = poller.poll()
            if r != "ok":
                sys.stderr.write(f"usage poll: {r}\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"usage poll error: {e}\n")
            poller.stop()
        # While the phone/watch is actively polling, take a live reading every few minutes.
        if time.time() - last_watch_seen < 60 and time.time() - _last_probe["t"] > int(os.environ.get("CLAUDE_WATCH_LIVE_EVERY", "300")):
            live_probe()
        time.sleep(int(os.environ.get("CLAUDE_WATCH_USAGE_EVERY", "15")))


history = []        # decided approvals, newest last: {tool, summary, allow, at}
usage_samples = []  # (time, five_hour_pct) for the burn-rate forecast
stats = {}          # today's usage computed from Claude Code transcripts
_file_cache = {}    # path -> (mtime, size, {msg_id: (ts, session, model, in, out, cache_read, cache_write)})


def note_sample():
    with lock:
        p = usage.get("five_hour_pct")
        if p is None:
            return
        now = time.time()
        if not usage_samples or usage_samples[-1][1] != p or now - usage_samples[-1][0] > 600:
            usage_samples.append((now, p))
        del usage_samples[:-200]


def forecast():
    """Hours until the 5h window is full at the recent burn rate (None if idle/unknown)."""
    with lock:
        p = usage.get("five_hour_pct")
        pts = [x for x in usage_samples if x[0] > time.time() - 5400]
        reset = usage.get("five_hour_resets_at")
    if p is None or len(pts) < 2:
        return None
    (t0, p0), (t1, p1) = pts[0], pts[-1]
    if p1 <= p0 or t1 - t0 < 120:
        return None
    rate = (p1 - p0) / (t1 - t0)               # percent per second
    secs = (100 - p) / rate
    if reset and time.time() + secs > reset:
        return None                             # window resets before it would fill
    return {"seconds_to_full": secs, "pct_per_hour": rate * 3600}


def _scan_file(path):
    out = {}
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                if d.get("type") != "assistant" or not u:
                    continue
                out[m.get("id") or d.get("uuid")] = (
                    d.get("timestamp", ""), d.get("sessionId", ""), m.get("model", "?"),
                    u.get("input_tokens", 0) or 0, u.get("output_tokens", 0) or 0,
                    u.get("cache_read_input_tokens", 0) or 0, u.get("cache_creation_input_tokens", 0) or 0)
    except OSError:
        pass
    return out


def compute_stats():
    import glob
    from datetime import datetime
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    files = glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl"))
    tot = {"messages": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    sessions, models, hourly = set(), {}, [0] * 24
    for path in files:
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < midnight:
            continue
        c = _file_cache.get(path)
        if not c or c[0] != st.st_mtime or c[1] != st.st_size:
            c = (st.st_mtime, st.st_size, _scan_file(path))
            _file_cache[path] = c
        for ts, sess, model, i, o, cr, cw in c[2].values():
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if t < midnight:
                continue
            tot["messages"] += 1
            tot["input"] += i
            tot["output"] += o
            tot["cache_read"] += cr
            tot["cache_write"] += cw
            sessions.add(sess)
            models[model] = models.get(model, 0) + o + i
            hourly[datetime.fromtimestamp(t).hour] += o + i
    top = sorted(models.items(), key=lambda kv: -kv[1])[:3]
    return {**tot, "sessions": len(sessions), "models": [{"name": k, "tokens": v} for k, v in top],
            "hourly": hourly, "computed_at": time.time()}


def stats_loop():
    global stats
    while True:
        try:
            stats = compute_stats()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"stats error: {e}\n")
        time.sleep(45)


def snapshot_state():
    with lock:
        items = [
            p["info"] for p in pending.values() if p["decision"] is None
        ]
        snap = {"usage": dict(usage), "pending": sorted(items, key=lambda x: x["created_at"]),
                "server_time": time.time(), "history": list(history[-8:])[::-1], "stats": stats,
                "host": socket.gethostname()}
    snap["forecast"] = forecast()
    return snap


def lan_addresses():
    import socket
    found = []
    try:
        sck = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sck.connect(("10.255.255.255", 1))
        found.append(sck.getsockname()[0])
        sck.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith(("127.", "169.254.")) and ip not in found:
                found.append(ip)
    except OSError:
        pass
    return found


def connect_page():
    """Pairing page shown on this Mac: scan the QR with Orbit on the iPhone (token stays in the #fragment)."""
    import base64
    import io
    cards = []
    try:
        import qrcode
        import qrcode.image.svg
    except ImportError:
        qrcode = None
    def qr(url):
        if not qrcode:
            return "<p>未安装 qrcode。请运行 install.sh，或 pip install qrcode。</p>"
        buf = io.BytesIO()
        qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage).save(buf)
        return f'<img alt="QR" src="data:image/svg+xml;base64,{base64.b64encode(buf.getvalue()).decode()}">'
    for ip in lan_addresses():
        music = ""
        if IS_MAC:
            music = f"<figure>{qr(f'http://{ip}:{PORT}/app/9/#{TOKEN}')}<figcaption>音乐控制（这台 Mac 的“音乐”）</figcaption></figure>"
        cards.append(f"<section><h2>{ip}</h2><div class=row>"
                     f"<figure>{qr(f'http://{ip}:{PORT}/orbit#{TOKEN}')}<figcaption>Claude 额度与授权</figcaption></figure>"
                     f"{music}</div></section>")
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>连接 Orbit</title><style>body{font:17px system-ui;background:#101018;color:#fafafa;max-width:720px;"
            "margin:50px auto;padding:24px}section{background:#242430;border-radius:24px;padding:24px;margin:24px 0}"
            "img{width:220px;background:#fff;padding:10px;border-radius:12px}.row{display:flex;gap:24px;flex-wrap:wrap}figure{margin:0}figcaption{margin-top:8px}h2{margin-top:0;font-size:18px}</style>"
            "<h1>用 Orbit 扫码连接这台 Mac</h1><p>iPhone 与这台 Mac 连同一个 Wi-Fi，在 Orbit 里扫码：Claude 页点“扫码连接”，音乐页点“扫码添加”，"
            "各扫对应的二维码（选与你 Wi-Fi 对应的那一组）。二维码含有访问密钥，请勿截图分享。</p>"
            + ("".join(cards) or "<p>没找到局域网地址，请连接 Wi-Fi。</p>"))


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudeWatchBridge/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---------- helpers ----------
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, code=200):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 1_000_000:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def _is_local(self):
        return self.client_address[0] in LOCAL_ADDRS

    def _lan_only(self):
        import ipaddress
        try:
            ip = ipaddress.ip_address(self.client_address[0].replace("::ffff:", ""))
        except ValueError:
            return False
        return ip.is_private or ip.is_loopback or ip.is_link_local

    def _locked_out(self):
        ip = self.client_address[0]
        recent = [t for t in _failures.get(ip, []) if t > time.time() - 300]
        _failures[ip] = recent
        return len(recent) >= 8

    def _check(self, ok):
        if not ok:
            _failures.setdefault(self.client_address[0], []).append(time.time())
        return ok

    def _authed(self):
        if self._locked_out():
            return False
        return self._check(secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN))

    def _bearer(self):
        if self._locked_out():
            return False
        return self._check(secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + TOKEN))

    # ---------- WebSocket (push channel used by the Apple-Music-Remote web UI) ----------
    WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def _ws_send(self, payload, opcode=1):
        data = payload.encode() if isinstance(payload, str) else payload
        n = len(data)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([n])
        elif n < 65536:
            hdr += bytes([126]) + n.to_bytes(2, "big")
        else:
            hdr += bytes([127]) + n.to_bytes(8, "big")
        self.wfile.write(hdr + data)
        self.wfile.flush()

    def _ws_recv(self, timeout):
        """-> (opcode, payload) | None on timeout; raises EOFError when the peer is gone."""
        conn = self.connection
        if not select.select([conn], [], [], timeout)[0]:
            return None
        conn.settimeout(5)

        def exact(n):
            buf = b""
            while len(buf) < n:
                chunk = conn.recv(n - len(buf))
                if not chunk:
                    raise EOFError
                buf += chunk
            return buf
        b0, b1 = exact(2)
        ln = b1 & 0x7F
        if ln == 126:
            ln = int.from_bytes(exact(2), "big")
        elif ln == 127:
            ln = int.from_bytes(exact(8), "big")
        if ln > 4096:
            raise EOFError
        mask = exact(4) if b1 & 0x80 else b""
        data = exact(ln)
        if mask:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return b0 & 0x0F, data

    def _ws_events(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        acc = base64.b64encode(hashlib.sha1((key + self.WS_GUID).encode()).digest()).decode()
        self.protocol_version = "HTTP/1.1"         # browsers require an HTTP/1.1 status line for the upgrade
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", acc)
        self.end_headers()
        self.close_connection = True
        try:
            first = self._ws_recv(3)               # first frame carries the token (kept out of URLs/logs)
            tok = ""
            if first and first[0] == 1:
                try:
                    tok = str(json.loads(first[1]).get("token", ""))
                except ValueError:
                    tok = ""
            if self._locked_out() or not self._check(secrets.compare_digest(tok, TOKEN)):
                self._ws_send((1008).to_bytes(2, "big") + b"Invalid pairing", opcode=8)
                return
            sent_key, sent_pos, sent_at, last_sent = None, 0.0, 0.0, 0.0
            while True:
                msg = self._ws_recv(0.25)
                force = False
                if msg is not None:
                    op, data = msg
                    if op == 8:
                        return
                    if op == 9:
                        self._ws_send(data, opcode=10)
                    elif op == 1 and data == b"clock":
                        self._ws_send(json.dumps({"type": "clock"}))
                    elif op == 1 and data == b"refresh":
                        force = True
                st = music_mac.state()             # also keeps the poller awake
                k = (st.get("title"), st.get("artist"), st.get("album"), st.get("playing"), st.get("volume"),
                     st.get("muted"), st.get("cover"), st.get("available"), st.get("message"))
                now = time.time()
                expected = sent_pos + ((now - sent_at) if st.get("playing") else 0)
                if (force or k != sent_key or abs((st.get("position") or 0) - expected) > 1.2 or now - last_sent > 4):
                    self._ws_send(json.dumps(st, ensure_ascii=False))
                    sent_key, sent_pos, sent_at, last_sent = k, st.get("position") or 0, now, now
        except (EOFError, OSError, ValueError):
            return

    # ---------- routes ----------
    def do_GET(self):
        if not self._lan_only():
            return self._json(403, {"error": "private network only"})
        if self.path.split("?")[0] == "/connect":
            if self.client_address[0] not in LOCAL_ADDRS:
                return self._json(403, {"error": "open this page on the Mac itself"})
            return self._html(connect_page())
        path = self.path.split("?")[0]
        if IS_MAC and path == "/events" and "websocket" in self.headers.get("Upgrade", "").lower():
            return self._ws_events()
        if IS_MAC and (path == "/" or (path.startswith("/app/") and path.endswith("/"))):
            try:
                body = open(os.path.join(WEB, "index.html"), "rb").read()
            except OSError:
                return self._json(404, {"error": "music UI not installed"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if IS_MAC and path.startswith("/assets/") and path[8:] in ("apple-touch-icon-v6.png", "favicon-v6.png", "jsQR-1.4.0.js", "pairing-v7.js"):
            try:
                body = open(os.path.join(WEB, "assets", path[8:]), "rb").read()
            except OSError:
                return self._json(404, {"error": "not found"})
            self.send_response(200)
            self.send_header("Content-Type", "image/png" if path.endswith(".png") else "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if IS_MAC and path in ("/api/cover", "/api/queue", "/api/lyrics"):
            if not self._bearer():
                return self._json(401, {"error": "bad token"})
            try:
                if path == "/api/cover":
                    from urllib.parse import parse_qs, urlparse
                    ver = (parse_qs(urlparse(self.path).query).get("v") or [""])[0]
                    art = music_mac.cover(ver)
                    if not art:
                        return self._json(404, {"error": "no artwork"})
                    self.send_response(200)
                    self.send_header("Content-Type", art[1])
                    self.send_header("Content-Length", str(len(art[0])))
                    self.end_headers()
                    self.wfile.write(art[0])
                    return
                return self._json(200, music_mac.queue() if path == "/api/queue" else music_mac.lyrics())
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)})
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path == "/state":
            if not self._authed():
                return self._json(401, {"error": "bad token"})
            global last_watch_seen
            last_watch_seen = time.time()
            return self._json(200, snapshot_state())
        if IS_MAC and path == "/api/state":
            if not self._bearer():
                return self._json(401, {"error": "bad token"})
            return self._json(200, music_mac.state())
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._lan_only():
            return self._json(403, {"error": "private network only"})
        try:
            body = self._body()
        except Exception as e:  # noqa: BLE001
            return self._json(400, {"error": str(e)})

        if self.path == "/usage":
            if not self._is_local():
                return self._json(403, {"error": "local only"})
            rl = body.get("rate_limits") or {}
            merge_usage(_pct(rl.get("five_hour")), _pct(rl.get("seven_day")), "live",
                        {"model": (body.get("model") or {}).get("display_name"),
                         "context_pct": (body.get("context_window") or {}).get("used_percentage")})
            return self._json(200, {"ok": True})

        if self.path == "/request":
            if not self._is_local():
                return self._json(403, {"error": "local only"})
            rid = secrets.token_hex(6)
            pushed = send_apns(body, rid)
            if not pushed and time.time() - last_watch_seen > WATCH_ACTIVE_SECONDS:
                # No push possible and the watch app isn't open: don't delay the normal Mac prompt.
                return self._json(200, {"decision": None, "reason": "watch inactive"})
            tool, text = summarize(body)
            ev = threading.Event()
            info = {
                "id": rid,
                "tool": tool,
                "summary": text,
                "cwd": os.path.basename(body.get("cwd") or "") or None,
                "created_at": time.time(),
            }
            with lock:
                pending[rid] = {"info": info, "event": ev, "decision": None}
            wait = min(int(body.get("_wait", WAIT_SECONDS)), 600)
            ev.wait(wait)
            with lock:
                decision = pending.pop(rid)["decision"]
            return self._json(200, {"decision": decision})  # None = no answer, fall back to Mac prompt

        if IS_MAC and self.path == "/api/command":
            if not self._bearer():
                return self._json(401, {"error": "bad token"})
            try:
                ok = music_mac.command(body.get("action"), body.get("value"))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._json(503, {"error": str(e)})
            return self._json(200, {"ok": ok, "state": music_mac.state()})

        if IS_MAC and self.path == "/api/queue/play":
            if not self._bearer():
                return self._json(401, {"error": "bad token"})
            try:
                music_mac.queue_play(body.get("index", 0), body.get("queue_id", ""))
            except ValueError as e:
                return self._json(409, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                return self._json(503, {"error": str(e)})
            return self._json(200, {"ok": True})

        if self.path == "/refresh":
            if not self._authed():
                return self._json(401, {"error": "bad token"})
            ok = live_probe()
            if not ok:
                poll_once_fallback()
            return self._json(200, {"ok": ok, "state": snapshot_state()})

        if self.path == "/register":
            if not self._authed():
                return self._json(401, {"error": "bad token"})
            tok = str(body.get("token", ""))
            if not tok.isalnum() or len(tok) < 32:
                return self._json(400, {"error": "bad device token"})
            register_device(tok)
            return self._json(200, {"ok": True})

        if self.path == "/decide":
            if not self._authed():
                return self._json(401, {"error": "bad token"})
            rid, allow = body.get("id"), bool(body.get("allow"))
            with lock:
                p = pending.get(rid)
                if not p or p["decision"] is not None:
                    return self._json(404, {"error": "request gone"})
                p["decision"] = {"behavior": "allow"} if allow else {
                    "behavior": "deny", "message": "在 Apple Watch 上被拒绝"}
                p["event"].set()
                history.append({"tool": p["info"]["tool"], "summary": p["info"]["summary"][:80],
                                "allow": allow, "at": time.time()})
            return self._json(200, {"ok": True})

        self._json(404, {"error": "not found"})


def _serve():
    global TOKEN
    if not TOKEN:
        TOKEN = load_token()
    os.makedirs(HOME, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    threading.Thread(target=usage_loop, daemon=True).start()
    threading.Thread(target=stats_loop, daemon=True).start()
    print(f"Orbit bridge listening on :{PORT}", file=sys.stderr)
    return srv


def start_in_thread():
    """Run the bridge inside another program (Windows: next to the Apple Music remote)."""
    srv = _serve()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def main():
    _serve().serve_forever()


if __name__ == "__main__":
    main()
