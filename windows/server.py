"""Local iPhone remote for Apple Music on Windows. Python 3.11."""
import argparse
import asyncio
import base64
import contextlib
import faulthandler
import hashlib
import io
import ipaddress
import json
import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import webbrowser

import os
from aiohttp import web
from player_worker import PlayerWorker
from timed_lyrics import LyricService

ORBIT_CLAUDE_PORT = int(os.environ.get("ORBIT_CLAUDE_PORT", "8766"))   # Claude bridge (usage + approvals)

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger('remote')
FAULT_LOG = None
UI_VERSION = '9'


def load_settings():
    path = ROOT / 'settings.json'
    settings = {'scanner_url':'', 'online_lyrics':False}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        from urllib.parse import urlsplit
        if not isinstance(data, dict):
            return settings
        settings['online_lyrics'] = data.get('online_lyrics') is True
        url = data.get('scanner_url', '')
        if not isinstance(url, str):
            return settings
        parsed = urlsplit(url)
        if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
            settings['scanner_url'] = url
    except (OSError, ValueError, TypeError):
        pass
    return settings


def saved_token(path):
    """Keep pairing across restarts; never silently replace a damaged key."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('x', encoding='ascii') as stream:
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    token = path.read_text(encoding='ascii').strip()
    if len(token) != 43 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in token):
        raise ValueError('Pairing key is damaged. Restore .state/pairing.key or delete it and pair again.')
    return token


def is_apple(source):
    return 'applemusic' in source.lower().replace(' ', '').replace('.', '')


def media_labels(artist, album):
    artist, album = artist or '', album or ''
    # Apple's Windows SMTC integration sometimes puts both labels in Artist.
    if not album and ' — ' in artist:
        artist, album = artist.rsplit(' — ', 1)
    return artist, album


def local_addresses():
    found = set()
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('192.0.2.1', 9))
            found.add(sock.getsockname()[0])
    # The active route gives the primary address without a hostname/DNS lookup.
    # Only fall back to hostname resolution if there is no usable route.
    if not found:
        with contextlib.suppress(OSError):
            for entry in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.add(entry[4][0])
    return sorted(ip for ip in found if not ip.startswith(('127.', '169.254.')))


class WindowsPlayer:
    async def start(self):
        # Keep WinRT and Core Audio on this event loop's thread.
        sys.coinit_flags = 0
        import comtypes
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
        self.comtypes = comtypes
        self.manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        self.art = b''
        self.art_type = 'image/jpeg'
        self.art_version = ''
        self.art_key = None
        self.art_checked = 0
        self.art_task = None
        self.art_cache = {}
        self.art_retry_until = 0
        self.art_changed_at = 0
        self.current_album_key = None
        self.previous_album_key = None
        self.previous_art_version = ''
        self.loop = asyncio.get_running_loop()
        self.subscriptions = []
        self.manager_event = self.manager.add_sessions_changed(self.sessions_changed)
        self.watch_sessions()

    def media_changed(self, sender, args):
        self.loop.call_soon_threadsafe(self.signal_change)

    def sessions_changed(self, sender, args):
        self.loop.call_soon_threadsafe(self.watch_and_signal)

    def watch_and_signal(self):
        self.watch_sessions()
        self.signal_change()

    def signal_change(self):
        if hasattr(self, 'on_change'):
            self.on_change()

    def watch_sessions(self):
        for session, name, token in self.subscriptions:
            with contextlib.suppress(Exception):
                getattr(session, 'remove_' + name)(token)
        self.subscriptions = []
        for session in self.manager.get_sessions():
            if is_apple(session.source_app_user_model_id):
                for name in ('media_properties_changed', 'playback_info_changed', 'timeline_properties_changed'):
                    token = getattr(session, 'add_' + name)(self.media_changed)
                    self.subscriptions.append((session, name, token))

    async def close(self):
        if self.art_task:
            self.art_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.art_task
        for session, name, token in self.subscriptions:
            with contextlib.suppress(Exception):
                getattr(session, 'remove_' + name)(token)
        with contextlib.suppress(Exception):
            self.manager.remove_sessions_changed(self.manager_event)

    def session(self):
        sessions = [s for s in self.manager.get_sessions() if is_apple(s.source_app_user_model_id)]
        # Never send commands to another media app.
        for session in sessions:
            if int(session.get_playback_info().playback_status) == 4:
                return session
        return sessions[0] if sessions else None

    def endpoint(self):
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        device = AudioUtilities.GetSpeakers()
        if hasattr(device, 'EndpointVolume'):
            return device.EndpointVolume
        # QueryInterface owns an additional COM reference. ctypes.cast creates an
        # alias without AddRef, so two wrappers can release one native reference.
        interface = device.Activate(IAudioEndpointVolume._iid_, self.comtypes.CLSCTX_ALL, None)
        return interface.QueryInterface(IAudioEndpointVolume)

    def schedule_artwork(self, props, key):
        now = time.monotonic()
        if key != self.art_key:
            self.previous_art_version = self.art_version
            self.previous_album_key = self.current_album_key
            artist, album = media_labels(props.artist, props.album_title)
            self.current_album_key = (album, getattr(props, 'album_artist', '') or artist) if album else ('track', props.title, artist)
            cached = self.art_cache.get(self.current_album_key)
            self.art, self.art_type, self.art_version = cached if cached else (b'', 'image/jpeg', '')
            self.art_key = key
            self.art_checked = 0
            self.art_changed_at = now
            self.art_retry_until = now + 6
            if self.art_task:
                self.art_task.cancel()
                self.art_task = None
        if self.art_task and not self.art_task.done():
            return
        if now - self.art_checked < (.15 if now < self.art_retry_until else 1):
            return
        self.art_checked = now
        self.art_task = asyncio.create_task(self.artwork(props, key))

    async def artwork(self, props, key):
        try:
            await self.load_artwork(props, key)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.debug('Artwork unavailable', exc_info=True)
        finally:
            self.signal_change()

    async def load_artwork(self, props, key):
        if not props.thumbnail:
            self.art, self.art_version = b'', ''
            return
        from winsdk.windows.storage.streams import Buffer, InputStreamOptions
        stream = await asyncio.wait_for(props.thumbnail.open_read_async(), 3)
        try:
            size = stream.size
            if not 0 < size <= 8 * 1024 * 1024:
                return
            buf = await asyncio.wait_for(stream.read_async(Buffer(size), size, InputStreamOptions.READ_AHEAD), 3)
            data = bytes(buf)
            kind = stream.content_type or 'image/jpeg'
            if kind.startswith('image/') and data and key == self.art_key:
                # A small cover reaches the phone without transferring multi-MB artwork.
                from PIL import Image
                try:
                    with Image.open(io.BytesIO(data)) as image:
                        image.thumbnail((600, 600))
                        output = io.BytesIO()
                        image.convert('RGB').save(output, 'JPEG', quality=85, optimize=True)
                        data, kind = output.getvalue(), 'image/jpeg'
                except Exception:
                    LOG.debug('Cannot resize artwork', exc_info=True)
                version = hashlib.sha256(data).hexdigest()[:16]
                old_for_new_album = version == self.previous_art_version and self.current_album_key != self.previous_album_key
                if old_for_new_album and time.monotonic() - self.art_changed_at < 1.5:
                    return
                self.art, self.art_type, self.art_version = data, kind, version
                if not old_for_new_album or time.monotonic() - self.art_changed_at > 8:
                    self.art_cache[self.current_album_key] = (data, kind, version)
                    if len(self.art_cache) > 24:
                        self.art_cache.pop(next(iter(self.art_cache)))
        finally:
            stream.close()

    async def snapshot(self):
        state = dict(available=False, title='', artist='', album='', playing=False,
                     position=0, duration=0, cover='', volume=None, muted=False,
                     controls={}, message='请在电脑上打开 Apple Music，并先播放一首歌曲。')
        try:
            endpoint = self.endpoint()
            state.update(volume=round(endpoint.GetMasterVolumeLevelScalar() * 100),
                         muted=bool(endpoint.GetMute()))
        except Exception:
            LOG.debug('Core Audio unavailable', exc_info=True)
        session = self.session()
        if not session:
            if self.art_task:
                self.art_task.cancel()
                self.art_task = None
            self.art, self.art_version, self.art_key = b'', '', None
            return state
        if not self.subscriptions or all(s.source_app_user_model_id != session.source_app_user_model_id for s, _, _ in self.subscriptions):
            self.watch_sessions()
        info = session.get_playback_info()
        controls = info.controls
        props = await asyncio.wait_for(session.try_get_media_properties_async(), 3)
        artist, album = media_labels(props.artist, props.album_title)
        state.update(available=True, title=props.title or '未提供歌曲名称', artist=artist,
                     album=album, playing=int(info.playback_status) == 4,
                     controls={'toggle': bool(controls.is_play_pause_toggle_enabled),
                               'next': bool(controls.is_next_enabled),
                               'previous': bool(controls.is_previous_enabled)}, message='')
        state['controls']['seek'] = True  # Native API, with the desktop timeline as fallback.
        try:
            timeline = session.get_timeline_properties()
            start = timeline.start_time.total_seconds()
            duration = max(0, timeline.end_time.total_seconds() - start)
            position = max(0, timeline.position.total_seconds() - start)
            if state['playing']:
                import datetime
                position += max(0, (datetime.datetime.now(datetime.timezone.utc) - timeline.last_updated_time).total_seconds())
            state.update(position=min(position, duration) if duration else 0, duration=duration,
                         position_sampled_at=time.monotonic())
        except Exception:
            pass
        self.schedule_artwork(props, (session.source_app_user_model_id, props.title, props.artist, props.album_title))
        state['cover'] = self.art_version
        return state

    async def command(self, action, value=None):
        if action in ('volume', 'mute'):
            endpoint = self.endpoint()
            if action == 'volume':
                endpoint.SetMasterVolumeLevelScalar(value / 100, None)
            else:
                endpoint.SetMute(not endpoint.GetMute(), None)
            return True
        session = self.session()
        if session is None:
            raise ValueError('未找到 Apple Music。请先在电脑上播放一首歌。')
        if action == 'seek':
            if not session.get_playback_info().controls.is_playback_position_enabled:
                return False
            start = session.get_timeline_properties().start_time.total_seconds()
            return await asyncio.wait_for(session.try_change_playback_position_async(round((start + value) * 10_000_000)), 4)
        methods = {'toggle': session.try_toggle_play_pause_async,
                   'next': session.try_skip_next_async,
                   'previous': session.try_skip_previous_async}
        return await asyncio.wait_for(methods[action](), 4)


class DemoPlayer:
    """Explicit simulation for UI and protocol testing; never claimed as live music."""
    async def start(self):
        self.playing, self.volume, self.muted = True, 35, False
        self.index = 0
        self.position = 47
        self.art, self.art_type = b'', 'image/jpeg'

    async def snapshot(self):
        return dict(available=True, title=['界面演示 · 山间来信', '界面演示 · 夜色'][self.index],
                    artist='演示数据 · 未连接 Apple Music', album='Local Remote',
                    playing=self.playing, volume=self.volume, muted=self.muted,
                    position=self.position, duration=245, cover='',
                    controls=dict(toggle=True, next=True, previous=True, seek=True), message='演示模式')

    async def command(self, action, value=None):
        if action == 'toggle': self.playing = not self.playing
        elif action == 'seek': self.position = value
        elif action == 'volume': self.volume = value
        elif action == 'mute': self.muted = not self.muted
        else: self.index = 1 - self.index
        return True


class Remote:
    def __init__(self, player, token=None):
        self.player = player
        self.token = token or secrets.token_urlsafe(32)
        self.state = dict(available=False, playing=False, controls={}, volume=None,
                          title='', artist='', album='', cover='', position=0, duration=0,
                          muted=False, message='正在连接电脑上的 Apple Music，请稍候…')
        self.lock = asyncio.Lock()
        self.task = None
        self.refresh_requested = asyncio.Event()
        self.player_ready = False
        self.stopping = False
        self.pairing_task = None
        self.pairing_expires = 0
        self.pairing_port = None
        self.index_body = (ROOT / 'index.html').read_bytes()
        self.sockets = {}
        self.generation = 0
        self.queue_task = None
        self.queue_expires = 0
        self.asset_files = {}
        self.settings = load_settings()
        self.desktop_lock = asyncio.Lock()
        self.lyrics_task = None
        self.lyrics_key = None
        self.lyrics_checked_at = 0
        self.lyric_service = LyricService()
        self.queue_id = ''
        self.queue_items = []
        self.queue_source_task = None

    def wake(self):
        self.refresh_requested.set()

    async def poll(self):
        while not self.stopping:
            self.refresh_requested.clear()
            generation = self.generation
            try:
                # Slow async metadata/artwork reads must not queue playback commands.
                state = await asyncio.wait_for(self.player.snapshot(), 5)
                if generation != self.generation:
                    continue
                self.state = state
            except Exception:
                LOG.exception('Cannot refresh player')
                self.state = dict(available=False, playing=False, controls={}, volume=None,
                                  title='', artist='', album='', cover='', position=0, duration=0,
                                  muted=False, message='读取失败，请确认 Apple Music 正在运行；必要时重启遥控器。')
            await self.broadcast()
            try:
                await asyncio.wait_for(self.refresh_requested.wait(), timeout=.25)
            except asyncio.TimeoutError:
                pass

    async def startup(self, app):
        # Start listening immediately, even when the Windows bridge is slow.
        if hasattr(self.player, 'on_change'):
            self.player.on_change = self.wake
        self.task = asyncio.create_task(self.initialize())

    async def initialize(self):
        while True:
            try:
                await self.player.start()
                self.player_ready = True
                break
            except Exception:
                LOG.exception('Media initialization failed; retrying')
                self.state['message'] = '暂时无法连接 Windows 媒体接口，正在重试…'
                await asyncio.sleep(3)
        await self.poll()

    async def cleanup(self, app):
        self.stopping = True
        self.refresh_requested.set()
        await self.lyric_service.close()
        if self.lyrics_task and not self.lyrics_task.done():
            self.lyrics_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.lyrics_task
        for socket in list(self.sockets):
            await socket.close(code=1001, message=b'Server restarting')
        if self.pairing_task:
            self.pairing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.pairing_task
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if hasattr(self.player, 'close'):
            await self.player.close()
        if self.queue_task and not self.queue_task.done():
            self.queue_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.queue_task

    @web.middleware
    async def guard(self, request, handler):
        # Reject DNS-rebinding hosts. LAN HTTP is intentionally local only.
        try:
            host = request.host.split(':')[0]
            if host != 'localhost':
                ipaddress.ip_address(host)
        except ValueError:
            raise web.HTTPForbidden(text='Invalid host')
        origin = request.headers.get('Origin')
        if origin and origin != f'{request.scheme}://{request.host}':
            raise web.HTTPForbidden(text='Cross-origin request rejected')
        if request.path.startswith('/api/'):
            supplied = request.headers.get('Authorization', '')
            if not secrets.compare_digest(supplied, 'Bearer ' + self.token):
                raise web.HTTPUnauthorized(text='请重新扫描电脑上的二维码。')
        response = await handler(request)
        response.headers['Cache-Control'] = ('private, max-age=31536000, immutable'
                                            if request.path.startswith('/assets/') or request.path == f'/app/{UI_VERSION}/' else 'no-store')
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        return response

    async def index(self, request):
        return web.Response(body=self.index_body, content_type='text/html', charset='utf-8')

    async def asset(self, request):
        name = request.match_info['name']
        name = {'pairing-v6.js':'pairing-v7.js',
                'apple-touch-icon-v2.png':'apple-touch-icon-v6.png',
                'apple-touch-icon-v5.png':'apple-touch-icon-v6.png',
                'favicon-v2.png':'favicon-v6.png', 'favicon-v5.png':'favicon-v6.png'}.get(name, name)
        if name not in ('jsQR-1.4.0.js', 'pairing-v7.js', 'apple-touch-icon-v6.png', 'favicon-v6.png'):
            raise web.HTTPNotFound()
        if name not in self.asset_files:
            self.asset_files[name] = await asyncio.to_thread((ROOT / 'assets' / name).read_bytes)
        kind = 'application/javascript' if name.endswith('.js') else 'image/png'
        return web.Response(body=self.asset_files[name], content_type=kind)

    async def status(self, request):
        return web.json_response(self.payload())

    def payload(self, previous_cover=None):
        state = {**self.state, 'ui_version': UI_VERSION, **self.settings}
        sampled_at = state.pop('position_sampled_at', None)
        if sampled_at is not None and state.get('playing') and state.get('available'):
            # A reconnect must receive the current position, not an old cached sample.
            state['position'] = min(state.get('duration', 0), state.get('position', 0)
                                    + max(0, time.monotonic() - sampled_at))
        version = state.get('cover', '')
        if version and previous_cover != version and self.player.art and version == getattr(self.player, 'art_version', version):
            state['cover_data'] = 'data:' + self.player.art_type + ';base64,' + base64.b64encode(self.player.art).decode('ascii')
        return state

    async def send_state(self, socket):
        payload = self.payload(self.sockets.get(socket))
        await asyncio.wait_for(socket.send_json(payload), timeout=1)
        self.sockets[socket] = payload.get('cover', '')

    async def broadcast(self):
        async def send(socket):
            try:
                await self.send_state(socket)
            except (ConnectionError, asyncio.TimeoutError, RuntimeError):
                self.sockets.pop(socket, None)
                await socket.close()
        if self.sockets:
            await asyncio.gather(*(send(socket) for socket in list(self.sockets)))

    async def events(self, request):
        socket = web.WebSocketResponse(heartbeat=15, max_msg_size=1024)
        await socket.prepare(request)
        try:
            # First-frame auth avoids putting the pairing secret into URLs/logs.
            auth = await socket.receive_json(timeout=3)
            if not isinstance(auth, dict) or not secrets.compare_digest(str(auth.get('token', '')), self.token):
                await socket.close(code=1008, message=b'Invalid pairing')
                return socket
            self.sockets[socket] = None
            await self.send_state(socket)
            async for message in socket:
                if message.type == web.WSMsgType.TEXT and message.data == 'refresh':
                    self.wake()
                elif message.type == web.WSMsgType.TEXT and message.data == 'clock':
                    await socket.send_json({'type': 'clock'})
        except (asyncio.TimeoutError, ValueError, TypeError):
            await socket.close(code=1008, message=b'Pairing required')
        finally:
            self.sockets.pop(socket, None)
        return socket

    async def cover(self, request):
        if not self.player.art:
            raise web.HTTPNotFound()
        requested = request.query.get('v')
        if requested and requested != getattr(self.player, 'art_version', requested):
            raise web.HTTPNotFound()
        return web.Response(body=self.player.art, content_type=self.player.art_type)

    async def command(self, request):
        if not self.player_ready:
            return web.json_response({'error': 'Windows 媒体接口正在连接，请稍后再试。'}, status=503)
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
        try:
            data = await request.json()
            if not isinstance(data, dict): raise ValueError('请求格式错误')
            action = data.get('action')
            if action not in ('toggle', 'next', 'previous', 'volume', 'mute', 'seek'):
                raise ValueError('未知操作')
            value = data.get('value')
            if action == 'volume' and (type(value) is not int or not 0 <= value <= 100):
                raise ValueError('音量必须是 0–100 的整数')
            if action == 'seek':
                duration = self.state.get('duration', 0)
                if duration <= 0 or type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= duration:
                    raise ValueError('进度必须在歌曲时长以内')
                key = '\n'.join(str(self.state.get(k, '')) for k in ('title','artist','album'))
                if data.get('track') is not None and data['track'] != key:
                    return web.json_response({'error':'歌曲已变化，请重新拖动进度。'}, status=409)
            async with self.lock:
                result = await self.player.command(action, value)
                if action == 'seek' and not result:
                    result = (await self.desktop_action('Seek', value, self.state.get('title',''))).get('available', False)
                if result:
                    self.generation += 1
                    self.refresh_requested.set()
            if not result:
                return web.json_response({'error': 'Apple Music 暂未接受这个操作，请在电脑上确认播放状态。'}, status=409)
            return web.json_response({'ok': True, 'state': self.payload()})
        except (ValueError, TypeError):
            return web.json_response({'error': '操作无效，或未找到 Apple Music；请在电脑上先开始播放。'}, status=400)
        except Exception:
            LOG.exception('Command failed')
            return web.json_response({'error': '执行失败，请在电脑上确认 Apple Music 和音频设备。'}, status=503)

    async def read_queue(self):
        async with self.desktop_lock:
            return await self.read_queue_unlocked()

    async def read_queue_unlocked(self):
        if sys.platform != 'win32':
            return {'available': False, 'tracks': [], 'message': '待播放列表需要 Windows Apple Music。'}
        process = await asyncio.create_subprocess_exec(
            'powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
            '-File', str(ROOT / 'queue_reader.ps1'),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            output, error = await asyncio.wait_for(process.communicate(), timeout=8)
            if process.returncode:
                raise ValueError('Queue helper failed')
            data = json.loads(output.decode('utf-8-sig'))
            if not data.get('available'):
                data = {'available': False, 'tracks': [], 'message': '暂时无法读取待播放列表，请在电脑上打开并解锁 Apple Music 后重试。'}
            return data
        except (asyncio.TimeoutError, ValueError, UnicodeError):
            return {'available': False, 'tracks': [], 'message': '读取队列没有完成，请在电脑上确认 Apple Music 已打开后重试。'}
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def desktop_action(self, action, value=None, title=None):
        if sys.platform != 'win32':
            return {'available':False}
        command = ['powershell.exe','-NoProfile','-STA','-ExecutionPolicy','Bypass','-File',str(ROOT/'desktop_actions.ps1'),'-Action',action]
        if value is not None:
            command.extend(['-Position',str(value)])
        if title:
            command.extend(['-ExpectedTitle',title])
        async with self.desktop_lock:
            process = await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                output, _ = await asyncio.wait_for(process.communicate(), timeout=8)
                return json.loads(output.decode('utf-8-sig')) if process.returncode == 0 else {'available':False}
            except (asyncio.TimeoutError,ValueError,UnicodeError):
                return {'available':False}
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def lyrics(self, request):
        snapshot = dict(self.state)
        key = '\n'.join(str(self.state.get(k,'')) for k in ('title','artist','album'))
        title = self.state.get('title','')
        if self.lyrics_task is None or self.lyrics_key != key or self.lyrics_task.cancelled() or (self.lyrics_task.done() and (self.lyrics_task.exception() is not None or (not self.lyrics_task.result().get('available') and time.monotonic()-self.lyrics_checked_at>10))):
            self.lyrics_key = key
            self.lyrics_checked_at = time.monotonic()
            self.lyrics_task = asyncio.create_task(self.desktop_action('Lyrics'))
        data = dict(await asyncio.shield(self.lyrics_task))
        current = '\n'.join(str(self.state.get(k,'')) for k in ('title','artist','album'))
        if current != key or (data.get('title') and data['title'] != title):
            return web.json_response({'available':False,'lines':[],'message':'歌曲已变化，请刷新歌词。'})
        if not data.get('available'):
            data = {'available':False,'lines':[],'message':'Apple Music 当前没有可读取的歌词；请在电脑上确认该歌曲有歌词，且 Apple Music 已打开。'}
        data = await self.lyric_service.resolve(snapshot, data, online=self.settings.get('online_lyrics',False), force=request.query.get('refresh')=='1')
        current = '\n'.join(str(self.state.get(k,'')) for k in ('title','artist','album'))
        if current != key:
            return web.json_response({'available':False,'lines':[],'message':'歌曲已变化，请刷新歌词。'})
        data['track'] = key
        return web.json_response(data)

    async def queue(self, request):
        if (self.queue_task is None or self.queue_task.cancelled()
                or (self.queue_task.done() and (self.queue_task.exception() is not None
                    or time.monotonic() >= self.queue_expires))):
            self.queue_task = asyncio.create_task(self.read_queue())
            self.queue_expires = time.monotonic() + 4
        data = dict(await asyncio.shield(self.queue_task))
        if self.queue_source_task is not self.queue_task:
            self.queue_source_task = self.queue_task
            self.queue_id = secrets.token_urlsafe(16)
            self.queue_items = data.get('tracks', [])
        data['queue_id'] = self.queue_id
        data['tracks'] = [{k:v for k,v in row.items() if k != 'identity'} for row in data.get('tracks', [])]
        return web.json_response(data)

    async def queue_play(self, request):
        if request.content_type != 'application/json':
            raise web.HTTPUnsupportedMediaType()
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError()
            index = data.get('index')
            if type(index) is not int or not 0 <= index < len(self.queue_items):
                raise ValueError()
            if data.get('queue_id') != self.queue_id:
                return web.json_response({'error':'队列已经变化，请刷新列表后重试。'}, status=409)
            item = self.queue_items[index]
            result = await self.queue_activate(item.get('index', index), item.get('identity',''))
            if not result.get('available'):
                return web.json_response({'error':'没有播放这首歌。请确认电脑上的 Apple Music 已打开，刷新待播放列表后重试。'}, status=409)
            self.queue_expires = 0
            self.queue_id = ''
            self.queue_items = []
            self.generation += 1
            self.wake()
            return web.json_response({'ok':True})
        except (ValueError,TypeError):
            return web.json_response({'error':'歌曲选择无效，请刷新列表。'},status=400)

    async def queue_activate(self, index, identity):
        if not identity or sys.platform != 'win32':
            return {'available':False}
        command = ['powershell.exe','-NoProfile','-STA','-ExecutionPolicy','Bypass','-File',str(ROOT/'queue_activate.ps1'),'-Index',str(index),'-ExpectedName',identity]
        async with self.desktop_lock:
            process = await asyncio.create_subprocess_exec(*command,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                output, _ = await asyncio.wait_for(process.communicate(),8)
                return json.loads(output.decode('utf-8-sig')) if process.returncode == 0 else {'available':False}
            except (asyncio.TimeoutError,ValueError,UnicodeError):
                return {'available':False}
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

    async def connect(self, request):
        try:
            if not ipaddress.ip_address(request.remote).is_loopback:
                raise web.HTTPForbidden(text='Open this page on the Windows PC.')
        except ValueError:
            raise web.HTTPForbidden()
        port = request.url.port
        if (self.pairing_task is None or self.pairing_task.cancelled()
                or (self.pairing_task.done() and (self.pairing_task.exception() is not None
                    or self.pairing_port != port or time.monotonic() >= self.pairing_expires))):
            self.pairing_port = port
            self.pairing_task = asyncio.create_task(asyncio.to_thread(self.pairing_html, port))
            self.pairing_expires = time.monotonic() + 30
        try:
            html = await asyncio.wait_for(asyncio.shield(self.pairing_task), timeout=3)
        except asyncio.TimeoutError:
            return web.Response(text='<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="2"><p>正在读取电脑网络地址，请稍候，页面将自动刷新。</p>', content_type='text/html')
        return web.Response(text=html, content_type='text/html')

    def pairing_html(self, port):
        # DNS fallback, QR encoding and image compression cannot block HTTP requests.
        import qrcode
        def qr_img(url):
            image = qrcode.make(url)
            output = io.BytesIO()
            image.save(output, format='PNG')
            return '<img alt="QR" src="data:image/png;base64,' + base64.b64encode(output.getvalue()).decode() + '">'
        cards = []
        for address in local_addresses():
            music = f'http://{address}:{port}/app/{UI_VERSION}/#{self.token}'
            claude = f'http://{address}:{ORBIT_CLAUDE_PORT}/orbit#{self.token}'
            cards.append(f'<section><h2>{address}</h2><div class="row">'
                         f'<figure>{qr_img(music)}<figcaption>音乐控制 Music</figcaption></figure>'
                         f'<figure>{qr_img(claude)}<figcaption>Claude 额度与授权 Claude</figcaption></figure>'
                         '</div></section>')
        html = '''<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>连接 Orbit</title><style>body{font:17px system-ui;background:#101018;color:#fafafa;max-width:760px;margin:50px auto;padding:24px}section{background:#242430;border-radius:24px;padding:24px;margin:24px 0;overflow-wrap:anywhere}img{width:220px;max-width:100%;background:#fff;padding:10px;border-radius:12px}.row{display:flex;gap:24px;flex-wrap:wrap}figure{margin:0}figcaption{margin-top:8px}h2{margin-top:0;font-size:18px}a{color:#ffa4b2}p{line-height:1.7}</style><h1>用 Orbit 扫码连接这台电脑</h1><p>iPhone 和电脑连同一个 Wi-Fi。在 Orbit 里：<b>音乐</b>页点“扫码添加”扫左边的码；<b>Claude</b>页点“扫码连接”扫右边的码。有多组二维码时，选与你家 Wi-Fi 对应的那一组。</p><p>先在电脑的 Apple Music 播放一首歌。配对信息会长期保存。二维码含有访问密钥，请勿截图分享。</p>'''
        html += ''.join(cards) or '<p>未找到局域网地址，请连接 Wi-Fi 或以太网后重启。</p>'
        html += f'<p><a href="/#{self.token}">在这台电脑上测试遥控器</a></p><p>如果手机打不开：将家庭网络设为 Private，并在 Windows 防火墙提示中允许 Python 访问专用网络。不要关闭防火墙。</p>'
        return html

    def application(self):
        app = web.Application(middlewares=[self.guard], client_max_size=2048)
        app.router.add_get('/', self.index)
        app.router.add_get('/app/{version}/', self.index)
        app.router.add_get('/events', self.events)
        app.router.add_get('/assets/{name}', self.asset)
        app.router.add_get('/connect', self.connect)
        app.router.add_get('/api/state', self.status)
        app.router.add_get('/api/queue', self.queue)
        app.router.add_post('/api/queue/play', self.queue_play)
        app.router.add_get('/api/lyrics', self.lyrics)
        app.router.add_get('/api/cover', self.cover)
        app.router.add_post('/api/command', self.command)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.cleanup)
        return app


def main():
    global FAULT_LOG
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--demo', action='store_true', help='Simulated UI only')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32' and not args.demo:
        parser.error('Real playback requires Windows. Use --demo for interface testing.')
    (ROOT / '.state').mkdir(exist_ok=True)
    fault_path = ROOT / '.state' / 'native-crash.log'
    if fault_path.exists() and fault_path.stat().st_size > 1024*1024:
        fault_path.replace(fault_path.with_suffix('.previous.log'))
    FAULT_LOG = fault_path.open('a', encoding='utf-8')
    faulthandler.enable(file=FAULT_LOG, all_threads=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s',
                        handlers=[logging.StreamHandler(), RotatingFileHandler(
                            ROOT / '.state' / 'server.log', maxBytes=2*1024*1024,
                            backupCount=2, encoding='utf-8')])
    remote = Remote(PlayerWorker(DemoPlayer() if args.demo else WindowsPlayer()),
                    token=saved_token(ROOT / '.state' / ('demo.key' if args.demo else 'pairing.key')))
    try:
        import orbit_bridge
        orbit_bridge.configure(port=ORBIT_CLAUDE_PORT, token=remote.token, home=ROOT / '.state' / 'claude')
        orbit_bridge.start_in_thread()
    except Exception:
        LOG.exception('Orbit Claude bridge failed to start (music still works)')

    async def serve():
        runner = web.AppRunner(remote.application(), access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, '0.0.0.0', args.port).start()
            url = f'http://127.0.0.1:{args.port}/connect'
            print('\nOrbit bridge — keep this window open.\nOpen on this PC: ' + url, flush=True)
            if args.demo: print('DEMO MODE: No actual player connected.', flush=True)
            if not args.no_browser: webbrowser.open(url)
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    except Exception:
        LOG.exception('Startup failed. Check README / port / dependencies.')
        raise SystemExit(1)


if __name__ == '__main__':
    main()
