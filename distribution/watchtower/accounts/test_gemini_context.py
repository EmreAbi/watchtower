"""Private Gemini hook fixtures; no provider login, model or live settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemini_context as hook


class GeminiContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="watchtower-gemini-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / ".gemini"
        self.home.mkdir()
        self.settings = self.home / "settings.json"
        self.env = {"GEMINI_CLI_HOME": str(self.root), "HERDR_PANE_ID": "w4:p18",
                    "RADIO_HANDLE": "gemini-worker", "RADIO_JOINED_SCOPE": "w4"}
        self.argv = ["gemini", "--approval-mode", "yolo", "--session-id", "fake-session"]

    def test_merges_without_losing_settings_and_is_idempotent(self):
        original = {"security": {"auth": {"selectedType": "oauth-personal"}},
                    "hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
                        {"name": "own-hook", "command": "example", "type": "command"}]}],
                        "BeforeTool": [{"hooks": []}]}}
        self.settings.write_text(json.dumps(original))
        hook.prepare_hook(self.env)
        actual = json.loads(self.settings.read_text())
        self.assertEqual(actual["security"], original["security"])
        self.assertEqual(actual["hooks"]["BeforeTool"], original["hooks"]["BeforeTool"])
        self.assertEqual(actual["hooks"]["SessionStart"][0], original["hooks"]["SessionStart"][0])
        self.assertEqual(actual["hooks"]["SessionStart"][-1]["hooks"][0]["name"], hook.HOOK_NAME)
        self.assertEqual(json.loads((self.home / "watchtower-radio/settings.before-hook.json").read_text()), original)
        before = self.settings.read_bytes()
        hook.prepare_hook(self.env)
        self.assertEqual(self.settings.read_bytes(), before)

    def test_disabled_or_malformed_hooks_preserve_original(self):
        for settings in ({"hooksConfig": {"enabled": False}},
                         {"hooksConfig": {"disabled": [hook.HOOK_NAME]}},
                         {"hooks": []}, {"hooks": {"SessionStart": ["bad"]}}):
            with self.subTest(settings=settings):
                self.settings.write_text(json.dumps(settings))
                before = self.settings.read_bytes()
                with self.assertRaises(ValueError):
                    hook.prepare_hook(self.env)
                self.assertEqual(self.settings.read_bytes(), before)

    def test_preserves_invalid_json_and_requires_absolute_home(self):
        self.settings.write_text("broken-json")
        with self.assertRaises(ValueError):
            hook.prepare_hook(self.env)
        self.assertEqual(self.settings.read_text(), "broken-json")
        with self.assertRaises(ValueError):
            hook.prepare_hook({"GEMINI_CLI_HOME": "relative"})

    def test_context_requires_exact_pane_handle_scope_and_session(self):
        hook.write_context(self.env, self.argv, "Radio identity only; no task.")
        event = {"hook_event_name": "SessionStart", "session_id": "fake-session"}
        self.assertEqual(hook.context_for(event, self.env)["hookSpecificOutput"]["additionalContext"],
                         "Radio identity only; no task.")
        for key, value in (("RADIO_HANDLE", "wrong"), ("RADIO_JOINED_SCOPE", "w5")):
            self.assertEqual(hook.context_for(event, {**self.env, key: value}), {})
        self.assertEqual(hook.context_for({**event, "session_id": "another-session"}, self.env), {})
        self.assertEqual(hook.context_for({**event, "hook_event_name": "BeforeTool"}, self.env), {})
        self.assertEqual(hook.context_for(event, {}), {})

    def test_invalid_identity_never_writes_context(self):
        for overrides in ({"HERDR_PANE_ID": "../../other"}, {"RADIO_HANDLE": "../../other"}, {"RADIO_JOINED_SCOPE": ""}):
            with self.assertRaises(ValueError):
                hook.write_context({**self.env, **overrides}, self.argv, "ignored")
        with self.assertRaises(ValueError):
            hook.write_context(self.env, ["gemini"], "ignored")
        self.assertFalse((self.home / "watchtower-radio").exists())

    def test_native_hook_emits_only_structured_context_and_safe_empty_fallback(self):
        hook.write_context(self.env, self.argv, "Synthetic Radio context")
        argv = [sys.executable, "-I", "-B", str(Path(hook.__file__).resolve())]
        for raw, expected in ((json.dumps({"hook_event_name": "SessionStart", "session_id": "fake-session"}), True),
                              ("bad json", False), ("{}", False)):
            result = subprocess.run(argv, input=raw, text=True, capture_output=True,
                                    env={**os.environ, **self.env}, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stderr, "")
            self.assertEqual(bool(json.loads(result.stdout)), expected)

    def test_contexts_from_two_sessions_do_not_overwrite_each_other(self):
        hook.write_context(self.env, self.argv, "First session")
        hook.write_context(self.env, ["gemini", "--session-id", "second-session"], "Second session")
        for session, expected in (("fake-session", "First session"), ("second-session", "Second session")):
            output = hook.context_for({"hook_event_name": "SessionStart", "session_id": session}, self.env)
            self.assertEqual(output["hookSpecificOutput"]["additionalContext"], expected)

    def test_oversized_settings_are_preserved(self):
        self.settings.write_bytes(b" " * (hook.MAX_BYTES + 1))
        with self.assertRaises(ValueError):
            hook.prepare_hook(self.env)
        self.assertEqual(self.settings.stat().st_size, hook.MAX_BYTES + 1)

    @unittest.skipUnless(os.name == "nt", "Windows shell hook command")
    def test_windows_hook_quotes_ampersand_paths_and_rejects_expansion(self):
        with patch.object(hook.sys, "executable", "C:/Users/A&B/python.exe"):
            hook.prepare_hook(self.env)
        config = json.loads(self.settings.read_text())
        command = config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertTrue(command.startswith('"C:/Users/A&B/python.exe" -I -B "'))
        before = self.settings.read_bytes()
        with patch.object(hook.sys, "executable", "C:/%USERPROFILE%/python.exe"):
            with self.assertRaises(ValueError):
                hook.prepare_hook(self.env)
        self.assertEqual(self.settings.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
