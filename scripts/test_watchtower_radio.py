#!/usr/bin/env python3
"""Private Radio adapter regression tests; no live host or provider calls."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / "distribution/watchtower/radio"
namespace = {"__file__": str(ROOT / "watchtower_radio.py"), "__name__": "adapter_test"}
exec(compile((ROOT / "watchtower_radio.py").read_bytes(), str(ROOT / "watchtower_radio.py"), "exec"), namespace)


class PrivateRadioTests(unittest.TestCase):
    def test_exact_bundle_and_no_stock_global_hooks(self):
        paths = namespace["verified_files"]()
        names = {path.relative_to(ROOT).as_posix() for path in paths}
        self.assertIn("vendor/AgentRadio/LICENSE", names)
        self.assertIn("bin/radio.cmd", names)
        self.assertFalse(any("setup-win" in name or "win-shim" in name or "autostart" in name for name in names))

    def test_modified_and_unlisted_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp) / "radio"
            shutil.copytree(ROOT, copied)
            extra = copied / "credential.json"
            extra.write_text("{}")
            with self.assertRaisesRegex(ValueError, "Unlisted"):
                namespace["verified_files"](copied)
            extra.unlink()
            (copied / "bin/radio.cmd").write_text("changed")
            with self.assertRaisesRegex(ValueError, "changed"):
                namespace["verified_files"](copied)

    def test_missing_or_original_state_rejected(self):
        for value in ("", "relative/state", str(Path.home() / ".local/share/herdr-radio")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                namespace["runtime_env"]({"RADIO_HOME": value})

    def test_original_herdr_binary_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "herdr.exe"
            binary.touch()
            with self.assertRaisesRegex(ValueError, "watchtower executable"):
                namespace["runtime_env"]({"RADIO_HOME": str(Path(temp) / "radio"), "HERDR_BIN_PATH": str(binary)})

    def test_child_env_preserves_selected_provider_login(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "watchtower.exe"
            binary.touch()
            source = {"RADIO_HOME": str(Path(temp) / "radio"), "HERDR_BIN_PATH": str(binary),
                      "PATH": "original-path", "CODEX_HOME": "selected-existing-login"}
            result = namespace["runtime_env"](source)
            self.assertEqual(source["PATH"], "original-path")
            self.assertEqual(result["CODEX_HOME"], source["CODEX_HOME"])
            self.assertEqual(result["PATH"], str(ROOT / "bin") + os.pathsep + "original-path")

    def test_relay_spawns_only_private_runtime_hidden(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "private-radio"
            env = {"RADIO_HOME": str(state), "HERDR_BIN_PATH": str(Path(temp) / "watchtower.exe"),
                   "HERDR_SOCKET_PATH": str(Path(temp) / "watchtower.sock")}
            with patch.object(subprocess, "Popen") as spawn:
                self.assertEqual(namespace["start_relay"](env), 0)
                args, kwargs = spawn.call_args
                self.assertEqual(args[0][-2:], [str(ROOT / "watchtower_radio.py"), "supervise"])
                self.assertEqual(kwargs["env"], env)
                self.assertEqual(kwargs["cwd"], state)
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                if os.name == "nt":
                    self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
                self.assertTrue((state / "relay.log").is_file())

    def test_supervisor_stops_only_its_child_when_own_host_stops(self):
        child = Mock()
        child.poll.return_value = None
        with patch.dict(namespace, {"host_running": Mock(side_effect=[True, False]), "record_relay": Mock(return_value=None)}), \
                patch.object(subprocess, "Popen", return_value=child) as spawn:
            self.assertEqual(namespace["supervise_relay"]({"RADIO_HOME": "private"}), 0)
        child.terminate.assert_called_once_with()
        child.kill.assert_not_called()
        child.wait.assert_called_once_with(timeout=5)
        self.assertEqual(spawn.call_args.args[0][-1], "relay-child")

    def test_repeated_startup_child_exit_does_not_kill_existing_relay(self):
        child = Mock()
        child.poll.return_value = 1  # Upstream's ledger lock is already held.
        with patch.dict(namespace, {"host_running": Mock(return_value=True), "record_relay": Mock(return_value=None)}), \
                patch.object(subprocess, "Popen", return_value=child):
            self.assertEqual(namespace["supervise_relay"]({"RADIO_HOME": "private"}), 0)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()

    def test_status_probe_uses_exact_socket_and_bounded_timeout(self):
        env = {"HERDR_BIN_PATH": "watchtower.exe", "HERDR_SOCKET_PATH": "/private/socket"}
        response = Mock(returncode=0, stdout=json.dumps({"running": True, "socket": "/other/socket"}))
        with patch.object(subprocess, "run", return_value=response) as probe:
            self.assertFalse(namespace["host_running"](env))
            self.assertEqual(probe.call_args.kwargs["timeout"], 4)
            self.assertEqual(probe.call_args.args[0], ["watchtower.exe", "status", "server", "--json"])

    def test_transient_status_errors_are_bounded_then_child_is_stopped(self):
        child = Mock()
        child.poll.return_value = None
        probe = Mock(side_effect=[True, None, None, None])
        with patch.dict(namespace, {"host_running": probe, "record_relay": Mock(return_value=None)}), \
                patch.object(subprocess, "Popen", return_value=child), patch.object(namespace["time"], "sleep"):
            self.assertEqual(namespace["supervise_relay"]({"RADIO_HOME": "private"}), 0)
        self.assertEqual(probe.call_count, 4)
        child.terminate.assert_called_once_with()

    def test_private_runtime_record_contains_only_own_process_and_endpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {"RADIO_HOME": temp, "HERDR_SOCKET_PATH": "private/socket", "HERDR_BIN_PATH": "watchtower.exe"}
            path = namespace["record_relay"](env, Mock(pid=54321))
            self.assertEqual(path.parent, Path(temp))
            self.assertEqual(json.loads(path.read_text()), {
                "supervisor_pid": os.getpid(), "relay_pid": 54321,
                "socket": "private/socket", "executable": "watchtower.exe",
            })
            self.assertFalse(path.with_suffix(".tmp").exists())

    @unittest.skipUnless(os.name == "nt", "Windows upstream view launcher")
    def test_view_launcher_selects_private_venv_and_bundled_view(self):
        launcher = ROOT / "vendor/AgentRadio/bin/run-view.py"
        view = {"__file__": str(launcher), "__name__": "private_view_test"}
        exec(compile(launcher.read_bytes(), str(launcher), "exec"), view)
        with tempfile.TemporaryDirectory() as temp:
            python = Path(temp) / "venv/Scripts/python.exe"
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.dict(os.environ, {"RADIO_HOME": temp}), \
                    patch.dict(view, {"detach_cwd": lambda: None}), \
                    patch.object(subprocess, "run", return_value=Mock(returncode=0)) as run:
                self.assertEqual(view["main"](), 0)
            self.assertEqual(run.call_args.args[0], [str(python), str(launcher.parent / "radio-view")])

    @unittest.skipUnless(os.name == "nt", "Windows private launcher")
    def test_windows_launcher_with_spaces_reads_empty_context_without_state_writes(self):
        with tempfile.TemporaryDirectory(prefix="Watchtower Radio ") as temp:
            package = Path(temp)
            copied = package / "radio"
            shutil.copytree(ROOT, copied)
            binary = package / "watchtower.exe"
            binary.touch()  # Empty ledger means no host call is necessary.
            state = package / "private state"
            env = dict(os.environ, RADIO_HOME=str(state), HERDR_BIN_PATH=str(binary))
            for name in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_WORKSPACE_ID"):
                env.pop(name, None)
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(copied / "bin/radio.cmd"), "tools", "context", "--json"],
                env=env, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["agents"], [])
            self.assertFalse(state.exists())

    @unittest.skipUnless(os.name == "nt", "Windows launcher Job ownership")
    def test_windows_hook_launches_actual_interpreter_inside_callers_job(self):
        try:
            from .watchtower_smoke_windows import PrivateJob
        except ImportError:
            from watchtower_smoke_windows import PrivateJob

        with tempfile.TemporaryDirectory(prefix="Watchtower launcher ") as temp:
            package = Path(temp)
            (package / "bin").mkdir()
            hook = package / "bin/hook.cmd"
            shutil.copyfile(ROOT / "bin/hook.cmd", hook)
            report = package / "process.json"
            (package / "watchtower_radio.py").write_text(
                "import json,os,sys,time\nfrom pathlib import Path\n"
                "Path(__file__).with_name('process.json').write_text(json.dumps({'pid':os.getpid()}))\n"
                "time.sleep(3)\n", encoding="utf-8",
            )
            job = PrivateJob()
            process = subprocess.Popen(
                ["cmd.exe", "/d", "/c", str(hook), "probe"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                job.attach(process)
                deadline = time.monotonic() + 5
                while not report.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(report.exists(), "Private launcher did not run")
                self.assertIn(json.loads(report.read_text())["pid"], job.pids())
            finally:
                job.close()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
