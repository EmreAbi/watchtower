"""macOS plugin packaging contracts; no provider calls or live state."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from scripts import watchtower_macos_plugins as plugins
from scripts.watchtower_radio import validated_radio_files

ROOT = Path(__file__).resolve().parents[1]


class MacOSPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="Watchtower macOS plugin ")
        self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name) / "package"

    def test_staged_manifests_keep_entrypoints_and_select_only_posix_commands(self):
        provenance = plugins.stage_plugins(ROOT, self.stage)
        for name in (*plugins.PLUGINS, "radio"):
            manifest = tomllib.loads((self.stage / name / "herdr-plugin.toml").read_text())
            self.assertEqual(manifest["platforms"], ["macos"])
            for section in ("startup", "panes", "actions"):
                for entry in manifest.get(section, []):
                    self.assertEqual(entry["command"][0], "sh")
                    self.assertFalse(any(arg.endswith(".cmd") for arg in entry["command"]))
            if name != "radio":
                self.assertIn("center", [pane["id"] for pane in manifest["panes"]])
            self.assertFalse((self.stage / name / "herdr-plugin.macos.toml").exists())
        files = list(self.stage.rglob("*"))
        self.assertFalse(any(file.suffix == ".cmd" for file in files))
        self.assertFalse(any(file.name.startswith("test_") for file in files))
        self.assertFalse(any(file.name in {"auth.json", "sessions", "__pycache__"} for file in files))
        self.assertEqual(len(validated_radio_files(self.stage / "radio")), len(provenance["files"]) + 1)
        original = json.loads((ROOT / "distribution/watchtower/radio/provenance.json").read_text())
        for relative in provenance["upstream_files"]:
            self.assertEqual(provenance["files"][relative], original["files"][relative])
        for relative in (*plugins.LAUNCHERS, "accounts/bin/center", "radio/bin/radio", "radio/bin/hook"):
            path = self.stage / relative
            self.assertTrue(path.read_bytes().startswith(b"#!/bin/sh\n"))
            self.assertNotIn(b"\r", path.read_bytes())
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o755)

    def test_existing_runtime_directory_rejected_before_copy(self):
        (self.stage / "accounts").mkdir(parents=True)
        (self.stage / "accounts/auth.json").write_text("private")
        with self.assertRaisesRegex(ValueError, "not fresh"):
            plugins.stage_plugins(ROOT, self.stage)
        self.assertFalse((self.stage / "radio").exists())

    def test_symlinked_macos_launcher_rejected_before_copy(self):
        launcher = ROOT / "distribution/watchtower/accounts/bin/center"
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == launcher):
            with self.assertRaisesRegex(ValueError, "links"):
                plugins.stage_plugins(ROOT, self.stage)
        self.assertFalse(self.stage.exists())

    def test_missing_platform_launcher_fails_without_partial_output(self):
        launcher = ROOT / "distribution/watchtower/accounts/bin/center"
        original = Path.is_file
        with patch.object(Path, "is_file", autospec=True, side_effect=lambda path: False if path == launcher else original(path)):
            with self.assertRaisesRegex(ValueError, "Missing"):
                plugins.stage_plugins(ROOT, self.stage)
        self.assertFalse(self.stage.exists())

    @unittest.skipIf(os.name == "nt", "native POSIX shell")
    def test_plugin_shims_preserve_arguments_in_paths_with_spaces(self):
        for name in plugins.PLUGINS:
            with self.subTest(plugin=name):
                bundle = self.stage / name
                (bundle / "bin").mkdir(parents=True)
                shutil.copyfile(ROOT / f"distribution/watchtower/{name}/bin/center", bundle / "bin/center")
                (bundle / "launcher.py").write_text(
                    "import json,os,sys\n"
                    "print(json.dumps({'args':sys.argv[1:],'pythonpath':os.getenv('PYTHONPATH')}))\n"
                )
                result = subprocess.run(
                    ["sh", str(bundle / "bin/center"), "argument with spaces", "--launch"],
                    capture_output=True, text=True, timeout=10,
                    env=dict(os.environ, PYTHONPATH="/untrusted/path"),
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {
                    "args": ["argument with spaces", "--launch"], "pythonpath": None,
                })

    @unittest.skipIf(os.name == "nt", "native POSIX shell")
    def test_setup_and_start_share_home_and_link_only_never_installs(self):
        self.stage.mkdir()
        fake_bin = self.stage / "test-bin"
        fake_bin.mkdir()
        python = fake_bin / "python3"
        python.write_text("#!/bin/sh\ncase \"$*\" in *pip*|*venv*) exit 55;; esac\nexit 0\n")
        python.chmod(0o755)
        binary = self.stage / "watchtower"
        binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$WATCHTOWER_HOME\" \"$@\"\n")
        binary.chmod(0o755)
        env = dict(os.environ, HOME=str(self.stage / "user home"),
                   PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
        env.pop("WATCHTOWER_HOME", None)
        expected_home = str(self.stage / "user home/.watchtower")
        for name in plugins.LAUNCHERS:
            shutil.copyfile(ROOT / "distribution/watchtower" / name, self.stage / name)
            args = ["--link-only"] if name == "setup-accounts" else []
            result = subprocess.run(["sh", str(self.stage / name), *args],
                                    capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            if name == "setup-accounts":
                expected = []
                for plugin in plugins.PLUGINS:
                    expected += [expected_home, "plugin", "link", str(self.stage / plugin)]
                self.assertEqual(result.stdout.splitlines(), expected)
            else:
                self.assertEqual(result.stdout.splitlines()[0], expected_home)
            invalid = subprocess.run(["sh", str(self.stage / name), *args], capture_output=True,
                                     text=True, env=dict(env, WATCHTOWER_HOME="relative"), timeout=10)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn("must be an absolute path", invalid.stderr)

    @unittest.skipIf(os.name == "nt", "native POSIX shell")
    def test_open_launcher_preserves_arguments_and_package_cwd(self):
        self.stage.mkdir()
        shutil.copyfile(ROOT / "distribution/watchtower/open-watchtower", self.stage / "open-watchtower")
        binary = self.stage / "watchtower"
        binary.write_text("#!/bin/sh\nprintf '%s\\n' \"$PWD\" \"$@\"\n")
        binary.chmod(0o755)
        result = subprocess.run(
            ["sh", str(self.stage / "open-watchtower"), "with spaces", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [str(self.stage), "with spaces", "--version"])


class AccountsSetupTests(unittest.TestCase):
    def test_link_only_check_never_invokes_pip_or_creates_a_venv(self):
        setup_path = ROOT / "distribution/watchtower/accounts/setup.py"
        spec = importlib.util.spec_from_file_location("watchtower_setup_check_test", setup_path)
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            python = home / "state/accounts/venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(setup, "default_home", return_value=home), patch.object(
                setup.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run:
                self.assertEqual(setup.main(["--check"]), 0)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command, [str(python), "-I", "-B", "-c", "import textual.app, PIL, pypdfium2"])

    def test_setup_uses_existing_private_runtime_with_pinned_dependencies(self):
        setup_path = ROOT / "distribution/watchtower/accounts/setup.py"
        spec = importlib.util.spec_from_file_location("watchtower_setup_test", setup_path)
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            python = home / "state/accounts/venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(setup, "default_home", return_value=home), patch.object(
                setup.subprocess, "run", return_value=SimpleNamespace(returncode=0)
            ) as run:
                self.assertEqual(setup.main([]), 0)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command[0], str(python))
            self.assertIn("textual==8.2.8", command)
            self.assertIn("Pillow==12.3.0", command)
            self.assertIn("pypdfium2==5.13.0", command)
            self.assertIn("-I", command)


if __name__ == "__main__":
    unittest.main()
