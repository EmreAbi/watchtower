"""Accounts dispatch tests with private fixtures and mocked process/API calls."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "distribution/watchtower/accounts"))
SPEC = importlib.util.spec_from_file_location(
    "accounts_integration", ROOT / "distribution/watchtower/accounts/integration.py"
)
integration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(integration)


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watchtower-account-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.service = SimpleNamespace(home=self.home, launch_plan=Mock(return_value={}),
                                       ensure_registered=Mock())
        self.env = {
            "HERDR_WORKSPACE_ID": "w9", "HERDR_PANE_ID": "w9:p99",
            "HERDR_SOCKET_PATH": str(self.home / "server.sock"),
            "HERDR_PLUGIN_ID": "watchtower-accounts",
            "HERDR_PLUGIN_ENTRYPOINT_ID": "launch-agent",
        }
        self.workspace = {"id": "w9", "name": "Sandbox", "cwd": str(self.home)}

    def write_ticket(self, **changes):
        token = "a" * 48
        ticket = {
            "profile": "personal", "handle": "worker-new", "workspace": "w9",
            "cwd": str(self.home), "socket": self.env["HERDR_SOCKET_PATH"],
            "created": time.time(),
        }
        ticket.update(changes)
        directory = integration._ticket_dir(self.service)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{token}.json").write_text(json.dumps(ticket), encoding="utf-8")
        return token

    def test_ticket_consumed_only_once(self):
        token = self.write_ticket()
        self.assertEqual(integration.claim_ticket(self.service, token, self.env)["handle"], "worker-new")
        with self.assertRaisesRegex(ValueError, "already used"):
            integration.claim_ticket(self.service, token, self.env)

    def test_mismatched_workspace_socket_or_pane_does_not_claim(self):
        token = self.write_ticket()
        for key, value in (
            ("HERDR_WORKSPACE_ID", "w4"),
            ("HERDR_SOCKET_PATH", str(self.home / "different.sock")),
            ("HERDR_PANE_ID", "w4:p99"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                integration.claim_ticket(self.service, token, {**self.env, key: value})
        self.assertFalse((integration._ticket_dir(self.service) / f"{token}.claimed").exists())

    def test_existing_shell_or_accounts_popup_cannot_consume_ticket(self):
        token = self.write_ticket()
        for key, value in (
            ("HERDR_PLUGIN_ID", "different-plugin"),
            ("HERDR_PLUGIN_ENTRYPOINT_ID", "center"),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                integration.claim_ticket(self.service, token, {**self.env, key: value})

    def test_expired_future_and_malformed_tickets_are_not_claimed(self):
        for created in (time.time() - 601, time.time() + 60):
            token = self.write_ticket(created=created)
            with self.assertRaisesRegex(ValueError, "expired"):
                integration.claim_ticket(self.service, token, self.env)
        with self.assertRaises(ValueError):
            integration.claim_ticket(self.service, "../other", self.env)

    def test_launch_opens_one_new_tab_without_typing_into_any_pane(self):
        with (
            patch.object(integration, "list_workspaces", return_value=[self.workspace]),
            patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
            patch.object(integration, "_cli", return_value={
                "plugin_pane": {"pane": {"pane_id": "w9:p99", "workspace_id": "w9"}}
            }) as cli,
        ):
            result = integration.launch_profile(self.service, "personal", "worker-new", "w9")
        self.assertEqual(result["pane_id"], "w9:p99")
        self.service.launch_plan.assert_called_once_with("personal", "worker-new", workspace="w9", model=None)
        cli.assert_called_once()
        args = cli.call_args.args[0]
        self.assertEqual(args[:3], ["plugin", "pane", "open"])
        self.assertEqual(args[args.index("--placement") + 1], "tab")
        self.assertNotIn("--target-pane", args)
        token_assignment = args[args.index("--env") + 1]
        self.assertTrue(token_assignment.startswith(integration.TICKET_ENV + "="))
        tickets = list(integration._ticket_dir(self.service).glob("*.json"))
        self.assertEqual(len(tickets), 1)
        self.assertEqual(set(json.loads(tickets[0].read_text())), {
            "profile", "handle", "workspace", "cwd", "socket", "created", "model",
        })

    def test_preflight_failure_creates_no_ticket_or_tab(self):
        self.service.launch_plan.side_effect = ValueError("Handle already registered")
        with (
            patch.object(integration, "list_workspaces", return_value=[self.workspace]),
            patch.object(integration, "_cli") as cli,
            self.assertRaises(ValueError),
        ):
            integration.launch_profile(self.service, "personal", "worker-new", "w9")
        cli.assert_not_called()
        self.assertFalse(integration._ticket_dir(self.service).exists())

    def test_uncertain_delivery_is_never_retried(self):
        with (
            patch.object(integration, "list_workspaces", return_value=[self.workspace]),
            patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
            patch.object(integration, "_cli", side_effect=RuntimeError("No automatic retry")) as cli,
            self.assertRaises(RuntimeError),
        ):
            integration.launch_profile(self.service, "personal", "worker-new", "w9")
        cli.assert_called_once()

    def test_wrong_workspace_delivery_is_unconfirmed(self):
        with (
            patch.object(integration, "list_workspaces", return_value=[self.workspace]),
            patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
            patch.object(integration, "_cli", return_value={
                "plugin_pane": {"pane": {"pane_id": "w4:p9", "workspace_id": "w4"}}
            }),
            self.assertRaises(RuntimeError),
        ):
            integration.launch_profile(self.service, "personal", "worker-new", "w9")

    def test_provider_env_clears_case_insensitive_overrides(self):
        result = integration.plan_environment({
            "unset_env": ["OPENAI_API_KEY", "CODEX_HOME"],
            "env": {"CODEX_HOME": "selected-home"},
        }, {"openai_api_key": "do-not-carry", "codex_home": "previous-home", **self.env})
        self.assertNotIn("openai_api_key", result)
        self.assertNotIn("codex_home", result)
        self.assertEqual(result["CODEX_HOME"], "selected-home")
        self.assertEqual(result["HERDR_PANE_ID"], "w9:p99")

    def test_interactive_login_uses_plan_env_and_never_captures_credentials(self):
        service = SimpleNamespace(connect_plan=Mock(return_value={
            "argv": ["codex.exe", "login"], "env": {"CODEX_HOME": "selected-home"},
            "unset_env": ["OPENAI_API_KEY"],
        }))
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "old-secret"}),
            patch.object(integration.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            self.assertEqual(integration.run_connect(service, "personal"), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["codex.exe", "login"])
        self.assertEqual(kwargs["env"]["CODEX_HOME"], "selected-home")
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("capture_output", kwargs)
        self.assertNotIn("stdout", kwargs)
        self.assertNotIn("stderr", kwargs)

    def test_google_and_claude_login_keep_profile_environment_and_native_terminal(self):
        for provider, key, command in (("gemini", "GEMINI_CLI_HOME", ["node.exe", "gemini.js"]),
                                       ("claude", "CLAUDE_CONFIG_DIR", ["claude.exe", "auth", "login", "--claudeai"])):
            with self.subTest(provider=provider):
                plan = {"provider": provider, "argv": command, "env": {key: str(self.home)},
                        "unset_env": ["GEMINI_API_KEY", "ANTHROPIC_API_KEY"], "cwd": str(self.home)}
                service = SimpleNamespace(connect_plan=Mock(return_value=plan))
                with (patch.dict(os.environ, {"GEMINI_API_KEY": "old-secret", "ANTHROPIC_API_KEY": "old-secret"}),
                      patch("builtins.print") as output,
                      patch.object(integration.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run):
                    self.assertEqual(integration.run_connect(service, "chosen"), 0)
                args, kwargs = run.call_args
                self.assertEqual(args[0], command)
                self.assertEqual(kwargs["env"][key], str(self.home))
                self.assertEqual(kwargs["cwd"], str(self.home))
                self.assertNotIn("GEMINI_API_KEY", kwargs["env"])
                self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
                self.assertNotIn("capture_output", kwargs)
                self.assertNotIn("shell", kwargs)
                self.assertNotIn("old-secret", str(output.call_args_list))

    def test_browser_button_opens_complete_auth_link_only(self):
        link = "https://auth.openai.com/oauth/authorize?state=fake&code_challenge=test%2Btest"
        with patch.object(integration.webbrowser, "open", return_value=True) as opener:
            self.assertTrue(integration.open_browser_login(link))
            opener.assert_called_once_with(link, new=2)
        for invalid in ("http://localhost:1455", "https://auth.openai.com.evil.test/oauth/authorize",
                        "file:///C:/Windows/notepad.exe", link + "\ncommand"):
            with patch.object(integration.webbrowser, "open") as opener:
                self.assertFalse(integration.open_browser_login(invalid))
                opener.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows clipboard adapter")
    def test_copy_passes_whole_url_on_stdin_not_in_shell_command(self):
        link = "https://auth.openai.com/oauth/authorize?state=fake&redirect_uri=http%3A%2F%2Flocalhost%3A1455"
        with patch.object(integration.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertTrue(integration.copy_login_link(link))
        args, kwargs = run.call_args
        self.assertEqual(kwargs["input"], link)
        self.assertNotIn(link, " ".join(args[0]))
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertIn("-NoProfile", args[0])
        with patch.object(integration.subprocess, "run") as run:
            self.assertFalse(integration.copy_login_link("http://localhost:1455"))
            run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows clipboard adapter")
    def test_clipboard_failure_does_not_report_success(self):
        link = "https://auth.openai.com/oauth/authorize?state=fake"
        for outcome in (SimpleNamespace(returncode=1), subprocess.TimeoutExpired("ignored", 5)):
            with patch.object(integration.subprocess, "run", **(
                {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            )):
                self.assertFalse(integration.copy_login_link(link))

    def test_cli_timeout_and_error_output_are_sanitized(self):
        with (
            patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
            patch.object(integration.subprocess, "run", side_effect=subprocess.TimeoutExpired("private-data", 15)),
            self.assertRaisesRegex(RuntimeError, "no automatic retry") as error,
        ):
            integration._cli(["pane", "list"])
        self.assertNotIn("private-data", str(error.exception))
        with (
            patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
            patch.object(integration.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=json.dumps({"error": {"code": "secret=abc", "message": "private-data"}}))),
            self.assertRaisesRegex(RuntimeError, "request_failed") as error,
        ):
            integration._cli(["pane", "list"])
        self.assertNotIn("private-data", str(error.exception))
        self.assertNotIn("secret", str(error.exception))

    def test_cli_malformed_shapes_use_controlled_error(self):
        for payload in (None, [], {"error": "bad shape"}):
            with (
                self.subTest(payload=payload),
                patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
                patch.object(integration.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(payload))),
                self.assertRaises(RuntimeError),
            ):
                integration._cli(["pane", "list"])

    def test_cli_reads_native_error_envelope_from_stderr(self):
        for stdout in ("", "diagnostic without JSON"):
            with (
                self.subTest(stdout=stdout),
                patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
                patch.object(integration.subprocess, "run", return_value=SimpleNamespace(
                    returncode=1, stdout=stdout, stderr=json.dumps({"id": "synthetic", "error": {
                        "code": "agent_not_found", "message": "PRIVATE_DIAGNOSTIC"}}))),
                self.assertRaisesRegex(RuntimeError, r"^Watchtower: agent_not_found\. No automatic retry\.$") as error,
            ):
                integration._cli(["agent", "get", "w9:p2"])
            self.assertNotIn("PRIVATE", str(error.exception))

    def test_cli_stderr_error_is_not_confused_with_success(self):
        success = json.dumps({"result": {"type": "ok"}})
        missing = json.dumps({"error": {"code": "agent_not_found"}})
        busy = json.dumps({"error": {"code": "agent_quit_busy"}})
        for returncode, stdout, stderr, expected in [
            (0, success, missing, "agent_not_found"),
            (1, success, missing, "agent_not_found"),
            (1, missing, busy, "request_failed"),
            (0, "", success, "invalid response"),
            (1, success, "", "request_failed"),
        ]:
            with (
                self.subTest(returncode=returncode, expected=expected),
                patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
                patch.object(integration.subprocess, "run", return_value=SimpleNamespace(
                    returncode=returncode, stdout=stdout, stderr=stderr)),
                self.assertRaisesRegex(RuntimeError, expected),
            ):
                integration._cli(["agent", "get", "w9:p2"])

    def test_cli_success_accepts_plain_stderr_diagnostic_without_reflecting_it(self):
        with (
            patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
            patch.object(integration.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps({"result": {"type": "ok"}}), stderr="PRIVATE_DIAGNOSTIC")),
        ):
            self.assertEqual(integration._cli(["pane", "list"]), {"type": "ok"})

    def test_cli_silent_success_only_for_documented_native_mutations(self):
        for args in (["pane", "run", "w9:p2", "synthetic command"],
                     ["pane", "report-agent-session", "w9:p2", "--source", "herdr:codex"]):
            with (patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
                  patch.object(integration.subprocess, "run", return_value=SimpleNamespace(
                      returncode=0, stdout="", stderr=""))):
                self.assertEqual(integration._cli(args), {"type": "ok"})
        for code, stdout, stderr, args in [
            (0, "", "", ["pane", "get", "w9:p2"]),
            (0, "", "", ["agent", "quit-if-idle", "w9:p2"]),
            (1, "", "", ["pane", "run", "w9:p2", "synthetic command"]),
            (0, "unexpected", "", ["pane", "run", "w9:p2", "synthetic command"]),
            (0, "", "unexpected", ["pane", "report-agent-session", "w9:p2"]),
        ]:
            with (patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
                  patch.object(integration.subprocess, "run", return_value=SimpleNamespace(
                      returncode=code, stdout=stdout, stderr=stderr)), self.assertRaises(RuntimeError)):
                integration._cli(args)

    def test_cli_bounds_both_json_channels_and_never_extracts_fragments(self):
        success = json.dumps({"result": {"type": "ok"}})
        for stdout, stderr in [("x" * 129, ""), (success, "x" * 129),
                               ("", 'diagnostic {"error":{"code":"agent_not_found"}}')]:
            with (
                patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
                patch.object(integration, "_CLI_JSON_LIMIT", 128),
                patch.object(integration.subprocess, "run", return_value=SimpleNamespace(
                    returncode=1, stdout=stdout, stderr=stderr)),
                self.assertRaisesRegex(RuntimeError, "invalid response") as error,
            ):
                integration._cli(["agent", "get", "w9:p2"])
            self.assertNotIn("agent_not_found", str(error.exception))

    def test_switch_inspection_accepts_native_missing_agent_stderr_after_exit(self):
        from switch_runtime import SwitchRuntime
        responses = [
            SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"result": {"pane": {
                "pane_id": "w9:p2", "agent": None, "agent_status": "unknown"}}})),
            SimpleNamespace(returncode=1, stdout="", stderr=json.dumps({"id": "synthetic", "error": {
                "code": "agent_not_found", "message": "No agent in this pane"}})),
            SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({"result": {"process_info": {
                "pane_id": "w9:p2", "foreground_processes": [{"name": "powershell.exe"}]}}})),
        ]
        with (
            patch.object(integration, "_host_env", return_value=("watchtower.exe", self.env)),
            patch.object(integration.subprocess, "run", side_effect=responses) as run,
        ):
            inspected = SwitchRuntime(cli=integration._cli).inspect("w9:p2")
        self.assertIsNone(inspected["agent"])
        self.assertEqual(inspected["process"]["foreground_processes"][0]["name"], "powershell.exe")
        self.assertEqual([call.args[0][1:3] for call in run.call_args_list],
                         [["pane", "get"], ["agent", "get"], ["pane", "process-info"]])

    def test_radio_main_uses_argv_and_native_provider_dispatch(self):
        root = self.home / "accounts"
        root.mkdir()
        radio_dir = self.home / "radio"
        (radio_dir / "vendor/AgentRadio/bin").mkdir(parents=True)
        radio = radio_dir / "vendor/AgentRadio/bin/radio"
        (radio_dir / "watchtower_radio.py").write_text("def verified_files(): return []\n")
        radio.write_text(
            "import os, sys\n"
            "def utf8_streams(): pass\n"
            "def main():\n"
            "    assert sys.argv[1:] == ['join', 'worker-new', '--new']\n"
            "    return launch_agent(['opencode', '--session', 'new'], dict(os.environ))\n"
        )
        plan = {"argv": [sys.executable, "-B", str(radio), "join", "worker-new", "--new"],
                "env": {"PROFILE_MARKER": "chosen"}, "unset_env": ["OPENAI_API_KEY"],
                "provider_extra_args": ["--standalone"]}
        old_argv = sys.argv[:]
        hook = Mock()
        with (
            patch.object(integration, "ROOT", root),
            patch.dict(sys.modules, {"backend": SimpleNamespace(provider_command=lambda _: ["opencode.exe"])}),
            patch.dict(sys.modules, {"opencode_context": SimpleNamespace(prepare_context=hook)}),
            patch.dict(os.environ, {"OPENAI_API_KEY": "old-secret"}),
            patch.object(integration.subprocess, "run", return_value=SimpleNamespace(returncode=17)) as run,
        ):
            self.assertEqual(integration.run_radio_plan(plan), 17)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["opencode.exe", "--standalone", "--session", "new"])
        self.assertEqual(kwargs["env"]["PROFILE_MARKER"], "chosen")
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertEqual(sys.argv, old_argv)
        hook.assert_called_once_with(kwargs["env"])

    def test_switch_codex_uses_owned_resume_bridge_once(self):
        root = self.home / "accounts"
        root.mkdir()
        radio_dir = self.home / "radio"
        (radio_dir / "vendor/AgentRadio/bin").mkdir(parents=True)
        radio = radio_dir / "vendor/AgentRadio/bin/radio"
        (radio_dir / "watchtower_radio.py").write_text("def verified_files(): return []\n")
        radio.write_text("import os\ndef utf8_streams(): pass\ndef main():\n"
                         "    return launch_agent(['codex', 'resume', 'synthetic-session'], dict(os.environ))\n")
        plan = {"provider": "codex", "kind": "switch", "resume_options": ["-m", "synthetic-model"],
                "argv": [sys.executable, "-B", str(radio), "join", "worker", "--resume"],
                "switch_candidate": {"session_id": "synthetic-session"},
                "env": {"CODEX_HOME": str(self.home / "chosen")}, "unset_env": []}
        bridge = Mock(return_value=17)
        with (patch.object(integration, "ROOT", root), patch.dict(os.environ),
              patch.dict(sys.modules, {"backend": SimpleNamespace(provider_command=lambda _: ["codex.exe"]),
                                       "switch_launch": SimpleNamespace(run_codex_resume=bridge)}),
              patch.object(integration.subprocess, "run") as run):
            self.assertEqual(integration.run_radio_plan(plan), 17)
        run.assert_not_called()
        bridge.assert_called_once()
        sent_plan, native_argv, child_env = bridge.call_args.args
        self.assertIs(sent_plan, plan)
        self.assertEqual(native_argv, ["codex.exe", "-m", "synthetic-model", "-c",
                                       "check_for_update_on_startup=false", "resume", "synthetic-session"])
        self.assertEqual(child_env["CODEX_HOME"], plan["env"]["CODEX_HOME"])

    def test_gemini_native_launch_gets_context_without_a_task_prompt(self):
        root = self.home / "accounts"
        root.mkdir()
        radio_dir = self.home / "radio"
        (radio_dir / "vendor/AgentRadio/bin").mkdir(parents=True)
        radio = radio_dir / "vendor/AgentRadio/bin/radio"
        (radio_dir / "watchtower_radio.py").write_text("def verified_files(): return []\n")
        radio.write_text(
            "import os\n"
            "def utf8_streams(): pass\n"
            "def briefing_text(handle, scope): return 'Radio ' + handle + ' ' + scope\n"
            "def main():\n"
            "    env = dict(os.environ, RADIO_HANDLE='gemini-worker', RADIO_JOINED_SCOPE='w9')\n"
            "    return launch_agent(['gemini', '--session-id', 'fake-session'], env)\n")
        profile = self.home / "gemini-account"
        plan = {"provider": "gemini", "argv": [sys.executable, "-B", str(radio), "join", "gemini-worker", "--new"],
                "env": {"GEMINI_CLI_HOME": str(profile), "HERDR_PANE_ID": "w9:p99"}, "unset_env": []}
        with (patch.object(integration, "ROOT", root), patch.dict(os.environ),
              patch.dict(sys.modules, {"backend": SimpleNamespace(provider_command=lambda _: ["node.exe", "gemini.js"])}),
              patch.object(integration.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run):
            self.assertEqual(integration.run_radio_plan(plan), 0)
        self.assertEqual(run.call_args.args[0], ["node.exe", "gemini.js", "--session-id", "fake-session"])
        self.assertEqual(run.call_args.kwargs["env"]["GEMINI_CLI_HOME"], str(profile))
        context = json.loads((profile / ".gemini/watchtower-radio/w9-p99-fake-session.json").read_text())
        self.assertEqual(context["briefing"], "Radio gemini-worker w9")
        self.assertEqual(context["session"], "fake-session")
        settings = json.loads((profile / ".gemini/settings.json").read_text())
        self.assertEqual(settings["hooks"]["SessionStart"][0]["hooks"][0]["name"], "watchtower-radio")

    def test_codex_launch_uses_central_updates_without_changing_account(self):
        root = self.home / "accounts"
        root.mkdir()
        radio_dir = self.home / "radio"
        (radio_dir / "vendor/AgentRadio/bin").mkdir(parents=True)
        radio = radio_dir / "vendor/AgentRadio/bin/radio"
        (radio_dir / "watchtower_radio.py").write_text("def verified_files(): return []\n")
        radio.write_text(
            "import os\n"
            "def utf8_streams(): pass\n"
            "def main():\n"
            "    return launch_agent(['codex', '-c', 'tui.terminal_title=[]', 'resume', 'saved-session'], dict(os.environ))\n"
        )
        plan = {"argv": [sys.executable, "-B", str(radio), "join", "worker-new", "--resume"],
                "env": {"CODEX_HOME": str(self.home / "chosen-account")}, "unset_env": ["OPENAI_API_KEY"]}
        with (
            patch.object(integration, "ROOT", root),
            patch.dict(sys.modules, {"backend": SimpleNamespace(provider_command=lambda _: ["codex.exe"])}),
            patch.dict(os.environ, {"OPENAI_API_KEY": "old-secret"}),
            patch.object(integration.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            self.assertEqual(integration.run_radio_plan(plan), 0)
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["codex.exe", "-c", "check_for_update_on_startup=false",
                                   "-c", "tui.terminal_title=[]", "resume", "saved-session"])
        self.assertEqual(kwargs["env"]["CODEX_HOME"], plan["env"]["CODEX_HOME"])
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])


if __name__ == "__main__":
    unittest.main()
