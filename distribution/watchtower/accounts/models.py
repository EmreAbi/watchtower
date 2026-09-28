"""Bounded, account-scoped model catalogs. Never performs model inference.

OpenCode 2's one-shot models command may return before provider settlement. Use
its native stdio-owned server lease, wait briefly for the available snapshot,
then close that exact child. Nothing attaches to the shared background service.
"""
from __future__ import annotations

import base64
from concurrent.futures import Future
from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

MAX_BYTES = 4 * 1024 * 1024
MAX_MODELS = 512
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
STARTUP_TIMEOUT = 8
SETTLE_TIMEOUT = 8
STABLE_WINDOW = .75
MIN_SETTLE = 2


def valid_model(provider, value):
    if value is None:
        return True
    if not isinstance(value, str) or not MODEL_RE.fullmatch(value):
        return False
    if provider == "codex":
        return "/" not in value
    if provider == "opencode":
        prefix, separator, model = value.partition("/")
        return bool(separator and model and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", prefix))
    return False


def _label(value, fallback):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 128
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return fallback
    return value.strip()


def unavailable(provider, notice=None):
    return dict(provider=provider, models=[], default_model=None, source="unavailable",
                notice=notice or "The model list is unavailable. Provider default remains available.")


def parse_codex(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("Invalid model cache")
    found = {}
    for row in payload["models"][:4096]:
        if not isinstance(row, dict) or row.get("visibility") not in (None, "list"):
            continue
        identifier = row.get("slug")
        if not valid_model("codex", identifier) or identifier is None:
            continue
        found.setdefault(identifier, dict(id=identifier, label=_label(row.get("display_name"), identifier)))
        if len(found) == MAX_MODELS:
            break
    return dict(provider="codex", models=list(found.values()), default_model=None, source="local_cache",
                notice="Cached models for this Codex profile; availability is checked by Codex at launch.")


def codex_catalog(profile):
    path = Path(profile["home"]) / "models_cache.json"
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Model cache too large")
        return parse_codex(json.loads(raw))
    except (OSError, ValueError, TypeError):
        return unavailable("codex", "No usable model cache for this Codex profile. Provider default remains available.")


def parse_opencode(payload, default=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Invalid OpenCode model snapshot")
    found = {}
    for row in payload["data"][:4096]:
        if not isinstance(row, dict) or row.get("enabled") is not True:
            continue
        provider, name = row.get("providerID"), row.get("id")
        if not isinstance(provider, str) or not isinstance(name, str):
            continue
        identifier = provider + "/" + name
        if not valid_model("opencode", identifier):
            continue
        label = _label(row.get("name"), name)
        found.setdefault(identifier, dict(id=identifier, label=label + " · " + provider))
        if len(found) == MAX_MODELS:
            break
    default_id = None
    if isinstance(default, dict) and isinstance(default.get("data"), dict):
        row = default["data"]
        if isinstance(row.get("providerID"), str) and isinstance(row.get("id"), str):
            candidate = row["providerID"] + "/" + row["id"]
            if candidate in found:
                default_id = candidate
    return dict(provider="opencode", models=list(found.values()), default_model=default_id,
                source="native_catalog", notice="Available models for this account outside project overrides. Listing does not verify credit or paid entitlement.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("Unexpected catalog redirect")


def _json_request(url, password):
    # Proxy settings and redirects must never forward this one-use local secret.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    credentials = base64.b64encode(("opencode:" + password).encode()).decode("ascii")
    request = urllib.request.Request(url, headers={"Authorization": "Basic " + credentials})
    with opener.open(request, timeout=2) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Model catalog too large")
    return json.loads(raw)


def _read_endpoint(stream, ready):
    consumed = 0
    try:
        while consumed <= 65536:
            line = stream.readline(65537)
            if not line:
                break
            consumed += len(line)
            if consumed > 65536:
                break
            try:
                item = json.loads(line)
            except ValueError:
                continue
            url = item.get("url") if isinstance(item, dict) else None
            if isinstance(url, str):
                parsed = urllib.parse.urlsplit(url)
                if (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                        and parsed.port and not parsed.username and not parsed.password
                        and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
                    ready.put(url.rstrip("/"))
                    return
    except (OSError, ValueError):
        pass
    ready.put(None)


def opencode_catalog(command, environment, cleanup):
    """Read via one owned native server; all returned data is allowlisted."""
    process = None
    try:
        env = dict(environment)
        # The selected profile supplies XDG homes; transient secrets/config from
        # an existing OpenCode pane must not redirect this discovery process.
        blocked = ("OPENCODE_PASSWORD", "OPENCODE_SERVER_PASSWORD", "OPENCODE_PTY_HANDOFF",
                   "OPENCODE_MODELS_URL", "OPENCODE_MODELS_PATH", "OPENCODE_SIMULATE",
                   "OPENCODE_CONFIG_PROJECT_DISABLE", "OPENCODE_DISABLE_PROJECT_CONFIG")
        for key in list(env):
            if key.upper() in blocked:
                env.pop(key)
        password = secrets.token_urlsafe(32)
        env.update(OPENCODE_PASSWORD=password, OPENCODE_SERVER_PASSWORD=password,
                   OPENCODE_CONFIG_PROJECT_DISABLE="1", OPENCODE_FILEWATCHER_DISABLE="1")
        with tempfile.TemporaryDirectory(prefix="watchtower-models-") as directory:
            options = dict(env=env, cwd=directory, stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                options["start_new_session"] = True
            process = subprocess.Popen([*command, "serve", "--stdio", "--hostname", "127.0.0.1", "--port", "0"], **options)
            ready = queue.Queue(maxsize=1)
            reader = threading.Thread(target=_read_endpoint, args=(process.stdout, ready), daemon=True)
            reader.start()
            try:
                url = ready.get(timeout=STARTUP_TIMEOUT)
                if not url:
                    raise ValueError("No private server endpoint")
                began = time.monotonic()
                deadline = began + SETTLE_TIMEOUT
                stable_since, previous = None, None
                while True:
                    if process.poll() is not None:
                        raise ValueError("Catalog server exited")
                    payload = _json_request(url + "/api/model", password)
                    result = parse_opencode(payload)
                    current = tuple((item["id"], item["label"]) for item in result["models"])
                    now = time.monotonic()
                    if current != previous:
                        previous, stable_since = current, now
                    # OpenCode snapshots explicitly may precede provider-plugin
                    # settlement. A short stable window avoids showing just the
                    # first provider that completed; the overall wait is bounded.
                    if current and ((now - stable_since >= STABLE_WINDOW and now - began >= MIN_SETTLE) or now >= deadline):
                        try:
                            return parse_opencode(payload, _json_request(url + "/api/model/default", password))
                        except (OSError, ValueError):
                            return result
                    if now >= deadline:
                        result["notice"] = "OpenCode returned no enabled models for this account. Check its provider connection; Provider default remains available."
                        return result
                    time.sleep(.25)
            finally:
                # Stdio is an ownership lease: EOF normally shuts down the
                # native server. Cleanup handles only this retained child.
                try:
                    process.stdin.close()
                    process.wait(timeout=2)
                except (OSError, subprocess.SubprocessError):
                    pass
                cleanup(process)
                reader.join(timeout=.5)
                process = None
    except (OSError, ValueError, TypeError, queue.Empty, subprocess.SubprocessError):
        return unavailable("opencode")
    finally:
        if process is not None:
            cleanup(process)


class CatalogCache:
    """Share one bounded discovery between roles selecting the same account."""
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def read(self, key, loader):
        with self.lock:
            now = time.monotonic()
            entry = self.entries.get(key)
            if entry and (not entry[1].done() or entry[0] > now):
                future, owner = entry[1], False
            else:
                future, owner = Future(), True
                self.entries[key] = (now + 60, future)
                if len(self.entries) > 64:
                    old = next((name for name, (_, item) in self.entries.items()
                                if name != key and item.done()), None)
                    if old is not None:
                        self.entries.pop(old)
        if owner:
            try:
                future.set_result(loader())
            except Exception as error:
                future.set_exception(error)
                with self.lock:
                    self.entries.pop(key, None)
                raise
        return deepcopy(future.result(timeout=30))


CACHE = CatalogCache()
