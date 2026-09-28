"""Results launch routing uses mocked APIs/runtimes; never opens a real pane."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("results_launcher", ROOT / "launcher.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class ResultsLauncherTests(unittest.TestCase):
    def test_open_captures_original_context_pane_without_target_or_placement(self):
        context = json.dumps({"focused_pane_id": "w9:p3", "workspace_id": "w9"})
        env = {"HERDR_PLUGIN_CONTEXT_JSON": context, "HERDR_PANE_ID": "w8:p1",
               "HERDR_SOCKET_PATH": "private-endpoint", "PYTHONHOME": "unrelated-runtime",
               "pythonpath": "unrelated-modules", "VIRTUAL_ENV": "unrelated-venv"}
        cli = Mock(return_value={"type": "ok"})
        with (patch.dict(os.environ, env, clear=True),
              patch.object(sys, "argv", ["launcher.py", "--open"]),
              patch.dict(sys.modules, {"bridge": SimpleNamespace(cli=cli)}),
              patch.object(launcher, "runtime") as runtime,
              patch.object(launcher.subprocess, "run") as run):
            self.assertEqual(launcher.main(), 0)
        cli.assert_called_once()
        argv, forwarded = cli.call_args.args
        self.assertEqual(argv, ["plugin", "pane", "open", "--plugin", "watchtower-results",
                               "--entrypoint", "center", "--env", "WATCHTOWER_RESULTS_PANE=w9:p3", "--focus"])
        self.assertNotIn("--target-pane", argv)
        self.assertNotIn("--placement", argv)
        self.assertNotIn("agent", argv)
        self.assertEqual(forwarded["HERDR_PLUGIN_CONTEXT_JSON"], context)
        self.assertEqual(forwarded["HERDR_SOCKET_PATH"], "private-endpoint")
        self.assertFalse({"PYTHONHOME", "pythonpath", "VIRTUAL_ENV"} & forwarded.keys())
        runtime.assert_not_called()
        run.assert_not_called()

    def test_open_falls_back_to_original_pane_environment(self):
        cli = Mock(return_value={"type": "ok"})
        with (patch.dict(os.environ, {"HERDR_PANE_ID": "w4:p2"}, clear=True),
              patch.object(sys, "argv", ["launcher.py", "--open"]),
              patch.dict(sys.modules, {"bridge": SimpleNamespace(cli=cli)}),
              patch.object(launcher.subprocess, "run") as run):
            self.assertEqual(launcher.main(), 0)
        self.assertIn("WATCHTOWER_RESULTS_PANE=w4:p2", cli.call_args.args[0])
        run.assert_not_called()

    def test_open_without_context_does_not_guess_another_pane(self):
        cli = Mock()
        with (patch.dict(os.environ, {}, clear=True),
              patch.object(sys, "argv", ["launcher.py", "--open"]),
              patch.dict(sys.modules, {"bridge": SimpleNamespace(cli=cli)}),
              patch.object(launcher, "runtime") as runtime,
              patch.object(launcher.subprocess, "run") as run,
              patch.object(sys, "stderr")):
            self.assertEqual(launcher.main(), 1)
        cli.assert_not_called()
        runtime.assert_not_called()
        run.assert_not_called()

    def test_explicit_file_action_preserves_complete_clicked_url_for_renderer(self):
        clicked_url = "file:///C:/demo/report%20%26%20review.md"
        context = json.dumps({"clicked_url": clicked_url, "focused_pane_id": "w9:p3"})
        python = ROOT / "fixture-runtime/python.exe"
        with (patch.dict(os.environ, {"HERDR_PLUGIN_CONTEXT_JSON": context}, clear=True),
              patch.object(sys, "argv", ["launcher.py", "--file"]),
              patch.object(launcher, "runtime", return_value=python) as runtime,
              patch.object(launcher.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run):
            self.assertEqual(launcher.main(), 0)
        runtime.assert_called_once()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], [str(python), "-I", "-B", str(ROOT / "launcher.py"), "--render-file"])
        self.assertEqual(run.call_args.kwargs["env"]["HERDR_PLUGIN_CONTEXT_JSON"], context)
        self.assertNotIn(clicked_url, run.call_args.args[0])

    def test_renderer_passes_exact_clicked_url_to_local_file_validation(self):
        clicked_url = "file:///C:/demo/report%20%26%20review.md"
        expected_path = ROOT / "fixture-report.md"
        state = ROOT / "fixture-state"
        local_file, action = Mock(return_value=expected_path), Mock(return_value="Opened in browser.")
        env = {"HERDR_PLUGIN_CONTEXT_JSON": json.dumps({"clicked_url": clicked_url}),
               "HERDR_PLUGIN_STATE_DIR": str(state)}
        with (patch.dict(os.environ, env, clear=True),
              patch.object(sys, "argv", ["launcher.py", "--render-file"]),
              patch.dict(sys.modules, {"files": SimpleNamespace(local_file=local_file, action=action)}),
              patch.object(launcher, "runtime") as runtime,
              patch.object(launcher.subprocess, "run") as run):
            self.assertEqual(launcher.main(), 0)
        local_file.assert_called_once_with(clicked_url)
        action.assert_called_once_with(expected_path, "preview", state / "previews")
        runtime.assert_not_called()
        run.assert_not_called()

    def test_open_rejection_is_not_retried_or_replaced_by_agent_launch(self):
        cli = Mock(side_effect=ValueError("Popup is unavailable."))
        with (patch.dict(os.environ, {"HERDR_PANE_ID": "w4:p2"}, clear=True),
              patch.object(sys, "argv", ["launcher.py", "--open"]),
              patch.dict(sys.modules, {"bridge": SimpleNamespace(cli=cli)}),
              patch.object(launcher.subprocess, "run") as run,
              patch.object(sys, "stderr")):
            self.assertEqual(launcher.main(), 1)
        cli.assert_called_once()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
