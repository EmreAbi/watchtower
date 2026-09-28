"""Button-driven Codex browser login through its official local app server.

The start/cancel/completed shapes were verified against the installed Codex
CLI's generated v2 protocol schemas. Only this module's owned app server is
terminated. The returned authorization URL is transient UI data, never a log
message; provider credentials and raw RPC errors are never exposed.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from urllib.parse import urlsplit

try:
    from .account_reader import _cleanup
    from .backend import plan_environment, provider_command
except ImportError:
    from account_reader import _cleanup
    from backend import plan_environment, provider_command

START_SECONDS = 15.0
LOGIN_SECONDS = 600.0
CANCEL_SECONDS = 1.0
CLOSE_SECONDS = 5.0
MAX_LINE_BYTES = 65536
MAX_MESSAGES = 1024
_END = object()
_INVALID = object()

_MESSAGES = {
    "startup_failed": "Could not start Codex sign-in. Close any other Codex sign-in attempt, then try again.",
    "startup_timeout": "Codex sign-in did not start in time. Please try again.",
    "invalid_response": "Codex returned an unsupported sign-in response. Update the CLI and try again.",
    "invalid_url": "Codex returned an unexpected sign-in address. The browser was not opened.",
    "login_failed": "Codex sign-in did not complete. Please try again.",
    "login_timeout": "Sign-in expired. Connect again to create a new browser link.",
    "cancelled": "Sign-in cancelled.",
    "success": "Codex account connected.",
    "unsupported_provider": "Browser sign-in is available here for Codex accounts.",
}


class BrowserLoginError(RuntimeError):
    def __init__(self, code):
        self.code = code if code in _MESSAGES else "startup_failed"
        self.message = _MESSAGES[self.code]
        super().__init__(self.message)


class _Cancelled(Exception):
    pass


def validate_auth_url(value):
    """Keep the original OAuth query intact, but allow exactly the auth endpoint."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 8192
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or "\\" in value):
        raise BrowserLoginError("invalid_url")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname == "auth.openai.com"
                 and parsed.netloc in ("auth.openai.com", "auth.openai.com:443")
                 and parsed.port in (None, 443) and parsed.username is None
                 and parsed.password is None and parsed.path == "/oauth/authorize"
                 and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise BrowserLoginError("invalid_url")
    return value


