"""Portable Accounts launcher contracts; no real credentials or processes."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "accounts_launcher", ROOT / "distribution/watchtower/accounts/launcher.py"
)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class AccountsLauncherTests(unittest.TestCase):
    def test_clean_env_preserves_host_and_account_context(self):
        source = {
            "PYTHONHOME": "wrong-python", "pythonpath": "wrong-modules",
            "VIRTUAL_ENV": "unrelated-venv", "WATCHTOWER_HOME": str(ROOT),
            "RADIO_HOME": str(ROOT / "radio"), "CODEX_HOME": "configured-account",
            "HERDR_SOCKET_PATH": "exact-endpoint",
        }
        cleaned = launcher.clean_env(source)
        self.assertFalse({"PYTHONHOME", "pythonpath", "VIRTUAL_ENV"} & cleaned.keys())
        self.assertEqual(cleaned["CODEX_HOME"], "configured-account")
        self.assertEqual(cleaned["HERDR_SOCKET_PATH"], "exact-endpoint")
        self.assertIn("PYTHONHOME", source)

    def test_private_runtime_precedes_radio_runtime(self):
        suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
        result = launcher.runtime_candidates({
            "WATCHTOWER_HOME": str(ROOT), "RADIO_HOME": str(ROOT / "private-radio"),
        })
        self.assertEqual(result, [
            ROOT / "state/accounts/venv" / suffix,
            ROOT / "private-radio/venv" / suffix,
        ])
        with self.assertRaises(ValueError):
            launcher.runtime_candidates({"WATCHTOWER_HOME": "relative"})

    def test_agent_runner_does_not_load_textual_or_drop_request_context(self):
        with (
            patch.dict(os.environ, {"WATCHTOWER_HOME": str(ROOT), "REQUEST_ID": "one-use"}),
            patch.object(launcher, "supports_textual", side_effect=AssertionError("UI import")),
            patch.object(launcher.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            self.assertEqual(launcher.main(["--launch"]), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-2:], [str(launcher.ROOT / "integration.py"), "--run"])
        self.assertEqual(kwargs["env"]["REQUEST_ID"], "one-use")

    def test_default_install_uses_backend_state_root_without_explicit_home(self):
        suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
        result = launcher.runtime_candidates({"XDG_STATE_HOME": str(ROOT / "user-state")})
        self.assertEqual(result, [ROOT / "user-state/watchtower/state/accounts/venv" / suffix])

    def test_missing_ui_dependency_never_installs_or_launches(self):
        with (
            patch.dict(os.environ, {"WATCHTOWER_HOME": str(ROOT)}),
            patch.object(launcher, "supports_textual", return_value=False),
            patch.object(launcher.subprocess, "run") as run,
            patch("sys.stderr"),
        ):
            self.assertEqual(launcher.main([]), 1)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
