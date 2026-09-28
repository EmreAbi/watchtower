"""Explicit, machine-wide Codex updates; account homes and running agents stay intact.

The native Windows layout, release metadata and -Release option follow the
official Codex install.ps1. Opening this module never probes or updates anything.
All subprocess output is private and only parsed version strings reach the UI.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading

try:
    from .account_reader import _cleanup
    from .backend import UNSET_ENV, provider_command
except ImportError:
    from account_reader import _cleanup
    from backend import UNSET_ENV, provider_command

RELEASE_URL = "https://releases.openai.com/codex/channels/latest"
INSTALLER_URL = "https://releases.openai.com/codex/install.ps1"
VERSION_SECONDS = 5
FETCH_SECONDS = 15
UPDATE_SECONDS = 180
MAX_DOWNLOAD = 2 * 1024 * 1024
_VERSION = re.compile(r"(?:rust-v|v)?([0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})\Z")
_CLI_VERSION = re.compile(r"codex-cli ([0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6})\s*\Z")

# A separate owned process gives DNS, TLS and slow response bodies one absolute
# deadline. -I/-S avoid user Python hooks; stderr is discarded and stdout capped.
_FETCH_HELPER = r'''
import sys
from urllib.request import HTTPRedirectHandler, Request, build_opener
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise RuntimeError("source_changed")
url = sys.argv[1]
if url not in ("https://releases.openai.com/codex/channels/latest", "https://releases.openai.com/codex/install.ps1"):
    raise RuntimeError("source_changed")
request = Request(url, headers={"User-Agent": "Watchtower-Codex-Update/1"})
with build_opener(NoRedirect()).open(request, timeout=15) as response:
    data = response.read(2097153)
if len(data) > 2097152:
    raise RuntimeError("source_too_large")
sys.stdout.buffer.write(data)
'''


class _Failure(Exception):
    pass


def _version(value):
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    return match.group(1) if match else None


def _numbers(value):
    return tuple(int(part) for part in value.split("."))


def _same(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def _environment(source):
    # A selected account's CODEX_HOME must never redirect a global installation.
    denied = {name.upper() for name in UNSET_ENV}
    denied.update(("NODE_OPTIONS", "PYTHONPATH", "PYTHONHOME", "GITHUB_TOKEN", "GH_TOKEN"))
    return {key: value for key, value in source.items()
            if key.upper() not in denied and not key.upper().startswith("CODEX_")}


def _fetch(url, timeout=FETCH_SECONDS):
    if url not in (RELEASE_URL, INSTALLER_URL):
        raise _Failure("source_changed")
    result = _run([sys.executable, "-I", "-S", "-c", _FETCH_HELPER, url],
                  env=_environment(os.environ), timeout=timeout, capture=True,
                  output_limit=MAX_DOWNLOAD)
    if result["returncode"] != 0:
        raise _Failure("source_unavailable")
    return result["stdout"]


def _run(argv, *, env, timeout, capture=False, output_limit=4096):
    """Hidden owned process, bounded cleanup; installer output is never retained."""
    options = dict(stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                   env=env, shell=False)
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(list(argv), **options)
    try:
        output, _ = process.communicate(timeout=timeout)
        if output and len(output) > output_limit:
            raise _Failure("invalid_output")
        return {"returncode": process.returncode, "stdout": output or b""}
    finally:
        _cleanup(process)


@contextmanager
def _install_lock(root):
    """OS lock is shared across Watchtower homes and released on process exit.

    Keep the empty lock file: unlinking it after unlock can split concurrent
    contenders across two inodes. Do not interfere with Codex's own install.lock.
    """
    path = root / "watchtower-update.lock"
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise _Failure("busy") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ToolUpdateService:
    """Synchronous worker API; the frontend runs calls outside its event loop.

    runner(argv, *, env, timeout, capture) -> {returncode, stdout};
    fetch(fixed_url, timeout=...) -> bytes; resolver('codex') -> native argv.
    Inject these dependencies in tests; no test needs a real install or network.
    """

    def __init__(self, home=None, *, runner=None, fetch=None, resolver=None,
                 environ=None, platform=None):
        self.home = home  # Accepted for UI consistency; never used for global lock scope.
        self._runner = runner or _run
        self._fetch = fetch or _fetch
        self._resolver = resolver or provider_command
        self._env = dict(os.environ if environ is None else environ)
        self._platform = platform or os.name
        self._latest = None
        self._checked_at = None
        self._operation = threading.Lock()

    def _result(self, state, message, installed=None, path=None, can_update=False):
        return dict(tool="codex", installed_version=installed, latest_version=self._latest,
                    installed_path=str(path) if path else None, state=state, message=message,
                    can_update=can_update, checked_at=self._checked_at)

    def _local(self):
        command = self._resolver("codex")
        if not isinstance(command, (list, tuple)) or not command:
            raise _Failure("missing")
        path = Path(command[0])
        result = self._runner([*command, "--version"], env=_environment(self._env),
                              timeout=VERSION_SECONDS, capture=True)
        output = result.get("stdout", b"")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        match = _CLI_VERSION.fullmatch(output.strip()) if isinstance(output, str) else None
        if result.get("returncode") != 0 or not match:
            raise _Failure("version")
        installed = match.group(1)
        if self._platform != "nt" or len(command) != 1 or path.name.lower() != "codex.exe":
            return installed, path, None
        profile = self._env.get("USERPROFILE")
        local = self._env.get("LOCALAPPDATA")
        if not profile or not local or not Path(profile).is_absolute() or not Path(local).is_absolute():
            return installed, path, None
        root = Path(profile) / ".codex" / "packages" / "standalone"
        visible = Path(local) / "Programs/OpenAI/Codex/bin/codex.exe"
        current = root / "current/bin/codex.exe"
        if (not visible.is_file() or not current.is_file() or not root.is_dir()
                or not _same(visible, current) or not _same(path, current)):
            return installed, path, None
        resolved = path.resolve()
        try:
            relative = resolved.relative_to((root / "releases").resolve())
        except ValueError:
            return installed, path, None
        # Only the official versioned package layout; no overwrite-in-place installs.
        if (len(relative.parts) != 3 or relative.parts[1:] != ("bin", "codex.exe")
                or not re.fullmatch(re.escape(installed) + r"-(?:x86_64|aarch64)-pc-windows-msvc", relative.parts[0])):
            return installed, path, None
        return installed, path, root

    def _read_latest(self):
        data = self._fetch(RELEASE_URL, timeout=FETCH_SECONDS)
        if not isinstance(data, bytes) or len(data) > MAX_DOWNLOAD:
            raise _Failure("invalid_release")
        value = json.loads(data)
        latest = _version(value.get("tag_name")) if isinstance(value, dict) else None
        if not latest or value.get("draft") is True or value.get("prerelease") is True:
            raise _Failure("invalid_release")
        self._latest = latest
        self._checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def _status(self, check_latest):
        try:
            installed, path, root = self._local()
        except Exception:
            return self._result("unavailable", "The installed Codex CLI version could not be read.")
        if check_latest:
            try:
                self._read_latest()
            except Exception:
                return self._result("error", "Could not check the official Codex release. Try again.", installed, path)
        if root is None:
            return self._result("unsupported", "Automatic update supports the official Windows native installation only. Update this installation with its original package manager.", installed, path)
        if self._latest is None:
            return self._result("unchecked", "Check for the latest official Codex release.", installed, path)
        if _numbers(installed) >= _numbers(self._latest):
            return self._result("current", "Codex is up to date. Running agents keep their current process.", installed, path)
        return self._result("update_available", "A Codex update is available. Install once for all accounts; running agents stay open.", installed, path, True)

    def status(self, check_latest=False):
        if not self._operation.acquire(blocking=False):
            return self._result("busy", "A Codex tool operation is already in progress.")
        try:
            return self._status(check_latest)
        finally:
            self._operation.release()

    def check(self):
        return self.status(check_latest=True)

    def update(self):
        if not self._operation.acquire(blocking=False):
            return self._result("busy", "A Codex tool operation is already in progress.")
        try:
            try:
                installed, path, root = self._local()
            except Exception:
                return self._result("unavailable", "The installed Codex CLI version could not be read.")
            if root is None:
                return self._result("unsupported", "Automatic update supports the official Windows native installation only. Update this installation with its original package manager.", installed, path)
            try:
                with _install_lock(root):
                    # Refresh inside the shared lock: another Accounts window may
                    # have installed the update since this modal last checked.
                    before = self._status(True)
                    if before["state"] != "update_available":
                        return before
                    target = self._latest
                    script = self._fetch(INSTALLER_URL, timeout=FETCH_SECONDS)
                    if not isinstance(script, bytes) or not 32 <= len(script) <= MAX_DOWNLOAD:
                        raise _Failure("installer")
                    shell = Path(self._env.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
                    if not shell.is_file():
                        raise _Failure("powershell")
                    env = _environment(self._env)
                    env.update(CODEX_HOME=str(root.parent.parent), CODEX_NON_INTERACTIVE="1",
                               CODEX_INSTALLER_USE_RELEASES_OPENAI_COM="1")
                    with tempfile.TemporaryDirectory(prefix="watchtower-codex-update-") as directory:
                        installer = Path(directory) / "install.ps1"
                        installer.write_bytes(script)
                        result = self._runner([str(shell), "-NoLogo", "-NoProfile", "-NonInteractive",
                                               "-ExecutionPolicy", "Bypass", "-File", str(installer),
                                               "-Release", target], env=env, timeout=UPDATE_SECONDS, capture=False)
                    if result.get("returncode") != 0:
                        raise _Failure("installer")
                    actual, updated_path, updated_root = self._local()
                    if actual != target or updated_root is None or not _same(updated_root, root):
                        raise _Failure("verification")
                    return self._result("updated", "Codex updated. New agents use this version; existing agents stay open until you restart them.", actual, updated_path)
            except _Failure as error:
                if str(error) == "busy":
                    return self._result("busy", "Another Accounts window is already updating this Codex installation.", installed, path)
                return self._result("error", "The Codex update could not be completed or verified. Check again before retrying; running agents were not restarted.", installed, path)
            except Exception:
                return self._result("error", "The Codex update could not be completed or verified. Check again before retrying; running agents were not restarted.", installed, path)
        finally:
            self._operation.release()