class _Pipe:
    """Bounded stdout reader. Raw messages stay inside the owned worker."""
    def __init__(self, process):
        self.process = process
        self.messages = queue.Queue(maxsize=64)
        self.overflow = threading.Event()
        self.thread = threading.Thread(target=self._pump, daemon=True, name="watchtower-signin-stdout")
        self.thread.start()

    def _pump(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_LINE_BYTES + 1)
                if not line:
                    self.messages.put_nowait(_END)
                    return
                if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                    self.messages.put_nowait(_INVALID)
                    return
                self.messages.put_nowait(line)
        except (OSError, ValueError, queue.Full):
            self.overflow.set()

    def send(self, method, params=None, request_id=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        if request_id is not None:
            message["id"] = request_id
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def receive(self, timeout):
        if self.overflow.is_set():
            raise BrowserLoginError("invalid_response")
        try:
            line = self.messages.get(timeout=timeout)
        except queue.Empty:
            return None
        if line is _END:
            raise BrowserLoginError("login_failed")
        if line is _INVALID:
            raise BrowserLoginError("invalid_response")
        try:
            result = json.loads(line)
        except (ValueError, UnicodeError):
            raise BrowserLoginError("invalid_response") from None
        if not isinstance(result, dict):
            raise BrowserLoginError("invalid_response")
        return result


class BrowserLogin:
    """One cancellable login attempt; construction performs no I/O.

    ``start`` may run on a UI worker. It returns ``{"url": ...}`` within the
    startup deadline or raises a safe BrowserLoginError. ``poll`` never waits.
    ``cancel`` signals immediately, including during startup. ``close`` also
    waits at most CLOSE_SECONDS for owned cleanup and may run off the UI loop.
    Successful completion requires the exact loginId from this attempt.
    """
    def __init__(self, service, profile):
        self.service = service
        self.profile = profile
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._ready = threading.Event()
        self._worker = None
        self._process = None
        self._result = None
        self._url = None
        self._login_id = None
        self._closed = False
        self._pending = []
        self._messages = 0

    def start(self):
        with self._lock:
            if self._closed or self._cancel.is_set():
                raise BrowserLoginError("cancelled")
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, daemon=True, name="watchtower-browser-signin")
                self._worker.start()
        if not self._ready.wait(START_SECONDS):
            self._finish("error", "startup_timeout")
            self._cancel.set()
        with self._lock:
            if self._result and self._result["state"] != "success":
                raise BrowserLoginError(self._result["code"])
            if not self._url:
                raise BrowserLoginError("startup_failed")
            return {"url": self._url}

    def poll(self):
        with self._lock:
            if self._result is None:
                return None
            return {key: value for key, value in self._result.items() if key != "code"}

    def _finish(self, state, code):
        with self._lock:
            if state == "success" and self._cancel.is_set():
                state, code = "cancelled", "cancelled"
            if self._result is None:
                self._result = {"state": state, "message": _MESSAGES[code], "code": code}
            self._ready.set()

    def cancel(self):
        self._cancel.set()
        self._finish("cancelled", "cancelled")

    def close(self):
        with self._lock:
            self._closed = True
            finished = self._result is not None
            worker = self._worker
        if not finished:
            self.cancel()
        else:
            self._cancel.set()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=CLOSE_SECONDS)
        with self._lock:
            self._url = None

    def _next(self, pipe, deadline):
        if self._cancel.is_set():
            raise _Cancelled()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserLoginError("login_timeout" if self._login_id else "startup_timeout")
        message = pipe.receive(min(remaining, 0.1))
        if message is not None:
            self._messages += 1
            if self._messages > MAX_MESSAGES:
                raise BrowserLoginError("invalid_response")
        return message

    def _response(self, pipe, request_id, deadline):
        while True:
            message = self._next(pipe, deadline)
            if message is None:
                continue
            if message.get("method") == "account/login/completed":
                # The notification may beat its start response. Keep a small
                # bounded list and correlate after loginId becomes available.
                if len(self._pending) >= 16:
                    raise BrowserLoginError("invalid_response")
                self._pending.append(message)
            if message.get("id") == request_id:
                if "error" in message:
                    raise BrowserLoginError("startup_failed")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise BrowserLoginError("invalid_response")
                return result

    def _completed(self, message):
        if message.get("method") != "account/login/completed":
            return False
        params = message.get("params")
        if not isinstance(params, dict) or params.get("loginId") != self._login_id:
            return False
        if params.get("success") is True:
            if self._cancel.is_set():
                raise _Cancelled()
            self._finish("success", "success")
        elif params.get("success") is False:
            self._finish("error", "login_failed")
        else:
            raise BrowserLoginError("invalid_response")
        return True

    def _cancel_rpc(self, pipe):
        if self._login_id is None:
            return
        try:
            pipe.send("account/login/cancel", {"loginId": self._login_id}, 3)
            deadline = time.monotonic() + CANCEL_SECONDS
            while time.monotonic() < deadline:
                message = pipe.receive(min(0.1, max(0, deadline - time.monotonic())))
                if message is not None and message.get("id") == 3:
                    return
        except (BrowserLoginError, OSError, ValueError):
            pass

    def _run(self):
        process = None
        pipe = None
        try:
            deadline = time.monotonic() + START_SECONDS
            if self._cancel.is_set():
                raise _Cancelled()
            plan = self.service.connect_plan(self.profile)
            if plan.get("provider") != "codex":
                raise BrowserLoginError("unsupported_provider")
            native = provider_command("codex")
            # Preserve the managed-profile file-store override from the same
            # connect plan; do not manufacture a second identity environment.
            prefix = len(native)
            if plan.get("argv", [])[:prefix] != native or plan["argv"][prefix:prefix + 1] != ["login"]:
                raise BrowserLoginError("invalid_response")
            options = plan["argv"][prefix + 1:]
            if options not in ([], ["-c", 'cli_auth_credentials_store="file"']):
                raise BrowserLoginError("invalid_response")
            if self._cancel.is_set() or time.monotonic() >= deadline:
                raise _Cancelled() if self._cancel.is_set() else BrowserLoginError("startup_timeout")
            environment = plan_environment(plan)
            command = native + ["app-server", "--listen", "stdio://"] + options
            kwargs = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          env=environment, cwd=environment.get("CODEX_HOME"), bufsize=0)
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **kwargs)
            with self._lock:
                self._process = process
            if self._cancel.is_set():
                raise _Cancelled()
            pipe = _Pipe(process)
            pipe.send("initialize", {"clientInfo": {"name": "watchtower_accounts_login", "version": "0.1.0"}}, 1)
            self._response(pipe, 1, deadline)
            if self._cancel.is_set():
                raise _Cancelled()
            pipe.send("initialized")
            pipe.send("account/login/start", {"type": "chatgpt"}, 2)
            response = self._response(pipe, 2, deadline)
            identifier = response.get("loginId")
            if (response.get("type") != "chatgpt" or not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                    or any(ord(char) <= 32 or ord(char) == 127 for char in identifier)):
                raise BrowserLoginError("invalid_response")
            self._login_id = identifier
            url = validate_auth_url(response.get("authUrl"))
            with self._lock:
                if self._cancel.is_set():
                    raise _Cancelled()
                self._url = url
                self._ready.set()
            for message in self._pending:
                if self._completed(message):
                    return
            self._pending.clear()
            deadline = time.monotonic() + LOGIN_SECONDS
            while True:
                message = self._next(pipe, deadline)
                if message is not None and self._completed(message):
                    return
        except _Cancelled:
            self._finish("cancelled", "cancelled")
        except BrowserLoginError as error:
            self._finish("error", error.code)
        except Exception:
            # No raw platform/RPC exception is safe to render: it can contain
            # paths, auth URLs, or a server's token-bearing error text.
            self._finish("error", "startup_failed")
        finally:
            result = self.poll()
            if pipe is not None and (result is None or result["state"] != "success"):
                self._cancel_rpc(pipe)
            if process is not None:
                _cleanup(process)
            if pipe is not None:
                pipe.thread.join(timeout=0.2)
            with self._lock:
                self._process = None
                self._pending.clear()
