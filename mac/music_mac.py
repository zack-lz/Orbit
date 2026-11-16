"""Apple Music (macOS Music.app) backend for the Orbit bridge.
Speaks the same API as zack-lz/Apple-Music-Remote so its web UI works unchanged on a Mac.
Controls Music.app with AppleScript; macOS asks once for Automation permission."""
import secrets
import subprocess
import threading
import time

SEP = "\x1f"
_lock = threading.Lock()
_saved_volume = {"v": 50}
_queue = {"id": "", "base": 0, "track": ""}


def _osa(script, timeout=6):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "osascript failed")
    return r.stdout.rstrip("\n")


def _num(s):
    try:
        return float(s.replace(",", "."))
    except ValueError:
        return 0.0


def _running():
    try:
        return _osa('application id "com.apple.Music" is running') == "true"
    except Exception:  # noqa: BLE001
        return False


def _base(**kw):
    d = {"available": False, "playing": False, "title": "", "artist": "", "album": "", "cover": "",
         "position": 0, "duration": 0, "volume": None, "muted": False, "ui_version": "9",
         "online_lyrics": False, "controls": {}, "message": ""}
    d.update(kw)
    return d


_state = {"v": _base(message="正在连接“音乐”…"), "t": 0.0, "ok_at": 0.0, "ver": 0}
_wanted = {"t": 0.0}
_poller = {"started": False}


def _read():
    """One AppleScript round trip -> state dict (raises on failure)."""
    if not _running():
        return _base(message="请先打开“音乐”App 并播放一首歌")
    script = f'''
tell application "Music"
  set pstate to player state as string
  set vlevel to sound volume
  if pstate is "stopped" then return pstate & "{SEP}" & vlevel
  set t to current track
  set pid to persistent ID of t
  set nart to count of artworks of t
  return pstate & "{SEP}" & vlevel & "{SEP}" & (name of t) & "{SEP}" & (artist of t) & "{SEP}" & (album of t) & "{SEP}" & (duration of t) & "{SEP}" & player position & "{SEP}" & pid & "{SEP}" & nart
end tell'''
    p = _osa(script).split(SEP)
    vol = int(_num(p[1]))
    v = _base(available=True, playing=p[0] == "playing", volume=vol, muted=vol == 0,
              controls={"toggle": True, "next": True, "previous": True, "seek": True})
    if len(p) >= 9:
        v.update(title=p[2], artist=p[3], album=p[4], duration=_num(p[5]), position=_num(p[6]),
                 cover=p[7] if int(_num(p[8])) > 0 else "")
    else:
        v["message"] = "没有正在播放的歌曲"
    return v


def _loop():
    while True:
        # Only poll while a client has asked recently; otherwise stay idle.
        if time.time() - _wanted["t"] > 20:
            time.sleep(0.5)
            continue
        t0 = time.time()
        try:
            v = _read()
            v["_sampled"] = time.time()
            with _lock:
                _state.update(v=v, t=time.time(), ok_at=time.time(), ver=_state["ver"] + 1)
        except Exception as e:  # noqa: BLE001
            # Tolerate transient failures: keep serving the last good state for a few seconds.
            if time.time() - _state["ok_at"] > 6:
                with _lock:
                    _state.update(v=_base(message=f"无法控制“音乐”：{e}（请在 系统设置 → 隐私与安全性 → 自动化 里允许）"),
                                  t=time.time(), ver=_state["ver"] + 1)
        time.sleep(max(0.05, 0.6 - (time.time() - t0)))


def touch():
    """Called by every API request: wakes the poller."""
    _wanted["t"] = time.time()
    if not _poller["started"]:
        _poller["started"] = True
        threading.Thread(target=_loop, daemon=True).start()


def state():
    """Latest known state, instantly (position advanced by the age of the sample)."""
    touch()
    with _lock:
        v = dict(_state["v"])
    smp = v.pop("_sampled", None)
    if smp and v.get("playing") and v.get("available"):
        v["position"] = min(v.get("duration", 0) or 1e9, v.get("position", 0) + max(0, time.time() - smp))
    return v


def version():
    return _state["ver"]


def refresh_now():
    """Force the poller to re-read soon (after a command)."""
    with _lock:
        _state["ok_at"] = _state["ok_at"]
    touch()


