"""macOS integration contracts; synthetic metadata and fake processes only."""
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import backend
import integration
import switch_service

AUTH_URL = "https://auth.openai.com/oauth/authorize?state=synthetic&code_challenge=fake"


class MacAccountsTest(unittest.TestCase):
    def test_copy_oauth_link_uses_pbcopy_stdin_without_shell(self):
        with patch("integration.sys.platform", "darwin"), patch("integration.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(integration.copy_login_link(AUTH_URL))
        args, kwargs = run.call_args
        self.assertEqual(args[0], ["/usr/bin/pbcopy"])
        self.assertEqual(kwargs["input"], AUTH_URL)
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertNotIn("shell", kwargs)
        self.assertNotIn("creationflags", kwargs)

    def test_clipboard_rejects_untrusted_url_and_handles_failure(self):
        with patch("integration.sys.platform", "darwin"), patch("integration.subprocess.run") as run:
            self.assertFalse(integration.copy_login_link("file:///private/example"))
            run.assert_not_called()
            run.return_value.returncode = 1
            self.assertFalse(integration.copy_login_link(AUTH_URL))
            run.side_effect = subprocess.TimeoutExpired("pbcopy", 5)
            self.assertFalse(integration.copy_login_link(AUTH_URL))

    def test_posix_provider_resolution_preserves_single_argv_with_spaces(self):
        with tempfile.TemporaryDirectory() as temporary:
            for provider in backend.PROVIDERS:
                path = Path(temporary) / "tools with spaces" / provider
                with patch("backend.os", SimpleNamespace(name="posix")), \
                        patch("backend.shutil.which", return_value=str(path)) as lookup:
                    self.assertEqual(backend.provider_command(provider), [str(path.resolve())])
                lookup.assert_called_once_with(provider)

    def test_posix_state_home_preserves_explicit_context_and_ignores_appdata(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch("backend.os", SimpleNamespace(name="posix", PathLike=os.PathLike)), patch.object(Path, "home", return_value=home):
                self.assertEqual(backend.default_home({"LOCALAPPDATA": "windows-only"}), home / ".local/state/watchtower")
                self.assertEqual(backend.default_home({"WATCHTOWER_HOME": str(home)}), home)
                self.assertEqual(backend.default_home({"XDG_STATE_HOME": str(home)}), home / "watchtower")

    def test_unix_switch_is_rejected_before_ledger_process_or_ticket_access(self):
        service, runtime = Mock(), Mock()
        switch = switch_service.SwitchService(service, runtime)
        with patch("switch_service.os", SimpleNamespace(name="posix")):
            self.assertFalse(switch_service.switch_supported())
            for operation in (lambda: switch.switch_candidates("source"),
                              lambda: switch.prepare_switch("source", "target", ["w1:p1"]),
                              lambda: switch.switch_accounts("token"),
                              lambda: switch_service.run_ticket("unread-ticket")):
                with self.assertRaises(backend.AccountError) as error:
                    operation()
                self.assertEqual(error.exception.code, "switch_platform_unsupported")
                self.assertIn("New agent", error.exception.message)
        self.assertEqual(service.mock_calls, [])
        self.assertEqual(runtime.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
