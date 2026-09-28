"""Team service tests use synthetic metadata and never invoke a live creator."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from catalog import get_template
from integration import TeamLaunchError
from scaffold import TeamPlanError
from service import TeamError, TeamService


class PassiveAccounts:
    def __init__(self, root):
        self.calls = []
        self.state = {"profiles": [
            {"id": "work", "label": "Work", "provider": "codex", "home": str(root / "never-open-auth-home"),
             "env": {"SECRET": "private-env-value"}, "auth": "private-auth-value",
             "status": {"access_token": "private-token-value"}, "configuration_error": None},
            {"id": "review", "label": "Review", "provider": "codex", "home": str(root / "never-open-review-home")},
            {"id": "open", "label": "Open", "provider": "opencode", "home": str(root / "never-open-opencode-home")},
            {"id": "broken", "label": "Broken", "provider": "codex", "configuration_error": "repair-needed"},
            {"id": "other", "label": "Other", "provider": "claude"},
        ], "defaults": {"default_profile": "work"}}
        self.catalogs = {
            "work": {"models": [{"id": "codex-small", "label": "Codex small"}, {"id": "codex-large", "label": "Codex large"}],
                     "default_model": "codex-small", "notice": "Cached models."},
            "review": {"models": [{"id": "codex-large", "label": "Codex large"}], "default_model": None, "notice": ""},
            "open": {"models": [{"id": "opencode/zen-model", "label": "Zen model"}], "default_model": None, "notice": "Connected models."},
        }

    def list(self):
        self.calls.append("list")
        return deepcopy(self.state)

    def models(self, profile_id):
        self.calls.append(("models", profile_id))
        return deepcopy(self.catalogs[profile_id])

    def __getattr__(self, name):
        raise AssertionError(f"Unexpected account action: {name}")


class TeamServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watchtower-team-service-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "existing project"
        self.project.mkdir()
        (self.project / "keep.txt").write_text("Existing unrelated work", encoding="utf-8")
        self.accounts = PassiveAccounts(self.root)
        self.creator = Mock(return_value={"state": "launch_requested", "workspace_id": "w12",
            "roles": [{"id": "assistant", "pane_id": "w12:p1", "tab_id": "t1", "state": "launch_requested"}],
            "notice": "Agent launches requested; readiness has not yet been confirmed."})
        self.service = TeamService(accounts=self.accounts, creator=self.creator)

    def prepare(self, template="development"):
        roles = get_template(template)["roles"]
        return self.service.prepare(template, "New demo", self.project,
            {role["id"]: "review" if role["id"] == "reviewer" else "work" for role in roles})

    def test_templates_are_passive_copies_with_explicit_review_policy(self):
        templates = self.service.templates()
        self.assertEqual({t["id"] for t in templates}, {"quick-task", "research", "development"})
        self.assertEqual(self.accounts.calls, [])
        self.creator.assert_not_called()
        for item in templates:
            if item["id"] == "quick-task":
                self.assertIn("not required", item["completion_policy"])
            else:
                self.assertIn("independent review required", item["completion_policy"])
                self.assertIn("Global review is optional", item["completion_policy"])
        templates[0]["roles"][0]["label"] = "mutated UI label"
        self.assertNotEqual(self.service.templates()[0]["roles"][0]["label"], "mutated UI label")

    def test_profiles_are_passive_allowlisted_metadata_without_account_secrets(self):
        with patch.object(Path, "open", side_effect=AssertionError("Do not read authentication files")):
            profiles = self.service.profiles()
        self.assertEqual(profiles, {"profiles": [dict(id="work", label="Work", provider="codex"),
                                                dict(id="review", label="Review", provider="codex"),
                                                dict(id="open", label="Open", provider="opencode")],
                                    "default_profile": "work"})
        for value in ("private-env-value", "private-auth-value", "private-token-value", "never-open", "repair-needed"):
            self.assertNotIn(value, json.dumps(profiles))
        self.assertEqual(self.accounts.calls, ["list"])
        self.creator.assert_not_called()

    def test_prepare_only_checks_project_and_retains_exact_assignments_for_all_templates(self):
        before = (self.project / "keep.txt").stat()
        for template in ("quick-task", "research", "development"):
            with self.subTest(template=template):
                with patch.object(Path, "open", side_effect=AssertionError("Prepare cannot read file content")), \
                        patch.object(Path, "mkdir", side_effect=AssertionError("Prepare cannot create directories")), \
                        patch.object(Path, "write_text", side_effect=AssertionError("Prepare cannot write files")), \
                        patch.object(Path, "write_bytes", side_effect=AssertionError("Prepare cannot write files")), \
                        patch("subprocess.run", side_effect=AssertionError("Prepare cannot invoke tools")), \
                        patch("subprocess.Popen", side_effect=AssertionError("Prepare cannot start agents")):
                    public = self.prepare(template)
                expected = {role["id"]: "review" if role["id"] == "reviewer" else "work"
                            for role in get_template(template)["roles"]}
                self.assertEqual({role["role"]: role["profile_id"] for role in public["roles"]}, expected)
                self.assertTrue(all(role["provider"] == "codex" for role in public["roles"]))
                self.assertEqual(public["project"], str(self.project.resolve()))
                self.assertEqual(public["label"], "New demo")
                self.assertTrue(public["token"])
                for value in ("private-env-value", "private-auth-value", "private-token-value", "never-open"):
                    self.assertNotIn(value, json.dumps(public))
                self.creator.assert_not_called()
        after = (self.project / "keep.txt").stat()
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["existing project"])
        self.assertEqual((self.project / "keep.txt").read_text(encoding="utf-8"), "Existing unrelated work")

    def test_bad_or_extra_assignments_fail_before_creator(self):
        for assignments in ({"lead": "work"}, {"lead": "work", "worker": "work", "reviewer": "review", "extra": "work"},
                            {"lead": "work", "worker": "other", "reviewer": "review"},
                            {"lead": "work", "worker": "broken", "reviewer": "review"}):
            with self.subTest(assignments=assignments), self.assertRaises(TeamError):
                self.service.prepare("development", "Demo", self.project, assignments)
        self.creator.assert_not_called()
        self.assertEqual(self.service._plans, {})

    def test_public_plan_mutation_cannot_change_the_created_team(self):
        public = self.prepare()
        token = public["token"]
        public["label"] = "Unreviewed name"
        public["roles"][0]["profile_id"] = "other"
        self.service.create(token)
        accounts, prepared = self.creator.call_args.args
        self.assertIs(accounts, self.accounts)
        self.assertEqual(prepared["workspace_label"], "New demo")
        self.assertEqual(prepared["roles"]["lead"]["profile_id"], "work")
        self.assertEqual(prepared["roles"]["reviewer"]["profile_id"], "review")
        self.assertEqual(prepared["agent_permissions"], {"mode": "inherit"})
        for value in ("private-env-value", "private-auth-value", "private-token-value", "never-open"):
            self.assertNotIn(value, json.dumps(prepared))

    def test_launch_requested_maps_to_created_without_claiming_agent_readiness(self):
        public = self.prepare("quick-task")
        outcome = self.service.create(public["token"])
        self.assertEqual(outcome["state"], "created")
        self.assertEqual(outcome["workspace_id"], "w12")
        self.assertEqual(outcome["panes"], [dict(role="assistant", pane_id="w12:p1", tab_id="t1", state="launch_requested")])
        self.assertEqual(outcome["message"], self.creator.return_value["notice"])
        self.assertIn("not yet been confirmed", outcome["message"])
        with self.assertRaisesRegex(TeamError, "already used or expired"):
            self.service.create(public["token"])
        self.creator.assert_called_once()

    def test_partial_and_failed_creator_results_also_consume_token_and_preserve_ids(self):
        for status, expected in (("unconfirmed", "partial"), ("partial", "partial"), ("failed", "failed")):
            with self.subTest(status=status):
                creator = Mock(return_value={"state": status, "workspace_id": "w13",
                                            "roles": [{"id": "worker", "pane_id": "w13:p2", "state": "unconfirmed"}],
                                            "notice": "Inspect the created workspace before trying again."})
                service = TeamService(self.accounts, creator)
                plan = service.prepare("quick-task", "Demo", self.project, {"assistant": "work"})
                outcome = service.create(plan["token"])
                self.assertEqual(outcome["state"], expected)
                self.assertEqual(outcome["workspace_id"], "w13")
                self.assertEqual(outcome["panes"][0]["pane_id"], "w13:p2")
                with self.assertRaises(TeamError):
                    service.create(plan["token"])
                creator.assert_called_once()

    def test_creator_exception_is_unconfirmed_not_retryable_and_does_not_leak_details(self):
        public = self.prepare("quick-task")
        self.creator.side_effect = RuntimeError("private-launch-environment")
        outcome = self.service.create(public["token"])
        self.assertEqual(outcome["state"], "partial")
        self.assertIn("unconfirmed", outcome["message"])
        self.assertNotIn("private-launch-environment", json.dumps(outcome))
        with self.assertRaises(TeamError):
            self.service.create(public["token"])
        self.creator.assert_called_once()

    def test_safe_preflight_and_claim_errors_keep_explanation_without_allowing_retries(self):
        errors = (
            TeamLaunchError("Store team metadata outside the selected project."),
            TeamLaunchError("This team creation is already in progress. It will not be repeated."),
            TeamPlanError("Choose an existing absolute project directory."),
        )
        for error in errors:
            with self.subTest(message=error.message):
                creator = Mock(side_effect=error)
                service = TeamService(self.accounts, creator)
                public = service.prepare("quick-task", "Demo", self.project, {"assistant": "work"})
                outcome = service.create(public["token"])
                self.assertEqual(outcome, {"state": "partial", "workspace_id": None,
                                           "panes": [], "message": error.message})
                with self.assertRaisesRegex(TeamError, "already used or expired"):
                    service.create(public["token"])
                creator.assert_called_once()

    def test_invalid_or_other_service_token_never_calls_creator(self):
        plan = self.prepare("quick-task")
        for token in ("unknown-token", ""):
            with self.assertRaises(TeamError):
                self.service.create(token)
        other = TeamService(self.accounts, self.creator)
        with self.assertRaises(TeamError):
            other.create(plan["token"])
        self.creator.assert_not_called()

    def test_account_failures_are_sanitized_before_ui_display(self):
        with patch.object(self.accounts, "list", side_effect=RuntimeError("private-account-diagnostic")):
            with self.assertRaises(TeamError) as profiles:
                self.service.profiles()
            with self.assertRaises(TeamError) as prepare:
                self.prepare()
        self.assertNotIn("private-account-diagnostic", str(profiles.exception))
        self.assertNotIn("private-account-diagnostic", str(prepare.exception))
        self.creator.assert_not_called()

    def test_model_catalog_only_exposes_public_fields(self):
        self.accounts.catalogs["open"].update(auth="private-auth-value", home="private-home", env={"TOKEN": "private-env-value"})
        self.accounts.catalogs["open"]["models"][0].update(api_key="private-key-value", auth={"token": "private-token-value"})
        catalog = self.service.models("open")
        self.assertEqual(catalog, {"models": [{"id": "opencode/zen-model", "label": "Zen model"}],
                                   "default_model": None, "notice": "Connected models."})
        self.assertEqual(self.accounts.calls, [("models", "open")])
        self.creator.assert_not_called()

    def test_catalog_failures_return_safe_default_option_and_reject_explicit_models(self):
        with patch.object(self.accounts, "models", side_effect=RuntimeError("private-provider-diagnostic")):
            result = self.service.models("open")
            self.assertEqual(result["models"], [])
            self.assertIsNone(result["default_model"])
            self.assertIn("Provider default", result["notice"])
            self.assertNotIn("private-provider-diagnostic", str(result))
            with self.assertRaisesRegex(TeamError, "catalog"):
                self.service.prepare("quick-task", "Demo", self.project, {"assistant": "open"},
                                     {"assistant": "opencode/zen-model"})
            default = self.service.prepare("quick-task", "Demo", self.project, {"assistant": "open"},
                                           {"assistant": None})
        self.assertIsNone(default["roles"][0]["model"])
        self.creator.assert_not_called()

    def test_mixed_provider_exact_models_are_detached_and_checked_once_per_account(self):
        assignments = {"lead": "work", "worker": "open", "reviewer": "work"}
        models = {"lead": "codex-small", "worker": "opencode/zen-model", "reviewer": "codex-large"}
        public = self.service.prepare("development", "Mixed team", self.project, assignments, models)
        self.assertEqual({r["role"]: (r["provider"], r["model"]) for r in public["roles"]},
                         {"lead": ("codex", "codex-small"), "worker": ("opencode", "opencode/zen-model"),
                          "reviewer": ("codex", "codex-large")})
        self.assertEqual(self.accounts.calls.count(("models", "work")), 1)
        self.assertEqual(self.accounts.calls.count(("models", "open")), 1)
        models["worker"] = "opencode/other-model"
        public["roles"][1]["model"] = "opencode/unreviewed-model"
        self.service.create(public["token"])
        private = self.creator.call_args.args[1]
        self.assertEqual(private["roles"]["worker"]["model"], "opencode/zen-model")
        self.assertEqual(private["roles"]["worker"]["provider"], "opencode")

    def test_invalid_and_unavailable_models_reject_before_create_or_filesystem_changes(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        for model in ("--help", "opencode/model with spaces", "opencode/model\n--flag", "model-without-provider",
                      "opencode/not-in-catalog", "opencode/" + "x" * 129):
            with self.subTest(model=model), self.assertRaises(TeamError):
                self.service.prepare("quick-task", "Demo", self.project, {"assistant": "open"}, {"assistant": model})
        with self.assertRaises(TeamError):
            self.service.prepare("quick-task", "Demo", self.project, {"assistant": "work"}, {"assistant": "opencode/zen-model"})
        with self.assertRaises(TeamError):
            self.service.prepare("quick-task", "Demo", self.project, {"assistant": "work"}, {"unassigned": "codex-small"})
        self.assertEqual(self.service._plans, {})
        self.assertEqual(sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")), before)
        self.creator.assert_not_called()

    def test_explicit_provider_defaults_need_no_catalog_and_remain_null_in_plan(self):
        with patch.object(self.accounts, "models", side_effect=AssertionError("Defaults need no model catalog")):
            public = self.service.prepare("development", "Mixed defaults", self.project,
                {"lead": "work", "worker": "open", "reviewer": "review"},
                {"lead": None, "worker": None, "reviewer": None})
        self.assertTrue(all("model" in role and role["model"] is None for role in public["roles"]))
        self.creator.assert_not_called()

    def test_launcher_runtime_import_still_starts_fresh_teams_view_not_accounts(self):
        teams = Path(__file__).resolve().parent
        fake_python = self.root / "state/accounts/venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        fake_python.parent.mkdir(parents=True)
        fake_python.write_bytes(b"synthetic marker; never execute")
        original_path = list(sys.path)
        try:
            spec = importlib.util.spec_from_file_location("synthetic_team_launcher", teams / "launcher.py")
            launcher = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(launcher)
            with patch.dict(os.environ, {"WATCHTOWER_HOME": str(self.root), "PYTHONHOME": "untrusted-home",
                                         "PYTHONPATH": "untrusted-search", "VIRTUAL_ENV": "untrusted-venv"}), \
                    patch.object(sys, "argv", [str(teams / "launcher.py")]), \
                    patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(launcher.main(), 0)
            self.assertEqual(run.call_count, 2)  # Bounded runtime probe, then fresh chooser process.
            command = run.call_args.args[0]
            self.assertEqual(command, [str(fake_python), "-B", str(teams / "view.py")])
            self.assertEqual(run.call_args.kwargs["cwd"], teams)
            for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
                self.assertNotIn(key, run.call_args.kwargs["env"])
            self.assertEqual(run.call_args.kwargs["env"]["WATCHTOWER_HOME"], str(self.root))
        finally:
            # Importing the Accounts runtime helper adds its source to sys.path;
            # this is only in the launcher process, not the fresh Teams view.
            sys.path[:] = original_path


if __name__ == "__main__":
    unittest.main()
