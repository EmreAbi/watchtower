"""Hermetic OS-tree proofs; live smoke uses only disposable shell/Python jobs."""
from contextlib import ExitStack
from dataclasses import replace
import unittest
from unittest.mock import patch

import switch_process as process


class Held:
    def __init__(self, name, created, alive=True):
        self.name, self.created, self.running = name, created, alive

    def alive(self):
        return self.running

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class ProcessTests(unittest.TestCase):
    def proof(self, method, entries, *, second=None, created=None, dead=(), shell=100):
        table = {entry.pid: entry for entry in entries}
        created = created or {pid: pid for pid in table}
        with ExitStack() as patches:
            patches.enter_context(patch.object(process, "_WINDOWS", True))
            patches.enter_context(patch.object(process.os, "getpid", return_value=300))
            patches.enter_context(patch.object(process, "_snapshot", side_effect=[table, second or table]))
            patches.enter_context(patch.object(process, "_Process", side_effect=lambda pid:
                Held(table[pid].name, created[pid], pid not in dead)))
            return method(shell)

    def test_direct_runner_and_venv_python_redirector_belong_to_exact_shell(self):
        root = process._Entry(100, 1, "powershell.exe")
        self.assertTrue(self.proof(process.runner_belongs_to_shell,
                                  [root, process._Entry(300, 100, "python.exe")]))
        self.assertTrue(self.proof(process.runner_belongs_to_shell,
                                  [root, process._Entry(200, 100, "python.exe"),
                                   process._Entry(300, 200, "python.exe")]))

    def test_api_foreground_projection_is_not_used_to_prove_ownership(self):
        entries = [process._Entry(100, 1, "powershell.exe"),
                   process._Entry(300, 999, "python.exe")]
        self.assertFalse(self.proof(process.runner_belongs_to_shell, entries))

    def test_non_python_wrapper_and_too_long_chain_are_rejected(self):
        root = process._Entry(100, 1, "powershell.exe")
        self.assertFalse(self.proof(process.runner_belongs_to_shell,
                                   [root, process._Entry(200, 100, "cmd.exe"),
                                    process._Entry(300, 200, "python.exe")]))
        chain = [root, process._Entry(300, 250, "python.exe"),
                 process._Entry(250, 240, "python.exe"), process._Entry(240, 230, "python.exe"),
                 process._Entry(230, 220, "python.exe"), process._Entry(220, 100, "python.exe")]
        self.assertFalse(self.proof(process.runner_belongs_to_shell, chain))

    def test_parent_pid_reuse_or_reparenting_is_rejected(self):
        entries = [process._Entry(100, 1, "powershell.exe"), process._Entry(300, 100, "python.exe")]
        self.assertFalse(self.proof(process.runner_belongs_to_shell, entries,
                                   created={100: 500, 300: 400}))
        changed = {entry.pid: entry for entry in entries}
        changed[300] = replace(changed[300], parent_pid=999)
        self.assertFalse(self.proof(process.runner_belongs_to_shell, entries, second=changed))

    def test_sibling_work_is_rejected_but_runners_own_metadata_child_is_allowed(self):
        entries = [process._Entry(100, 1, "powershell.exe"), process._Entry(300, 100, "python.exe")]
        self.assertFalse(self.proof(process.runner_belongs_to_shell,
                                   entries + [process._Entry(400, 100, "python.exe")]))
        self.assertTrue(self.proof(process.runner_belongs_to_shell,
                                  entries + [process._Entry(400, 300, "watchtower.exe")]))

    def test_dead_generation_and_wrong_root_are_rejected(self):
        entries = [process._Entry(100, 1, "powershell.exe"), process._Entry(300, 100, "python.exe")]
        self.assertFalse(self.proof(process.runner_belongs_to_shell, entries, dead=(100,)))
        entries[0] = replace(entries[0], name="cmd.exe")
        self.assertFalse(self.proof(process.runner_belongs_to_shell, entries))

    def test_idle_shell_excludes_every_child_including_non_agent_python(self):
        root = process._Entry(100, 1, "pwsh.exe")
        self.assertTrue(self.proof(process.shell_has_no_children, [root]))
        for name in ("python.exe", "notepad.exe", "codex.exe"):
            self.assertFalse(self.proof(process.shell_has_no_children,
                                       [root, process._Entry(200, 100, name)]))

    def test_unrelated_process_is_not_a_shell_child(self):
        self.assertTrue(self.proof(process.shell_has_no_children,
                                  [process._Entry(100, 1, "powershell.exe"),
                                   process._Entry(400, 999, "python.exe")]))

    def test_unsupported_unverifiable_and_invalid_pids_fail_closed(self):
        for method in (process.runner_belongs_to_shell, process.shell_has_no_children):
            for pid in (None, True, 0, -1, "100", 2**40):
                self.assertFalse(method(pid))
            with patch.object(process, "_WINDOWS", False):
                self.assertFalse(method(100))
            with patch.object(process, "_WINDOWS", True), \
                    patch.object(process, "_snapshot", side_effect=OSError("private detail")), \
                    patch.object(process, "_Process", side_effect=OSError("private detail")):
                self.assertFalse(method(100))


if __name__ == "__main__":
    unittest.main()
