"""Inspect local test definitions and run synthetic private packs without providers."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import service
import private_packs
from test_private_packs import FIXTURE_SPECS, NOT_READY_FIXTURE, install_fixture, make_pack


class ChoiceRunner:
    """Return only synthetic reference responses; no provider process exists."""
    def __init__(self, pack):
        self.answers = {case["prompt"]: case["expected"] for case in pack["cases"]}
        self.calls = []

    def profiles(self):
        return {"profiles": [dict(id="fixture", label="Fixture", provider="codex")]}

    def models(self, profile):
        return {"models": [dict(id="model-a"), dict(id="model-b")]}

    def run(self, profile, model, prompt, timeout, cancel):
        self.calls.append(prompt)
        return dict(status="ok", text=json.dumps(self.answers[prompt]), elapsed_ms=1,
                    adapter="synthetic-choice", provider_version="fixture-1",
                    conditions={"execution_kind": "synthetic"})


class PackInspectionTest(unittest.TestCase):
    def test_every_bundled_test_is_inspectable_without_accounts_or_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            runner = Mock()
            runner.profiles.side_effect = AssertionError("Inspection must not read accounts")
            runner.models.side_effect = AssertionError("Inspection must not discover models")
            runner.run.side_effect = AssertionError("Inspection must not start inference")
            lab = service.LabService(runner, directory)
            for pack_id in service.packs.MANIFEST:
                with self.subTest(pack=pack_id):
                    definition = lab.inspect_pack(pack_id)
                    original = service.packs.load_pack(pack_id)
                    self.assertEqual(definition["cases"], original["cases"])
                    self.assertEqual(definition["hash"], original["hash"])
                    self.assertEqual(definition["quick_case_ids"], original["quick_case_ids"])
                    self.assertTrue(definition["output_schema"])
                    self.assertTrue(definition["scoring"]["rule"])
                    definition["cases"][0]["expected"] = {"tampered": True}
                    self.assertEqual(lab.inspect_pack(pack_id)["cases"][0]["expected"], original["cases"][0]["expected"])
            self.assertEqual(runner.mock_calls, [])
            self.assertEqual(lab._plans, {})
            self.assertFalse(lab.root.exists())

    def test_invalid_test_ids_fail_without_provider_work(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            runner = Mock()
            lab = service.LabService(runner, directory)
            for identifier in ("../private", "pi-../../outside", "unknown", None, 15):
                with self.subTest(identifier=identifier), self.assertRaises(service.LabError):
                    lab.inspect_pack(identifier)
            self.assertEqual(runner.mock_calls, [])

    def test_private_catalog_and_every_case_inspect_without_accounts_or_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            runner = Mock()
            lab = service.LabService(runner, directory)
            for identifier in FIXTURE_SPECS:
                original = install_fixture(lab.pack_root, identifier)
                inspected = lab.inspect_pack(identifier)
                self.assertEqual(inspected["cases"], original["cases"])
                self.assertEqual(inspected["unscored_cases"], original["unscored_cases"])
                self.assertEqual(inspected["scoring"]["kind"], "reference-agreement")
                self.assertEqual(inspected["output_schema"], original["output_schema"])
                self.assertEqual(inspected["not_ready_scenarios"], NOT_READY_FIXTURE)
            self.assertEqual(len(lab.packs()), 11)
            self.assertEqual(runner.mock_calls, [])
            self.assertFalse(lab.root.exists())

    def test_full_single_target_preserves_inputs_and_freezes_inspectable_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            library = Path(directory) / "state/benchmarks/packs"
            original = install_fixture(library)
            runner = ChoiceRunner(original)
            lab = service.LabService(runner, directory)
            plan = lab.prepare(original["id"], "full", [dict(profile_id="fixture", model="model-a")],
                               request_limit=120)
            self.assertEqual(plan["request_count"], 9)
            self.assertIn("private pack", plan["notice"])
            self.assertEqual(plan["conditions"]["test_input_protocol"], private_packs.PROTOCOL)
            self.assertEqual(runner.calls, [])
            summary = lab.run(plan["token"])
            row = summary["rows"][0]
            self.assertEqual((summary["status"], summary["completed"], row["total"], row["passed"]),
                             ("completed", 9, 9, 9))
            self.assertEqual(row["benchmark_score"], 100)
            self.assertEqual(runner.calls, [case["prompt"] for case in original["cases"]])
            self.assertFalse(any("PRIVATE_REFERENCE_EVIDENCE_DO_NOT_SEND_93f2" in prompt for prompt in runner.calls))
            excluded = {case["prompt"] for case in original["unscored_cases"]}
            self.assertFalse(set(runner.calls) & excluded)
            snapshot = json.loads((lab.root / summary["run_id"] / "pack.json").read_text(encoding="utf-8"))
            self.assertEqual(private_packs.validate_snapshot(snapshot), original)
            for case in snapshot["cases"]:
                self.assertEqual(case["prompt"], private_packs.render_request(case["request"]))
                self.assertEqual(list(case["request"]["questions"]["fixture_choice"]["criteria"]),
                                 ["product", "service", "uncertain"])
            # Browsing saved evidence remains valid after the installed library disappears.
            library.rename(library.with_name("moved-library"))
            report = lab.report(summary["run_id"])
            self.assertEqual(report["cases"][0]["expected"], original["cases"][0]["expected"])
            self.assertEqual(report["scoring"]["kind"], "reference-agreement")
            self.assertTrue(lab.compare([summary["run_id"]])["comparable"])
            self.assertEqual(lab.history()[0]["status"], "completed")
            self.assertEqual(len(runner.calls), 9)

    def test_two_full_targets_rejected_before_account_lookup_or_any_request(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            runner = Mock()
            lab = service.LabService(runner, directory)
            install_fixture(lab.pack_root)
            with self.assertRaisesRegex(service.LabError, "18 requests"):
                lab.prepare("private-decisions", "full", [dict(profile_id="fixture", model="model-a"),
                            dict(profile_id="fixture", model="model-b")], request_limit=10)
            self.assertEqual(runner.mock_calls, [])
            self.assertFalse(lab.root.exists())

    def test_changed_private_reference_version_cannot_compare_with_old_run(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory).resolve()
            lab = service.LabService(Mock(), directory)
            original = install_fixture(lab.pack_root, "private-selection")
            runner = ChoiceRunner(original)
            lab.runner = runner
            def run():
                plan = lab.prepare(original["id"], "quick", [dict(profile_id="fixture", model="model-a")])
                return lab.run(plan["token"])["run_id"]
            first = run()
            changed = make_pack("private-selection")
            changed["cases"][0]["evidence"] = "Revised synthetic reference basis."
            install_fixture(lab.pack_root, pack=changed)
            second = run()
            self.assertFalse(lab.compare([first, second])["comparable"])
            self.assertEqual(lab.report(first)["pack_hash"], original["hash"])


if __name__ == "__main__":
    unittest.main()
