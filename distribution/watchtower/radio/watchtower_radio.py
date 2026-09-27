#!/usr/bin/env python3
"""Private Watchtower adapter for the unmodified, pinned AgentRadio runtime.

No global launcher, PATH registry, provider login, or existing Radio state is
changed. The Watchtower host supplies RADIO_HOME and HERDR_BIN_PATH to children.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / "vendor" / "AgentRadio"
RADIO = UPSTREAM / "bin" / "radio"
PIN = "bf3d460cd66da9d65d7bb9650bd9dbe8bfb1d906"
VERSION = "0.7.1"


def verified_files(root: Path = ROOT) -> list[Path]:
    """Validate the exact allowlist used by the portable package builder."""
    manifest = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
    if manifest.get("commit") != PIN or manifest.get("version") != VERSION:
        raise ValueError("Radio provenance does not match the pinned release")
    files = []
    for name, expected in manifest["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError(f"Invalid bundled Radio path: {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Bundled Radio file changed: {name}")
        files.append(path)
    actual = {str(path.relative_to(root)).replace("\\", "/")
              for path in root.rglob("*") if path.is_file()}
    if actual != set(manifest["files"]) | {"provenance.json"}:
        raise ValueError("Unlisted files in the private Radio package")
    return files + [root / "provenance.json"]


def runtime_env(environ=None) -> dict[str, str]:
    """Fail closed outside a host that deliberately selected private state."""
    env = dict(os.environ if environ is None else environ)
    state = env.get("RADIO_HOME", "")
    if not state or not Path(state).is_absolute():
        raise ValueError("Run Radio inside Watchtower (an absolute RADIO_HOME is required)")
    if Path(state).resolve() == (Path.home() / ".local/share/herdr-radio").resolve():
        raise ValueError("Watchtower Radio must not use the existing Herdr Radio ledger")
    binary = Path(env.get("HERDR_BIN_PATH", ""))
    if not binary.is_absolute() or not binary.is_file() or binary.stem.lower() != "watchtower":
        raise ValueError("HERDR_BIN_PATH must select this distribution's watchtower executable")
    env["RADIO_HOME"] = str(Path(state).resolve())
    env["HERDR_BIN_PATH"] = str(binary.resolve())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PATH"] = str(ROOT / "bin") + os.pathsep + env.get("PATH", "")
    return env


def quiet_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def start_relay(env: dict[str, str]) -> int:
    """Detach a supervisor; only its own relay child is stopped with the host."""
    socket = env.get("HERDR_SOCKET_PATH", "")
    if not socket or not Path(socket).is_absolute():
        raise ValueError("The private relay requires an absolute Watchtower socket path")
    state = Path(env["RADIO_HOME"])
    state.mkdir(parents=True, exist_ok=True)
    flags = quiet_flags()
    if os.name == "nt":
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
    with (state / "relay.log").open("a", encoding="utf-8") as log:
        subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "watchtower_radio.py"), "supervise"],
            env=env, cwd=state, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=flags, start_new_session=os.name != "nt",
        )
    return 0


def host_running(env: dict[str, str]) -> bool | None:
    """Read-only status of the exact startup socket; None is a transient error."""
    try:
        result = subprocess.run(
            [env["HERDR_BIN_PATH"], "status", "server", "--json"],
            env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=4, creationflags=quiet_flags(),
        )
        if result.returncode:
            return None
        status = json.loads(result.stdout)
        expected = os.path.normcase(os.path.normpath(env["HERDR_SOCKET_PATH"]))
        actual = os.path.normcase(os.path.normpath(status.get("socket", "")))
        if actual != expected:
            return False
        return status.get("running") if type(status.get("running")) is bool else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def record_relay(env: dict[str, str], child) -> Path:
    """Expose owned process IDs for diagnostics; never use them to kill by PID."""
    path = Path(env["RADIO_HOME"]) / f"relay-runtime-{os.getpid()}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "supervisor_pid": os.getpid(), "relay_pid": child.pid,
        "socket": env["HERDR_SOCKET_PATH"], "executable": env["HERDR_BIN_PATH"],
    }) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def supervise_relay(env: dict[str, str]) -> int:
    """Watch the owning server; never enumerate or kill another Radio process."""
    # Startup can precede the first accepted ping by a few milliseconds.
    for attempt in range(3):
        if host_running(env) is True:
            break
        if attempt == 2:
            print("Watchtower Radio: host unavailable; relay not started", flush=True)
            return 1
        time.sleep(2)
    child = subprocess.Popen(
        [sys.executable, "-B", str(ROOT / "watchtower_radio.py"), "relay-child"],
        env=env, cwd=env["RADIO_HOME"], stdin=subprocess.DEVNULL,
        creationflags=quiet_flags(),
    )
    failures = 0
    record = None
    try:
        record = record_relay(env, child)
        while child.poll() is None:
            running = host_running(env)
            failures = failures + 1 if running is None else 0
            if running is False or failures >= 3:
                print("Watchtower Radio: host stopped or unavailable; stopping private relay", flush=True)
                break
            time.sleep(2)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if record is not None:
            record.unlink(missing_ok=True)
    # A repeated startup exits here as soon as the child's upstream lock rejects
    # it. The already-running relay and its own supervisor remain untouched.
    return 0


def relay_child() -> int:
    """Run the pinned upstream relay without its detached self-update handover.

    The private supervisor must own the one child for its entire lifetime.
    Portable bundle updates take effect on the next Watchtower server start.
    Upstream source stays unmodified and hash-verifiable on disk.
    """
    namespace = {"__file__": str(RADIO), "__name__": "watchtower_agent_radio"}
    exec(compile(RADIO.read_bytes(), str(RADIO), "exec"), namespace)
    namespace["script_changed"] = lambda *_args: False
    namespace["utf8_streams"]()
    return namespace["cmd_relay"](namespace["build_parser"]().parse_args(["relay"]))


def run_cli(args: list[str], env: dict[str, str]) -> int:
    return subprocess.run([sys.executable, "-B", str(RADIO), *args], env=env).returncode


def context_watch(env: dict[str, str]) -> int:
    """A dependency-free context pane; never queries provider account quotas."""
    try:
        while True:
            result = subprocess.run(
                [sys.executable, "-B", str(RADIO), "tools", "context", "--table"],
                env=env, capture_output=True, text=True, encoding="utf-8",
                errors="replace", creationflags=quiet_flags(),
            )
            print("\x1b[2J\x1b[H", end="")
            print("Watchtower / Agent context — refresh 30s; Ctrl+C to close\n")
            print(result.stdout if result.returncode == 0 else result.stderr, end="", flush=True)
            time.sleep(30)
    except KeyboardInterrupt:
        return 0


def install_view(env: dict[str, str]) -> int:
    """Explicit optional dependency setup in private state, never a startup hook."""
    venv = Path(env["RADIO_HOME"]) / "venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        result = subprocess.run([sys.executable, "-m", "venv", str(venv)], env=env)
        if result.returncode:
            return result.returncode
    return subprocess.run([str(python), "-m", "pip", "install", "textual>=1.0"], env=env).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "startup", "supervise", "relay-child", "cli", "context", "open-view", "install-view"))
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.action == "verify":
            print(f"AgentRadio {VERSION}: {len(verified_files())} verified files; commit {PIN}")
            return 0
        env = runtime_env()
        if args.action == "startup":
            verified_files()
            return start_relay(env)
        if args.action == "supervise":
            return supervise_relay(env)
        if args.action == "relay-child":
            return relay_child()
        if args.action == "context":
            return context_watch(env)
        if args.action == "install-view":
            return install_view(env)
        if args.action == "open-view":
            return subprocess.run(
                [env["HERDR_BIN_PATH"], "plugin", "pane", "open", "--plugin", "radio",
                 "--entrypoint", "view-win" if os.name == "nt" else "view"],
                env=env, creationflags=quiet_flags(),
            ).returncode
        return run_cli(args.args, env)
    except (OSError, ValueError) as exc:
        print(f"Watchtower Radio: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
