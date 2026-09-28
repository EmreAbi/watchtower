"""Read the configured CODEX_HOME account through the official local app server.

Only sanitized display metadata is cached, in memory, for ten minutes. Changes
to auth.json or config.toml metadata invalidate that cache; neither file is read.
Keyring-only login changes can remain cached for up to ten minutes. This is the
configured account for the supplied home, not the identity of a running agent.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time


CACHE_SECONDS = 600
RPC_SECONDS = 12
_MAX_LINE_BYTES = 65536
_MAX_MESSAGES = 128


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _empty(error=None):
    return dict(email_masked=None, email_domain=None, plan=None, auth_type=None,
                remaining_percent=None, quota_label=None, quota_windows=[], observed_at=_now(),
                error=error)


def _mask_email(value):
    if (not isinstance(value, str) or len(value) > 254
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._%+\-]*@'
                            r'[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?', value) is None):
        return None
    user, domain = value.split('@')
    if len(user) > 64:
        return None
    return user[:2] + '***@' + domain[:2] + '***'


def _email_domain(value):
    if _mask_email(value) is None:
        return None
    domain = value.split('@', 1)[1].lower()
    if not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
               for label in domain.split('.')):
        return None
    return domain


def _plan(value):
    # Plan names are metadata, not arbitrary text suitable for echoing to the UI.
    known = {'free', 'go', 'plus', 'pro', 'team', 'business', 'enterprise', 'edu',
             'unknown'}
    return value.lower() if isinstance(value, str) and value.lower() in known else None


def _fingerprint(home):
    result = []
    for name in ('auth.json', 'config.toml'):
        try:
            info = (home / name).stat()
            result.append((info.st_dev, info.st_ino, info.st_size,
                           info.st_mtime_ns, info.st_ctime_ns))
        except FileNotFoundError:
            result.append(None)
        except OSError:
            result.append('unavailable')
    return tuple(result)


def _quota_windows(payload):
    if not isinstance(payload, dict):
        return []
    # Other buckets can have unrelated allowances. Never combine them or choose
    # one simply because it is first in an object returned by the server.
    buckets = payload.get('rateLimitsByLimitId')
    bucket = buckets.get('codex') if isinstance(buckets, dict) else None
    if not isinstance(bucket, dict):
        bucket = payload.get('rateLimits')
        if not isinstance(bucket, dict) or bucket.get('limitId') not in (None, 'codex'):
            return []
    windows = [(name, bucket.get(name)) for name in ('primary', 'secondary')]
    valid = []
    for name, window in windows:
        if not isinstance(window, dict):
            continue
        used = window.get('usedPercent')
        duration = window.get('windowDurationMins')
        if (type(used) not in (int, float) or used < 0 or used > 100
                or not math.isfinite(used)):
            continue
        if duration is not None and (type(duration) is not int or not 0 < duration <= 5256000):
            continue
        reset = window.get('resetsAt')
        # A Unix timestamp fitting the supported calendar; bool is not an int.
        if type(reset) is not int or not 0 <= reset <= 253402300799:
            reset = None
        valid.append(dict(name=name, used_percent=used, remaining_percent=100 - used,
                          window_minutes=duration, resets_at=reset))
    return valid


def _selected_quota(windows):
    for window in windows:
        if window['window_minutes'] == 7 * 24 * 60:
            return window['remaining_percent'], 'Week'
    for window in windows:
        if window['name'] == 'primary':
            return window['remaining_percent'], 'Primary'
    return None, None


def _quota(payload):
    return _selected_quota(_quota_windows(payload))


class _Unavailable(Exception):
    def __init__(self, code='unavailable'):
        self.code = code


def _cli_command():
    # Import lazily: backend creates readers, while this reusable probe uses
    # the same native/npm resolution contract as interactive login and launch.
    try:
        from .backend import AccountError, provider_command
    except ImportError:
        from backend import AccountError, provider_command
    try:
        command = provider_command('codex')
    except AccountError:
        raise _Unavailable('cli_unavailable') from None
    return command + ['app-server', '--listen', 'stdio://']


def _cleanup(process):
    """Reap the owned process, including npm shim children, with bounded waits."""
    try:
        if os.name == 'nt' and process.poll() is None:
            # PID belongs exclusively to our new child. /T includes its shim's
            # app-server child, never other already running Codex processes.
            killer = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                         'System32' / 'taskkill.exe')
            try:
                subprocess.run([killer, '/PID', str(process.pid), '/T', '/F'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2,
                               creationflags=subprocess.CREATE_NO_WINDOW,
                               check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        elif os.name != 'nt':
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
    except (OSError, subprocess.SubprocessError):
        # Never show raw platform errors, which can contain private paths.
        pass
    finally:
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class _Rpc:
    def __init__(self, process, deadline):
        self.process = process
        self.deadline = deadline
        self.messages = queue.Queue(maxsize=16)
        self.failed = threading.Event()
        self.count = 0
        self.thread = threading.Thread(target=self._pump, daemon=True,
                                       name='watchtower-account-stdout')
        self.thread.start()

    def _pump(self):
        try:
            while True:
                line = self.process.stdout.readline(_MAX_LINE_BYTES + 1)
                if not line:
                    self.messages.put_nowait(None)
                    return
                if len(line) > _MAX_LINE_BYTES or not line.endswith(b'\n'):
                    self.failed.set()
                    return
                self.messages.put_nowait(line)
        except (OSError, ValueError, queue.Full):
            self.failed.set()

    def send(self, method, params=None, request_id=None):
        message = {'method': method}
        if params is not None:
            message['params'] = params
        if request_id is not None:
            message['id'] = request_id
        try:
            self.process.stdin.write((json.dumps(message) + '\n').encode('utf-8'))
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise _Unavailable() from None

    def receive(self, request_id):
        while self.count < _MAX_MESSAGES:
            if self.failed.is_set():
                raise _Unavailable('invalid_response')
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise _Unavailable('timeout')
            try:
                line = self.messages.get(timeout=remaining)
            except queue.Empty:
                raise _Unavailable('timeout') from None
            if line is None:
                raise _Unavailable()
            self.count += 1
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                raise _Unavailable('invalid_response') from None
            if not isinstance(message, dict):
                raise _Unavailable('invalid_response')
            if message.get('id') != request_id:
                continue
            if 'error' in message or not isinstance(message.get('result'), dict):
                raise _Unavailable()
            return message['result']
        raise _Unavailable('invalid_response')


class CodexAccountReader:
    """Return sanitized account metadata for read(home: Path), never credentials.

    Known auth_type values follow the API: ``chatgpt`` and ``apiKey``. Missing
    fields are None. ``observed_at`` is the actual snapshot/attempt time in UTC,
    and is preserved when serving a cache hit. Failures are cached as well.
    """

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()

    def read(self, home: Path, force=False):
        try:
            if not isinstance(home, (str, os.PathLike)) or home == '':
                return _empty('invalid_home')
            home = Path(home).expanduser().resolve()
            if not home.is_dir():
                return _empty('invalid_home')
        except (OSError, ValueError, TypeError, RuntimeError):
            return _empty('invalid_home')
        key = os.path.normcase(str(home))
        with self._lock:
            fingerprint = _fingerprint(home)
            cached = self._cache.get(key)
            if (not force and cached is not None and cached[0] == fingerprint
                    and time.monotonic() - cached[1] < CACHE_SECONDS):
                return deepcopy(cached[2])
            try:
                result = self._probe(home)
            except _Unavailable as error:
                result = _empty(error.code)
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                result = _empty('unavailable')
            after = _fingerprint(home)
            if after != fingerprint:
                result = _empty('account_changed')
            self._cache[key] = (after, time.monotonic(), deepcopy(result))
            return deepcopy(result)

    def _probe(self, home):
        command = _cli_command()
        environment = os.environ.copy()
        excluded = {'OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_ACCESS_TOKEN',
                    'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN'}
        for name in list(environment):
            if name.upper() in excluded:
                environment.pop(name, None)
        environment['CODEX_HOME'] = str(home)
        options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                       stderr=subprocess.DEVNULL, env=environment, bufsize=0)
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            options['start_new_session'] = True
        deadline = time.monotonic() + RPC_SECONDS
        process = subprocess.Popen(command, **options)
        rpc = None
        try:
            rpc = _Rpc(process, deadline)
            rpc.send('initialize', {'clientInfo': {'name': 'watchtower_accounts',
                                                  'version': '0.1.0'}}, 1)
            rpc.receive(1)
            rpc.send('initialized')
            rpc.send('account/read', {'refreshToken': False}, 2)
            payload = rpc.receive(2)
            account = payload.get('account')
            result = _empty()
            if not isinstance(account, dict):
                result['error'] = 'account_unavailable'
                return result
            auth_type = account.get('type')
            if auth_type not in ('chatgpt', 'apiKey'):
                result['error'] = 'account_unavailable'
                return result
            result['auth_type'] = auth_type
            if auth_type == 'apiKey':
                return result
            result['email_masked'] = _mask_email(account.get('email'))
            result['email_domain'] = _email_domain(account.get('email'))
            result['plan'] = _plan(account.get('planType'))
            # An unavailable quota does not erase a successfully read identity.
            try:
                rpc.send('account/rateLimits/read', request_id=3)
                result['quota_windows'] = _quota_windows(rpc.receive(3))
                result['remaining_percent'], result['quota_label'] = _selected_quota(result['quota_windows'])
            except _Unavailable:
                pass
            result['observed_at'] = _now()
            return result
        finally:
            _cleanup(process)
            if rpc is not None:
                rpc.thread.join(timeout=0.2)
