"""Accounts tests use temporary homes, an isolated Radio ledger and fake auth."""

from copy import deepcopy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import backend
from backend import AccountError, AccountService, default_home, plan_environment
from account_reader import CodexAccountReader, _quota_windows, _mask_email, _email_domain, _cli_command


class AccountsTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reader = Mock()
        self.service = AccountService(self.root / "watchtower", self.root / "radio", reader=self.reader)
        self.defaults = {name: self.root / "existing" / name for name in backend.PROVIDERS}
        self.defaults["gemini"] /= ".gemini"
        self.service._default_home = lambda provider: self.defaults[provider]

    def add(self, name="work", provider="codex"):
        return self.service.add(name, provider, label="Work account")

    def sql(self, statement, values=()):
        with closing(sqlite3.connect(self.root / "radio/radio.db")) as connection:
            connection.execute(statement, values)
            connection.commit()

    def test_listing_does_not_create_state(self):
        self.assertEqual(self.service.list()["profiles"], [])
        self.assertFalse(self.service.home.exists())
        self.assertFalse(self.service.radio_home.exists())

    def test_existing_auth_is_discovered_without_reading_credentials(self):
        self.defaults["codex"].mkdir(parents=True)
        secret = self.defaults["codex"] / "auth.json"
        secret.write_text("credential-must-never-appear")
        original = Path.read_text

        def guarded(path, *args, **kwargs):
            self.assertNotEqual(path, secret)
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", guarded):
            result = self.service.list()
        self.assertEqual(result["profiles"][0]["status"]["state"], "credential_present")
        self.assertNotIn("credential-must-never-appear", json.dumps(result))
        self.assertFalse(self.service.home.exists())

    def test_add_creates_isolated_file_store_and_registers_radio(self):
        profile = self.add()
        self.assertEqual(profile["radio_account"], "work")
        self.assertEqual(profile["source"], "managed")
        self.assertEqual((Path(profile["home"]) / "config.toml").read_text(), 'cli_auth_credentials_store = "file"\n')
        self.assertFalse((Path(profile["home"]) / "auth.json").exists())
        self.assertEqual(profile["status"]["state"], "not_connected")
        self.assertFalse(profile["custom_environment"])

    def test_add_opencode_isolates_all_xdg_roots(self):
        profile = self.add("zen", "opencode")
        env = self.service._profile_env(profile)
        self.assertEqual(set(env), {"XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"})
        self.assertTrue(all(Path(value).is_dir() for value in env.values()))
        self.assertFalse(self.service.get("zen")["custom_environment"])

    def test_gemini_home_mapping_is_shared_by_radio_and_native_login(self):
        profile = self.add("google-work", "gemini")
        home = Path(profile["home"])
        self.assertEqual(home.name, ".gemini")
        self.assertEqual(home.parent.name, "gemini")
        expected = {"GEMINI_CLI_HOME": str(home.parent), "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "false",
                    **dict.fromkeys(backend._GEMINI_BLANK_ENV, "")}
        self.assertEqual(self.service._profile_env(profile), expected)
        self.assertFalse(profile["custom_environment"])
        with closing(sqlite3.connect(self.root / "radio/radio.db")) as connection:
            stored_home, overrides = connection.execute("SELECT home,env FROM accounts WHERE name='google-work'").fetchone()
        self.assertEqual(stored_home, str(home))
        self.assertEqual(json.loads(overrides), expected)
        with patch("backend.provider_command", return_value=["node.exe", "gemini.js"]), \
                patch.object(self.service, "_supports_gemini_home", return_value=True):
            plan = self.service.connect_plan("google-work")
        self.assertEqual(plan["argv"], ["node.exe", "gemini.js"])
        self.assertEqual(plan["cwd"], str(home))
        self.assertEqual(plan["env"], expected)
        self.assertNotIn("HOME", plan["env"])
        self.assertNotIn("USERPROFILE", plan["env"])
        self.assertFalse((home / "oauth_creds.json").exists())

    def test_claude_profile_isolated_native_home_and_login(self):
        profile = self.add("anthropic-work", "claude")
        self.assertEqual(Path(profile["home"]).name, "claude")
        self.assertFalse(profile["custom_environment"])
        self.assertFalse((Path(profile["home"]) / ".credentials.json").exists())
        with patch("backend.provider_command", return_value=["claude.exe"]), patch("backend.subprocess.run") as process:
            plan = self.service.connect_plan("anthropic-work")
        process.assert_not_called()
        self.assertEqual(plan["argv"], ["claude.exe", "auth", "login", "--claudeai"])
        self.assertEqual(plan["env"], {"CLAUDE_CONFIG_DIR": profile["home"]})
        self.assertEqual(plan["cwd"], profile["home"])

    def test_existing_gemini_and_claude_discovery_never_reads_credential_files(self):
        secrets = []
        for provider, filename in (("gemini", "oauth_creds.json"), ("claude", ".credentials.json")):
            self.defaults[provider].mkdir(parents=True)
            secret = self.defaults[provider] / filename
            secret.write_text("credential-must-never-appear")
            secrets.append(secret)
        original = Path.read_text

        def guarded(path, *args, **kwargs):
            self.assertNotIn(path, secrets)
            return original(path, *args, **kwargs)

        with patch.object(Path, "read_text", guarded):
            result = self.service.list()
        self.assertEqual({profile["provider"] for profile in result["profiles"]}, {"gemini", "claude"})
        self.assertTrue(all(profile["status"]["state"] == "credential_present" for profile in result["profiles"]))
        self.assertNotIn("credential-must-never-appear", json.dumps(result))
        self.assertFalse(self.service.home.exists())

    def test_existing_gemini_registration_uses_parent_override_and_preserves_auth(self):
        home = self.defaults["gemini"]
        home.mkdir(parents=True)
        (home / "oauth_creds.json").write_text("private-auth")
        profile = self.service.ensure_registered("gemini-default")
        self.assertFalse(profile["custom_environment"])
        self.assertEqual(self.service._profile_env(profile)["GEMINI_CLI_HOME"], str(home.parent))
        self.assertEqual((home / "oauth_creds.json").read_text(), "private-auth")

    def test_gemini_existing_home_must_be_actual_config_directory(self):
        target = self.root / "existing-wrong-home"
        target.mkdir()
        with self.assertRaises(AccountError) as error:
            self.service.add("google-work", "gemini", home=target)
        self.assertEqual(error.exception.code, "invalid_gemini_home")
        self.assertFalse(self.service.path.exists())

    def test_gemini_wrong_or_missing_radio_parent_override_is_blocked(self):
        profile = self.add("google-work", "gemini")
        for env in ({}, {"GEMINI_CLI_HOME": profile["home"]},
                    {"GEMINI_CLI_HOME": str(Path(profile["home"]).parent)},
                    {"GEMINI_CLI_HOME": str(Path(profile["home"]).parent), "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE": "true"}):
            with self.subTest(env=env):
                self.sql("UPDATE accounts SET env=? WHERE name='google-work'", (json.dumps(env),))
                self.assertTrue(self.service.get("google-work")["custom_environment"])
                with patch("backend.provider_command", return_value=["gemini.exe"]), \
                        patch.object(self.service, "_supports_gemini_home", return_value=True), self.assertRaises(AccountError):
                    self.service.launch_plan("google-work", "worker", "w9")

    def test_gemini_credential_overrides_in_radio_are_detected_without_exposing_values(self):
        profile = self.add("google-work", "gemini")
        env = self.service._profile_env(profile)
        env["GOOGLE_API_KEY"] = "private-credential"
        self.sql("UPDATE accounts SET env=? WHERE name='google-work'", (json.dumps(env),))
        profiles = self.service.list()
        self.assertTrue(profiles["profiles"][0]["custom_environment"])
        self.assertNotIn("private-credential", json.dumps(profiles))
        accounts, _ = self.service._ledger()
        self.assertNotIn("private-credential", json.dumps(accounts))

    def test_gemini_plan_blocks_project_env_auth_refill_without_rewriting_policy(self):
        profile = self.add("google-work", "gemini")
        plan = self.service._plan(profile, "launch", ["gemini.exe"])
        env = plan_environment(plan, {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": "policy/settings.json",
                                      "GEMINI_API_KEY": "inherited-secret"})
        self.assertEqual(env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"], "policy/settings.json")
        for key in backend._GEMINI_BLANK_ENV:
            self.assertIn(key, env)
            self.assertEqual(env[key], "")
        self.assertEqual(env["GEMINI_FORCE_ENCRYPTED_FILE_STORAGE"], "false")

    def test_gemini_old_or_unknown_cli_cannot_connect_or_launch_shared_login(self):
        self.add("google-work", "gemini")
        for output, code in ((b"0.1.12\n", 0), (b"unknown", 0), (b"0.61.0\n", 1)):
            self.service._gemini_isolation.clear()
            with patch("backend.provider_command", return_value=["gemini.exe"]), \
                    patch("backend.subprocess.run", return_value=subprocess.CompletedProcess([], code, output)):
                for operation in (lambda: self.service.connect_plan("google-work"),
                                  lambda: self.service.launch_plan("google-work", "worker", "w9")):
                    with self.assertRaises(AccountError) as error:
                        operation()
                    self.assertEqual(error.exception.code, "gemini_update_required")

    def test_gemini_supported_version_probe_is_bounded_noninteractive(self):
        with patch("backend.subprocess.run", return_value=subprocess.CompletedProcess([], 0, b"0.61.0\n")) as process:
            self.assertTrue(self.service._supports_gemini_home(["node.exe", "gemini.js"]))
        self.assertEqual(process.call_args.args[0], ["node.exe", "gemini.js", "--version"])
        self.assertEqual(process.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(process.call_args.kwargs["timeout"], 8)

    def test_gemini_refresh_only_reports_presence_and_no_quota(self):
        profile = self.add("google-work", "gemini")
        secret = Path(profile["home"]) / "oauth_creds.json"
        secret.write_text("secret")
        with patch("backend.subprocess.run") as process:
            result = self.service.refresh("google-work")["status"]
        process.assert_not_called()
        self.reader.read.assert_not_called()
        self.assertEqual(result["state"], "credential_present")
        self.assertEqual(result["error"], "provider_status_not_verified")
        self.assertEqual(result["quota_windows"], [])
        self.assertIsNone(result["balance"])
        self.assertNotIn("secret", json.dumps(result))

    def test_names_cannot_be_commands_or_paths(self):
        for name in ("../work", "x;y", "x&whoami", "x\ny", "", "x" * 49, "-work"):
            with self.subTest(name=name), self.assertRaises(AccountError):
                self.service.add(name, "codex")
        self.assertFalse(self.service.home.exists())

    def test_labels_cannot_inject_terminal_controls(self):
        with self.assertRaises(AccountError):
            self.service.add("work", "codex", "bad\x1b[2J")
        self.assertFalse((self.service.home / "state/accounts/profiles").exists())

    def test_duplicate_case_insensitive_names_and_home_rejected(self):
        profile = self.add()
        with self.assertRaises(AccountError) as duplicate:
            self.add("WORK")
        self.assertEqual(duplicate.exception.code, "duplicate_profile")
        with self.assertRaises(AccountError) as home:
            self.service.add("other", "codex", home=profile["home"])
        self.assertEqual(home.exception.code, "duplicate_home")

    def test_failed_registration_preserves_retryable_metadata(self):
        with patch.object(self.service, "_radio", side_effect=AccountError("radio_unavailable")):
            with self.assertRaises(AccountError):
                self.add()
        self.assertIsNone(self.service.get("work")["radio_account"])
        repaired = self.service.ensure_registered("work")
        self.assertEqual(repaired["radio_account"], "work")
        self.assertEqual(len(self.service._store()["profiles"]), 1)

    def test_default_is_metadata_only_and_survives_reload(self):
        self.add()
        self.service.set_default("work")
        with patch("backend.subprocess.run") as process:
            reloaded = AccountService(self.service.home, self.service.radio_home)
            self.assertEqual(reloaded.default_profile()["id"], "work")
            process.assert_not_called()

    def test_existing_default_registration_keeps_auth_and_config(self):
        home = self.defaults["codex"]
        home.mkdir(parents=True)
        (home / "auth.json").write_text("private-auth")
        (home / "config.toml").write_text("private-config")
        profile = self.service.ensure_registered("codex-default")
        self.assertEqual(profile["radio_account"], "codex-default")
        self.assertEqual((home / "auth.json").read_text(), "private-auth")
        self.assertEqual((home / "config.toml").read_text(), "private-config")

    def test_refresh_codex_uses_official_reader_and_keeps_identity_unverified(self):
        self.add()
        self.reader.read.return_value = {"auth_type": "chatgpt", "email_masked": "em***@ce***",
                                        "email_domain": "example.com", "plan": "pro", "quota_windows": [], "error": None}
        result = self.service.refresh("work")
        self.assertEqual(result["status"]["state"], "connected")
        self.assertFalse(result["identity_verified"])
        self.reader.read.assert_called_once_with(Path(result["home"]), force=True)
        self.assertEqual(self.service.list()["profiles"][0]["status"]["plan"], "pro")

    def test_opencode_status_cannot_claim_valid_credentials_or_balance(self):
        profile = self.add("zen", "opencode")
        self.assertEqual(profile["status"]["state"], "unknown")
        (Path(profile["home"]) / "opencode").mkdir()
        (Path(profile["home"]) / "opencode/auth.json").write_text("secret")
        with patch.object(self.service, "_opencode_status", side_effect=AccountError("unsupported")):
            status = self.service.refresh("zen")["status"]
        self.assertEqual(status["state"], "credential_present")
        self.assertIsNone(status["balance"])
        self.reader.read.assert_not_called()

    def test_native_opencode_status_discards_connection_identifiers(self):
        result = self.service._parse_opencode_status([
            {"id": "opencode", "name": "OpenCode Zen", "connections": [
                {"id": "secret-connection-id", "label": "private-label", "type": "api", "token": "secret-token"}]}
        ])
        self.assertEqual(result["state"], "credential_present")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("private-label", json.dumps(result))
        self.assertIsNone(result["balance"])
        self.assertFalse(result["identity_verified"])

    def test_native_opencode_status_does_not_confuse_other_providers_with_zen(self):
        result = self.service._parse_opencode_status([{"id": "openai", "connections": [{"type": "oauth"}]}])
        self.assertEqual(result["state"], "not_connected")
        for invalid in ({}, [{"id": "opencode"}], [{"id": "opencode", "connections": "secret"}]):
            with self.assertRaises(AccountError):
                self.service._parse_opencode_status(invalid)

    def test_claude_status_only_returns_masked_allowlisted_metadata(self):
        result = self.service._parse_claude_status({
            "loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
            "email": "alice@example.com", "subscriptionType": "team",
            "orgId": "private-org", "orgName": "private-org-name", "accessToken": "secret-token",
            "quota": {"remaining": 99},
        })
        self.assertEqual(result["state"], "credential_present")
        self.assertEqual(result["email_domain"], "example.com")
        self.assertEqual(result["email_masked"], "al***@ex***")
        self.assertEqual(result["plan"], "team")
        self.assertEqual(result["auth_type"], "claude.ai")
        self.assertEqual(result["quota_windows"], [])
        self.assertFalse(result["identity_verified"])
        serialized = json.dumps({key: value for key, value in result.items() if key != "observed_at"})
        for private in ("alice@example.com", "private-org", "secret-token", "99"):
            self.assertNotIn(private, serialized)

    def test_claude_status_rejects_invalid_payload_and_unsafe_display_values(self):
        for payload in ([], {}, {"loggedIn": "true"}, {"loggedIn": 1}):
            with self.assertRaises(AccountError):
                self.service._parse_claude_status(payload)
        result = self.service._parse_claude_status({"loggedIn": True, "authMethod": "secret-token",
                                                  "email": "bad\n@example.com", "subscriptionType": "private-plan"})
        self.assertIsNone(result["auth_type"])
        self.assertIsNone(result["email_masked"])
        self.assertIsNone(result["plan"])
        logged_out = self.service._parse_claude_status({"loggedIn": False, "email": "old@example.com",
                                                       "subscriptionType": "team", "authMethod": "claude.ai"})
        self.assertEqual(logged_out["state"], "not_connected")
        self.assertIsNone(logged_out["email_masked"])
        self.assertIsNone(logged_out["plan"])

    def test_claude_refresh_uses_scoped_cli_and_no_codex_reader(self):
        profile = self.add("anthropic-work", "claude")
        process = Mock(returncode=0)
        process.communicate.return_value = (b'{"loggedIn":true,"authMethod":"claude.ai","subscriptionType":"pro"}', None)
        with patch("backend.provider_command", return_value=["claude.exe"]), \
                patch("backend.subprocess.Popen", return_value=process) as start, patch("backend._cleanup") as cleanup:
            result = self.service.refresh("anthropic-work")["status"]
        self.assertEqual(start.call_args.args[0], ["claude.exe", "auth", "status", "--json"])
        self.assertEqual(start.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"], profile["home"])
        self.assertEqual(start.call_args.kwargs["cwd"], profile["home"])
        self.assertEqual(start.call_args.kwargs["stdin"], subprocess.DEVNULL)
        process.communicate.assert_called_once_with(timeout=12)
        cleanup.assert_called_once_with(process)
        self.reader.read.assert_not_called()
        self.assertEqual(result["plan"], "pro")
        self.assertEqual(result["state"], "credential_present")

    def test_claude_probe_timeout_cleans_up_and_preserves_honest_presence(self):
        profile = self.add("anthropic-work", "claude")
        (Path(profile["home"]) / ".credentials.json").write_text("never-read-token")
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired(["claude.exe"], 12)
        with patch("backend.provider_command", return_value=["claude.exe"]), \
                patch("backend.subprocess.Popen", return_value=process), patch("backend._cleanup") as cleanup:
            result = self.service.refresh("anthropic-work")["status"]
        cleanup.assert_called_once_with(process)
        self.assertEqual(result["state"], "credential_present")
        self.assertEqual(result["error"], "provider_status_not_verified")
        self.assertEqual(result["quota_windows"], [])

    def test_claude_logged_out_native_exit_one_is_supported(self):
        profile = self.add("anthropic-work", "claude")
        process = Mock(returncode=1)
        process.communicate.return_value = (b'{"loggedIn":false}', None)
        with patch("backend.provider_command", return_value=["claude.exe"]), \
                patch("backend.subprocess.Popen", return_value=process), patch("backend._cleanup"):
            result = self.service.refresh("anthropic-work")["status"]
        self.assertEqual(result["state"], "not_connected")
        self.assertIsNone(result["error"])

    def test_launch_plan_new_session_and_never_starts_process(self):
        self.add()
        with patch("backend.provider_command", return_value=["codex.exe"]), patch("backend.subprocess.run") as process:
            plan = self.service.launch_plan("work", "new-agent", "w9")
        process.assert_not_called()
        self.assertIn("--new", plan["argv"])
        self.assertNotIn("--resume", plan["argv"])
        self.assertNotIn("move", plan["argv"])
        self.assertEqual(plan["handle"], "new-agent")
        self.assertEqual(plan["env"]["CODEX_HOME"], self.service.get("work")["home"])

    def test_gemini_and_claude_launch_new_radio_sessions_in_selected_profiles(self):
        for provider in ("gemini", "claude"):
            name = provider + "-work"
            self.add(name, provider)
            with patch("backend.provider_command", return_value=[provider + ".exe"]), \
                    patch.object(self.service, "_supports_gemini_home", return_value=True), \
                    patch("backend.subprocess.run") as process:
                plan = self.service.launch_plan(name, provider + "-agent", "w9")
            process.assert_not_called()
            self.assertEqual(plan["provider"], provider)
            self.assertEqual(plan["provider_argv"], [provider + ".exe"])
            self.assertEqual(plan["provider_extra_args"], [])
            self.assertIn("--new", plan["argv"])
            self.assertEqual(plan["argv"][plan["argv"].index("--account") + 1], name)

    def test_launch_rejects_existing_workspace_binding(self):
        self.add()
        self.sql("INSERT INTO handles(workspace,pane_workspace,name,session_ref,created_at,last_seen,account) VALUES(?,?,?,?,?,?,?)",
                 ("w9", "w9", "lead", "herdr:w9:p2", "now", "now", "work"))
        with self.assertRaises(AccountError) as error:
            self.service.launch_plan("work", "LEAD", "w9")
        self.assertEqual(error.exception.code, "handle_in_use")
        binding = self.service.get("work")["bindings"][0]
        self.assertFalse(binding["identity_verified"])
        self.assertEqual(binding["state"], "unverified")

    def test_launch_requires_explicit_valid_workspace(self):
        self.add()
        for workspace in (None, "", "w0", "w1;p2", "w1\n", "../w1"):
            with self.subTest(workspace=workspace), self.assertRaises(AccountError):
                self.service.launch_plan("work", "worker", workspace)

    def test_selected_model_is_carried_for_codex_and_native_radio_opencode(self):
        self.add()
        self.add("zen", "opencode")
        with patch("backend.provider_command",return_value=["native.exe"]), patch.object(self.service,"_supports_standalone",return_value=True):
            codex=self.service.launch_plan("work","worker","w9",model="gpt-example")
            opencode=self.service.launch_plan("zen","worker","w9",model="opencode/example")
            self.assertEqual(codex["model"],"gpt-example")
            self.assertNotIn("--model",codex["argv"])
            self.assertEqual(opencode["argv"][-2:],["--model","opencode/example"])
            for value in ("", "--help", "provider/model", "space here", 123):
                with self.assertRaises(AccountError):self.service.launch_plan("work","worker","w9",model=value)
            with self.assertRaises(AccountError):self.service.launch_plan("zen","worker","w9",model="bare")

    def test_models_use_selected_codex_cache_without_native_process(self):
        profile=self.add()
        path=Path(profile["home"])/"models_cache.json"
        path.write_text(json.dumps({"models":[{"slug":"selected-model","visibility":"list"}]}),encoding="utf-8")
        with patch("backend.provider_command") as resolve,patch("backend.subprocess.Popen") as spawn:
            result=self.service.models("work")
        self.assertEqual(result["models"][0]["id"],"selected-model")
        resolve.assert_not_called();spawn.assert_not_called()

    def test_models_open_code_environment_is_account_scoped_and_custom_env_is_rejected(self):
        profile=self.add("zen","opencode")
        with patch("backend.provider_command",return_value=["opencode.exe"]), \
                patch.object(self.service,"_supports_standalone",return_value=True), \
                patch("models.opencode_catalog",return_value={"models":[],"default_model":None}) as catalog:
            self.service.models("zen")
            self.service.models("zen")
        catalog.assert_called_once()
        environment=catalog.call_args.args[1]
        self.assertEqual(environment["XDG_DATA_HOME"],profile["home"])
        self.assertIn("XDG_CONFIG_HOME",environment)
        self.sql("UPDATE accounts SET env=? WHERE name='zen'",(json.dumps({"OPENAI_API_KEY":"private"}),))
        with self.assertRaises(AccountError):self.service.models("zen")

    def test_connect_uses_native_argv_and_isolated_home(self):
        self.add()
        with patch("backend.provider_command", return_value=["native codex.exe"]), patch("backend.subprocess.run") as process:
            plan = self.service.connect_plan("work")
        process.assert_not_called()
        self.assertEqual(plan["argv"], ["native codex.exe", "login", "-c", 'cli_auth_credentials_store="file"'])

    def test_opencode_connect_and_launch_require_private_server(self):
        self.add("zen", "opencode")
        with patch("backend.provider_command", return_value=["opencode.exe"]), patch.object(self.service, "_supports_standalone", return_value=True):
            self.assertIn("--standalone", self.service.connect_plan("zen")["argv"])
            self.assertEqual(self.service.launch_plan("zen", "worker", "w9")["provider_extra_args"], ["--standalone"])
        with patch("backend.provider_command", return_value=["opencode.exe"]), patch.object(self.service, "_supports_standalone", return_value=False):
            with self.assertRaises(AccountError):
                self.service.connect_plan("zen")
            with self.assertRaises(AccountError):
                self.service.launch_plan("zen", "worker", "w9")

    def test_inherited_credentials_removed_case_insensitively(self):
        env = plan_environment({"env": {"CODEX_HOME": "isolated"}, "unset_env": backend.UNSET_ENV},
                               {"OpenAI_Api_Key": "secret", "OPENCODE_CONFIG_CONTENT": "secret", "PATH": "tools", "HERDR_PANE_ID": "w9:p1"})
        self.assertEqual(env, {"CODEX_HOME": "isolated", "PATH": "tools", "HERDR_PANE_ID": "w9:p1"})

    def test_provider_auth_overrides_are_scrubbed_but_home_and_project_tools_remain(self):
        inherited = {key.lower(): "must-not-inherit" for key in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_GENAI_USE_VERTEXAI", "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE")}
        inherited.update({"gemini_cli_home": "wrong-home", "HOME": "real-home", "USERPROFILE": "real-home",
                          "PATH": "tools", "HERDR_PANE_ID": "w9:p1"})
        plan = {"env": {"GEMINI_CLI_HOME": "selected-parent"}, "unset_env": backend.UNSET_ENV}
        result = plan_environment(plan, inherited)
        self.assertEqual(result, {"GEMINI_CLI_HOME": "selected-parent", "HOME": "real-home", "USERPROFILE": "real-home",
                                  "PATH": "tools", "HERDR_PANE_ID": "w9:p1"})

    def test_existing_open_code_profile_drops_other_accounts_companion_xdg_homes(self):
        inherited={"XDG_DATA_HOME":"other-data", "xdg_config_home":"other-config", "XDG_CACHE_HOME":"other-cache",
                   "XDG_STATE_HOME":"other-state", "OPENCODE_CLI_CONFIG_CONTENT":"private-settings", "PATH":"tools"}
        existing=self.service._profile("existing","opencode",self.root/"native-data",source="system")
        plan=self.service._plan(existing,"models",[])
        actual=plan_environment(plan,inherited)
        self.assertEqual(actual,{"XDG_DATA_HOME":str(self.root/"native-data"),"PATH":"tools"})
        managed=self.service._profile("managed","opencode",self.root/"managed/data",source="managed")
        actual=plan_environment(self.service._plan(managed,"launch",[]),inherited)
        self.assertEqual(actual["XDG_CONFIG_HOME"],str(self.root/"managed/config"))
        self.assertEqual(actual["XDG_CACHE_HOME"],str(self.root/"managed/cache"))
        self.assertEqual(actual["XDG_STATE_HOME"],str(self.root/"managed/state"))
        self.assertNotIn("private-settings",str(actual))

    def test_native_default_homes_match_provider_environment_meanings(self):
        service = AccountService(self.root / "other-watchtower", self.root / "radio")
        with patch.dict(os.environ, {"GEMINI_CLI_HOME": str(self.root / "google-root"),
                                      "CLAUDE_CONFIG_DIR": str(self.root / "claude-root")}, clear=True):
            self.assertEqual(service._default_home("gemini"), self.root / "google-root/.gemini")
            self.assertEqual(service._default_home("claude"), self.root / "claude-root")

    def test_radio_home_drift_cannot_launch_or_probe_wrong_account(self):
        self.add()
        self.sql("UPDATE accounts SET home=? WHERE name='work'", (str(self.root / "other"),))
        with patch("backend.provider_command", return_value=["codex.exe"]), self.assertRaises(AccountError) as error:
            self.service.launch_plan("work", "worker", "w9")
        self.assertEqual(error.exception.code, "radio_profile_changed")
        self.assertEqual(self.service.refresh("work")["status"]["state"], "unavailable")
        self.reader.read.assert_not_called()

    def test_custom_credentials_not_read_or_probed(self):
        self.add()
        self.sql("UPDATE accounts SET env=? WHERE name='work'", (json.dumps({"OPENAI_API_KEY": "never-echo-this"}),))
        result = self.service.list()
        self.assertTrue(result["profiles"][0]["custom_environment"])
        self.assertNotIn("never-echo-this", json.dumps(result))
        self.service.refresh("work")
        self.reader.read.assert_not_called()

    def test_missing_managed_xdg_isolation_is_blocked(self):
        self.add("zen", "opencode")
        self.sql("UPDATE accounts SET env='{}' WHERE name='zen'")
        self.assertTrue(self.service.get("zen")["custom_environment"])

    def test_failed_radio_stdout_is_not_in_error(self):
        fake = subprocess.CompletedProcess([], 1, b"credential=secret", b"another-secret")
        with patch("backend.subprocess.run", return_value=fake), self.assertRaises(AccountError) as error:
            self.service._radio(["account", "list"])
        self.assertNotIn("secret", str(error.exception))

    def test_corrupt_metadata_not_overwritten(self):
        self.service.path.parent.mkdir(parents=True)
        self.service.path.write_text("{broken")
        with self.assertRaises(AccountError):
            self.service.add("work", "codex")
        self.assertEqual(self.service.path.read_text(), "{broken")

    def test_busy_registry_not_modified(self):
        self.service.path.parent.mkdir(parents=True)
        self.service.path.with_suffix(".lock").write_text("")
        with self.assertRaises(AccountError) as error:
            self.service.add("work", "codex")
        self.assertEqual(error.exception.code, "accounts_busy")

    def test_default_home_explicit_xdg_and_windows_fallback(self):
        self.assertEqual(default_home({"WATCHTOWER_HOME": str(self.root)}), self.root)
        self.assertEqual(default_home({"XDG_STATE_HOME": str(self.root)}), self.root / "watchtower")
        if os.name == "nt":
            self.assertEqual(default_home({"LOCALAPPDATA": str(self.root)}), self.root / "watchtower")
            self.assertEqual(default_home({"LOCALAPPDATA": str(self.root), "WATCHTOWER_CONTEXT_HOME": str(self.root / "watchtower-dev")}), self.root / "watchtower-dev")


class ReaderTest(unittest.TestCase):
    def test_probe_uses_shared_native_or_node_resolution_without_shell(self):
        for resolved in (["native codex.exe"], ["node.exe", "package/codex.js"]):
            with patch("backend.provider_command", return_value=resolved):
                self.assertEqual(_cli_command(), resolved + ["app-server", "--listen", "stdio://"])

    @unittest.skipUnless(os.name == "nt", "Windows npm shim resolution")
    def test_cmd_only_npm_install_resolves_node_script(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "node_modules/@openai/codex/bin/codex.js"
            script.parent.mkdir(parents=True)
            script.write_text("// fake package launcher")
            def lookup(name):
                return {"codex.cmd": str(root / "codex.cmd"), "node.exe": str(root / "node.exe")}.get(name)
            with patch("backend.shutil.which", side_effect=lookup):
                result = _cli_command()
            self.assertEqual(result, [str(root / "node.exe"), str(script), "app-server", "--listen", "stdio://"])
            self.assertFalse(any(value.lower().endswith(".cmd") for value in result))

    @unittest.skipUnless(os.name == "nt", "Windows npm shim resolution")
    def test_gemini_and_claude_npm_resolution_never_executes_command_shims(self):
        for provider, relative in (("gemini", "@google/gemini-cli/bundle/gemini.js"),
                                   ("gemini", "@google/gemini-cli/dist/index.js"),
                                   ("claude", "@anthropic-ai/claude-code/cli.js")):
            with self.subTest(provider=provider, path=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                script = root / "node_modules" / relative
                script.parent.mkdir(parents=True)
                script.write_text("// fake package launcher")
                def lookup(name):
                    return {provider + ".cmd": str(root / (provider + ".cmd")), "node.exe": str(root / "node.exe")}.get(name)
                with patch("backend.shutil.which", side_effect=lookup):
                    command = backend.provider_command(provider)
                self.assertEqual(command, [str(root / "node.exe"), str(script)])
                self.assertFalse(any(value.lower().endswith((".cmd", ".ps1")) for value in command))

    @unittest.skipUnless(os.name == "nt", "Windows native CLI resolution")
    def test_claude_native_install_preferred_over_npm(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "claude.exe"
            with patch("backend.shutil.which", return_value=str(executable)) as lookup:
                self.assertEqual(backend.provider_command("claude"), [str(executable.resolve())])
            lookup.assert_called_once_with("claude.exe")

    def test_unrelated_quota_bucket_never_used(self):
        value = {"rateLimitsByLimitId": {"other": {"primary": {"usedPercent": 10}}}}
        self.assertEqual(_quota_windows(value), [])

    def test_remaining_and_unknown_quota_values(self):
        value = {"rateLimitsByLimitId": {"codex": {"primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1000}}}}
        self.assertEqual(_quota_windows(value)[0]["remaining_percent"], 75)
        for invalid in (None, True, -1, 101, float("nan"), "25"):
            changed = deepcopy(value)
            changed["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = invalid
            self.assertEqual(_quota_windows(changed), [])

    def test_masking_never_returns_full_email_or_control_text(self):
        self.assertEqual(_mask_email("user@example.com"), "us***@ex***")
        self.assertEqual(_email_domain("user@example.com"), "example.com")
        self.assertIsNone(_mask_email("user@domain\nsecret"))
        self.assertIsNone(_email_domain("a@-bad.com"))

    def test_force_refresh_bypasses_cache_without_reading_auth_file(self):
        with tempfile.TemporaryDirectory() as directory:
            reader = CodexAccountReader()
            with patch.object(reader, "_probe", return_value={"auth_type": "chatgpt"}) as probe:
                reader.read(Path(directory))
                reader.read(Path(directory))
                self.assertEqual(probe.call_count, 1)
                reader.read(Path(directory), force=True)
                self.assertEqual(probe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
