"""Owned, account-scoped native inference for explicitly requested Model Lab runs.

There is no login, Radio binding, shared server, session resume, or model fallback.
OpenCode uses its native stateless generate API (not an agent loop). Provider
errors are deliberately not echoed: native diagnostics may contain credentials.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 32000
MAX_PROMPT = 64000
MAX_TIMEOUT = 180
MAX_DIAGNOSTIC_BYTES = 32768
_VERSION = re.compile(r"(?:codex-cli\s+|opencode\s+v)?(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?)\s*\Z")
ERROR_MESSAGES = {
    "provider_error": "The provider could not complete this case.",
    "cancelled": "Run cancelled; the owned provider process was stopped.",
    "timeout": "The case exceeded its time limit; no automatic retry was made.",
    "invalid_input": "Check the explicit model, prompt and time limit.",
    "authorization": "Provider authorization was rejected. Check this account in Accounts.",
    "rate_limit": "The provider rate limit was reached; no automatic retry was made.",
    "model_unavailable": "The selected model is unavailable in this account or generation context.",
    "opencode_free_tier": "OpenCode restricts this free model to its own session flow; Model Lab's stateless requests are rejected. Select an API-enabled model.",
    "unsupported_protocol": "This provider CLI does not support the required Model Lab protocol.",
    "configuration_error": "Codex rejected Model Lab's startup configuration. Update Watchtower and reopen Model Lab.",
    "output_limit": "The provider response exceeded the bounded output limit.",
    "tool_attempt": "The provider attempted a tool action; the case was rejected.",
    "model_mismatch": "The provider reported a different model; the case was rejected.",
    "ownership_failed": "The provider process could not be safely contained. No inference was started.",
    "no_response": "The provider returned no assistant text.",
}
NOTICE_MESSAGES = {
    "codex_experimental_isolation": "Codex reports that the skill-discovery isolation flag is experimental.",
    "codex_code_mode_disabled": "Code Mode is disabled for this answer-only benchmark.",
}
_ISOLATION_NOTICE = re.compile(re.escape(
    "Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete and may behave unpredictably. "
    "To suppress this warning, set `suppress_unstable_features_warning = true` in ") + r"[^\r\n]{1,2048}config\.toml\.")
_CODE_MODE_NOTICE = (
    "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
    "enable `features.code_mode_host` and install `codex-code-mode-host`.")
_CODEX_TOOL_ITEMS = frozenset(("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list"))


def _codex_notice(message):
    # Codex 0.157.1 wraps these startup warnings in item.error. Never expose
    # their raw text (the first includes CODEX_HOME), or allow arbitrary errors.
    if isinstance(message, str):
        if _ISOLATION_NOTICE.fullmatch(message):
            return "codex_experimental_isolation"
        if message == _CODE_MODE_NOTICE:
            return "codex_code_mode_disabled"
    return None


def _accounts_module():
    name = "_watchtower_model_lab_accounts"
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(Path(__file__).resolve().parent.parent / "accounts")]
        sys.modules[name] = package
    full_name = name + ".backend"
    if full_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(full_name, Path(sys.modules[name].__path__[0]) / "backend.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[full_name] = module
        spec.loader.exec_module(module)
    return sys.modules[full_name]


class _Failure(Exception):
    def __init__(self, status, message, code="provider_error"):
        self.status, self.message = status, message
        self.code = status if status in ("cancelled", "timeout") else code if code in ERROR_MESSAGES else "provider_error"


class _WindowsLease:
    """A non-inheritable kernel Job, closed by Windows even after UI termination."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                        ("Flags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                        ("MaxWorkingSet", ctypes.c_size_t), ("ActiveLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD), ("Scheduling", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", ctypes.c_size_t),
                        ("JobMemory", ctypes.c_size_t), ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        # NULL security attributes produce a handle children cannot inherit.
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError("Unable to create owned Job")
        limits = Extended()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError("Unable to configure owned Job")

    def attach(self, process):
        from ctypes import wintypes
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise OSError("Unable to contain owned process")

    def resume(self, process):
        # Popen closes CreateProcess's primary-thread handle. Find the one
        # thread of our still-suspended child, never another process's thread.
        import ctypes
        from ctypes import wintypes

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
                        ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

        kernel = self.kernel
        kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
        kernel.Thread32First.restype = wintypes.BOOL
        kernel.Thread32Next.argtypes = kernel.Thread32First.argtypes
        kernel.Thread32Next.restype = wintypes.BOOL
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel.ResumeThread.restype = wintypes.DWORD
        snapshot = kernel.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD, read-only.
        if snapshot == ctypes.c_void_p(-1).value:
            raise OSError("Unable to locate owned primary thread")
        try:
            entry = ThreadEntry()
            entry.dwSize = ctypes.sizeof(entry)
            found = []
            more = kernel.Thread32First(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32OwnerProcessID == process.pid:
                    found.append(entry.th32ThreadID)
                entry.dwSize = ctypes.sizeof(entry)
                more = kernel.Thread32Next(snapshot, ctypes.byref(entry))
            if len(found) != 1:
                raise OSError("Unable to verify owned primary thread")
            thread = kernel.OpenThread(0x2, False, found[0])  # THREAD_SUSPEND_RESUME only.
            if not thread:
                raise OSError("Unable to open owned primary thread")
            try:
                if kernel.ResumeThread(thread) != 1:
                    raise OSError("Unable to resume owned primary thread")
            finally:
                kernel.CloseHandle(thread)
        finally:
            kernel.CloseHandle(snapshot)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _new_lease():
    return _WindowsLease() if os.name == "nt" else None


def _spawn(argv, options, cleanup):
    """Contain before executing even a Node shim, let alone sending a prompt."""
    process, lease = None, None
    try:
        lease = _new_lease()
        options = dict(options)
        if lease is not None:
            options["creationflags"] = options.get("creationflags", 0) | 0x4  # CREATE_SUSPENDED
        process = subprocess.Popen(argv, **options)
        if lease is not None:
            lease.attach(process)
            process._watchtower_model_lab_lease = lease
            lease.resume(process)
        return process
    except Exception:
        if lease is not None:
            lease.close()
        if process is not None:
            cleanup(process)
        raise _Failure("error", ERROR_MESSAGES["ownership_failed"], "ownership_failed") from None


def _finish(process, cleanup):
    # Close first: an exited wrapper may leave a live grandchild holding stdout.
    # Closing our Job stops that family before the stream reader is joined.
    lease = getattr(process, "_watchtower_model_lab_lease", None)
    try:
        if lease is not None:
            lease.close()
    finally:
        cleanup(process)


def _check(deadline, cancel):
    if cancel.is_set():
        raise _Failure("cancelled", "Run cancelled; the owned provider process was stopped.")
    if time.monotonic() >= deadline:
        raise _Failure("timeout", "The case exceeded its time limit; no automatic retry was made.")


def _options(env, cwd):
    options = dict(env=env, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    return options


def _lines(stream, output):
    consumed = 0
    try:
        while True:
            line = stream.readline(MAX_BYTES + 1)
            if not line:
                break
            consumed += len(line)
            if consumed > MAX_BYTES:
                output.put(_Failure("error", "Provider output exceeded the bounded response limit.", "output_limit"))
                return
            output.put(line)
    except (OSError, ValueError):
        pass
    finally:
        output.put(None)


def _input(process, prompt):
    try:
        process.stdin.write(prompt.encode("utf-8"))
        process.stdin.close()
    except (OSError, ValueError):
        pass


class _Diagnostics:
    """Drain stderr without blocking the child; retain only a bounded prefix.

    Raw diagnostics stay in memory, never in results, reports, logs or errors.
    Only fixed classifications derived from this buffer may leave the adapter.
    """
    def __init__(self):
        self.data = bytearray()
        self.lock = threading.Lock()

    def read(self, stream):
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                with self.lock:
                    remaining = MAX_DIAGNOSTIC_BYTES-len(self.data)
                    if remaining > 0:
                        self.data.extend(chunk[:remaining])
                # Excess data is drained and discarded, not accumulated.
        except (OSError, ValueError):
            pass

    def code(self):
        with self.lock:
            return _diagnostic_code(bytes(self.data))


def _diagnostic_code(raw):
    if isinstance(raw, bytes):
        text = raw[:MAX_DIAGNOSTIC_BYTES].decode("utf-8", errors="replace").lower()
    else:
        text = str(raw)[:MAX_DIAGNOSTIC_BYTES].lower()
    if any(term in text for term in ("error loading config", "unknown configuration field", "invalid configuration")):
        return "configuration_error"
    if any(term in text for term in ("unexpected argument", "unrecognized option", "unknown option")):
        return "unsupported_protocol"
    if any(term in text for term in ("401 unauthorized", "authentication failed", "invalid api key", "not logged in",
                                    "login required", "please run codex login", "refresh token has expired")):
        return "authorization"
    if any(term in text for term in ("rate limit", "rate_limit", "too many requests", "usage limit")):
        return "rate_limit"
    if "model" in text and any(term in text for term in ("not supported", "does not exist", "not available", "not found", "do not have access")):
        return "model_unavailable"
    return "provider_error"


def _native_failure(diagnostics, event=None):
    code = diagnostics.code()
    if code == "provider_error" and event is not None:
        code = _diagnostic_code(json.dumps(event, ensure_ascii=False))
    return _Failure("error", ERROR_MESSAGES[code], code)


def _receive(output, process, deadline, cancel):
    while True:
        _check(deadline, cancel)
        try:
            value = output.get(timeout=min(.05, max(.001, deadline - time.monotonic())))
        except queue.Empty:
            continue
        if isinstance(value, _Failure):
            raise value
        return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise _Failure("error", "The private provider endpoint returned an unexpected redirect.")


def _http(url, password, method, body, timeout):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    auth = base64.b64encode(("opencode:" + password).encode()).decode("ascii")
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
        headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        # Bounded native details stay in memory; only fixed classifications leave.
        try:
            details = error.read(MAX_DIAGNOSTIC_BYTES + 1)
        except (OSError, ValueError):
            details = b""
        finally:
            error.close()
        code = _opencode_http_error(error.code, details)
        raise _Failure("error", ERROR_MESSAGES[code], code) from None
    if len(raw) > MAX_BYTES:
        raise _Failure("error", "Provider output exceeded the bounded response limit.", "output_limit")
    return json.loads(raw)


def _opencode_http_error(status, raw):
    if status in (401, 403):
        return "authorization"
    if status == 429:
        return "rate_limit"
    if len(raw) > MAX_DIAGNOSTIC_BYTES:
        return "provider_error"
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return "provider_error"
    if not isinstance(data, dict) or data.get("_tag") not in ("InvalidRequestError", "ServiceUnavailableError"):
        return "provider_error"
    message = data.get("message")
    if not isinstance(message, str):
        return "provider_error"
    if message == "OpenCode's free tier can only be used from within OpenCode":
        return "opencode_free_tier"
    if message == "Generation credentials are unavailable":
        return "authorization"
    if message.startswith("Model unavailable: "):
        return "model_unavailable"
    if data.get("_tag") == "InvalidRequestError" and data.get("kind") in ("Query", "Payload"):
        return "unsupported_protocol"
    return _diagnostic_code(message)


def _request(url, password, method, body, deadline, cancel):
    """Exactly one HTTP request; owner remains responsive to cancellation."""
    _check(deadline, cancel)
    result = queue.Queue(maxsize=1)
    def send():
        try:
            result.put((True, _http(url, password, method, body, max(.1, deadline-time.monotonic()))))
        except Exception as error:
            result.put((False, error))
    worker = threading.Thread(target=send, daemon=True)
    worker.start()
    while True:
        _check(deadline, cancel)
        try:
            success, value = result.get(timeout=.05)
            if success:
                return value
            if isinstance(value, _Failure):
                raise value
            raise _Failure("error", "The native provider request failed; no fallback or retry was made.") from None
        except queue.Empty:
            pass


def _plain_text(value):
    if not isinstance(value, str) or not value.strip():
        raise _Failure("error", ERROR_MESSAGES["no_response"], "no_response")
    if len(value) > MAX_TEXT:
        raise _Failure("error", "Provider text exceeded the bounded response limit.", "output_limit")
    return value


def _number(value):
    return value if type(value) in (float, int) and math.isfinite(value) and value >= 0 else None


class ProviderRunner:
    def __init__(self, accounts=None):
        self.backend = _accounts_module()
        self.accounts = accounts if accounts is not None else self.backend.AccountService()

    def profiles(self):
        state = self.accounts.list()
        return {"profiles": [{"id": p["id"], "label": p.get("label") or p["id"], "provider": p["provider"]}
                for p in state["profiles"] if p.get("provider") in ("codex", "opencode")
                and not p.get("configuration_error") and not p.get("custom_environment")],
                "default_profile": state.get("defaults", {}).get("default_profile")}

    def models(self, profile_id):
        result = self.accounts.models(profile_id)
        return {key: result[key] for key in ("provider", "models", "default_model", "source", "notice") if key in result}

    def run(self, profile_id, model, prompt, timeout_seconds, cancel_event):
        began = time.monotonic()
        result = dict(text="", elapsed_ms=0, usage={"input_tokens": None, "output_tokens": None, "cost_usd": None},
                      provider=None, model=model, observed_model=None, provider_version=None,
                      adapter=None, conditions={}, status="error", error=None, error_code=None, notice_codes=[])
        try:
            if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                    or not 1 <= timeout_seconds <= MAX_TIMEOUT):
                raise _Failure("error", "Choose a case timeout between 1 and 180 seconds.", "invalid_input")
            deadline = began + timeout_seconds
            _check(deadline, cancel_event)
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT or "\0" in prompt:
                raise _Failure("error", "The benchmark prompt is empty or exceeds its input limit.", "invalid_input")
            profile = self.accounts.get(profile_id)
            provider = profile["provider"]
            result["provider"] = provider
            catalogs = __import__(self.backend.__package__ + ".models", fromlist=["valid_model"])
            if provider not in ("codex", "opencode") or not model or not catalogs.valid_model(provider, model):
                raise _Failure("error", "Select an explicit valid Codex or OpenCode model.", "invalid_input")
            env = self.backend.plan_environment(self.accounts._plan(profile, "benchmark", []))
            command = self.backend.provider_command(provider)
            with tempfile.TemporaryDirectory(prefix="watchtower-model-lab-") as cwd:
                env = self._neutral_env(env, provider, cwd)
                result["provider_version"] = self._version(command, env, cwd, deadline, cancel_event)
                if provider == "opencode":
                    self._opencode(result, command, env, cwd, prompt, deadline, cancel_event)
                else:
                    self._codex(result, command, env, cwd, prompt, deadline, cancel_event)
            result["status"] = "ok"
        except _Failure as error:
            result.update(status=error.status, error=error.message, error_code=error.code, text="")
        except Exception:
            result.update(status="error", text="", error_code="provider_error",
                          error="The provider could not run this case. Check the selected account and CLI installation.")
        result["elapsed_ms"] = round((time.monotonic() - began) * 1000)
        return result

    @staticmethod
    def _neutral_env(environment, provider, cwd):
        env = {key: value for key, value in environment.items()
               if not key.upper().startswith(("HERDR_", "RADIO_", "WATCHTOWER_", "CODEX_THREAD_", "CODEX_INTERNAL_"))}
        if provider == "opencode":
            # Native SQLite auth remains in the selected XDG_DATA_HOME. Neither
            # credentials nor provider files are copied to this temporary home.
            for key in list(env):
                if key.upper().startswith("OPENCODE_"):
                    env.pop(key)
            config = str(Path(cwd) / "config")
            Path(config).mkdir()
            env.update(OPENCODE_CONFIG_DIR=config, OPENCODE_CONFIG_PROJECT_DISABLE="1",
                       OPENCODE_FILEWATCHER_DISABLE="1")
        else:
            for key in list(env):
                if key.upper().startswith("CODEX_") and key.upper() != "CODEX_HOME":
                    env.pop(key)
        return env

    def _version(self, command, env, cwd, deadline, cancel):
        process = _spawn([*command, "--version"], _options(env, cwd), self.backend._cleanup)
        output = queue.Queue()
        reader = threading.Thread(target=_lines, args=(process.stdout, output), daemon=True)
        reader.start()
        try:
            line = _receive(output, process, min(deadline, time.monotonic()+5), cancel)
            if not line:
                raise _Failure("error", "The provider CLI version could not be verified.", "unsupported_protocol")
            match = _VERSION.fullmatch(line.decode("utf-8", errors="replace").strip())
            if not match:
                raise _Failure("error", "The provider CLI version could not be verified.", "unsupported_protocol")
            return match.group(1)
        finally:
            _finish(process, self.backend._cleanup)
            reader.join(timeout=.2)

    def _opencode(self, result, command, env, cwd, prompt, deadline, cancel):
        result.update(adapter="opencode-native-stateless", conditions={
            "execution_kind": "stateless", "tool_policy": "no-tools",
            "context": "fresh stateless native generation; no session or agent loop",
            "tools": "none; native generate endpoint supplies no tools",
            "configuration": "neutral config directory and cwd; selected native auth data retained",
            "sampling": "native provider defaults; temperature, seed and output-token limit not exposed",
            "usage": "native endpoint does not expose token usage or billed cost",
            "adapter_retries": 0, "provider_retries": "native behavior; not exposed",
            "model_fallback": False})
        password = secrets.token_urlsafe(32)
        env = dict(env, OPENCODE_PASSWORD=password, OPENCODE_SERVER_PASSWORD=password)
        process = _spawn([*command, "serve", "--stdio", "--hostname", "127.0.0.1", "--port", "0"],
                         _options(env, cwd), self.backend._cleanup)
        catalogs = __import__(self.backend.__package__ + ".models", fromlist=["_read_endpoint"])
        ready = queue.Queue(maxsize=1)
        reader = threading.Thread(target=catalogs._read_endpoint, args=(process.stdout, ready), daemon=True)
        reader.start()
        try:
            url = _receive(ready, process, min(deadline, time.monotonic()+10), cancel)
            if not url:
                raise _Failure("error", "OpenCode did not provide an owned private endpoint.")
            # Verify the installed native contract before dispatching the only
            # generation request. Unknown/older endpoints fail closed.
            schema = _request(url+"/openapi.json", password, "GET", None, deadline, cancel)
            route = schema.get("paths", {}).get("/api/experimental/generate", {}).get("post", {})
            if route.get("operationId") != "experimental.generate.text":
                raise _Failure("error", "This OpenCode CLI lacks the stateless Model Lab endpoint. Update OpenCode.", "unsupported_protocol")
            self._settle_opencode(url, password, result["model"], deadline, cancel,
                                  directory=env["OPENCODE_CONFIG_DIR"])
            provider, model = result["model"].split("/", 1)
            response = _request(url+"/api/experimental/generate", password, "POST",
                {"prompt": prompt, "model": {"providerID": provider, "id": model}}, deadline, cancel)
            result["text"] = _plain_text(response.get("data", {}).get("text"))
            # The endpoint does not return an observed model; do not invent one.
        finally:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            _finish(process, self.backend._cleanup)
            reader.join(timeout=.2)

    @staticmethod
    def _settle_opencode(url, password, selected, deadline, cancel, *, directory=None):
        """Only read the native catalog; never retry the generation itself."""
        # Native stateless generation uses global.config, not the server cwd.
        # Warm that same Location's plugins/catalog before the single POST.
        catalog_url = url + "/api/model"
        if directory is not None:
            catalog_url += "?" + urllib.parse.urlencode({"location[directory]": directory})
        began = time.monotonic()
        finish = min(deadline, began+8)
        previous, stable_since = None, began
        while True:
            payload = _request(catalog_url, password, "GET", None, deadline, cancel)
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise _Failure("error", "OpenCode returned an unsupported model catalog.", "unsupported_protocol")
            ids = tuple(sorted({row["providerID"]+"/"+row["id"] for row in rows[:4096]
                                if isinstance(row, dict) and row.get("enabled") is True
                                and isinstance(row.get("providerID"), str) and isinstance(row.get("id"), str)}))
            now = time.monotonic()
            if ids != previous:
                previous, stable_since = ids, now
            if (ids and now-began >= 2 and now-stable_since >= .75) or now >= finish:
                if selected not in ids:
                    raise _Failure("error", "This model is not enabled in the account's settled native catalog. No inference was sent.", "model_unavailable")
                return
            _check(deadline, cancel)
            cancel.wait(.1)

    def _codex(self, result, command, env, cwd, prompt, deadline, cancel):
        result.update(adapter="codex-exec-json", conditions={
            "execution_kind": "cli-agent",
            "tool_policy": "environment-disabled-tool-events-rejected",
            "context": "fresh ephemeral CLI agent thread; no resume",
            "configuration": "user config and exec rules ignored; project documents disabled; neutral cwd",
            "tools": "environment tools disabled; any emitted tool item rejects the case",
            "limitations": "native planning/patch tool schemas may remain advertised; read-only sandbox retained",
            "sampling": "native model defaults; temperature, seed and output-token limit not exposed",
            "usage": "CLI-reported token usage; billed cost unavailable",
            "adapter_retries": 0, "provider_retries": "native behavior; not exposed",
            "model_fallback": False})
        # --ignore-user-config retains the selected CODEX_HOME auth without
        # importing personal MCP, prompts, plugins or notification commands.
        disabled = ("shell_tool", "unified_exec", "shell_snapshot", "apps", "plugins", "remote_plugin",
                    "browser_use", "browser_use_external", "computer_use", "image_generation", "view_image",
                    "multi_agent", "multi_agent_v2", "code_mode", "code_mode_host", "hooks", "goals",
                    "skill_search", "skill_mcp_dependency_install", "memories", "workspace_dependencies",
                    "in_app_local_automation", "realtime_conversation", "sleep_tool")
        argv = [*command, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--sandbox", "read-only", "--color", "never", "--strict-config",
                "--model", result["model"], "--cd", cwd]
        settings = ["approval_policy=\"never\"", "project_doc_max_bytes=0", "web_search=\"disabled\"",
                    "features.skip_host_skill_discovery=true", "notify=[]",
                    "mcp_servers={}", "check_for_update_on_startup=false", "memories.generate_memories=false",
                    "developer_instructions=\"Answer the supplied benchmark directly. Return only the requested answer. Do not use tools, read files, run commands, or delegate.\""]
        settings += ["features." + name + "=false" for name in disabled]
        for value in settings:
            argv.extend(["-c", value])
        argv.append("-")
        options = _options(env, cwd)
        options["stderr"] = subprocess.PIPE
        process = _spawn(argv, options, self.backend._cleanup)
        output = queue.Queue()
        diagnostics = _Diagnostics()
        errors = threading.Thread(target=diagnostics.read, args=(process.stderr,), daemon=True)
        errors.start()
        reader = threading.Thread(target=_lines, args=(process.stdout, output), daemon=True)
        reader.start()
        writer = threading.Thread(target=_input, args=(process, prompt), daemon=True)
        writer.start()
        complete, answer, turn_started = False, None, False
        try:
            while True:
                line = _receive(output, process, deadline, cancel)
                if line is None:
                    break
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    raise _Failure("error", "Codex returned an unsupported event stream.", "unsupported_protocol") from None
                if not isinstance(event, dict):
                    raise _Failure("error", "Codex returned an unsupported event stream.", "unsupported_protocol")
                kind = event.get("type")
                if kind in ("error", "turn.failed"):
                    raise _native_failure(diagnostics, event)
                if kind in ("item.started", "item.updated", "item.completed"):
                    item = event.get("item", {})
                    if not isinstance(item, dict):
                        raise _Failure("error", ERROR_MESSAGES["unsupported_protocol"], "unsupported_protocol")
                    item_type = item.get("type")
                    if item_type == "error":
                        notice = _codex_notice(item.get("message")) if kind == "item.completed" and not turn_started else None
                        if notice is None:
                            raise _native_failure(diagnostics, item)
                        notices = result.setdefault("notice_codes", [])
                        if notice not in notices:
                            notices.append(notice)
                    elif item_type in _CODEX_TOOL_ITEMS:
                        raise _Failure("error", "Codex attempted a tool action. This case was rejected and its process stopped.", "tool_attempt")
                    elif item_type not in ("agent_message", "reasoning"):
                        raise _Failure("error", ERROR_MESSAGES["unsupported_protocol"], "unsupported_protocol")
                    if kind == "item.completed" and item_type == "agent_message":
                        answer = item.get("text")
                elif kind == "turn.started":
                    turn_started = True
                elif kind == "turn.completed":
                    usage = event.get("usage") or {}
                    result["usage"].update(input_tokens=_number(usage.get("input_tokens")),
                                           output_tokens=_number(usage.get("output_tokens")))
                    complete = True
                elif kind not in ("thread.started", "turn.started"):
                    raise _Failure("error", "Codex returned an unsupported event type; the case was rejected.", "unsupported_protocol")
                if isinstance(event.get("model"), str):
                    if event["model"] != result["model"]:
                        raise _Failure("error", "The provider reported a different model; this case was rejected.", "model_mismatch")
                    result["observed_model"] = event["model"]
            _check(deadline, cancel)
            # A complete event is necessary but not sufficient: a later native
            # failure must not be recorded as a successful benchmark case.
            try:
                returncode = process.wait(timeout=max(.001, min(1, deadline-time.monotonic())))
            except subprocess.TimeoutExpired:
                raise _Failure("error", "Codex did not finish cleanly after its response.") from None
            errors.join(timeout=.2)
            if returncode or not complete:
                raise _native_failure(diagnostics)
            result["text"] = _plain_text(answer)
        finally:
            _finish(process, self.backend._cleanup)
            reader.join(timeout=.2)
            writer.join(timeout=.2)
            errors.join(timeout=.2)
            if process.stderr is not None:
                process.stderr.close()