def command(action, value=None):
    if not _running() and action != "toggle":
        raise ValueError("请先打开“音乐”App")
    if action == "toggle":
        _osa('tell application "Music" to playpause')
    elif action == "next":
        _osa('tell application "Music" to next track')
    elif action == "previous":
        _osa('tell application "Music" to previous track')
    elif action == "volume":
        _osa(f'tell application "Music" to set sound volume to {max(0, min(100, int(value)))}')
    elif action == "mute":
        vol = int(state().get("volume") or 0)
        if vol > 0:
            _saved_volume["v"] = vol
            _osa('tell application "Music" to set sound volume to 0')
        else:
            _osa(f'tell application "Music" to set sound volume to {_saved_volume["v"]}')
    elif action == "seek":
        _osa(f'tell application "Music" to set player position to {float(value)}')
    else:
        raise ValueError("unknown action")
    time.sleep(0.15)
    try:
        v = _read()
        v["_sampled"] = time.time()
        with _lock:
            _state.update(v=v, t=time.time(), ok_at=time.time(), ver=_state["ver"] + 1)
    except Exception:  # noqa: BLE001
        pass
    return True


def cover(version):
    """Artwork bytes of the current track (version = persistent id)."""
    import os
    import tempfile
    s = state()
    if not s.get("cover") or (version and version != s["cover"]):
        return None
    path = os.path.join(tempfile.gettempdir(), f"orbit-art-{os.getuid()}.bin")
    _osa(f'''
tell application "Music"
  set d to raw data of artwork 1 of current track
end tell
set f to open for access (POSIX file "{path}") with write permission
set eof f to 0
write d to f
close access f''', timeout=10)
    data = open(path, "rb").read()
    os.remove(path)
    ctype = "image/png" if data[:4] == b"\x89PNG" else ("image/jpeg" if data[:2] == b"\xff\xd8" else "image/tiff")
    return data, ctype


def _fmt(sec):
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def queue(limit=30):
    if not _running():
        return {"available": False, "tracks": [], "message": "请先打开“音乐”App"}
    script = f'''
tell application "Music"
  if player state is stopped then return ""
  set pl to current playlist
  set idx to index of current track
  set n to count of tracks of pl
  set outp to (persistent ID of current track) & "{SEP}" & idx
  repeat with i from (idx + 1) to (idx + {limit})
    if i > n then exit repeat
    set t to track i of pl
    set outp to outp & "\\n" & (name of t) & "{SEP}" & (artist of t) & " — " & (album of t) & "{SEP}" & (duration of t)
  end repeat
  return outp
end tell'''
    try:
        raw = _osa(script, timeout=15)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "tracks": [], "message": str(e)}
    lines = [l for l in raw.split("\n") if l]
    if not lines:
        return {"available": True, "tracks": [], "message": "没有正在播放的歌曲"}
    pid, idx = lines[0].split(SEP)
    _queue.update(id=secrets.token_urlsafe(12), base=int(_num(idx)), track=pid)
    tracks = []
    for l in lines[1:]:
        p = l.split(SEP)
        if len(p) >= 3:
            tracks.append({"title": p[0], "detail": p[1], "duration": _fmt(_num(p[2]))})
    return {"available": True, "tracks": tracks, "queue_id": _queue["id"]}


def queue_play(index, queue_id):
    if queue_id != _queue["id"]:
        raise ValueError("队列已变化，请刷新")
    cur = state()
    if cur.get("cover") is None:
        raise ValueError("无法确认当前歌曲")
    n = _queue["base"] + 1 + int(index)
    _osa(f'tell application "Music" to play track {n} of current playlist')
    time.sleep(0.15)
    try:
        v = _read()
        v["_sampled"] = time.time()
        with _lock:
            _state.update(v=v, t=time.time(), ok_at=time.time(), ver=_state["ver"] + 1)
    except Exception:  # noqa: BLE001
        pass
    return True


def lyrics():
    if not _running():
        return {"available": False, "lines": [], "message": "请先打开“音乐”App"}
    try:
        text = _osa('tell application "Music" to return lyrics of current track', timeout=8)
    except Exception:  # noqa: BLE001
        text = ""
    text = text.replace("\r", "\n")
    lines = [l.strip() for l in text.split("\n")]
    if not any(lines):
        return {"available": False, "lines": [], "message": "这首歌在“音乐”App 里没有歌词（Mac 版只提供文本歌词，不含时间轴）。"}
    return {"available": True, "source": "apple", "mode": "text", "lines": lines}
