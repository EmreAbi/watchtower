"""Catalog and model-launch tests use synthetic payloads and owned fake children."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import backend
import integration
import models


def row(identifier="one", provider="example", enabled=True, **extra):
    return dict(id=identifier, providerID=provider, name="One model", enabled=enabled, **extra)


class CatalogTest(unittest.TestCase):
    def test_codex_allowlist_visibility_dedup_and_no_identity_export(self):
        payload = dict(identity={"secret":"never-export"}, models=[
            dict(slug="gpt-example", display_name="Example", visibility="list", instructions="private"),
            dict(slug="gpt-example", display_name="duplicate"),
            dict(slug="hidden", visibility="hide"), dict(slug="bad\nvalue"),
            dict(slug="other", display_name="bad\x1bvalue", visibility="list")])
        result = models.parse_codex(payload)
        self.assertEqual(result["models"], [{"id":"gpt-example","label":"Example"},{"id":"other","label":"other"}])
        self.assertIsNone(result["default_model"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("never-export", json.dumps(result))

    def test_codex_reads_selected_home_only_and_bounds_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as root:
            profile = {"home": root}
            path = Path(root).resolve() / "models_cache.json"
            self.assertEqual(models.codex_catalog(profile)["models"], [])
            path.write_text(json.dumps({"models":[{"slug":"my-model"}]}), encoding="utf-8")
            with patch("models.subprocess.Popen") as start:
                self.assertEqual(models.codex_catalog(profile)["models"][0]["id"], "my-model")
            start.assert_not_called()
            with patch("models.MAX_BYTES", 4):
                self.assertEqual(models.codex_catalog(profile)["models"], [])
            path.write_text("invalid")
            self.assertEqual(models.codex_catalog(profile)["models"], [])

    def test_opencode_available_enabled_models_and_default_allowlist(self):
        payload = {"location":{"directory":"private-path"}, "data":[
            row(), row(), row("hidden",enabled=False), row("nested/model"),
            row("bad value"), row("token",provider="bad provider"),
            {"id":"no-enabled", "providerID":"example"}]}
        result = models.parse_opencode(payload, {"data":row("nested/model")})
        self.assertEqual([item["id"] for item in result["models"]], ["example/one","example/nested/model"])
        self.assertEqual(result["default_model"], "example/nested/model")
        self.assertNotIn("private-path", json.dumps(result))
        self.assertIn("does not verify", result["notice"])
        self.assertIsNone(models.parse_opencode(payload,{"data":row("missing")})["default_model"])

    def test_payload_and_output_count_are_bounded(self):
        for bad in ([], {}, {"data":None}):
            with self.assertRaises(ValueError): models.parse_opencode(bad)
        with patch("models.MAX_MODELS", 2):
            result = models.parse_opencode({"data":[row(str(n)) for n in range(10)]})
            self.assertEqual(len(result["models"]), 2)

    def test_model_syntax_never_accepts_flags_controls_or_wrong_provider_format(self):
        for provider in ("codex","opencode","claude"):
            self.assertTrue(models.valid_model(provider,None))
            for value in ("", "--help", "x y", "x\ny", "x;z", 123, "a"*129):
                self.assertFalse(models.valid_model(provider,value))
        self.assertTrue(models.valid_model("codex","gpt-example"))
        self.assertFalse(models.valid_model("codex","openai/gpt-example"))
        self.assertTrue(models.valid_model("opencode","provider/nested/model:version"))
        self.assertFalse(models.valid_model("opencode","bare-model"))
        self.assertFalse(models.valid_model("claude","anything"))

    def test_endpoint_only_accepts_owned_loopback_plain_url(self):
        for url in ("https://127.0.0.1:40", "http://evil.example:40", "http://user:secret@127.0.0.1:40", "http://127.0.0.1:40/path"):
            ready = queue.Queue()
            models._read_endpoint(io.BytesIO((json.dumps({"url":url})+"\n").encode()), ready)
            self.assertIsNone(ready.get_nowait())
        ready = queue.Queue()
        models._read_endpoint(io.BytesIO(b'{"url":"http://127.0.0.1:1234"}\n'), ready)
        self.assertEqual(ready.get_nowait(), "http://127.0.0.1:1234")

    def test_private_server_waits_for_settlement_cleans_up_and_disables_project_config(self):
        process = Mock()
        process.stdout = io.BytesIO(b'{"url":"http://127.0.0.1:1234"}\n')
        process.stdin = io.BytesIO()
        process.poll.return_value = None
        cleanup = Mock()
        inherited = {"XDG_DATA_HOME":"selected-account", "opencode_password":"other-server-secret", "OPENCODE_MODELS_URL":"https://wrong"}
        responses = [{"data":[]},{"data":[row()]},{"data":row()}]
        with patch("models.subprocess.Popen",return_value=process) as start, \
                patch("models._json_request",side_effect=responses) as read, patch("models.time.sleep"), \
                patch("models.STABLE_WINDOW",0), patch("models.MIN_SETTLE",0):
            result = models.opencode_catalog(["native-opencode.exe"], inherited, cleanup)
        self.assertEqual(result["models"][0]["id"], "example/one")
        self.assertEqual(result["default_model"], "example/one")
        self.assertEqual(read.call_count,3)
        self.assertEqual(start.call_args.args[0], ["native-opencode.exe","serve","--stdio","--hostname","127.0.0.1","--port","0"])
        env = start.call_args.kwargs["env"]
        self.assertEqual(env["XDG_DATA_HOME"], "selected-account")
        self.assertEqual(env["OPENCODE_CONFIG_PROJECT_DISABLE"], "1")
        self.assertNotIn("OPENCODE_MODELS_URL",env)
        self.assertNotIn("other-server-secret",str(env))
        self.assertNotEqual(start.call_args.kwargs["cwd"], str(Path.cwd()))
        self.assertTrue(process.stdin.closed)
        cleanup.assert_called_once_with(process)
        self.assertNotIn(env["OPENCODE_PASSWORD"],json.dumps(result))

    def test_failed_or_empty_server_has_honest_fallback_and_owned_cleanup(self):
        for bad in (b'',b'{"url":"http://evil:99"}\n'):
            process=Mock(stdout=io.BytesIO(bad),stdin=io.BytesIO())
            cleanup=Mock()
            with patch("models.subprocess.Popen",return_value=process):
                result=models.opencode_catalog(["opencode"],{},cleanup)
            self.assertEqual(result["models"],[])
            self.assertIsNone(result["default_model"])
            cleanup.assert_called_once_with(process)
        with patch("models.subprocess.Popen",side_effect=OSError("private failure")):
            self.assertNotIn("private failure",json.dumps(models.opencode_catalog(["opencode"],{},Mock())))

    def test_snapshot_wait_is_bounded_when_no_models_are_connected(self):
        process=Mock(stdout=io.BytesIO(b'{"url":"http://127.0.0.1:1234"}\n'),stdin=io.BytesIO())
        process.poll.return_value=None
        cleanup=Mock()
        with patch("models.subprocess.Popen",return_value=process), patch("models._json_request",return_value={"data":[]}) as read, patch("models.SETTLE_TIMEOUT",0):
            result=models.opencode_catalog(["opencode"],{},cleanup)
        self.assertEqual(read.call_count,1)
        self.assertEqual(result["models"],[])
        self.assertIn("no enabled models",result["notice"])
        cleanup.assert_called_once_with(process)

    def test_provisional_catalog_is_not_returned_before_connection_policy_settles(self):
        process=Mock(stdout=io.BytesIO(b'{"url":"http://127.0.0.1:1234"}\n'),stdin=io.BytesIO())
        process.poll.return_value=None
        clock=[0.0]
        def sleep(seconds):clock[0]+=seconds
        def snapshot(url,password):
            if url.endswith("default"):return {"data":row("free")}
            if clock[0] == 0:return {"data":[]}
            if clock[0] < 1:return {"data":[row("free"),row("provisional-paid")]}
            return {"data":[row("free")]}
        with patch("models.subprocess.Popen",return_value=process),patch("models._json_request",side_effect=snapshot), \
                patch("models.time.monotonic",side_effect=lambda:clock[0]),patch("models.time.sleep",side_effect=sleep):
            result=models.opencode_catalog(["opencode"],{},Mock())
        self.assertGreaterEqual(clock[0],models.MIN_SETTLE)
        self.assertEqual([item["id"] for item in result["models"]],["example/free"])

    def test_cache_is_singleflight_per_profile_returns_copies_and_separates_identity(self):
        cache=models.CatalogCache()
        started,release=threading.Event(),threading.Event()
        def load():
            started.set();release.wait(timeout=2)
            return {"models":[{"id":"one"}]}
        loader=Mock(side_effect=load)
        with ThreadPoolExecutor(3) as pool:
            first=pool.submit(cache.read,("one","home-one"),loader)
            self.assertTrue(started.wait(1))
            second=pool.submit(cache.read,("one","home-one"),loader)
            third=pool.submit(cache.read,("one","home-one"),loader)
            release.set()
            values=[future.result(timeout=3) for future in (first,second,third)]
        loader.assert_called_once()
        values[0]["models"].clear()
        self.assertEqual(len(cache.read(("one","home-one"),loader)["models"]),1)
        cache.read(("one","changed-home"),loader)
        self.assertEqual(loader.call_count,2)


class NativeModelTest(unittest.TestCase):
    def test_codex_native_model_keeps_existing_radio_context_and_permissions(self):
        args=["codex","-c",'developer_instructions="Radio role"',"--sandbox","workspace-write"]
        with patch("backend.provider_command",return_value=["native-codex.exe"]):
            actual=integration._native_provider_argv({"provider":"codex","model":"gpt-example"},args)
            inherited=integration._native_provider_argv({"provider":"codex","model":None},args)
        self.assertEqual(actual[:3],["native-codex.exe","-m","gpt-example"])
        self.assertEqual(actual[5:],args[1:])
        self.assertNotIn("-m",inherited)
        self.assertNotIn("danger-full-access",actual)
        with patch("backend.provider_command",return_value=["codex"]),self.assertRaises(ValueError):
            integration._native_provider_argv({"model":"--help"},args)

    def test_launch_ticket_preserves_explicit_model_without_sending_task(self):
        with tempfile.TemporaryDirectory() as root:
            service=Mock(home=Path(root).resolve())
            with patch("integration.list_workspaces",return_value=[{"id":"w9","cwd":root}]), \
                    patch("integration._host_env",return_value=("watchtower.exe",{"HERDR_SOCKET_PATH":"private.sock"})), \
                    patch("integration._cli",return_value={"plugin_pane":{"pane":{"pane_id":"w9:p2","workspace_id":"w9"}}}):
                integration.launch_profile(service,"work","worker","w9",model="gpt-example")
            service.launch_plan.assert_called_once_with("work","worker",workspace="w9",model="gpt-example")
            tickets=list((Path(root).resolve()/"state/accounts/launches").glob("*.json"))
            self.assertEqual(len(tickets),1)
            self.assertEqual(json.loads(tickets[0].read_text())["model"],"gpt-example")


if __name__ == "__main__":
    unittest.main()
