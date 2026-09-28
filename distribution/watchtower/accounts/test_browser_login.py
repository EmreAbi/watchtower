"""Fake-RPC browser login coverage. No provider login or real process is used."""

import json
import os
import queue
import threading
import time
import unittest
from unittest.mock import Mock, patch

import browser_login as login

AUTH_URL = "https://auth.openai.com/oauth/authorize?state=one-use-ui-value&code_challenge=fake"


class FakeOutput:
    def __init__(self):
        self.queue = queue.Queue()

    def readline(self, limit):
        return self.queue.get(timeout=3)

    def close(self):
        self.queue.put(b"")


class FakeInput:
    def __init__(self, process):
        self.process = process

    def write(self, value):
        message = json.loads(value)
        self.process.sent.append(message)
        self.process.respond(message)

    def flush(self):
        pass

    def close(self):
        pass


class FakeProcess:
    def __init__(self, startup=None):
        self.stdout = FakeOutput()
        self.stdin = FakeInput(self)
        self.sent = []
        self.startup = startup
        self.cleaned = threading.Event()

    def emit(self, value):
        self.stdout.queue.put((json.dumps(value) + "\n").encode())

    def complete(self, success=True, identifier="ours", **extra):
        self.emit({"method": "account/login/completed", "params": {"loginId": identifier, "success": success, **extra}})

    def respond(self, message):
        if message["method"] == "initialize":
            self.emit({"id": 1, "result": {}})
        elif message["method"] == "account/login/start":
            if self.startup:
                self.startup(self)
            else:
                self.start_response()
        elif message["method"] == "account/login/cancel":
            self.emit({"id": 3, "result": {"status": "canceled"}})

    def start_response(self, **kwargs):
        self.emit({"id": 2, "result": {"type": "chatgpt", "loginId": "ours", "authUrl": AUTH_URL, **kwargs}})


