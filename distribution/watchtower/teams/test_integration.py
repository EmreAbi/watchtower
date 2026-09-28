"""Hermetic workspace setup and at-most-once role launch contracts."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import integration
import scaffold
from service import TeamService


class FakeAccounts:
    def __init__(self, root):
        self.home = root / "watchtower"
        self.radio_home = root / "radio"
        self.home.mkdir()
        self.radio_home.mkdir()
        self.profiles = [dict(id="work", provider="codex", home=str(root / "profile-one")),
                         dict(id="personal", provider="codex", home=str(root / "profile-two")),
                         dict(id="open", provider="opencode", home=str(root / "profile-open"))]
        self.registered = []
        self.plans = []
        self.catalog_calls = []
        self.catalogs = {"work": ["codex-small", "codex-large"], "personal": ["codex-small"],
                         "open": ["opencode/zen-model", "opencode/other-model"]}

    def list(self):
        return {"profiles": deepcopy(self.profiles)}

    def get(self, profile):
        return deepcopy(next(p for p in self.profiles if p["id"] == profile))

    def ensure_registered(self, profile):
        self.registered.append(profile)
        return self.get(profile)

    def models(self, profile):
        self.catalog_calls.append(profile)
        return {"models": [{"id": model, "label": model} for model in self.catalogs[profile]],
                "default_model": None, "notice": "Synthetic catalog"}

    def launch_plan(self, profile, handle, workspace=None, model=None):
        self.plans.append((profile, handle, workspace, model))
        provider = self.get(profile)["provider"]
        argv = ["python.exe", "radio", "join", handle, "--new", "--account", profile, "--workspace-frequency"]
        if model is not None:
            argv.extend(["--model", model])
        return dict(provider=provider, argv=argv,
                    env={"CODEX_HOME" if provider == "codex" else "XDG_DATA_HOME": self.get(profile)["home"]}, unset_env=[])


class FakeCLI:
    def __init__(self):
        self.calls = []
        self.panes = {}
        self.fail_open = None

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["workspace", "create"]:
            return dict(workspace=dict(workspace_id="w42"), tab=dict(tab_id="w42:t1"),
                        root_pane=dict(pane_id="w42:p1", workspace_id="w42"))
        if args[:3] == ["plugin", "pane", "open"]:
            if self.fail_open == len(self.panes):
                raise TimeoutError("Delivery may have happened")
            count = len(self.panes) + 2
            pane = dict(pane_id=f"w42:p{count}", workspace_id="w42", tab_id=f"w42:t{count}",
                        cwd=args[args.index("--cwd") + 1], agent=None)
            self.panes[pane["pane_id"]] = pane
            return dict(plugin_pane=dict(pane=deepcopy(pane)))
        if args[:2] == ["pane", "get"]:
            return {"pane": deepcopy(self.panes[args[2]])}
        if args[:2] in (["tab", "rename"], ["pane", "report-metadata"]):
            return {"type": "ok"}
        raise AssertionError(args)


class TeamIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / "Project & examples"
        self.project.mkdir()
        self.accounts = FakeAccounts(self.root)
        self.binary = self.root / "watchtower.exe"
        self.binary.write_bytes(b"not an executable; CLI is mocked")
        self.env = dict(HERDR_BIN_PATH=str(self.binary), HERDR_SOCKET_PATH=str(self.root / "herdr.sock"),
                        RADIO_HOME=str(self.accounts.radio_home), WATCHTOWER_HOME=str(self.accounts.home))
        self.cli = FakeCLI()

    def plan(self, template="development", team="example", models=None):
        assignments = {"lead": "work", "worker": "personal", "reviewer": "work"}
        if template == "quick-task":
            assignments = {"assistant": "work"}
        elif template == "research":
            assignments = {"researcher": "work", "reviewer": "personal"}
        return scaffold.plan(team, "Example project", str(self.project), template, assignments, self.accounts.list()["profiles"], models=models)

    def create(self, plan=None, **kwargs):
        return integration.create_team(self.accounts, plan or self.plan(), cli=self.cli, environ=self.env, **kwargs)

    def launch_env(self, index=0):
        opens = [args for args in self.cli.calls if args[:3] == ["plugin", "pane", "open"]]
        args = opens[index]
        token = args[args.index("--env") + 1].split("=", 1)[1]
        pane = list(self.cli.panes)[index]
        env = {**self.env, "HERDR_PLUGIN_ID": integration.PLUGIN,
               "HERDR_PLUGIN_ENTRYPOINT_ID": integration.ENTRYPOINT,
               "HERDR_WORKSPACE_ID": "w42", "HERDR_PANE_ID": pane,
               integration.TICKET_ENV: token}
        return token, env

    def ticket_path(self, token):
        team, suffix = token.split(".")
        return self.accounts.home / "state/teams-operations" / team / "tickets" / (suffix + ".json")

    def test_creation_uses_new_workspace_and_fresh_owned_tabs_only(self):
        with patch("integration.subprocess.run") as process:
            result = self.create()
        process.assert_not_called()
        self.assertEqual(result["state"], "launch_requested")
        self.assertEqual(result["workspace_id"], "w42")
        self.assertEqual([item["id"] for item in result["roles"]], ["lead", "worker", "reviewer"])
        self.assertEqual(len({item["pane_id"] for item in result["roles"]}), 3)
        self.assertEqual(self.accounts.registered, ["work", "personal"])
        self.assertEqual(self.cli.calls[0], ["workspace", "create", "--cwd", str(self.project), "--label", "Example project", "--focus"])
        self.assertIn(["tab", "rename", "w42:t1", "Project terminal"], self.cli.calls)
        for args in self.cli.calls:
            self.assertNotIn("prompt", args)
            self.assertNotIn("send-text", args)
            self.assertNotIn("--target-pane", args)
            self.assertNotIn("close", args)
            self.assertNotIn("reload", args)
        self.assertEqual(list(self.project.iterdir()), [])
        config = json.loads((Path(result["team_dir"]) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["agent_permissions"], {"mode": "inherit"})
        self.assertEqual(config["review"]["global_mode"], "on_demand")
        self.assertEqual(config["roles"]["worker"]["cwd"], str(self.project))
        self.assertEqual(config["roles"]["lead"]["cwd"], result["team_dir"])
        self.assertEqual(Path(config["workflow_cli"]).name, "workflow.py")
        self.assertEqual(config["watchtower_home"], str(self.accounts.home))

    def test_each_template_creates_exact_role_count_without_startup_task(self):
        for template, count in (("quick-task", 1), ("research", 2), ("development", 3)):
            self.cli = FakeCLI()
            result = self.create(self.plan(template, template))
            self.assertEqual(len(result["roles"]), count)
            self.assertEqual(result["state"], "launch_requested")
            self.assertTrue(all(item["state"] == "launch_requested" for item in result["roles"]))

    def test_same_creation_id_is_never_replayed_even_from_another_service_instance(self):
        plan = self.plan()
        first = self.create(plan)
        calls = deepcopy(self.cli.calls)
        registrations = list(self.accounts.registered)
        second = self.create(plan)
        self.assertEqual(first, second)
        self.assertEqual(self.cli.calls, calls)
        self.assertEqual(self.accounts.registered, registrations)

    def test_unknown_workspace_response_leaves_reservation_and_never_retries(self):
        def timeout(args):
            self.cli.calls.append(args)
            raise TimeoutError("Maybe workspace already exists")
        self.cli = Mock(side_effect=timeout)
        first = self.create()
        self.assertEqual(first["state"], "unconfirmed")
        self.assertIsNone(first["workspace_id"])
        self.assertEqual(self.cli.call_count, 1)
        self.assertEqual(self.create(), first)
        self.assertEqual(self.cli.call_count, 1)
        self.assertFalse(Path(first["team_dir"]).exists())

    def test_journal_reservation_is_written_before_workspace_mutation(self):
        real = self.cli
        def inspecting(args):
            if args[:2] == ["workspace", "create"]:
                path = self.accounts.home / "state/teams-operations/example/outcome.json"
                self.assertEqual(json.loads(path.read_text())["state"], "workspace_requested")
            return real(args)
        self.cli = inspecting
        self.assertEqual(self.create()["state"], "launch_requested")

    def test_final_journal_write_failure_returns_known_workspace_and_roles(self):
        atomic = integration._atomic
        def fail_final(path, value):
            if value.get("state") == "launch_requested":
                raise OSError("Synthetic disk-full failure")
            return atomic(path, value)
        with patch("integration._atomic", side_effect=fail_final):
            result = self.create()
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["workspace_id"], "w42")
        self.assertEqual(len(result["roles"]), 3)
        self.assertTrue(all(role["pane_id"] for role in result["roles"]))
        self.assertIn("journal update failed", result["notice"])
        calls = deepcopy(self.cli.calls)
        self.create()
        self.assertEqual(self.cli.calls, calls)

    def test_native_metadata_silent_success_is_only_allowed_for_that_command(self):
        empty_success = Mock(returncode=0, stdout="", stderr="")
        with patch("integration.subprocess.run", return_value=empty_success):
            self.assertEqual(integration._cli(["pane", "report-metadata", "w42:p2"], self.env), {"type":"ok"})
            for args in (["pane", "get", "w42:p2"], ["workspace", "create"]):
                with self.assertRaises(integration.TeamLaunchError):
                    integration._cli(args, self.env)
        with patch("integration.subprocess.run", return_value=Mock(returncode=1, stdout="", stderr="rejected")):
            with self.assertRaises(integration.TeamLaunchError):
                integration._cli(["pane", "report-metadata", "w42:p2"], self.env)

    def test_partial_role_delivery_stops_without_closing_or_relaunching_anything(self):
        self.cli.fail_open = 1
        first = self.create()
        self.assertEqual(first["state"], "partial")
        self.assertEqual(first["workspace_id"], "w42")
        self.assertEqual(len(first["roles"]), 2)
        self.assertEqual(first["roles"][0]["pane_id"], "w42:p2")
        self.assertIsNone(first["roles"][1]["pane_id"])
        self.assertTrue(Path(first["team_dir"]).is_dir())
        calls = deepcopy(self.cli.calls)
        self.assertEqual(self.create(), first)
        self.assertEqual(self.cli.calls, calls)
        self.assertFalse(any("close" in args for args in self.cli.calls))

    def test_existing_team_directory_is_preserved_before_any_mutation(self):
        destination = self.root / "existing-team"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("Do not overwrite")
        with self.assertRaises(integration.TeamLaunchError):
            self.create(team_root=destination)
        self.assertEqual(marker.read_text(), "Do not overwrite")
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.accounts.registered, [])

    def test_changed_policy_existing_workspace_and_invalid_context_fail_before_mutation(self):
        for mutation in (lambda p: p["agent_permissions"].update(mode="full_access"),
                         lambda p: p.update(workspace_id="w9"),
                         lambda p: p["roles"]["reviewer"].update(scope="Implement source")):
            plan = self.plan()
            mutation(plan)
            with self.assertRaises(ValueError):
                self.create(plan)
        self.env["RADIO_HOME"] = str(self.root / "other-ledger")
        with self.assertRaises(integration.TeamLaunchError):
            self.create()
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.accounts.registered, [])

    def test_non_codex_or_changed_accounts_cannot_create_team(self):
        plan = self.plan()
        self.accounts.profiles[0]["provider"] = "claude"
        with self.assertRaises(ValueError):
            self.create(plan)
        self.assertEqual(self.cli.calls, [])

    def test_template_metadata_cannot_be_written_under_selected_project(self):
        with self.assertRaises(integration.TeamLaunchError):
            self.create(team_root=self.project / "teams")
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(self.cli.calls, [])

    def test_crashed_empty_operation_claim_is_not_reused(self):
        operation = self.accounts.home / "state/teams-operations/example"
        operation.mkdir(parents=True)
        result = self.create()
        self.assertEqual(result["state"], "unconfirmed")
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.accounts.registered, [])

    def test_runner_injects_role_through_radio_and_keeps_native_permissions(self):
        self.create()
        token, env = self.launch_env(1)  # implementation worker / personal profile
        runner = Mock(return_value=0)
        cwd = Path.cwd()
        self.assertEqual(integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner), 0)
        self.assertEqual(Path.cwd(), cwd)
        plan = runner.call_args.args[0]
        self.assertEqual(plan["provider"], "codex")
        self.assertIn("--new", plan["argv"])
        self.assertIn("--workspace-frequency", plan["argv"])
        self.assertNotIn("--resume", plan["argv"])
        self.assertEqual(plan["env"], {"CODEX_HOME": self.accounts.get("personal")["home"]})
        role_text = plan["argv"][plan["argv"].index("--role") + 1]
        self.assertIn("only template", role_text)
        self.assertIn("Wait for an explicit user task", role_text)
        self.assertIn("Global review is optional", " ".join(role_text.split()))
        self.assertNotIn("danger-full-access", str(plan))
        self.assertNotIn("approval_policy=never", str(plan))
        metadata = [args for args in self.cli.calls if args[:2] == ["pane", "report-metadata"]][-1]
        self.assertIn("team_role=⚙  WORKER", metadata)
        self.assertIn("team_frequency=042", metadata)

    def test_mixed_models_flow_from_service_prepare_through_config_ticket_and_radio(self):
        creator = Mock(side_effect=lambda accounts, prepared: integration.create_team(
            accounts, prepared, cli=self.cli, environ=self.env))
        service = TeamService(self.accounts, creator)
        assignments = {"lead": "work", "worker": "open", "reviewer": "personal"}
        models = {"lead": "codex-large", "worker": "opencode/zen-model", "reviewer": None}
        public = service.prepare("development", "Mixed providers", self.project, assignments, models)
        with patch("integration.subprocess.run") as process:
            result = service.create(public["token"])
            self.assertEqual(result["state"], "created")
            prepared = creator.call_args.args[1]
            config_path = self.accounts.home / "state/teams" / prepared["team_id"] / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual({name: role.get("model") for name, role in config["roles"].items()}, models)
            self.assertEqual({name: role["provider"] for name, role in config["roles"].items()},
                             {"lead": "codex", "worker": "opencode", "reviewer": "codex"})
            for index, role_id in enumerate(("lead", "worker", "reviewer")):
                with self.subTest(role=role_id):
                    token, env = self.launch_env(index)
                    ticket = json.loads(self.ticket_path(token).read_text(encoding="utf-8"))
                    self.assertEqual(ticket["config_digest"], integration._hash(config))
                    self.assertEqual(ticket["profile"]["provider"], config["roles"][role_id]["provider"])
                    runner = Mock(return_value=0)
                    integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner)
                    launch = runner.call_args.args[0]
                    self.assertEqual(launch["provider"], config["roles"][role_id]["provider"])
                    if models[role_id] is None:
                        self.assertNotIn("--model", launch["argv"])
                    else:
                        self.assertEqual(launch["argv"].count("--model"), 1)
                        self.assertEqual(launch["argv"][launch["argv"].index("--model") + 1], models[role_id])
                    metadata = [args for args in self.cli.calls if args[:2] == ["pane", "report-metadata"]][-1]
                    self.assertEqual(metadata[metadata.index("--agent") + 1], config["roles"][role_id]["provider"])
                    self.assertEqual(self.accounts.plans[-1], (assignments[role_id], role_id, "w42", models[role_id]))
                    self.assertIn("Wait for an explicit user task", launch["argv"][-1])
            process.assert_not_called()
        self.assertEqual(list(self.project.iterdir()), [])
        self.assertEqual(self.accounts.registered, ["work", "open", "personal"])

    def test_model_removed_after_review_is_rejected_before_any_workspace_mutation(self):
        creator = lambda accounts, prepared: integration.create_team(accounts, prepared, cli=self.cli, environ=self.env)
        service = TeamService(self.accounts, creator)
        public = service.prepare("quick-task", "OpenCode", self.project, {"assistant": "open"},
                                 {"assistant": "opencode/zen-model"})
        self.accounts.catalogs["open"] = []
        outcome = service.create(public["token"])
        self.assertEqual(outcome["state"], "partial")
        self.assertIn("selected model is unavailable", outcome["message"])
        self.assertEqual(self.cli.calls, [])
        self.assertEqual(self.accounts.registered, [])
        self.assertEqual(self.accounts.plans, [])
        self.assertFalse((self.accounts.home / "state").exists())
        with self.assertRaisesRegex(ValueError, "already used"):
            service.create(public["token"])

    def test_invalid_and_unavailable_plan_models_fail_before_journal_or_registration(self):
        for model in ("--bad-model", "codex-small\n--flag", "codex-not-in-catalog"):
            with self.subTest(model=model):
                prepared = self.plan(models={"lead": "codex-small", "worker": None, "reviewer": None})
                prepared["roles"]["lead"]["model"] = model
                with self.assertRaises(ValueError):
                    self.create(prepared)
                self.assertEqual(self.cli.calls, [])
                self.assertEqual(self.accounts.registered, [])
                self.assertFalse((self.accounts.home / "state").exists())

    def test_tampered_config_model_or_ticket_provider_is_rejected_before_claim(self):
        prepared = self.plan(models={"lead": "codex-small", "worker": None, "reviewer": None})
        result = self.create(prepared)
        token, env = self.launch_env()
        config_path = Path(result["team_dir"]) / "config.json"
        original_config = config_path.read_text(encoding="utf-8")
        original_ticket = self.ticket_path(token).read_text(encoding="utf-8")
        config = json.loads(original_config)
        config["roles"]["lead"]["model"] = "codex-large"  # Valid ID, but not the reviewed config digest.
        config_path.write_text(json.dumps(config), encoding="utf-8")
        before_calls = deepcopy(self.cli.calls)
        runner = Mock()
        with self.assertRaises(integration.TeamLaunchError):
            integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner)
        self.assertFalse(self.ticket_path(token).with_suffix(".claimed").exists())
        config_path.write_text(original_config, encoding="utf-8")
        ticket = json.loads(original_ticket)
        ticket["profile"]["provider"] = "opencode"
        self.ticket_path(token).write_text(json.dumps(ticket), encoding="utf-8")
        with self.assertRaises(integration.TeamLaunchError):
            integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner)
        self.assertFalse(self.ticket_path(token).with_suffix(".claimed").exists())
        self.assertEqual(self.cli.calls, before_calls)
        runner.assert_not_called()

    def test_changed_launch_provider_is_rejected_before_metadata_or_radio_start(self):
        self.create()
        token, env = self.launch_env()
        original = self.accounts.launch_plan
        def changed_provider(*args, **kwargs):
            plan = original(*args, **kwargs)
            plan["provider"] = "opencode"
            return plan
        runner = Mock()
        with patch.object(self.accounts, "launch_plan", side_effect=changed_provider):
            with self.assertRaisesRegex(integration.TeamLaunchError, "provider changed"):
                integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner)
        runner.assert_not_called()
        self.assertFalse(any(args[:2] == ["pane", "report-metadata"] for args in self.cli.calls))
        self.assertTrue(self.ticket_path(token).with_suffix(".claimed").exists())

    def test_restored_or_repeated_ticket_never_launches_another_agent(self):
        self.create()
        token, env = self.launch_env()
        runner = Mock(return_value=0)
        integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner)
        with self.assertRaisesRegex(integration.TeamLaunchError, "already used"):
            integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner)
        runner.assert_called_once()

    def test_ticket_cannot_move_to_another_workspace_socket_or_plugin(self):
        self.create()
        token, env = self.launch_env()
        for key, value in (("HERDR_WORKSPACE_ID", "w43"), ("HERDR_PANE_ID", "w43:p2"),
                           ("HERDR_SOCKET_PATH", str(self.root / "other.sock")),
                           ("HERDR_PLUGIN_ID", "other-plugin"), ("HERDR_PLUGIN_ENTRYPOINT_ID", "center")):
            with self.subTest(key=key), self.assertRaises(integration.TeamLaunchError):
                integration.claim_ticket(self.accounts, token, {**env, key: value})
        self.assertFalse(self.ticket_path(token).with_suffix(".claimed").exists())

    def test_expired_or_malformed_ticket_cannot_launch(self):
        self.create()
        token, env = self.launch_env()
        ticket = json.loads(self.ticket_path(token).read_text())
        ticket["created"] = time.time() - 601
        self.ticket_path(token).write_text(json.dumps(ticket), encoding="utf-8")
        for bad in (token, "../../escape", "not-a-ticket", ""):
            with self.assertRaises((ValueError, OSError)):
                integration.claim_ticket(self.accounts, bad, env)

    def test_changed_team_or_account_rejected_before_ticket_consumption(self):
        result = self.create()
        token, env = self.launch_env()
        config_path = Path(result["team_dir"]) / "config.json"
        original = config_path.read_text(encoding="utf-8")
        config = json.loads(original)
        config["roles"]["lead"]["cwd"] = str(self.project)
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(integration.TeamLaunchError):
            integration.claim_ticket(self.accounts, token, env)
        config_path.write_text(original, encoding="utf-8")
        self.accounts.profiles[0]["home"] = str(self.root / "replaced-home")
        with self.assertRaises(integration.TeamLaunchError):
            integration.claim_ticket(self.accounts, token, env)
        self.assertFalse(self.ticket_path(token).with_suffix(".claimed").exists())

    def test_runner_never_takes_over_an_existing_agent_or_wrong_directory(self):
        for setting, value in (("agent", "codex"), ("cwd", str(self.root))):
            team = "case-" + setting
            self.cli = FakeCLI()
            self.create(self.plan(team=team))
            token, env = self.launch_env()
            self.cli.panes[env["HERDR_PANE_ID"]][setting] = value
            runner = Mock()
            with self.assertRaises(integration.TeamLaunchError):
                integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner)
            runner.assert_not_called()
            # A failed claimed attempt is never silently replayed.
            self.assertTrue(self.ticket_path(token).with_suffix(".claimed").exists())

    def test_runner_failure_remains_claimed_and_preserves_working_directory(self):
        self.create()
        token, env = self.launch_env()
        runner = Mock(side_effect=OSError("synthetic native launch failure"))
        cwd = Path.cwd()
        with self.assertRaises(OSError):
            integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner)
        self.assertEqual(Path.cwd(), cwd)
        with self.assertRaises(integration.TeamLaunchError):
            integration.run_ticket(self.accounts, token, cli=self.cli, environ=env, runner=runner)
        self.assertEqual(runner.call_count, 1)


if __name__ == "__main__":
    unittest.main()
