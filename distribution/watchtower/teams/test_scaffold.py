"""Only synthetic temporary projects; no runtime, account homes or agents opened."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import shlex
from types import SimpleNamespace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from catalog import get_template
import scaffold as module
from scaffold import TeamPlanError, plan, scaffold


class ScaffoldTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project with spaces"
        self.project.mkdir()
        (self.project / "keep.txt").write_text("Existing unrelated work", encoding="utf-8")
        self.state = self.base / "team-state"
        self.state.mkdir()
        self.profiles = [
            {"id": "work-account", "provider": "codex", "home": str(self.base / "must-not-open-auth"),
             "env": {"SECRET": "private-environment"}, "auth": "private-authentication", "label": "private-account-label"},
            {"id": "review-account", "provider": "codex", "home": str(self.base / "must-not-open-review-auth")},
        ]

    def prepared(self, template="development", **changes):
        assignments = {role["id"]: "review-account" if role["id"] == "reviewer" else "work-account"
                       for role in get_template(template)["roles"]}
        values = dict(team_id="demo-team", label="Demo Team", project_dir=self.project,
                      template_id=template, assignments=assignments, profiles=self.profiles)
        values.update(changes)
        return plan(**values)

    def target(self):
        return self.state / "demo-team"

    def runtime(self):
        return {key: str(self.base / (key + " tool")) for key in
                ("herdr", "radio_home", "radio_entrypoint", "watchtower_home", "workflow_cli", "python")}

    def test_plan_reads_no_account_files_and_retains_only_profile_references(self):
        with patch.object(Path, "open", side_effect=AssertionError("No account/config files may be opened")):
            prepared = self.prepared()
        text = json.dumps(prepared)
        for private in ("private-authentication", "private-environment", "private-account-label", "must-not-open"):
            self.assertNotIn(private, text)
        self.assertEqual(prepared["roles"]["lead"]["profile_id"], "work-account")
        self.assertEqual(prepared["roles"]["reviewer"]["profile_id"], "review-account")
        self.assertEqual(prepared["agent_permissions"], {"mode": "inherit"})
        self.assertFalse(self.target().exists())
        self.assertEqual(list(self.project.iterdir()), [self.project / "keep.txt"])

    def test_template_and_assignments_are_validated_before_any_write(self):
        cases = [dict(team_id="../escape"), dict(team_id="CON"), dict(team_id="con"),
                 dict(team_id="-bad"), dict(label="Bad\nlabel"), dict(template_id="unknown"),
                 dict(project_dir="relative"), dict(project_dir=self.base / "missing"),
                 dict(assignments={"lead": "work-account"}),
                 dict(assignments={"lead": "work-account", "worker": "work-account", "reviewer": "missing"}),
                 dict(workspace_id="w0")]
        for changed in cases:
            with self.subTest(changed=changed), self.assertRaises(TeamPlanError):
                self.prepared(**changed)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_unsupported_and_ambiguous_or_invalid_profile_metadata_rejected(self):
        for profiles in (
            [{"id": "work-account", "provider": "claude"}],
            [{"id": "work-account", "provider": "gemini"}],
            [{"id": "work-account", "provider": "codex", "configuration_error": "profile_changed"}],
            [{"id": "work-account", "provider": "codex"}, {"id": "WORK-account", "provider": "codex"}],
            [{"id": "../unsafe", "provider": "codex"}],
            [None],
        ):
            with self.subTest(profiles=profiles), self.assertRaises(TeamPlanError):
                self.prepared("quick-task", profiles=profiles)
        with self.assertRaisesRegex(TeamPlanError, "Codex and OpenCode profiles only"):
            self.prepared("quick-task", profiles=[{"id": "work-account", "provider": "claude"}])

    def test_mixed_provider_model_choices_survive_scaffold_without_account_data(self):
        self.profiles[1]["provider"] = "opencode"
        prepared = self.prepared(models={"lead": "gpt-6-astra", "reviewer": "opencode/deepseek-v4-flash"})
        self.assertEqual(prepared["roles"]["reviewer"]["provider"], "opencode")
        self.assertEqual(prepared["roles"]["reviewer"]["model"], "opencode/deepseek-v4-flash")
        self.assertIsNone(prepared["roles"]["worker"]["model"])
        config = scaffold(prepared, self.target(), workspace_id="w2", runtime=self.runtime())
        self.assertEqual(config["roles"]["lead"]["model"], "gpt-6-astra")
        self.assertEqual(config["roles"]["reviewer"]["model"], "opencode/deepseek-v4-flash")
        self.assertNotIn("private-authentication", json.dumps(config))
        reviewer_brief = Path(config["roles"]["reviewer"]["entrypoint"]).read_text(encoding="utf-8")
        self.assertIn("Independent review", reviewer_brief)
        self.assertNotIn("Codex", reviewer_brief)
        self.assertTrue(config["review"]["required"])

    def test_model_defaults_preserve_legacy_plan_shape(self):
        prepared = self.prepared()
        self.assertEqual(prepared, self.prepared(models=None))
        self.assertTrue(all("model" not in role for role in prepared["roles"].values()))
        config = scaffold(prepared, self.target(), workspace_id="w2")
        self.assertTrue(all("model" not in role for role in config["roles"].values()))
        explicit = self.prepared(models={})
        self.assertTrue(all(role["model"] is None for role in explicit["roles"].values()))

    def test_model_syntax_and_unknown_roles_rejected_before_writes(self):
        self.profiles[1]["provider"] = "opencode"
        cases = [[], {"unknown-role": None}, {"lead": "openai/gpt-6"}, {"lead": "--model"},
                 {"lead": "gpt-6\nignored"}, {"lead": 7}, {"lead": "a" * 129},
                 {"reviewer": "deepseek-v4-flash"}, {"reviewer": "/model"},
                 {"reviewer": "opencode/"}, {"reviewer": "opencode/model;command"},
                 {"reviewer": " opencode/model"}, {"reviewer": "opencode/model\x00"}]
        for models in cases:
            with self.subTest(models=models), self.assertRaises(TeamPlanError):
                self.prepared(models=models)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_revalidation_rejects_invalid_or_inconsistently_recorded_models(self):
        for change in (lambda p: p["roles"]["reviewer"].update(model="opencode/not-a-codex-model"),
                       lambda p: p["roles"]["worker"].pop("model")):
            prepared = self.prepared(models={"lead": "gpt-6-astra"})
            change(prepared)
            with self.assertRaises(TeamPlanError):
                scaffold(prepared, self.target(), workspace_id="w2")
        self.assertFalse(self.target().exists())

    def test_assignment_ids_follow_accounts_case_insensitive_lookup(self):
        prepared = self.prepared("quick-task", assignments={"assistant": "WORK-ACCOUNT"})
        self.assertEqual(prepared["roles"]["assistant"]["profile_id"], "work-account")
        self.profiles[0]["provider"] = "changed-after-plan"
        self.assertEqual(prepared["roles"]["assistant"]["provider"], "codex")

    def test_development_roots_roles_and_real_workflow_metadata_are_separate(self):
        before = (self.project / "keep.txt").stat().st_mtime_ns
        config = scaffold(self.prepared(), self.target(), workspace_id="w12", runtime=self.runtime())
        self.assertEqual(config["workspace_id"], "w12")
        self.assertEqual(config["frequency_display"], "012")
        self.assertEqual(config["roles"]["worker"]["cwd"], str(self.project))
        for role_id in ("lead", "reviewer"):
            self.assertEqual(config["roles"][role_id]["cwd"], str(self.target()))
        self.assertEqual(config["project_path"], str(self.project))
        self.assertEqual(config["review"]["enforcement"], "workflow_cli")
        self.assertEqual(config["review"]["global_mode"], "on_demand")
        self.assertIsNone(config["review"]["global_reviewer"])
        self.assertEqual(config["agent_permissions"], {"mode": "inherit"})
        self.assertEqual(set(p.name for p in self.target().iterdir()),
                         {"config.json", "BRIEF.md", "roles", "work", "outputs", "work-items", "reviews", "reports"})
        for role_id, role in config["roles"].items():
            self.assertEqual(Path(role["entrypoint"]), self.target() / "roles" / (role_id + ".md"))
            self.assertTrue(Path(role["entrypoint"]).is_file())
        self.assertEqual(json.loads((self.target() / "config.json").read_text(encoding="utf-8")), config)
        self.assertEqual((self.project / "keep.txt").read_text(encoding="utf-8"), "Existing unrelated work")
        self.assertEqual((self.project / "keep.txt").stat().st_mtime_ns, before)
        self.assertEqual(list(self.project.iterdir()), [self.project / "keep.txt"])

    def test_research_and_quick_have_correct_working_directories_and_review_scope(self):
        for template in ("research", "quick-task"):
            with self.subTest(template=template):
                target = self.state / template
                config = scaffold(self.prepared(template), target, workspace_id="w2")
                if template == "research":
                    self.assertEqual(config["roles"]["researcher"]["cwd"], str(target / "work"))
                    self.assertEqual(config["roles"]["reviewer"]["cwd"], str(target))
                    self.assertEqual(config["review"]["lead"], "researcher")
                    self.assertTrue(config["review"]["required"])
                else:
                    self.assertEqual(list(config["roles"]), ["assistant"])
                    self.assertEqual(config["roles"]["assistant"]["cwd"], str(self.project))
                    self.assertFalse(config["review"]["required"])
                    self.assertIsNone(config["review"]["local_reviewer"])
                self.assertEqual(config["review"]["enforcement"], "instructions_only")
                self.assertIn("No workflow CLI is bound", (target / "BRIEF.md").read_text(encoding="utf-8"))

    def test_briefs_keep_wait_start_permissions_and_independent_workflow_explicit(self):
        config = scaffold(self.prepared(), self.target(), workspace_id="w2", runtime=self.runtime())
        brief = (self.target() / "BRIEF.md").read_text(encoding="utf-8")
        self.assertIn("wait for an\nexplicit user task", brief)
        self.assertIn("not OS security isolation", brief)
        self.assertIn("does not request Full Access", brief)
        self.assertIn("does not automatically review every action", brief)
        role_text = {role_id: Path(role["entrypoint"]).read_text(encoding="utf-8") for role_id, role in config["roles"].items()}
        self.assertIn("begin --team-dir", role_text["lead"])
        self.assertIn("complete --team-dir", role_text["lead"])
        self.assertNotIn("submit --team-dir", role_text["lead"])
        self.assertIn("submit --team-dir", role_text["worker"])
        self.assertNotIn("result --team-dir", role_text["worker"])
        self.assertIn("result --team-dir", role_text["reviewer"])
        self.assertIn("CHANGES_REQUIRED or BLOCKED", role_text["reviewer"])
        self.assertIn("JSON **file paths**", role_text["lead"])
        self.assertIn(str(self.target() / "BRIEF.md"), role_text["worker"])
        self.assertIn("Do not edit implementation", role_text["reviewer"])
        self.assertNotIn("approval_policy", json.dumps(config))

    def test_posix_workflow_commands_round_trip_paths_with_shell_metacharacters(self):
        config = scaffold(self.prepared(), self.target(), workspace_id="w2", runtime=self.runtime())
        config.update(python="/opt/runtime with spaces/python3",
                      workflow_cli="/opt/watchtower's tools/workflow.py",
                      team_root="/tmp/team $HOME; (example)")
        with patch.object(module, "os", SimpleNamespace(name="posix")):
            for role in ("lead", "worker", "reviewer"):
                text = module._workflow_instructions(config, role)
                self.assertIn("```sh", text)
                self.assertNotIn("```powershell", text)
                for block in text.split("```sh\n")[1:]:
                    command = block.split("\n```", 1)[0].splitlines()[0]
                    argv = shlex.split(command)
                    self.assertEqual(argv[:3], [config["python"], "-B", config["workflow_cli"]])
                    self.assertEqual(argv[argv.index("--team-dir") + 1], config["team_root"])

    def test_existing_files_and_empty_directory_are_never_overwritten(self):
        prepared = self.prepared()
        self.target().mkdir()
        with self.assertRaises(TeamPlanError):
            scaffold(prepared, self.target(), workspace_id="w2")
        self.assertEqual(list(self.target().iterdir()), [])
        sentinel = self.target() / "keep.txt"
        sentinel.write_text("Existing team")
        with self.assertRaises(TeamPlanError):
            scaffold(prepared, self.target(), workspace_id="w2")
        self.assertEqual(sentinel.read_text(), "Existing team")

    def test_no_project_metadata_writes_even_through_normalized_alias(self):
        for target in (self.project, self.project / "new-team", self.project / ".." / self.project.name / "new-team"):
            with self.subTest(target=target), self.assertRaises(TeamPlanError):
                scaffold(self.prepared(), target, workspace_id="w2")
        self.assertEqual(list(self.project.iterdir()), [self.project / "keep.txt"])

    def test_runtime_whitelist_rejects_auth_env_and_relative_paths(self):
        for runtime in ({"auth": "private"}, {"env": {"SECRET": "private"}},
                        {"agent_permissions": {"approval_policy": "never"}}, {"herdr": "relative.exe"}):
            with self.subTest(runtime=runtime), self.assertRaises(TeamPlanError):
                scaffold(self.prepared(), self.target(), workspace_id="w2", runtime=runtime)
        self.assertFalse(self.target().exists())

    def test_modified_plan_or_different_binding_is_rejected_without_writes(self):
        for mutation in (
            lambda p: p["agent_permissions"].update(approval_policy="never"),
            lambda p: p["roles"]["reviewer"].update(scope="Edit implementation"),
            lambda p: p["review"].update(required=False),
            lambda p: p.update(workspace_id="w3"),
        ):
            prepared = self.prepared()
            mutation(prepared)
            with self.assertRaises(TeamPlanError):
                scaffold(prepared, self.target(), workspace_id="w2")
        self.assertFalse(self.target().exists())

    def test_failed_staging_leaves_no_destination_and_cleans_only_owned_stage(self):
        unrelated = self.state / ".team-stage-unrelated"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("Keep me")
        with patch("scaffold._publish_new", side_effect=OSError("Synthetic failure")):
            with self.assertRaises(TeamPlanError):
                scaffold(self.prepared(), self.target(), workspace_id="w2")
        self.assertFalse(self.target().exists())
        self.assertEqual(list(self.state.iterdir()), [unrelated])
        self.assertEqual((unrelated / "keep.txt").read_text(), "Keep me")

    def test_racing_empty_destination_is_not_replaced_by_publish(self):
        original = module._publish_new

        def collision(staging, destination):
            destination.mkdir()
            original(staging, destination)

        with patch("scaffold._publish_new", side_effect=collision), self.assertRaises(TeamPlanError):
            scaffold(self.prepared(), self.target(), workspace_id="w2")
        self.assertTrue(self.target().is_dir())
        self.assertEqual(list(self.target().iterdir()), [])
        self.assertEqual(list(self.state.iterdir()), [self.target()])

    def test_concurrent_scaffolds_publish_exactly_one_complete_team(self):
        original = module._publish_new
        barrier = threading.Barrier(2)

        def together(staging, destination):
            barrier.wait(timeout=5)
            original(staging, destination)

        def create(workspace):
            try:
                return scaffold(self.prepared(), self.target(), workspace_id=workspace)
            except TeamPlanError:
                return None

        with patch("scaffold._publish_new", side_effect=together), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(create, ["w2", "w3"]))
        successful = [result for result in results if result is not None]
        self.assertEqual(len(successful), 1)
        persisted = json.loads((self.target() / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(persisted, successful[0])
        self.assertEqual(list(self.state.iterdir()), [self.target()])

    def test_dangling_destination_symlink_is_not_followed(self):
        other = self.base / "outside-destination"
        try:
            self.target().symlink_to(other, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is unavailable to this test account")
        with self.assertRaises(TeamPlanError):
            scaffold(self.prepared(), self.target(), workspace_id="w2")
        self.assertTrue(self.target().is_symlink())
        self.assertFalse(other.exists())


if __name__ == "__main__":
    unittest.main()
