"""Current-user background runner. No service, administrator rights or network changes."""
import argparse
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
STATE = ROOT / '.state'
STOP = STATE / 'stop'
LOCK = STATE / 'runner.lock'


def acquire():
    STATE.mkdir(parents=True, exist_ok=True)
    handle = LOCK.open('a+b')
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def release(handle):
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    handle.close()


def running():
    handle = acquire()
    if handle is None:
        return True
    release(handle)
    return False


def stop():
    if not running():
        return
    STOP.touch()
    for _ in range(150):
        if not running():
            return
        time.sleep(.1)
    raise RuntimeError('Could not stop the background runner. See .state/runner.log.')


def supervise(demo=False):
    handle = acquire()
    if handle is None:
        return
    child = None
    try:
        STOP.unlink(missing_ok=True)
        logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(
            STATE / 'runner.log', maxBytes=1024*1024, backupCount=2, encoding='utf-8')],
            format='%(asctime)s %(levelname)s %(message)s')
        failures = 0
        while not STOP.exists():
            command = [sys.executable, str(ROOT / 'server.py'), '--no-browser']
            if demo:
                command.append('--demo')
            started = time.monotonic()
            child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            logging.info('Started server pid=%s', child.pid)
            while child.poll() is None and not STOP.exists():
                time.sleep(.5)
            if STOP.exists():
                break
            failures = 0 if time.monotonic() - started > 60 else failures + 1
            delay = min(60, 2 ** min(failures, 6))
            logging.warning('Server exited (%s); retry in %ss', child.returncode, delay)
            for _ in range(delay * 2):
                if STOP.exists():
                    break
                time.sleep(.5)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        STOP.unlink(missing_ok=True)
        release(handle)


def start(demo=False):
    if not running():
        command = [str(Path(sys.executable).with_name('pythonw.exe')), str(Path(__file__).resolve()), '--run']
        if demo:
            command.append('--demo')
        subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    # Verify THIS installation's key, rather than opening an unrelated server on the port.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        try:
            token = (STATE / ('demo.key' if demo else 'pairing.key')).read_text(encoding='ascii').strip()
            request = urllib.request.Request('http://127.0.0.1:8765/api/state',
                                             headers={'Authorization': 'Bearer ' + token})
            with opener.open(request, timeout=.5) as response:
                if response.status == 200:
                    webbrowser.open('http://127.0.0.1:8765/connect')
                    return
        except (OSError, ValueError):
            pass
        time.sleep(.5)
    raise RuntimeError('Server did not start. Close the OLD START.bat window if it is running, then retry. See .state/server.log and .state/runner.log.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    if args.stop:
        stop()
    elif args.run:
        supervise(args.demo)
    else:
        start(args.demo)
