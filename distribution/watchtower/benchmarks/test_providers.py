"""Hermetic provider protocol tests. Never contact a provider or load real auth."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from . import providers
except ImportError:
    import providers


class _Input(io.BytesIO):
    def close(self):
        self.saved = self.getvalue()


class Process:
    def __init__(self, payload=b"", code=0, stderr=b""):
        self.stdin = _Input()
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(stderr)
        self.returncode = code
        self.pid = 123456789
        self.stopped = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class Accounts:
    def __init__(self):
        self.records = {
            "first": {"id": "first", "label": "Codex account", "provider": "codex", "home": "selected-codex", "source": "managed"},
            "second": {"id": "second", "label": "OpenCode account", "provider": "opencode", "home": "selected-data", "source": "system"},
        }

    def list(self):
        return {"profiles": list(self.records.values()), "defaults": {"default_profile": "first"}}

    def get(self, name):
        return self.records[name]

    def _plan(self, profile, kind, argv):
        backend = providers._accounts_module()
        return {"env": {"CODEX_HOME" if profile["provider"] == "codex" else "XDG_DATA_HOME": profile["home"]},
                "unset_env": backend.UNSET_ENV}

    def models(self, name):
        return {"provider": self.records[name]["provider"], "models": [{"id": "synthetic", "label": "Test"}],
                "default_model": None, "source": "fixture", "notice": "Not entitlement proof", "private": "secret"}


def stream(*events):
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def codex_ok(text="{\"answer\":42}"):
    return stream({"type": "thread.started", "thread_id": "synthetic"}, {"type": "turn.started"},
                  {"type": "item.completed", "item": {"type": "reasoning", "text": "not exported"}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
                  {"type": "turn.completed", "usage": {"input_tokens": 24, "output_tokens": 8}})


SCHEMA = {"paths": {"/api/experimental/generate": {"post": {"operationId": "experimental.generate.text"}}}}
ISOLATION_NOTICE = (
    "Under-development features enabled: skip_host_skill_discovery. Under-development features are incomplete and may behave unpredictably. "
    "To suppress this warning, set `suppress_unstable_features_warning = true` in C:\\private-profile\\config.toml.")
CODE_MODE_NOTICE = (
    "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
    "enable `features.code_mode_host` and install `codex-code-mode-host`.")


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.runner = providers.ProviderRunner(Accounts())
        self.cleanup = patch.object(self.runner.backend, "_cleanup", side_effect=lambda p: setattr(p, "stopped", True)).start()
        self.command = patch.object(self.runner.backend, "provider_command", return_value=["native-fixture.exe"]).start()
        self.version = patch.object(self.runner, "_version", return_value="2.0.16").start()
        self.settle = patch.object(self.runner, "_settle_opencode").start()
        self.lease = patch.object(providers, "_new_lease", return_value=None).start()
        self.addCleanup(patch.stopall)

    def run_case(self, provider="codex", **overrides):
        args = {"profile_id": "first" if provider == "codex" else "second",
                "model": "test-model" if provider == "codex" else "opencode/test-model",
                "prompt": "Return JSON only: {answer:42}", "timeout_seconds": 5, "cancel_event": threading.Event()}
        args.update(overrides)
        return self.runner.run(**args)

    def test_profiles_and_catalog_are_allowlisted(self):
        self.runner.accounts.records["first"]["token"] = "never-return"
        self.runner.accounts.records["first"]["bindings"] = ["private-pane"]
        state = self.runner.profiles()
        self.assertEqual(state["default_profile"], "first")
        self.assertEqual(set(state["profiles"][0]), {"id", "label", "provider"})
        self.assertNotIn("selected-codex", json.dumps(state))
        self.assertNotIn("private", self.runner.models("first"))

    def test_profiles_omit_unsupported_or_unisolated_accounts(self):
        self.runner.accounts.records["first"]["custom_environment"] = True
        self.runner.accounts.records["other"] = {"id": "other", "provider": "gemini"}
        self.assertEqual([p["id"] for p in self.runner.profiles()["profiles"]], ["second"])

    def test_explicit_model_timeout_and_prompt_validation_never_spawns(self):
        with patch.object(providers.subprocess, "Popen") as popen:
            for values in ({"model": None}, {"model": "bad;command"}, {"timeout_seconds": 181},
                           {"timeout_seconds": float("nan")}, {"prompt": ""}, {"prompt": "x"*64001}):
                self.assertEqual(self.run_case(**values)["status"], "error")
            popen.assert_not_called()

    def test_pre_cancel_does_not_lookup_accounts_or_start_process(self):
        event = threading.Event(); event.set()
        with patch.object(self.runner.accounts, "get") as get:
            self.assertEqual(self.run_case(cancel_event=event)["status"], "cancelled")
            get.assert_not_called()

    def test_codex_protocol_and_selected_home_no_shell(self):
        proc = Process(codex_ok())
        env = {"CODEX_HOME": "wrong", "OPENAI_API_KEY": "secret", "CODEX_THREAD_ID": "old", "RADIO_HANDLE": "lead"}
        with patch.dict(os.environ, env), patch.object(providers.subprocess, "Popen", return_value=proc) as popen:
            result = self.run_case()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["text"], '{"answer":42}')
        self.assertEqual(result["usage"], {"input_tokens": 24, "output_tokens": 8, "cost_usd": None})
        self.assertIsNone(result["observed_model"])
        self.assertEqual(result["conditions"]["execution_kind"], "cli-agent")
        argv, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--strict-config"):
            self.assertIn(flag, argv)
        self.assertIn("features.shell_tool=false", argv)
        self.assertIn("features.unified_exec=false", argv)
        self.assertIn("features.plugins=false", argv)
        self.assertIn("features.hooks=false", argv)
        self.assertIn("features.view_image=false", argv)
        self.assertNotIn("tools.view_image=false", argv)
        self.assertIn("project_doc_max_bytes=0", argv)
        self.assertEqual(argv[argv.index("--sandbox")+1], "read-only")
        self.assertEqual(kwargs["env"]["CODEX_HOME"], "selected-codex")
        for key in ("OPENAI_API_KEY", "CODEX_THREAD_ID", "RADIO_HANDLE"):
            self.assertNotIn(key, kwargs["env"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(proc.stdin.saved, b"Return JSON only: {answer:42}")
        self.assertFalse(Path(kwargs["cwd"]).exists())
        self.assertTrue(proc.stopped)

    def test_codex_every_tool_type_rejects_and_stops_owned_process(self):
        for tool in ("command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list"):
            proc = Process(stream({"type": "item.started", "item": {"type": tool}}))
            with patch.object(providers.subprocess, "Popen", return_value=proc):
                result = self.run_case()
            self.assertEqual(result["status"], "error", tool)
            self.assertEqual(result["text"], "")
            self.assertTrue(proc.stopped)
            self.assertIn("tool action", result["error"])
            self.assertEqual(result["error_code"], "tool_attempt")

    def test_codex_verified_startup_notices_allow_final_answer_and_export_only_codes(self):
        notices = stream(*({"type": "item.completed", "item": {"type": "error", "message": message}}
                           for message in (ISOLATION_NOTICE, CODE_MODE_NOTICE, CODE_MODE_NOTICE)))
        with patch.object(providers.subprocess, "Popen", return_value=Process(notices + codex_ok())):
            result = self.run_case()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["notice_codes"], ["codex_experimental_isolation", "codex_code_mode_disabled"])
        self.assertTrue(all(code in providers.NOTICE_MESSAGES for code in result["notice_codes"]))
        self.assertNotIn("private-profile", json.dumps(result))
        self.assertEqual(result["text"], '{"answer":42}')

    def test_codex_notice_allowlist_is_pre_turn_exact_and_not_success_on_its_own(self):
        for prefix, message in ((b"", "Unknown startup failure private-secret"),
                                (b"", ISOLATION_NOTICE.replace("skip_host_skill_discovery", "another_feature")),
                                (b"", CODE_MODE_NOTICE + "private-secret"),
                                (stream({"type": "turn.started"}), CODE_MODE_NOTICE)):
            payload = prefix + stream({"type": "item.completed", "item": {"type": "error", "message": message}}) + codex_ok()
            with patch.object(providers.subprocess, "Popen", return_value=Process(payload)):
                result = self.run_case()
            self.assertEqual(result["error_code"], "provider_error")
            self.assertEqual(result["notice_codes"], [])
            self.assertNotIn("private-secret", json.dumps(result))
        only_notice = stream({"type": "item.completed", "item": {"type": "error", "message": CODE_MODE_NOTICE}})
        with patch.object(providers.subprocess, "Popen", return_value=Process(only_notice)):
            self.assertEqual(self.run_case()["status"], "error")

    def test_codex_unknown_items_fail_closed_without_claiming_tool_execution(self):
        for item in ({"type": "unknown_future_tool"}, None, "wrong shape"):
            with patch.object(providers.subprocess, "Popen", return_value=Process(stream({"type": "item.started", "item": item}))):
                result = self.run_case()
            self.assertEqual(result["error_code"], "unsupported_protocol")
            self.assertEqual(result["text"], "")

    def test_codex_item_error_uses_safe_error_classification(self):
        payload = stream({"type": "item.completed", "item": {"type": "error", "message": "401 Unauthorized private-secret"}})
        with patch.object(providers.subprocess, "Popen", return_value=Process(payload)):
            result = self.run_case()
        self.assertEqual(result["error_code"], "authorization")
        self.assertNotIn("private-secret", json.dumps(result))

    def test_codex_native_error_is_not_echoed(self):
        proc = Process(stream({"type": "error", "message": "bearer-secret path C:/private"}))
        with patch.object(providers.subprocess, "Popen", return_value=proc):
            result = self.run_case()
        self.assertNotIn("bearer-secret", json.dumps(result))
        self.assertEqual(result["status"], "error")

    def test_codex_startup_config_error_is_actionable_without_exposing_stderr(self):
        proc = Process(code=1, stderr=b"Error loading config.toml: unknown configuration field `tools.view_image` in -c/--config override\nPRIVATE-TOKEN=abc\nC:/private-path")
        with patch.object(providers.subprocess, "Popen", return_value=proc):
            result = self.run_case()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "configuration_error")
        self.assertEqual(result["error"], providers.ERROR_MESSAGES["configuration_error"])
        for private in ("PRIVATE-TOKEN", "private-path", "tools.view_image"):
            self.assertNotIn(private, json.dumps(result))
        self.assertTrue(proc.stopped)

    def test_native_diagnostics_classify_only_known_categories(self):
        fixtures = {"error: unexpected argument '--new-flag'": "unsupported_protocol",
                    "401 Unauthorized token: PRIVATE": "authorization",
                    "Rate limit exceeded; secret=PRIVATE": "rate_limit",
                    "The model is not supported": "model_unavailable",
                    "private native error": "provider_error"}
        for raw, code in fixtures.items():
            self.assertEqual(providers._diagnostic_code(raw), code)

    def test_diagnostics_are_bounded_but_entire_stream_is_drained(self):
        raw = b"Error loading config.toml\n" + b"x"*(providers.MAX_DIAGNOSTIC_BYTES*3)
        stream = io.BytesIO(raw)
        diagnostics = providers._Diagnostics()
        diagnostics.read(stream)
        self.assertEqual(len(diagnostics.data), providers.MAX_DIAGNOSTIC_BYTES)
        self.assertEqual(stream.tell(), len(raw))
        self.assertEqual(diagnostics.code(), "configuration_error")

    def test_codex_requires_completed_turn_and_clean_exit(self):
        for payload, code in ((codex_ok(), 1), (stream({"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}), 0)):
            with patch.object(providers.subprocess, "Popen", return_value=Process(payload, code)):
                self.assertEqual(self.run_case()["status"], "error")

    def test_codex_model_mismatch_is_rejected(self):
        payload = stream({"type": "thread.started", "model": "fallback-model"})
        with patch.object(providers.subprocess, "Popen", return_value=Process(payload)):
            result = self.run_case()
        self.assertEqual(result["status"], "error")
        self.assertIn("different model", result["error"])

    def test_codex_large_text_or_stream_rejected(self):
        for payload in (codex_ok("x"*32001), b"x"*(providers.MAX_BYTES+1)):
            with patch.object(providers.subprocess, "Popen", return_value=Process(payload)):
                self.assertEqual(self.run_case()["status"], "error")

    def test_opencode_one_stateless_request_no_session_and_unknown_usage(self):
        proc = Process(b'{"url":"http://127.0.0.1:54321"}\n')
        with patch.object(providers.subprocess, "Popen", return_value=proc) as popen, \
                patch.object(providers, "_request", side_effect=[SCHEMA, {"data": {"text": "42"}}]) as request:
            result = self.run_case("opencode")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["adapter"], "opencode-native-stateless")
        self.assertEqual(result["conditions"]["tool_policy"], "no-tools")
        self.assertEqual(result["usage"], {"input_tokens": None, "output_tokens": None, "cost_usd": None})
        self.assertIsNone(result["observed_model"])
        self.assertEqual(request.call_count, 2)
        body = request.call_args.args[3]
        self.assertEqual(body["model"], {"providerID": "opencode", "id": "test-model"})
        self.assertEqual(set(body), {"model", "prompt"})
        self.assertEqual(request.call_args.args[2], "POST")
        argv, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(argv[-2:], ["--port", "0"])
        self.assertIn("--stdio", argv)
        self.assertEqual(kwargs["env"]["XDG_DATA_HOME"], "selected-data")
        self.assertEqual(kwargs["env"]["OPENCODE_CONFIG_PROJECT_DISABLE"], "1")
        self.assertTrue(kwargs["env"]["OPENCODE_CONFIG_DIR"].startswith(kwargs["cwd"]))
        self.assertNotIn(kwargs["env"]["OPENCODE_PASSWORD"], json.dumps(result))
        self.assertEqual(self.settle.call_args.kwargs["directory"], kwargs["env"]["OPENCODE_CONFIG_DIR"])
        self.assertTrue(proc.stopped)

    def test_opencode_catalog_matches_stateless_generation_location(self):
        settled = {"data": [{"providerID": "opencode", "id": "test-model", "enabled": True}]}
        event = unittest.mock.Mock()
        event.is_set.return_value = False
        directory = "C:/isolated config/ö & config"
        with patch.object(providers, "_request", return_value=settled) as request, \
                patch.object(providers.time, "monotonic", side_effect=[0, .1, .1, 2.1]):
            providers.ProviderRunner._settle_opencode("http://127.0.0.1:1", "ephemeral", "opencode/test-model", 10, event, directory=directory)
        query = providers.urllib.parse.parse_qs(providers.urllib.parse.urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query, {"location[directory]": [directory]})
        self.assertEqual(request.call_args.args[2], "GET")

    def test_opencode_structured_http_failures_are_classified_without_raw_details(self):
        cases = [
            (503, {"_tag": "ServiceUnavailableError", "message": "OpenCode's free tier can only be used from within OpenCode"}, "opencode_free_tier"),
            (400, {"_tag": "InvalidRequestError", "message": "Model unavailable: private/model"}, "model_unavailable"),
            (503, {"_tag": "ServiceUnavailableError", "message": "Generation credentials are unavailable"}, "authorization"),
            (503, {"_tag": "ServiceUnavailableError", "message": "private-secret-unknown-failure"}, "provider_error"),
            (400, {"_tag": "InvalidRequestError", "kind": "Query", "message": "private-query"}, "unsupported_protocol"),
            (503, {"_tag": "Other", "message": "OpenCode's free tier can only be used from within OpenCode"}, "provider_error"),
            (401, {}, "authorization"), (429, {}, "rate_limit"),
        ]
        for status, payload, expected in cases:
            with self.subTest(status=status, expected=expected):
                body = io.BytesIO(json.dumps(payload).encode())
                error = providers.urllib.error.HTTPError('http://127.0.0.1:1/private', status, 'private-reason', {}, body)
                opener = unittest.mock.Mock()
                opener.open.side_effect = error
                with patch.object(providers.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(providers._Failure) as failure:
                        providers._http('http://127.0.0.1:1', 'private-password', 'POST', {}, 1)
                self.assertEqual(failure.exception.code, expected)
                self.assertEqual(failure.exception.message, providers.ERROR_MESSAGES[expected])
                self.assertNotIn('private', failure.exception.message)
                self.assertTrue(body.closed)
        self.assertEqual(providers._opencode_http_error(503, b'x' * (providers.MAX_DIAGNOSTIC_BYTES + 1)), 'provider_error')
        self.assertEqual(providers._opencode_http_error(503, b'not json'), 'provider_error')

    def test_old_opencode_endpoint_stops_before_inference(self):
        proc = Process(b'{"url":"http://127.0.0.1:54321"}\n')
        with patch.object(providers.subprocess, "Popen", return_value=proc), patch.object(providers, "_request", return_value={}) as request:
            result = self.run_case("opencode")
        self.assertEqual(result["status"], "error")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[2], "GET")
        self.assertTrue(proc.stopped)

    def test_opencode_never_accepts_remote_or_inherited_endpoint(self):
        for url in ("https://evil.example", "http://localhost:123", "http://user:pass@127.0.0.1:123"):
            proc = Process(json.dumps({"url": url}).encode()+b"\n")
            with patch.object(providers.subprocess, "Popen", return_value=proc), patch.object(providers, "_request") as request:
                result = self.run_case("opencode")
            self.assertEqual(result["status"], "error")
            request.assert_not_called()
            self.assertTrue(proc.stopped)

    def test_opencode_cancel_during_request_stops_only_owned_child(self):
        proc = Process(b'{"url":"http://127.0.0.1:54321"}\n')
        with patch.object(providers.subprocess, "Popen", return_value=proc), \
                patch.object(providers, "_request", side_effect=[SCHEMA, providers._Failure("cancelled", "Cancelled")]):
            result = self.run_case("opencode")
        self.assertEqual(result["status"], "cancelled")
        self.cleanup.assert_called_once_with(proc)

    def test_opencode_settles_provisional_catalog_before_model_selection(self):
        provisional = {"data": [{"providerID": "opencode", "id": "provisional", "enabled": True}]}
        settled = {"data": [{"providerID": "opencode", "id": "test-model", "enabled": True}]}
        event = unittest.mock.Mock()
        event.is_set.return_value = False
        with patch.object(providers, "_request", side_effect=[provisional, settled, settled]) as request, \
                patch.object(providers.time, "monotonic", side_effect=[0, .1, .1, 1.1, 1.1, 2.1]):
            providers.ProviderRunner._settle_opencode("http://127.0.0.1:1", "ephemeral", "opencode/test-model", 10, event)
        self.assertEqual(request.call_count, 3)
        self.assertTrue(all(call.args[2] == "GET" for call in request.call_args_list))

    def test_opencode_disabled_model_prevents_generation(self):
        proc = Process(b'{"url":"http://127.0.0.1:54321"}\n')
        self.settle.side_effect = providers._Failure("error", "Not enabled; no inference sent")
        with patch.object(providers.subprocess, "Popen", return_value=proc), patch.object(providers, "_request", return_value=SCHEMA) as request:
            result = self.run_case("opencode")
        self.assertEqual(result["status"], "error")
        self.assertEqual(request.call_count, 1)
        self.assertTrue(proc.stopped)

    def test_request_timeout_and_cancel_have_no_retry(self):
        event = threading.Event()
        with patch.object(providers, "_http", side_effect=lambda *a: time.sleep(.1)) as request:
            with self.assertRaises(providers._Failure) as caught:
                providers._request("http://127.0.0.1:1", "ephemeral", "POST", {}, time.monotonic()+.02, event)
            self.assertEqual(caught.exception.status, "timeout")
            self.assertEqual(request.call_count, 1)

    def test_raw_spawn_errors_and_account_failures_are_safe(self):
        with patch.object(providers.subprocess, "Popen", side_effect=OSError("private-secret")):
            result = self.run_case()
        self.assertNotIn("private-secret", json.dumps(result))
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "ownership_failed")

    def test_owned_job_attach_before_resume_and_any_prompt(self):
        events = []
        proc = Process(codex_ok())
        lease = unittest.mock.Mock()
        lease.attach.side_effect = lambda p: events.append(("attach", p.stdin.getvalue()))
        lease.resume.side_effect = lambda p: events.append(("resume", p.stdin.getvalue()))
        lease.close.side_effect = lambda: events.append(("close", b""))
        self.lease.return_value = lease
        with patch.object(providers.subprocess, "Popen", return_value=proc) as popen:
            result = self.run_case()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(events[:2], [("attach", b""), ("resume", b"")])
        self.assertEqual(events[-1][0], "close")
        self.assertTrue(popen.call_args.kwargs["creationflags"] & 0x4)
        self.cleanup.assert_called_once_with(proc)

    def test_job_containment_failure_never_resumes_or_sends_input(self):
        proc = Process(codex_ok())
        lease = unittest.mock.Mock()
        lease.attach.side_effect = OSError("private platform details")
        self.lease.return_value = lease
        with patch.object(providers.subprocess, "Popen", return_value=proc):
            result = self.run_case()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_code"], "ownership_failed")
        self.assertEqual(proc.stdin.getvalue(), b"")
        lease.resume.assert_not_called()
        lease.close.assert_called_once()
        self.cleanup.assert_called_once_with(proc)
        self.assertNotIn("private platform", json.dumps(result))

    def test_finish_closes_job_before_cleanup_even_after_wrapper_exit(self):
        events = []
        proc = Process()
        proc._watchtower_model_lab_lease = unittest.mock.Mock()
        proc._watchtower_model_lab_lease.close.side_effect = lambda: events.append("close-job")
        providers._finish(proc, lambda _: events.append("cleanup"))
        self.assertEqual(events, ["close-job", "cleanup"])

    def test_error_codes_are_allowlisted(self):
        self.assertEqual(providers._Failure("error", "safe", "arbitrary secret").code, "provider_error")
        event = threading.Event(); event.set()
        self.assertEqual(self.run_case(cancel_event=event)["error_code"], "cancelled")

    def test_version_probe_is_bounded_and_allowlisted(self):
        for output, expected in ((b"codex-cli 0.157.1\n", "0.157.1"),
                                 (b"opencode v2.0.16\r\n", "2.0.16"),
                                 (b"2.0.16\n", "2.0.16"),
                                 (b"opencode v2.0.17-beta.1\n", "2.0.17-beta.1")):
            proc = Process(output)
            with self.subTest(output=output), tempfile.TemporaryDirectory() as cwd, \
                    patch.object(providers.subprocess, "Popen", return_value=proc) as popen:
                version = providers.ProviderRunner._version(self.runner, ["native.exe"], {}, cwd, time.monotonic()+1, threading.Event())
                self.assertEqual(version, expected)
                self.assertEqual(popen.call_args.args[0], ["native.exe", "--version"])
                self.assertTrue(proc.stopped)

    def test_version_probe_rejects_unknown_output_and_still_cleans_up(self):
        for output in (b"Welcome\n", b"opencode v2.0\n", b"opencode v2.0.16 extra\n", b""):
            proc = Process(output)
            with self.subTest(output=output), tempfile.TemporaryDirectory() as cwd, \
                    patch.object(providers.subprocess, "Popen", return_value=proc):
                with self.assertRaises(providers._Failure) as error:
                    providers.ProviderRunner._version(self.runner, ["native.exe"], {}, cwd, time.monotonic()+1, threading.Event())
                self.assertEqual(error.exception.code, "unsupported_protocol")
                self.assertTrue(proc.stopped)

    def test_opencode_native_version_prefix_reaches_one_stateless_generation(self):
        version = Process(b"opencode v2.0.16\r\n")
        server = Process(b'{"url":"http://127.0.0.1:54321"}\n')
        # Exercise the real probe instead of the shared test fixture's stub.
        probe = providers.ProviderRunner._version.__get__(self.runner)
        with patch.object(self.runner, "_version", probe), \
                patch.object(providers.subprocess, "Popen", side_effect=[version, server]) as popen, \
                patch.object(providers, "_request", side_effect=[SCHEMA, {"data": {"text": "42"}}]) as request:
            result = self.run_case("opencode")
        self.assertEqual((result["status"], result["provider_version"], result["text"]), ("ok", "2.0.16", "42"))
        self.assertEqual(popen.call_args_list[0].args[0], ["native-fixture.exe", "--version"])
        self.assertEqual([call.args[2] for call in request.call_args_list], ["GET", "POST"])
        self.assertTrue(version.stopped and server.stopped)


class NativeCodexStartupTests(unittest.TestCase):
    def test_installed_codex_accepts_exact_config_before_empty_stdin_exit(self):
        """Real CLI parsing boundary, fresh auth home and empty prompt: no inference.

        The broken legacy tools.view_image key used to fail strict-config here.
        A mocked JSON event stream cannot detect this compatibility regression.
        """
        runner = providers.ProviderRunner(accounts=object())
        try:
            command = runner.backend.provider_command("codex")
        except Exception:
            self.skipTest("Codex CLI is not installed")
        with tempfile.TemporaryDirectory(prefix="model-lab-config-test-") as cwd:
            argv = []
            def capture(args, *unused):
                argv.extend(args)
                raise RuntimeError("Capture argv without starting inference")
            with patch.object(providers, "_spawn", side_effect=capture), self.assertRaises(RuntimeError):
                runner._codex({"model": "gpt-6-luna"}, command, {}, cwd, "", time.monotonic()+10, threading.Event())
            env = runner.backend.plan_environment({"env": {"CODEX_HOME": cwd}, "unset_env": runner.backend.UNSET_ENV})
            env = runner._neutral_env(env, "codex", cwd)
            options = providers._options(env, cwd)
            options["stderr"] = subprocess.PIPE
            process = providers._spawn(argv, options, runner.backend._cleanup)
            try:
                stdout, stderr = process.communicate(input=b"", timeout=10)
                self.assertEqual(process.returncode, 1)
                self.assertEqual(stdout, b"")
                self.assertIn(b"No prompt provided via stdin", stderr)
                self.assertNotIn(b"Error loading config", stderr)
                self.assertNotIn(b"unknown configuration field", stderr)
            finally:
                providers._finish(process, runner.backend._cleanup)
                if process.stderr is not None:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
