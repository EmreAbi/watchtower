"""Exercise the real identity checks against synthetic pane metadata and SQLite.

No provider processes, personal ledgers, credentials or session files are opened.
"""
from copy import deepcopy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import review_engine
from review_engine import ReviewError, ReviewSystem
from workflow import Workflow


class ReviewIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.team = self.root / "mixed-team"
        self.team.mkdir()
        self.radio = self.root / "radio"
        self.radio.mkdir()
        self.config = {
            "workspace_id": "w12", "herdr": str(self.root / "herdr.exe"), "radio_home": str(self.radio),
            "roles": {role: {"provider": provider, "cwd": str(self.team)}
                      for role, provider in (("lead", "codex"), ("worker", "opencode"), ("reviewer", "opencode"))},
            "review": {"lead": "lead", "local_reviewer": "reviewer", "global_reviewer": None,
                       "global_mode": "on_demand", "required": True},
        }
        self.write_json(self.team / "config.json", self.config)
        self.panes = {}
        with closing(sqlite3.connect(self.radio / "radio.db")) as connection, connection:
            connection.execute("CREATE TABLE handles (name TEXT, session_ref TEXT, agent TEXT, agent_session TEXT, "
                               "workspace TEXT, pane_workspace TEXT)")
            for role, spec in self.config["roles"].items():
                pane = "w12:p-" + role
                session = "session-" + role
                self.panes[pane] = {"pane_id": pane, "workspace_id": "w12", "agent": spec["provider"],
                                    "agent_session": {"value": session}, "label": role, "cwd": str(self.team)}
                connection.execute("INSERT INTO handles VALUES (?,?,?,?,?,?)",
                                   (role, "herdr:" + pane, spec["provider"], session, "w12", "w12"))
        env = patch.dict(os.environ, {"HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w12",
                                     "RADIO_HOME": str(self.radio)}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        command = patch.object(review_engine, "run", side_effect=self.read_pane)
        command.start()
        self.addCleanup(command.stop)
        self.engine = ReviewSystem(self.root)
        self.activate("lead")

    def write_json(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def read_pane(self, args):
        self.assertEqual(args[:3], [self.config["herdr"], "pane", "get"])
        return SimpleNamespace(stdout=json.dumps({"result": {"pane": self.panes[args[3]]}}).encode())

    def activate(self, role):
        os.environ.update(HERDR_PANE_ID="w12:p-" + role, RADIO_HANDLE="w12:" + role,
                          RADIO_JOINED_SCOPE="w12")
        # OpenCode can inherit an unrelated Codex variable from an outer shell.
        os.environ["CODEX_THREAD_ID"] = "session-" + role if role == "lead" else "outer-codex-session"

    def update_ledger(self, clause, parameters):
        with closing(sqlite3.connect(self.radio / "radio.db")) as connection, connection:
            connection.execute("UPDATE handles SET " + clause + " WHERE name='reviewer'", parameters)

    def test_mixed_providers_use_exact_live_pane_and_ledger_identity(self):
        for role, provider in (("lead", "codex"), ("worker", "opencode"), ("reviewer", "opencode")):
            with self.subTest(role=role):
                self.activate(role)
                actual = self.engine.identity(self.config)
                self.assertEqual(actual, {"role": role, "workspace": "w12", "pane": "w12:p-" + role,
                                          "session": "session-" + role, "provider": provider})

    def test_legacy_role_without_provider_remains_codex_and_checks_thread_id(self):
        legacy = deepcopy(self.config)
        del legacy["roles"]["lead"]["provider"]
        self.assertEqual(self.engine.identity(legacy)["provider"], "codex")
        os.environ["CODEX_THREAD_ID"] = "another-codex-session"
        with self.assertRaises(ReviewError):
            self.engine.identity(legacy)

    def test_opencode_without_session_hook_or_ledger_backfill_fails_closed(self):
        self.activate("reviewer")
        pane = self.panes["w12:p-reviewer"]
        for session in (None, {}, {"value": ""}):
            pane["agent_session"] = session
            with self.subTest(session=session), self.assertRaises(ReviewError):
                self.engine.identity(self.config)
        pane["agent_session"] = {"value": "session-reviewer"}
        self.update_ledger("agent_session=?", (None,))
        with self.assertRaises(ReviewError):
            self.engine.identity(self.config)

    def test_opencode_provider_session_workspace_label_and_cwd_mismatches_fail_closed(self):
        self.activate("reviewer")
        pane = self.panes["w12:p-reviewer"]
        original = deepcopy(pane)
        for delta in ({"agent": "codex"}, {"agent_session": {"value": "replaced-session"}},
                      {"workspace_id": "w13"}, {"label": "lead"}, {"cwd": str(self.root)},
                      {"pane_id": "w12:p-other"}):
            pane.update(delta)
            with self.subTest(delta=delta), self.assertRaises(ReviewError):
                self.engine.identity(self.config)
            pane.clear()
            pane.update(deepcopy(original))
        for clause, values in (("agent=?", ("codex",)), ("workspace=?", ("w13",)),
                               ("pane_workspace=?", ("w13",)), ("agent_session=?", ("old-session",))):
            self.update_ledger(clause, values)
            with self.subTest(clause=clause), self.assertRaises(ReviewError):
                self.engine.identity(self.config)
            self.update_ledger("agent=?, workspace=?, pane_workspace=?, agent_session=?",
                               ("opencode", "w12", "w12", "session-reviewer"))

    def test_unsupported_provider_and_inherited_radio_identity_mismatch_fail_closed(self):
        self.activate("reviewer")
        other = deepcopy(self.config)
        other["roles"]["reviewer"]["provider"] = "claude"
        with self.assertRaises(ReviewError):
            self.engine.identity(other)
        for key, value in (("RADIO_HANDLE", "lead"), ("RADIO_JOINED_SCOPE", "w13"),
                           ("HERDR_WORKSPACE_ID", "w13"), ("RADIO_HOME", str(self.root)), ("HERDR_ENV", "0")):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}), self.assertRaises(ReviewError):
                self.engine.identity(self.config)

    def test_mixed_provider_workflow_requires_independent_opencode_reviewer(self):
        flow = Workflow(self.team, self.engine)
        begin = self.root / "begin.json"
        scope = {"task": "Deliver evidence", "acceptance_criteria": ["Evidence supports the result"]}
        self.write_json(begin, scope)
        artifact = self.root / "output.md"
        artifact.write_text("Verified evidence", encoding="utf-8")
        submission = self.root / "submission.json"
        self.write_json(submission, dict(scope, summary="Complete", artifacts=[{"path": str(artifact)}]))
        report = self.root / "report.md"
        report.write_text("Checked the exact evidence", encoding="utf-8")
        with patch.object(self.engine, "notify", return_value="sent"):
            flow.begin("task-1", begin)
            self.activate("worker")
            sha = flow.submit("task-1", submission)["digest"]
            with self.assertRaisesRegex(ReviewError, "independent reviewer"):
                flow.result("task-1", sha, "PASS", report)
            self.activate("reviewer")
            self.assertEqual(flow.result("task-1", sha, "PASS", report)["state"], "PASS")
            self.activate("lead")
            self.assertEqual(flow.complete("task-1", sha, "Evidence delivered")["state"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
