"""All release checks, CLI probes and installations are mocked."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import tool_updates as updates


class ToolUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.profile = self.base / "profile"
        self.local = self.base / "local"
        self.root = self.profile / ".codex/packages/standalone"
        self.release = self.root / "releases/0.157.1-x86_64-pc-windows-msvc/bin/codex.exe"
        self.visible = self.local / "Programs/OpenAI/Codex/bin/codex.exe"
        self.current = self.root / "current/bin/codex.exe"
        self.shell = self.base / "windows/System32/WindowsPowerShell/v1.0/powershell.exe"
        for path in (self.release, self.visible, self.current, self.shell):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.env = dict(USERPROFILE=str(self.profile), LOCALAPPDATA=str(self.local),
                        SystemRoot=str(self.base / "windows"), PATH="test-path",
                        CODEX_HOME="isolated-account", CODEX_INSTALL_DIR="wrong-place",
                        CODEX_RELEASE="evil", openai_api_key="secret", GH_TOKEN="secret",
                        NODE_OPTIONS="secret", CODEX_INSTALL_DAEMON_ONLY="1")
        self.version = "0.157.1"
        self.latest = "0.158.0"
        self.fetch = Mock(side_effect=self.fake_fetch)
        self.runner = Mock(side_effect=self.fake_run)
        self.resolver = Mock(side_effect=lambda provider: [str(self.release)])
        self.service = updates.ToolUpdateService(self.base / "watchtower-one", runner=self.runner,
                                               fetch=self.fetch, resolver=self.resolver,
                                               environ=self.env, platform="nt")
        # Portable fixture models the two native directory junctions without
        # requiring Windows administrator privileges to create a symlink.
        self.same = self.enterContext(patch("tool_updates._same", side_effect=self.same_path))

    def same_path(self, left, right):
        left, right = Path(left), Path(right)
        if left.name == right.name == "codex.exe":
            return True
        return left.resolve() == right.resolve()

    def fake_fetch(self, url, timeout):
        if url == updates.RELEASE_URL:
            return json.dumps({"tag_name": "rust-v" + self.latest, "draft": False}).encode()
        if url == updates.INSTALLER_URL:
            return b"# fake official installer, never executed by these tests\n"
        self.fail("Unexpected source")

    def fake_run(self, argv, *, env, timeout, capture):
        if argv[-1] == "--version":
            return dict(returncode=0, stdout=("codex-cli " + self.version + "\n").encode())
        self.assertEqual(argv[-2:], ["-Release", self.latest])
        self.assertFalse(capture)
        self.assertEqual(Path(argv[argv.index("-File") + 1]).read_bytes(), self.fake_fetch(updates.INSTALLER_URL, 1))
        self.version = self.latest
        self.release = self.root / ("releases/" + self.version + "-x86_64-pc-windows-msvc/bin/codex.exe")
        self.release.parent.mkdir(parents=True, exist_ok=True)
        self.release.touch()
        return dict(returncode=0, stdout=b"PRIVATE raw installer output ignored")

    def installer_calls(self):
        return [call for call in self.runner.call_args_list if "-File" in call.args[0]]

    def test_constructor_has_no_io(self):
        self.fetch.assert_not_called()
        self.runner.assert_not_called()
        self.resolver.assert_not_called()

    def test_status_only_probes_version_and_does_not_create_lock(self):
        result = self.service.status()
        self.assertEqual(result["state"], "unchecked")
        self.assertEqual(result["installed_version"], "0.157.1")
        self.assertFalse(result["can_update"])
        self.fetch.assert_not_called()
        self.assertFalse((self.root / "watchtower-update.lock").exists())

    def test_check_uses_fixed_official_source_and_sanitized_result(self):
        result = self.service.check()
        self.assertEqual(result["state"], "update_available")
        self.assertTrue(result["can_update"])
        self.assertEqual(result["latest_version"], "0.158.0")
        self.assertIsNotNone(result["checked_at"])
        self.fetch.assert_called_once_with(updates.RELEASE_URL, timeout=updates.FETCH_SECONDS)
        self.assertEqual(self.installer_calls(), [])

    def test_current_update_noops_without_fetching_installer(self):
        self.latest = self.version
        result = self.service.update()
        self.assertEqual(result["state"], "current")
        self.assertEqual(self.installer_calls(), [])
        self.fetch.assert_called_once_with(updates.RELEASE_URL, timeout=updates.FETCH_SECONDS)

    def test_newer_installed_never_downgrades(self):
        self.latest = "0.156.0"
        self.assertEqual(self.service.update()["state"], "current")
        self.assertEqual(self.installer_calls(), [])

    def test_update_verifies_new_version_and_versioned_binary_path(self):
        result = self.service.update()
        self.assertEqual(result["state"], "updated")
        self.assertEqual(result["installed_version"], self.latest)
        self.assertIn(self.latest, result["installed_path"])
        calls = self.installer_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["timeout"], updates.UPDATE_SECONDS)
        self.assertEqual(calls[0].args[0][0], str(self.shell))
        self.assertFalse(Path(calls[0].args[0][calls[0].args[0].index("-File") + 1]).exists())
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_installer_environment_ignores_account_and_install_overrides(self):
        self.service.update()
        env = self.installer_calls()[0].kwargs["env"]
        self.assertEqual(env["CODEX_HOME"], str(self.profile / ".codex"))
        self.assertEqual(env["CODEX_NON_INTERACTIVE"], "1")
        self.assertEqual(env["CODEX_INSTALLER_USE_RELEASES_OPENAI_COM"], "1")
        self.assertEqual(env["PATH"], "test-path")
        for key in ("openai_api_key", "GH_TOKEN", "NODE_OPTIONS", "CODEX_RELEASE", "CODEX_INSTALL_DIR", "CODEX_INSTALL_DAEMON_ONLY"):
            self.assertNotIn(key, env)

    def test_version_probe_environment_also_strips_auth(self):
        self.service.status()
        env = self.runner.call_args.kwargs["env"]
        self.assertNotIn("openai_api_key", env)
        self.assertNotIn("CODEX_HOME", env)

    def test_npm_install_is_not_replaced_by_native_installer(self):
        self.resolver.side_effect = None
        self.resolver.return_value = [str(self.base / "node.exe"), "codex.js"]
        result = self.service.update()
        self.assertEqual(result["state"], "unsupported")
        self.assertIn("original package manager", result["message"])
        self.fetch.assert_not_called()

    def test_non_windows_install_is_explicitly_unsupported(self):
        self.service._platform = "posix"
        self.assertEqual(self.service.status()["state"], "unsupported")

    def test_native_path_mismatch_is_unsupported(self):
        self.same.side_effect = lambda a, b: False
        self.assertEqual(self.service.update()["state"], "unsupported")
        self.fetch.assert_not_called()

    def test_unversioned_native_layout_is_unsupported(self):
        self.resolver.side_effect = None
        self.resolver.return_value = [str(self.current)]
        self.assertEqual(self.service.update()["state"], "unsupported")
        self.fetch.assert_not_called()

    def test_missing_cli_and_raw_error_are_sanitized(self):
        self.resolver.side_effect = OSError("PRIVATE secret path")
        result = self.service.check()
        self.assertEqual(result["state"], "unavailable")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.fetch.assert_not_called()

    def test_version_output_is_strictly_parsed(self):
        self.runner.side_effect = None
        self.runner.return_value = {"returncode": 0, "stdout": b"PRIVATE 0.157.1"}
        self.assertEqual(self.service.status()["state"], "unavailable")

    def test_release_rejects_draft_prerelease_and_injection(self):
        for data in ({"tag_name": "rust-v0.158.0", "draft": True},
                     {"tag_name": "rust-v0.158.0", "prerelease": True},
                     {"tag_name": "0.158.0; PRIVATE"}, {"tag_name": "0.158.0-alpha.1"},
                     {"tag_name": None}, []):
            with self.subTest(data=data):
                self.fetch.side_effect = None
                self.fetch.return_value = json.dumps(data).encode()
                result = self.service.update()
                self.assertEqual(result["state"], "error")
                self.assertFalse(result["can_update"])
                self.assertNotIn("PRIVATE", json.dumps(result))
                self.assertEqual(self.installer_calls(), [])

    def test_network_failure_does_not_reuse_cached_update_permission(self):
        self.assertTrue(self.service.check()["can_update"])
        self.fetch.side_effect = TimeoutError("PRIVATE url")
        result = self.service.update()
        self.assertEqual(result["state"], "error")
        self.assertFalse(result["can_update"])
        self.assertEqual(self.installer_calls(), [])

    def test_installer_failure_is_safe_and_does_not_claim_success(self):
        original = self.runner.side_effect
        self.runner.side_effect = lambda argv, **kw: ({"returncode": 1, "stdout": b"PRIVATE"}
                                                     if "-File" in argv else original(argv, **kw))
        result = self.service.update()
        self.assertEqual(result["state"], "error")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_installer_zero_exit_is_not_enough_to_claim_success(self):
        original = self.runner.side_effect
        self.runner.side_effect = lambda argv, **kw: ({"returncode": 0, "stdout": b""}
                                                     if "-File" in argv else original(argv, **kw))
        self.assertEqual(self.service.update()["state"], "error")

    def test_install_lock_is_shared_across_accounts_homes_and_released(self):
        with updates._install_lock(self.root):
            result = self.service.update()
            self.assertEqual(result["state"], "busy")
            self.fetch.assert_not_called()
        self.assertEqual(self.service.update()["state"], "updated")

    def test_same_service_busy_does_not_probe_or_fetch(self):
        with self.service._operation:
            self.assertEqual(self.service.check()["state"], "busy")
            self.assertEqual(self.service.update()["state"], "busy")
        self.runner.assert_not_called()
        self.fetch.assert_not_called()

    def test_lock_released_after_timeout_and_retry_succeeds(self):
        original = self.runner.side_effect
        def timed_out(argv, **kwargs):
            if "-File" in argv:
                raise subprocess.TimeoutExpired("PRIVATE", updates.UPDATE_SECONDS)
            return original(argv, **kwargs)
        self.runner.side_effect = timed_out
        self.assertEqual(self.service.update()["state"], "error")
        self.runner.side_effect = original
        self.assertEqual(self.service.update()["state"], "updated")

    def test_redirects_fail_closed(self):
        scope = {}
        with patch("sys.argv", ["helper", updates.RELEASE_URL]), patch("urllib.request.build_opener") as opener, patch("sys.stdout"):
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"{}"
            exec(updates._FETCH_HELPER, scope)
        with self.assertRaises(RuntimeError):
            scope["NoRedirect"]().redirect_request(None, None, 302, "", {}, "https://evil.example/install.ps1")

    def test_fetch_rejects_other_source_before_network(self):
        with patch("tool_updates._run") as runner:
            with self.assertRaises(updates._Failure):
                updates._fetch("https://evil.example")
            runner.assert_not_called()

    def test_fetch_has_whole_request_process_deadline_and_no_shell(self):
        with patch("tool_updates._run", return_value={"returncode": 0, "stdout": b"{}"}) as runner:
            self.assertEqual(updates._fetch(updates.RELEASE_URL), b"{}")
            argv = runner.call_args.args[0]
            self.assertEqual(argv[1:4], ["-I", "-S", "-c"])
            self.assertEqual(argv[-1], updates.RELEASE_URL)
            self.assertEqual(runner.call_args.kwargs["timeout"], updates.FETCH_SECONDS)
            self.assertEqual(runner.call_args.kwargs["output_limit"], updates.MAX_DOWNLOAD)

    def test_hidden_runner_cleans_only_its_owned_process_on_timeout(self):
        process = Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired("private", 1)
        with patch("tool_updates.subprocess.Popen", return_value=process) as popen, patch("tool_updates._cleanup") as cleanup:
            with self.assertRaises(subprocess.TimeoutExpired):
                updates._run(["fake.exe", "--version"], env={}, timeout=1, capture=True)
            cleanup.assert_called_once_with(process)
            self.assertFalse(popen.call_args.kwargs["shell"])
            self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
            if updates.os.name == "nt":
                self.assertEqual(popen.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