class BrowserLoginTest(unittest.TestCase):
    def setUp(self):
        self.service = Mock()
        self.service.connect_plan.return_value = {
            "provider": "codex", "argv": ["codex.exe", "login", "-c", 'cli_auth_credentials_store="file"'],
            "env": {"CODEX_HOME": "isolated-home"}, "unset_env": ["OPENAI_API_KEY"],
        }
        self.process = FakeProcess()
        self.popen = self.enterContext(patch("browser_login.subprocess.Popen", return_value=self.process))
        self.enterContext(patch("browser_login.provider_command", return_value=["codex.exe"]))

        def cleanup(process):
            process.cleaned.set()
            process.stdout.close()

        self.cleanup = self.enterContext(patch("browser_login._cleanup", side_effect=cleanup))
        self.session = login.BrowserLogin(self.service, "work")
        self.addCleanup(self.session.close)

    def until_done(self, session=None):
        session = session or self.session
        deadline = time.monotonic() + 1.5
        while session.poll() is None and time.monotonic() < deadline:
            time.sleep(0.005)
        result = session.poll()
        self.assertIsNotNone(result)
        return result

    def test_constructor_is_pure_and_poll_nonblocking(self):
        self.service.connect_plan.assert_not_called()
        self.popen.assert_not_called()
        self.assertIsNone(self.session.poll())

    def test_start_returns_url_without_opening_browser_or_claiming_success(self):
        self.assertEqual(self.session.start(), {"url": AUTH_URL})
        self.assertIsNone(self.session.poll())
        self.assertEqual([item["method"] for item in self.process.sent], ["initialize", "initialized", "account/login/start"])
        self.assertEqual(self.process.sent[-1]["params"], {"type": "chatgpt"})

    def test_profile_environment_and_file_store_are_preserved(self):
        with patch.dict(os.environ, {"OpenAI_Api_Key": "secret"}):
            self.session.start()
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], ["codex.exe", "app-server", "--listen", "stdio://", "-c", 'cli_auth_credentials_store="file"'])
        self.assertEqual(kwargs["env"]["CODEX_HOME"], "isolated-home")
        self.assertNotIn("OpenAI_Api_Key", kwargs["env"])
        self.assertEqual(kwargs["cwd"], "isolated-home")
        self.assertEqual(kwargs["stderr"], login.subprocess.DEVNULL)
        self.assertNotIn("shell", kwargs)

    def test_exact_completion_is_the_only_success(self):
        self.session.start()
        self.process.complete(identifier="other")
        self.process.complete(identifier=None)
        self.process.emit({"method": "account/updated", "params": {"type": "chatgpt"}})
        time.sleep(0.03)
        self.assertIsNone(self.session.poll())
        self.process.complete()
        self.assertEqual(self.until_done()["state"], "success")
        self.assertTrue(self.process.cleaned.wait(1))
        self.assertFalse(any(item["method"] == "account/login/cancel" for item in self.process.sent))

    def test_early_completion_before_start_response_is_preserved(self):
        def startup(process):
            process.complete()
            process.start_response()
        self.process.startup = startup
        self.assertEqual(self.session.start()["url"], AUTH_URL)
        self.assertEqual(self.until_done()["state"], "success")

    def test_failure_notification_does_not_expose_server_error(self):
        self.session.start()
        self.process.complete(False, error="token=do-not-print authUrl=" + AUTH_URL)
        result = self.until_done()
        self.assertEqual(result["state"], "error")
        self.assertNotIn("token", json.dumps(result))
        self.assertNotIn("one-use", json.dumps(result))

    def test_clean_process_exit_is_not_success(self):
        self.session.start()
        self.process.stdout.close()
        self.assertEqual(self.until_done()["state"], "error")

    def test_cancel_is_immediate_idempotent_and_uses_exact_login(self):
        self.session.start()
        before = time.monotonic()
        self.session.cancel()
        self.session.cancel()
        self.assertLess(time.monotonic() - before, 0.1)
        self.assertEqual(self.session.poll()["state"], "cancelled")
        self.session.close()
        requests = [item for item in self.process.sent if item["method"] == "account/login/cancel"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["params"], {"loginId": "ours"})
        self.cleanup.assert_called_once_with(self.process)
        self.session.close()
        self.cleanup.assert_called_once()

    def test_cancel_before_start_does_not_spawn(self):
        self.session.cancel()
        with self.assertRaises(login.BrowserLoginError) as error:
            self.session.start()
        self.assertEqual(error.exception.code, "cancelled")
        self.popen.assert_not_called()
        self.service.connect_plan.assert_not_called()

    def test_cancel_during_plan_resolution_prevents_spawn(self):
        entered, release = threading.Event(), threading.Event()
        plan = self.service.connect_plan.return_value
        def blocked_plan(profile):
            entered.set()
            release.wait(1)
            return plan
        self.service.connect_plan.side_effect = blocked_plan
        errors = []
        def start():
            try:
                self.session.start()
            except login.BrowserLoginError as error:
                errors.append(error.code)
        thread = threading.Thread(target=start)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.session.cancel()
        thread.join(0.3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["cancelled"])
        release.set()
        self.session.close()
        self.popen.assert_not_called()

    def test_late_spawn_after_cancel_is_cleaned_without_login_rpc(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_spawn(*args, **kwargs):
            entered.set()
            release.wait(1)
            return self.process
        self.popen.side_effect = blocked_spawn
        errors = []
        def start():
            try:
                self.session.start()
            except login.BrowserLoginError as error:
                errors.append(error.code)
        thread = threading.Thread(target=start)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.session.cancel()
        thread.join(0.3)
        self.assertEqual(errors, ["cancelled"])
        release.set()
        self.session.close()
        self.assertTrue(self.process.cleaned.is_set())
        self.assertEqual(self.process.sent, [])

    def test_start_timeout_is_bounded_and_cleans_owned_process(self):
        original = self.process.respond
        self.process.respond = lambda message: original(message) if message["method"] != "initialize" else None
        with patch("browser_login.START_SECONDS", 0.15):
            before = time.monotonic()
            with self.assertRaises(login.BrowserLoginError) as error:
                self.session.start()
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertEqual(error.exception.code, "startup_timeout")
        self.session.close()
        self.assertTrue(self.process.cleaned.is_set())

    def test_login_timeout_cancels_exact_attempt(self):
        with patch("browser_login.LOGIN_SECONDS", 0.05):
            self.session.start()
            result = self.until_done()
        self.assertEqual(result["state"], "error")
        self.assertIn("expired", result["message"])
        self.session.close()
        self.assertTrue(any(item["method"] == "account/login/cancel" for item in self.process.sent))

    def test_start_error_sanitizes_port_conflict_without_killing_external_process(self):
        self.process.startup = lambda process: process.emit({"id": 2, "error": {"message": "port1455 in use secret=" + AUTH_URL}})
        with self.assertRaises(login.BrowserLoginError) as error:
            self.session.start()
        self.assertNotIn("secret", str(error.exception))
        self.assertIn("other Codex sign-in", str(error.exception))
        self.session.close()
        self.cleanup.assert_called_once_with(self.process)

    def test_invalid_url_cancels_known_attempt_and_never_returns_url(self):
        self.process.startup = lambda process: process.start_response(authUrl="https://evil.example/authorize")
        with self.assertRaises(login.BrowserLoginError) as error:
            self.session.start()
        self.assertEqual(error.exception.code, "invalid_url")
        self.session.close()
        self.assertTrue(any(item["method"] == "account/login/cancel" for item in self.process.sent))

    def test_invalid_response_and_nonboolean_completion_are_rejected(self):
        self.session.start()
        self.process.complete(success="true")
        self.assertEqual(self.until_done()["state"], "error")

    def test_cancel_prevents_late_success(self):
        self.session.start()
        self.session.cancel()
        self.process.complete()
        self.session.close()
        self.assertEqual(self.session.poll()["state"], "cancelled")

    def test_repeated_start_does_not_create_second_server(self):
        self.session.start()
        self.session.start()
        self.popen.assert_called_once()

    def test_oversized_or_malformed_rpc_fails_closed(self):
        self.session.start()
        self.process.stdout.queue.put(b"X" * (login.MAX_LINE_BYTES + 1) + b"\n")
        self.assertEqual(self.until_done()["state"], "error")

    def test_other_provider_never_starts_codex(self):
        self.service.connect_plan.return_value["provider"] = "opencode"
        with self.assertRaises(login.BrowserLoginError) as error:
            self.session.start()
        self.assertEqual(error.exception.code, "unsupported_provider")
        self.popen.assert_not_called()


class AuthUrlTest(unittest.TestCase):
    def test_only_exact_https_authorization_endpoint_is_accepted(self):
        self.assertEqual(login.validate_auth_url(AUTH_URL), AUTH_URL)
        self.assertEqual(login.validate_auth_url("https://auth.openai.com:443/oauth/authorize"), "https://auth.openai.com:443/oauth/authorize")
        for url in ("http://auth.openai.com/oauth/authorize", "https://auth.openai.com.evil/oauth/authorize",
                    "https://evil@auth.openai.com/oauth/authorize", "https://auth.openai.com:444/oauth/authorize",
                    "https://auth.openai.com/logout", "https://auth.openai.com/oauth/authorize#fragment",
                    "https://auth.openai.com/oauth/authorize\n", " https://auth.openai.com/oauth/authorize",
                    "https://auth.openai.com\\evil/oauth/authorize", "javascript:alert(1)", None, "X" * 8193):
            with self.subTest(url=url), self.assertRaises(login.BrowserLoginError):
                login.validate_auth_url(url)


if __name__ == "__main__":
    unittest.main()
