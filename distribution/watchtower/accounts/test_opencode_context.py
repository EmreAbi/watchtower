"""Launch-only overlay tests against temporary settings; never account auth."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import opencode_context as context


class OpenCodeContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / "config" / "opencode"
        self.config.mkdir(parents=True)
        self.file = self.config / "cli.json"
        self.env = {"HERDR_ENV": "1", "HERDR_PANE_ID": "w8:p2", "HERDR_SOCKET_PATH": "synthetic-pipe",
                    "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_STATE_HOME": str(self.root / "state"),
                    "OPENCODE_CONFIG": str(self.root / "radio-brief.json"),
                    "OPENCODE_CONFIG_CONTENT": '{"server":{"port":0}}', "OTHER": "keep"}

    def plugins(self):
        return json.loads(self.env["OPENCODE_CLI_CONFIG_CONTENT"])["plugins"]

    def test_fresh_profile_injects_bundled_hook_without_writing_settings(self):
        original = deepcopy(self.env)
        result = context.prepare_context(self.env)
        self.assertEqual(result["state"], "prepared")
        self.assertEqual(self.plugins(), [str(context.HOOK_ROOT)])
        self.assertTrue((context.HOOK_ROOT / "tui.js").is_file())
        self.assertEqual({key: self.env[key] for key in original}, original)
        self.assertEqual(list(self.config.iterdir()), [])

    def test_jsonc_user_settings_and_plugin_order_are_preserved_byte_for_byte(self):
        text = '''{ // preserve comments
  "theme": {"name":"test"},
  "plugins": ["./own-plugin", {"package":"other-package","options":{"url":"https://a/b", "text":",] /*literal*/"}},],
}'''
        self.file.write_text(text, encoding="utf-8")
        context.prepare_context(self.env)
        self.assertEqual(self.plugins()[1:], ["./own-plugin", {"package": "other-package", "options": {
            "url": "https://a/b", "text": ",] /*literal*/"}}])
        self.assertEqual(self.file.read_text(encoding="utf-8"), text)
        # Native merge retains all disk settings; only plugins need an overlay.
        self.assertEqual(set(json.loads(self.env["OPENCODE_CLI_CONFIG_CONTENT"])), {"plugins"})

    def test_existing_inline_override_keeps_native_array_precedence_and_preferences(self):
        self.file.write_text('{"plugins":["disk-plugin"]}', encoding="utf-8")
        self.env["OPENCODE_CLI_CONFIG_CONTENT"] = '{"plugins":["inline-plugin"],"session":{"thinking":"hide"}}'
        context.prepare_context(self.env)
        overlay = json.loads(self.env["OPENCODE_CLI_CONFIG_CONTENT"])
        self.assertEqual(overlay["plugins"][1:], ["inline-plugin"])
        self.assertEqual(overlay["session"], {"thinking": "hide"})
        first = self.env["OPENCODE_CLI_CONFIG_CONTENT"]
        context.prepare_context(self.env)
        self.assertEqual(self.env["OPENCODE_CLI_CONFIG_CONTENT"], first)

    def test_disabled_integration_remains_disabled_with_no_env_or_disk_changes(self):
        for selector in ("-*", "-herdr.*", "-herdr.opencode.session-selection"):
            text = json.dumps({"plugins": [selector]})
            self.file.write_text(text, encoding="utf-8")
            original = deepcopy(self.env)
            with self.subTest(selector=selector), self.assertRaisesRegex(context.OpenCodeContextError, "disabled"):
                context.prepare_context(self.env)
            self.assertEqual(self.env, original)
            self.assertEqual(self.file.read_text(encoding="utf-8"), text)

    def test_explicit_reenable_after_disable_retains_user_order(self):
        self.file.write_text('{"plugins":["-herdr.*","herdr.opencode.session-selection"]}', encoding="utf-8")
        context.prepare_context(self.env)
        self.assertEqual(self.plugins()[1:], ["-herdr.*", "herdr.opencode.session-selection"])

    def test_invalid_or_oversized_settings_fail_without_env_mutation(self):
        for text in ('{"plugins":false}', '{"plugins":[7]}', '{"plugins":[{"package":true}]}',
                     '{"plugins":[{"package":"name","options":false}]}',
                     '{"plugins":["bad"] /* unfinished', '{"plugins":[NaN]}', '[]',
                     '{"plugins":["' + ('a' * context.MAX_CONFIG_BYTES) + '"]}'):
            self.file.write_text(text, encoding="utf-8")
            original = deepcopy(self.env)
            with self.subTest(length=len(text)), self.assertRaises(context.OpenCodeContextError):
                context.prepare_context(self.env)
            self.assertEqual(self.env, original)

    def test_legacy_migration_is_not_silently_skipped(self):
        legacy = self.config / "tui.json"
        legacy.write_text('{"plugin":["keep"]}', encoding="utf-8")
        with self.assertRaisesRegex(context.OpenCodeContextError, "migration"):
            context.prepare_context(self.env)
        self.assertFalse(self.file.exists())
        self.assertNotIn("OPENCODE_CLI_CONFIG_CONTENT", self.env)

    def test_scoped_config_override_and_no_credential_reads(self):
        separate = self.root / "scoped"
        separate.mkdir()
        (separate / "cli.json").write_text('{"plugins":["scoped-plugin"]}', encoding="utf-8")
        self.env["OPENCODE_CONFIG_DIR"] = str(separate)
        original_open = Path.open
        opened = []

        def guarded(path, *args, **kwargs):
            self.assertEqual(path, separate / "cli.json")
            opened.append(path)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded):
            context.prepare_context(self.env)
        self.assertEqual(opened, [separate / "cli.json"])
        self.assertEqual(self.plugins()[1:], ["scoped-plugin"])

    def test_missing_pane_hook_or_relative_profile_path_cannot_claim_ready(self):
        for changes in ({"HERDR_ENV": "0"}, {"HERDR_PANE_ID": ""}, {"HERDR_SOCKET_PATH": ""},
                        {"XDG_CONFIG_HOME": "relative"}):
            env = dict(self.env, **changes)
            with self.subTest(changes=changes), self.assertRaises(context.OpenCodeContextError):
                context.prepare_context(env)
        with patch.object(context, "HOOK_ROOT", self.root / "missing-hook"), self.assertRaises(context.OpenCodeContextError):
            context.prepare_context(self.env)

    def test_bundled_hook_reports_selected_root_session_without_real_socket(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable for the dependency-free hook harness")
        script = r'''
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import net from "node:net";
import { pathToFileURL } from "node:url";
const writes = [];
const endpoints = [];
net.createConnection = (endpoint, onConnect) => {
  endpoints.push(endpoint);
  const socket = new EventEmitter();
  socket.destroy = () => {};
  socket.write = (text) => {
    writes.push(JSON.parse(text));
    queueMicrotask(() => socket.emit("data", Buffer.from("ok")));
  };
  queueMicrotask(onConnect);
  return socket;
};
Object.assign(process.env, {HERDR_ENV:"1", HERDR_PANE_ID:"w8:p2", HERDR_SOCKET_PATH:"synthetic-hook-pipe"});
const plugin = (await import(pathToFileURL(process.argv[1]).href)).default;
assert.equal(plugin.id, "herdr.opencode.session-selection");
const sessions = new Map([
  ["root-a", {id:"root-a"}], ["child-a", {id:"child-a", parentID:"root-a"}],
  ["root-b", {id:"root-b"}]
]);
let route = {type:"session", sessionID:"root-a"};
let receive;
const api = {
  ui:{router:{current:() => route}},
  data:{
    session:{get:(id) => sessions.get(id), family:(id) => id === "root-a" ? ["child-a"] : [],
      status:() => "idle", permission:{list:() => []}, form:{list:() => []}},
    listen:(callback) => {receive=callback; return () => {};}
  }
};
const pause = () => new Promise((resolve) => setTimeout(resolve, 15));
const dispose = plugin.setup(api);
try {
  await pause();
  assert(writes.some((r) => r.method === "pane.report_agent_session" && r.params.agent_session_id === "root-a"));
  assert(writes.every((r) => r.params.agent === "opencode" && r.params.pane_id === "w8:p2"));
  route = {type:"session", sessionID:"child-a"};
  receive({details:{type:"session.execution.started", data:{sessionID:"child-a"}}});
  await pause();
  assert(writes.every((r) => r.params.agent_session_id === "root-a"));
  writes.length = 0;
  route = {type:"session", sessionID:"root-b"};
  receive({details:{type:"session.execution.started", data:{sessionID:"root-b"}}});
  await pause();
  assert(writes.some((r) => r.method === "pane.report_agent_session" && r.params.agent_session_id === "root-b"));
  assert(writes.every((r) => r.params.agent_session_id === "root-b"));
  assert(writes.some((r) => r.method === "pane.report_agent" && r.params.state === "working"));
} finally {
  dispose();
}
const before = writes.length;
receive({details:{type:"session.execution.succeeded", data:{sessionID:"root-b"}}});
await pause();
assert.equal(writes.length, before);
assert(endpoints.every((value) => value.endsWith("synthetic-hook-pipe")));
console.log(JSON.stringify({root_session_reports:true, child_session_promoted:false, real_socket_opened:false}));
'''
        result = subprocess.run([node, "--input-type=module", "--eval", script, str(context.HOOK_ROOT / "tui.js")],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"root_session_reports": True,
                                                    "child_session_promoted": False, "real_socket_opened": False})


if __name__ == "__main__":
    unittest.main()
