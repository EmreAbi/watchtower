#!/usr/bin/env python3
"""Exercise an extracted Watchtower preview in two disposable Windows homes.

No provider agents are launched. Each server belongs to a private Windows Job;
graceful shutdown is tested before the job is closed as a cleanup backstop.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


class SmokeFailure(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


class PrivateJob:
    """A kernel-owned process family, never a process-name or global PID kill."""

    def __init__(self) -> None:
        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("ProcessTime", ctypes.c_int64), ("JobTime", ctypes.c_int64),
                ("Flags", wintypes.DWORD), ("MinWorkingSet", ctypes.c_size_t),
                ("MaxWorkingSet", ctypes.c_size_t), ("ActiveLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("Priority", wintypes.DWORD),
                ("Scheduling", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes"
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("Basic", BasicLimits), ("IO", IoCounters),
                ("ProcessMemory", ctypes.c_size_t), ("JobMemory", ctypes.c_size_t),
                ("PeakProcessMemory", ctypes.c_size_t), ("PeakJobMemory", ctypes.c_size_t),
            ]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.SetInformationJobObject.restype = wintypes.BOOL
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        self.kernel.QueryInformationJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def attach(self, process: subprocess.Popen) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(process._handle))):
            raise ctypes.WinError(ctypes.get_last_error())

    def pids(self) -> set[int]:
        class ProcessIds(ctypes.Structure):
            _fields_ = [
                ("Assigned", wintypes.DWORD), ("Count", wintypes.DWORD),
                ("Ids", ctypes.c_size_t * 256),
            ]
        info = ProcessIds()
        if not self.kernel.QueryInformationJobObject(self.handle, 3, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        require(info.Count <= 256, "Unexpected smoke process count")
        return set(info.Ids[:info.Count])

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def wait_until(check, description: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (OSError, ValueError, SmokeFailure, subprocess.TimeoutExpired) as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise SmokeFailure(f"Timed out: {description}" + (f" ({last_error})" if last_error else ""))


def isolated_environment(home: Path, label: str) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(("HERDR_", "RADIO_", "WATCHTOWER_"))
    }
    env.update({
        "WATCHTOWER_HOME": str(home),
        "WATCHTOWER_SESSION": f"smoke-{label}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    })
    return env


class Instance:
    def __init__(self, binary: Path, home: Path, label: str, env: dict[str, str]):
        self.binary, self.home, self.label, self.env = binary, home, label, env
        self.process = None
        self.job = None
        self.log = None
        self.status_data = None
        self.relay = None
        self.pane = None
        home.mkdir()

    def run(self, *args: str, json_output=False, check=True, timeout=10):
        result = subprocess.run(
            [str(self.binary), *args], cwd=self.home, env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if check and result.returncode:
            raise SmokeFailure(f"{self.label}: {' '.join(args[:3])} failed ({result.returncode}): {result.stderr[-1800:]}")
        if json_output:
            payload = json.loads(result.stdout)
            if check and isinstance(payload, dict) and payload.get("error"):
                raise SmokeFailure(f"{self.label}: API error: {payload['error']}")
            return payload
        return result

    def start(self, plugin: Path) -> None:
        self.run("plugin", "link", str(plugin), json_output=True)
        require(not (self.home / "config/config.toml").exists(), "Plugin link unexpectedly replaced product defaults")
        self.run("config", "check")
        self.job = PrivateJob()
        self.log = (self.home / "server-output.log").open("wb")
        self.process = subprocess.Popen(
            [str(self.binary), "server"], env=self.env, cwd=self.home,
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        self.job.attach(self.process)

        def ready():
            require(self.process.poll() is None, f"Server {self.label} exited during startup")
            data = self.run("status", "server", "--json", json_output=True, timeout=4)
            if data.get("running") is True:
                require(Path(data["socket"]).resolve().is_relative_to(self.home), "Server socket escaped disposable home")
                require(data.get("session") == f"smoke-{self.label}", "Server selected another session")
                self.status_data = data
                return data
            return None
        wait_until(ready, f"server {self.label} ready")

    def create_pane(self) -> str:
        result = self.run("workspace", "create", "--cwd", str(self.home), "--label", f"SMOKE-{self.label}", json_output=True)
        self.pane = result["result"]["root_pane"]["pane_id"]
        return self.pane

    def snapshot(self):
        return self.run("api", "snapshot", json_output=True)["result"]["snapshot"]

    def wait_relay(self) -> dict:
        state = self.home / "state/radio"
        def ready():
            for path in state.rglob("relay-runtime-*.json"):
                data = json.loads(path.read_text(encoding="utf-8"))
                require(data["socket"] == self.status_data["socket"], "Radio supervisor is bound to another socket")
                pids = self.job.pids()
                require(
                    data["supervisor_pid"] in pids and data["relay_pid"] in pids,
                    "Radio processes are outside the owned server job: "
                    f"job_pids={sorted(pids)}, supervisor_pid={data['supervisor_pid']}, "
                    f"relay_pid={data['relay_pid']}, record={path}",
                )
                log = path.parent / "relay.log"
                if log.exists() and "radio relay" in log.read_text(encoding="utf-8", errors="replace").lower():
                    self.relay = {**data, "record": str(path)}
                    return self.relay
            return None
        return wait_until(ready, f"private Radio {self.label} relay startup")

    def wait_radio_exit(self, timeout: float = 20.0) -> None:
        """Check private runtime cleanup even if a launcher escaped the Job.

        This never signals a PID from a file. An escaped supervisor must notice
        its stopped host and exit itself; otherwise the smoke fails and retains
        its disposable home for diagnosis.
        """
        import msvcrt

        state = self.home / "state/radio"
        stable_checks = 0

        def quiet():
            nonlocal stable_checks
            records = list(state.rglob("relay-runtime-*.json"))
            if records:
                stable_checks = 0
                return False
            for lock in state.rglob("relay.lock"):
                try:
                    with lock.open("r+b") as handle:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    stable_checks = 0
                    return False
            stable_checks += 1
            return stable_checks >= 4

        wait_until(quiet, f"private Radio {self.label} records removed and locks released", timeout=timeout)

    def graceful_stop(self) -> None:
        require(self.process is not None and self.process.poll() is None, "Owned server was already stopped")
        self.run("server", "stop", timeout=15)
        self.process.wait(timeout=15)
        wait_until(lambda: not self.job.pids(), f"all owned {self.label} descendants exit", timeout=20)
        self.wait_radio_exit()

    def cleanup(self) -> list[str]:
        errors = []
        radio_waited = False
        if self.process is not None and self.process.poll() is None:
            try:
                self.run("server", "stop", check=False, timeout=5)
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.process is not None and self.process.poll() is not None:
            radio_waited = True
            try:
                # Give private supervisors their normal shutdown window before
                # closing the owned job or deleting a directory they may use.
                self.wait_radio_exit()
            except (OSError, SmokeFailure) as exc:
                errors.append(str(exc))
        if self.job is not None:
            self.job.close()  # Kills only this invocation's server family if needed.
        if self.process is not None and self.process.poll() is None:
            # Exact retained Popen handle, also covers an AssignProcessToJob failure.
            self.process.kill()
        if self.process is not None:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                errors.append(f"owned {self.label} server did not exit")
        if not radio_waited and self.process is not None and self.process.poll() is not None:
            try:
                self.wait_radio_exit()
            except (OSError, SmokeFailure) as exc:
                errors.append(str(exc))
        if self.log is not None:
            self.log.close()
        return errors


def smoke(binary: Path, temporary: Path) -> dict:
    report = {"ok": False, "checks": [], "instances": {}}
    instances = []
    try:
        require(os.name == "nt", "This smoke runs on Windows only")
        require(binary.name.lower() == "watchtower.exe" and binary.is_file(), "Select the packaged watchtower.exe")
        plugin = binary.parent / "radio"
        require((plugin / "provenance.json").is_file(), "Extract the complete ZIP with its private Radio plugin")
        a_home, b_home = temporary / "home-a", temporary / "home-b"
        a = Instance(binary, a_home, "a", isolated_environment(a_home, "a"))
        instances.append(a)
        version = a.run("--version").stdout.strip()
        require(version.startswith("watchtower 0.1.0-preview.1"), "The binary is not the expected Watchtower preview")
        report["version"] = version
        defaults = a.run("--default-config").stdout
        require('agent_panel_sort = "role"' in defaults and "$team_role" in defaults, "Role defaults missing")
        a.start(plugin)
        a.create_pane()
        a.wait_relay()
        a_config = a.home / "config/config.toml"
        a_config.write_text(defaults + "\n# disposable smoke A sentinel\n", encoding="utf-8")
        a.run("server", "reload-config", json_output=True)
        a_digest = hashlib.sha256(a_config.read_bytes()).hexdigest()

        b_env = isolated_environment(b_home, "b")
        b_env.update({
            "HERDR_SOCKET_PATH": a.status_data["socket"],
            "HERDR_CLIENT_SOCKET_PATH": str(Path(a.status_data["socket"]).with_name("herdr-client.sock")),
            "HERDR_CONFIG_PATH": str(a_config), "HERDR_PANE_ID": a.pane,
            "HERDR_SESSION": "smoke-a", "HERDR_ENV": "1",
            "HERDR_BIN_PATH": str(binary), "RADIO_HANDLE": "foreign-controller",
            "RADIO_HOME": str(a.home / "state/radio"),
        })
        require("WATCHTOWER_CONTEXT" not in b_env, "Foreign-routing test accidentally trusted its context")
        b = Instance(binary, b_home, "b", b_env)
        instances.append(b)
        b.start(plugin)
        b.create_pane()
        b.wait_relay()
        require(a.process.pid != b.process.pid, "Servers share a PID")
        require(a.status_data["socket"] != b.status_data["socket"], "Servers share a socket")
        require(a.relay["relay_pid"] != b.relay["relay_pid"], "Servers share a Radio relay")
        for current, forbidden in ((a, "SMOKE-b"), (b, "SMOKE-a")):
            require(all(item.get("label") != forbidden for item in current.snapshot()["workspaces"]), "Workspace leaked across instances")
        require(hashlib.sha256(a_config.read_bytes()).hexdigest() == a_digest, "Foreign config was modified")
        report["checks"].append("foreign routing stripped; config, sockets, sessions, workspaces and Radio separated")

        role = "\U0001f464 CONTROL"
        b.run("pane", "report-metadata", b.pane, "--source", "watchtower-smoke", "--token", f"team_role={role}", "--token", "team_target=Disposable smoke")
        pane = b.run("pane", "get", b.pane, json_output=True)["result"]["pane"]
        require(pane.get("tokens", {}).get("team_role") == role, "Role icon metadata did not round-trip")
        marker = "WATCHTOWER_SMOKE_PANE_OK"
        b.run("pane", "run", b.pane, f"echo {marker}")
        wait_until(lambda: marker in b.run("pane", "read", b.pane, "--source", "recent-unwrapped", "--lines", "40", "--format", "text").stdout, "ConPTY pane output")
        report["checks"].append("role/icon metadata round-trip and real ConPTY shell output")
        b_config = b.home / "config/config.toml"
        b_config.write_text(defaults.replace("sidebar_width = 30", "sidebar_width = 31"), encoding="utf-8")
        b.run("config", "check")
        b.run("server", "reload-config", json_output=True)
        require('agent_panel_sort = "role"' in b_config.read_text(encoding="utf-8"), "Config edit lost role ordering")
        require(hashlib.sha256(a_config.read_bytes()).hexdigest() == a_digest, "Reload touched other instance config")
        report["checks"].append("separate config edit/reload preserves role defaults")

        for instance in (a, b):
            report["instances"][instance.label] = {
                "server_pid": instance.process.pid,
                "session": instance.status_data["session"],
                "relay_pid": instance.relay["relay_pid"],
                "supervisor_pid": instance.relay["supervisor_pid"],
            }
        b.graceful_stop()
        require(a.process.poll() is None and a.run("status", "server", "--json", json_output=True)["running"], "Stopping B also stopped A")
        require(a.relay["relay_pid"] in a.job.pids(), "Stopping B also stopped A's relay")
        a.snapshot()
        report["checks"].append("B shutdown stops its private relay; A server and relay remain live")
        a.graceful_stop()
        report["checks"].append("A shutdown also leaves no owned processes")
        report["ok"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        for instance in instances:
            log = instance.home / "server-output.log"
            if log.exists():
                report.setdefault("server_log_tails", {})[instance.label] = log.read_text(encoding="utf-8", errors="replace")[-2500:]
    finally:
        cleanup_errors = []
        for instance in reversed(instances):
            cleanup_errors.extend(instance.cleanup())
        if cleanup_errors:
            report["ok"] = False
            report["cleanup_errors"] = cleanup_errors
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path, help="watchtower.exe in an extracted complete package")
    parser.add_argument("--report", type=Path, help="optional compact JSON result file")
    parser.add_argument("--keep-roots", action="store_true", help="keep only this run's disposable homes for diagnosis")
    args = parser.parse_args()
    temporary = Path(tempfile.mkdtemp(prefix="watchtower-smoke-")).resolve()
    report = smoke(args.exe.resolve(), temporary)
    if args.keep_roots or report.get("cleanup_errors"):
        report["disposable_root"] = str(temporary)
    else:
        try:
            resolved_temporary = temporary.resolve(strict=True)
            expected_parent = Path(tempfile.gettempdir()).resolve(strict=True)
            require(
                resolved_temporary.parent == expected_parent
                and resolved_temporary.name.startswith("watchtower-smoke-")
                and resolved_temporary == temporary,
                "Refusing recursive cleanup outside this run's disposable temp root",
            )
            shutil.rmtree(temporary)
        except (OSError, SmokeFailure) as exc:
            report["ok"] = False
            report["cleanup_errors"] = report.get("cleanup_errors", []) + [str(exc)]
            report["disposable_root"] = str(temporary)
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(output, encoding="utf-8")
    print(output)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
