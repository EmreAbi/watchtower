"""Synthetic providers exercise reproducibility, accounting and lifecycle."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import service


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.cancel_after = None
        self.protocols = {}

    def profiles(self):
        return {"profiles": [dict(id="one", label="One", provider="codex"), dict(id="two", label="Two", provider="opencode")], "default_profile": "one"}

    def models(self, profile):
        return dict(models=[dict(id="test/model-a", label="A"), dict(id="test/model-b", label="B")])

    def run(self, profile, model, prompt, timeout, cancel):
        self.calls.append((profile, model, prompt, timeout))
        if self.cancel_after == len(self.calls):
            cancel.set()
        if self.fail:
            raise RuntimeError("private-provider-credential")
        return dict(text='{"answer":"correct"}', elapsed_ms=10, status="ok", adapter=self.protocols.get(profile, "fake"),
                    conditions={"execution_kind": "synthetic"}, provider_version="test1", usage={"input_tokens": 3, "output_tokens": 2})


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runner = FakeRunner()
        self.lab = service.LabService(self.runner, self.root)
        self.pack = dict(id="synthetic", name="Synthetic", version=1, description="Fixture", hash="fixedhash", grading_version="1",
                         quick_case_ids=["a"], cases=[dict(id="a", prompt="public question A", expected="secret-reference"),
                                                      dict(id="b", prompt="public question B", expected="secret-reference")])
        patches = [patch.object(self.lab, "_load_pack", side_effect=lambda _: deepcopy(self.pack)),
                   patch.object(service.packs, "select_cases", side_effect=lambda p, mode: p["cases"][:1] if mode == "quick" else p["cases"]),
                   patch.object(service.packs, "public_request", side_effect=lambda c: c["prompt"]),
                   patch.object(service.grading, "grade", return_value=dict(score=1, passed=True, details="Correct", metrics={}))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def prepare(self, **kwargs):
        args = dict(pack_id="synthetic", mode="quick", assignments=[dict(profile_id="one", model="test/model-a"),
                    dict(profile_id="two", model="test/model-b")], repeats=1, timeout_seconds=60, request_limit=10)
        args.update(kwargs)
        return self.lab.prepare(**args)

    def test_prepare_is_passive_and_public_copy_cannot_alter_run(self):
        plan = self.prepare()
        self.assertFalse(self.lab.root.exists())
        self.assertEqual(self.runner.calls, [])
        plan["targets"][0]["model"] = "tampered"
        result = self.lab.run(plan["token"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.runner.calls[0][1], "test/model-a")
        self.assertTrue(all("secret-reference" not in row[2] for row in self.runner.calls))
        self.assertEqual(result["rows"][0]["score"], 100)
        self.assertIsNone(result["rows"][0]["cost_usd"])
        self.assertTrue(self.lab.compare([result["run_id"]])["comparable"])

    def test_request_budget_and_defaults_fail_before_inference(self):
        for params in (dict(request_limit=1), dict(repeats=4), dict(timeout_seconds=0),
                       dict(assignments=[dict(profile_id="one", model=None)]),
                       dict(assignments=[dict(profile_id="one", model="missing")])):
            with self.subTest(params=params), self.assertRaises(service.LabError):
                self.prepare(**params)
        self.assertFalse(self.lab.root.exists())
        self.assertEqual(self.runner.calls, [])

    def test_run_token_cannot_be_replayed(self):
        plan = self.prepare()
        self.lab.run(plan["token"])
        with self.assertRaises(service.LabError):
            self.lab.run(plan["token"])
        self.assertEqual(len(self.runner.calls), 2)

    def test_profile_reassignment_after_prepare_sends_nothing(self):
        plan = self.prepare()
        self.runner.profiles = lambda: {"profiles": [dict(id="one", label="One", provider="opencode")]}
        with self.assertRaisesRegex(service.LabError, "account was removed or changed"):
            self.lab.run(plan["token"])
        self.assertEqual(self.runner.calls, [])

    def test_observed_model_mismatch_stops_without_credit_or_further_requests(self):
        original = self.runner.run
        def mismatched(*args):
            return {**original(*args), "observed_model": "wrong-model"}
        self.runner.run = mismatched
        result = self.lab.run(self.prepare()["token"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["rows"][0]["score"], 0)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertFalse(self.lab.compare([result["run_id"]])["comparable"])

    def test_case_order_rotates_targets_and_repeats_all_fixed_cases(self):
        result = self.lab.run(self.prepare(mode="full", repeats=2)["token"])
        self.assertEqual(result["completed"], 8)
        self.assertEqual([r[0] for r in self.runner.calls], ["one", "two", "two", "one", "two", "one", "one", "two"])
        self.assertEqual([r["total"] for r in result["rows"]], [4, 4])

    def test_different_quick_full_or_timeout_versions_cannot_compare(self):
        ids = [self.lab.run(self.prepare(**params)["token"])["run_id"] for params in ({}, {"mode": "full"}, {"timeout_seconds": 90})]
        for identifier in ids[1:]:
            comparison = self.lab.compare([ids[0], identifier])
            self.assertFalse(comparison["comparable"])
            self.assertEqual(comparison["rows"], [])

    def test_different_pack_hash_or_adapter_cannot_compare(self):
        first = self.lab.run(self.prepare()["token"])["run_id"]
        self.pack["hash"] = "changed-version"
        second = self.lab.run(self.prepare()["token"])["run_id"]
        self.assertFalse(self.lab.compare([first, second])["comparable"])
        self.runner.protocols["two"] = "other-execution-protocol"
        third = self.lab.run(self.prepare()["token"])["run_id"]
        self.assertFalse(self.lab.compare([third])["comparable"])
        self.assertEqual(len(self.lab.report(third)["rows"]), 2)

    def test_cancelled_run_is_preserved_and_never_ranked(self):
        self.runner.cancel_after = 1
        result = self.lab.run(self.prepare(mode="full")["token"], threading.Event())
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["completed"], 1)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertEqual([row["total"] for row in result["rows"]], [2, 2])
        self.assertFalse(self.lab.compare([result["run_id"]])["comparable"])
        self.assertEqual(self.lab.history()[0]["status"], "cancelled")

    def test_failure_stays_in_denominator_and_exception_is_not_written(self):
        self.runner.fail = True
        result = self.lab.run(self.prepare()["token"])
        self.assertEqual(result["rows"][0]["score"], 0)
        self.assertEqual(result["rows"][0]["errors"], 1)
        self.assertEqual(result["rows"][0]["total"], 1)
        self.assertNotIn("private-provider-credential", json.dumps(self.lab.report(result["run_id"])))
        self.assertEqual(len(self.runner.calls), 2)

    def test_allowlisted_error_is_actionable_without_native_diagnostics(self):
        self.runner.run = lambda *args: dict(status="error", error_code="authorization", error="private-provider-secret")
        summary = self.lab.run(self.prepare()["token"])
        report = self.lab.report(summary["run_id"])
        self.assertEqual(report["results"][0]["error_code"], "authorization")
        self.assertIn("Accounts", report["results"][0]["error"])
        self.assertNotIn("private-provider-secret", json.dumps(report))

    def test_infrastructure_failure_stops_first_case_and_notifies_ui(self):
        for code in ("unsupported_protocol", "configuration_error", "authorization", "rate_limit", "ownership_failed", "opencode_free_tier"):
            with self.subTest(code=code):
                calls, progress = [], []
                def fail(*args):
                    calls.append(args)
                    return dict(status="error", error_code=code)
                self.runner.run = fail
                result = self.lab.run(self.prepare(mode="full")["token"], progress_callback=progress.append)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["completed"], 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(progress[0]["error_code"], code)
                self.assertIn(service.ERROR_MESSAGES[code], result["error"])
                self.assertFalse(self.lab.compare([result["run_id"]])["comparable"])

    def test_repeated_unknown_provider_errors_stop_after_three(self):
        self.runner.fail = True
        result = self.lab.run(self.prepare(mode="full", repeats=2,
                              assignments=[dict(profile_id="one", model="test/model-a")])["token"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["completed"], 3)
        self.assertEqual(len(self.runner.calls), 3)

    def test_other_target_successes_do_not_reset_a_failing_models_counter(self):
        original = self.runner.run
        for failing_profile in ("two", "one"):
            with self.subTest(failing_profile=failing_profile):
                self.runner.calls.clear()
                def respond(*args):
                    result = original(*args)
                    if args[1] == "test/model-b":
                        result.update(status="error", error_code="provider_error")
                    return result
                self.runner.run = respond
                assignments = [dict(profile_id="one", model="test/model-a"),
                               dict(profile_id=failing_profile, model="test/model-b")]
                summary = self.lab.run(self.prepare(mode="full", repeats=2, assignments=assignments)["token"])
                self.assertEqual(summary["status"], "failed")
                self.assertEqual(summary["completed"], 5)
                self.assertEqual([call[1] for call in self.runner.calls],
                                 ["test/model-a", "test/model-b", "test/model-b", "test/model-a", "test/model-b"])
                self.assertEqual([row["completed"] for row in summary["rows"]], [2, 3])
                self.assertTrue(all(row["benchmark_score"] is None for row in summary["rows"]))
                self.assertFalse(self.lab.compare([summary["run_id"]])["comparable"])

    def test_same_target_success_resets_its_error_and_timeout_streak(self):
        original = self.runner.run
        statuses = iter(("error", "timeout", "ok", "error", "timeout", "error"))
        def respond(*args):
            result = original(*args)
            if args[1] == "test/model-b":
                status = next(statuses)
                result.update(status=status)
                if status != "ok":
                    result["error_code"] = "timeout" if status == "timeout" else "provider_error"
            return result
        self.runner.run = respond
        summary = self.lab.run(self.prepare(mode="full", repeats=3, request_limit=12)["token"])
        report = self.lab.report(summary["run_id"])
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["completed"], 11)
        self.assertEqual([item["status"] for item in report["results"] if item["target_id"] == "t2"],
                         ["error", "timeout", "ok", "error", "timeout", "error"])
        self.assertEqual(len(self.runner.calls), 11)

    def test_failure_policy_change_blocks_comparison_with_previous_runs(self):
        previous_plan = self.prepare()
        self.lab._plans[previous_plan["token"]]["conditions"]["failure_policy"] = "stop-on-infrastructure-or-three-consecutive-errors-v1"
        previous = self.lab.run(previous_plan["token"])
        current_plan = self.prepare()
        self.assertEqual(current_plan["conditions"]["failure_policy"],
                         "stop-on-infrastructure-or-three-consecutive-errors-per-target-v2")
        current = self.lab.run(current_plan["token"])
        self.assertTrue(self.lab.compare([previous["run_id"]])["comparable"])
        comparison = self.lab.compare([previous["run_id"], current["run_id"]])
        self.assertFalse(comparison["comparable"])
        self.assertEqual(comparison["rows"], [])

    def test_wrong_answers_do_not_trigger_infrastructure_stop(self):
        service.grading.grade.return_value = dict(score=0, passed=False, details="Incorrect answer", metrics={})
        result = self.lab.run(self.prepare(mode="full")["token"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["completed"], 4)
        self.assertEqual(len(self.runner.calls), 4)

    def test_known_notices_are_preserved_without_raw_native_message(self):
        code = next(iter(service.NOTICE_MESSAGES))
        original = self.runner.run
        self.runner.run = lambda *args: {**original(*args), "notice_codes": [code, code, "private-warning-content"]}
        result = self.lab.run(self.prepare()["token"])
        report = self.lab.report(result["run_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(report["results"][0]["notices"], [dict(code=code, message=service.NOTICE_MESSAGES[code])])
        self.assertNotIn("private-warning-content", json.dumps(report))

    def test_cancel_request_keeps_cancelled_even_when_provider_returns_error(self):
        event = threading.Event()
        def fail(*args):
            event.set()
            return dict(status="error", error_code="unsupported_protocol")
        self.runner.run = fail
        result = self.lab.run(self.prepare(mode="full")["token"], event)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["completed"], 1)

    def test_progress_callback_failure_never_retries_request(self):
        def fail(_):
            raise RuntimeError("UI closed")
        result = self.lab.run(self.prepare()["token"], progress_callback=fail)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(len(self.runner.calls), 2)

    def test_record_tampering_is_visible_and_unrankable(self):
        result = self.lab.run(self.prepare()["token"])
        path = self.lab.root / result["run_id"] / "report.json"
        content = json.loads(path.read_text())
        content["results"][0]["score"] = 0
        path.write_text(json.dumps(content))
        with self.assertRaises(service.LabError):
            self.lab.compare([result["run_id"]])
        self.assertEqual(self.lab.history()[0]["status"], "invalid")

    def test_missing_or_altered_frozen_cases_are_not_comparable(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                result = self.lab.run(self.prepare()["token"])
                path = self.lab.root / result["run_id"] / "pack.json"
                if missing:
                    path.unlink()
                else:
                    content = json.loads(path.read_text())
                    content["cases"][0]["prompt"] = "changed prompt"
                    path.write_text(json.dumps(content))
                with self.assertRaises(service.LabError):
                    self.lab.compare([result["run_id"]])

    def test_history_does_not_create_state_or_contact_providers(self):
        self.assertEqual(self.lab.history(), [])
        self.assertFalse(self.lab.root.exists())
        self.assertEqual(self.runner.calls, [])

    def test_traversal_and_duplicate_comparison_ids_are_rejected(self):
        with self.assertRaises(service.LabError):
            self.lab.report("../accounts")
        with self.assertRaises(service.LabError):
            self.lab.compare(["a" * 32, "a" * 32])

    def test_fractional_case_credit_has_equal_case_weight(self):
        service.grading.grade.side_effect = [
            dict(score=.5, passed=False, details="1/2 checks", metrics={"tests_passed": 1, "tests_total": 2}),
            dict(score=1, passed=True, details="100/100 checks", metrics={"tests_passed": 100, "tests_total": 100}),
        ]
        summary = self.lab.run(self.prepare(mode="full", assignments=[dict(profile_id="one", model="test/model-a")])["token"])
        row = summary["rows"][0]
        self.assertEqual((row["score"], row["benchmark_score"], row["quality_score"]), (75, 75, 75))
        self.assertEqual((row["answered"], row["wrong"], row["passed"], row["coverage"]), (2, 1, 1, 100))
        self.assertEqual((row["execution_errors"], row["cancelled"], row["unrun"]), (0, 0, 0))
        self.assertEqual((row["provider"], row["model"], row["profile_label"]), ("codex", "test/model-a", "One"))
        self.assertEqual(summary["scoring"]["version"], "case-credit-v1")

    def test_completed_technical_failures_reduce_benchmark_not_response_quality(self):
        attempts = iter([("ok", 100), ("timeout", 999), ("ok", 300), ("error", 888)])
        original = self.runner.run
        def respond(*args):
            status, elapsed = next(attempts)
            return {**original(*args), "status": status, "elapsed_ms": elapsed}
        self.runner.run = respond
        service.grading.grade.side_effect = [dict(score=.5, passed=False), dict(score=1, passed=True)]
        summary = self.lab.run(self.prepare(mode="full", repeats=2, assignments=[dict(profile_id="one", model="test/model-a")])["token"])
        row = summary["rows"][0]
        self.assertEqual(summary["status"], "completed")
        self.assertEqual((row["benchmark_score"], row["quality_score"], row["coverage"]), (37.5, 75, 50))
        self.assertEqual((row["answered"], row["wrong"], row["execution_errors"], row["unrun"]), (2, 1, 2, 0))
        self.assertEqual((row["median_response_ms"], row["p95_response_ms"]), (200, 300))
        self.assertEqual((row["median_ms"], row["p95_ms"]), (594, 999))
        self.assertEqual(self.lab.compare([summary["run_id"]])["verdict"]["kind"], "single")

    def test_completed_without_responses_is_unscored_but_wrong_answers_score_zero(self):
        original = self.runner.run
        self.runner.run = lambda *args: {**original(*args), "status": "error"}
        empty = self.lab.run(self.prepare(assignments=[dict(profile_id="one", model="test/model-a")])["token"])
        row = empty["rows"][0]
        self.assertEqual(empty["status"], "completed")
        self.assertEqual((row["score"], row["answered"], row["coverage"]), (0, 0, 0))
        for field in ("quality_score", "benchmark_score", "median_response_ms", "p95_response_ms"):
            self.assertIsNone(row[field])
        comparison = self.lab.compare([empty["run_id"]])
        self.assertFalse(comparison["comparable"])
        self.assertIn("No scored response", comparison["reason"])
        self.assertNotIn("verdict", comparison)
        self.runner.run = original
        service.grading.grade.return_value = dict(score=0, passed=False)
        wrong = self.lab.run(self.prepare(assignments=[dict(profile_id="one", model="test/model-a")])["token"])
        self.assertEqual((wrong["rows"][0]["benchmark_score"], wrong["rows"][0]["quality_score"]), (0, 0))
        self.assertTrue(self.lab.compare([wrong["run_id"]])["comparable"])

    def test_cancelled_attempts_and_missing_cases_never_get_a_benchmark_score(self):
        original = self.runner.run
        def respond(*args):
            result = original(*args)
            if len(self.runner.calls) == 2:
                result["status"] = "cancelled"
            return result
        self.runner.run = respond
        summary = self.lab.run(self.prepare(mode="full", repeats=2, assignments=[dict(profile_id="one", model="test/model-a")])["token"])
        row = summary["rows"][0]
        self.assertEqual(summary["status"], "cancelled")
        self.assertIsNone(row["benchmark_score"])
        self.assertEqual((row["quality_score"], row["answered"], row["coverage"]), (100, 1, 25))
        self.assertEqual((row["cancelled"], row["execution_errors"], row["unrun"]), (1, 0, 2))
        self.assertFalse(self.lab.compare([summary["run_id"]])["comparable"])

    def test_cancelled_run_cannot_rank_a_target_even_if_its_attempts_finished(self):
        self.runner.cancel_after = 1
        summary = self.lab.run(self.prepare()["token"])
        self.assertEqual(summary["rows"][0]["completed"], summary["rows"][0]["total"])
        self.assertIsNone(summary["rows"][0]["benchmark_score"])
        self.assertEqual(summary["rows"][0]["quality_score"], 100)

    def test_compare_ranks_unrounded_credit_and_explains_display_rounding(self):
        service.grading.grade.side_effect = [dict(score=.80001, passed=False), dict(score=.80002, passed=False)]
        summary = self.lab.run(self.prepare()["token"])
        comparison = self.lab.compare([summary["run_id"]])
        self.assertEqual([row["benchmark_score"] for row in comparison["rows"]], [80, 80])
        self.assertEqual(comparison["verdict"]["kind"], "winner")
        self.assertEqual(comparison["verdict"]["targets"][0]["target_id"], "t2")
        self.assertIn("Highest score in this run", comparison["verdict"]["text"])
        self.assertIn("unrounded case credit", comparison["verdict"]["text"])

    def test_compare_tie_never_uses_latency_or_cost_as_tiebreaker(self):
        service.grading.grade.return_value = dict(score=.5, passed=False)
        original = self.runner.run
        def respond(*args):
            first = args[0] == "one"
            return {**original(*args), "elapsed_ms": 1 if first else 1000,
                    "usage": {"cost_usd": 1 if first else 100}}
        self.runner.run = respond
        summary = self.lab.run(self.prepare()["token"])
        verdict = self.lab.compare([summary["run_id"]])["verdict"]
        self.assertEqual(verdict["kind"], "tie")
        self.assertEqual({row["target_id"] for row in verdict["targets"]}, {"t1", "t2"})
        self.assertIn("(tie)", verdict["text"])

    def test_across_run_winner_and_single_target_wording_are_scoped(self):
        single = [dict(profile_id="one", model="test/model-a")]
        service.grading.grade.return_value = dict(score=.5, passed=False)
        first = self.lab.run(self.prepare(assignments=single)["token"])
        self.assertEqual(self.lab.compare([first["run_id"]])["verdict"]["kind"], "single")
        service.grading.grade.return_value = dict(score=1, passed=True)
        second = self.lab.run(self.prepare(assignments=single)["token"])
        verdict = self.lab.compare([first["run_id"], second["run_id"]])["verdict"]
        self.assertEqual(verdict["kind"], "winner")
        self.assertEqual(verdict["targets"][0]["run_id"], second["run_id"])
        self.assertIn("Highest score in this comparison", verdict["text"])

    def test_scorecards_and_frozen_case_details_are_derived_without_rewriting_evidence(self):
        summary = self.lab.run(self.prepare()["token"])
        path = self.lab.root / summary["run_id"] / "report.json"
        before = path.read_bytes()
        saved = json.loads(before)
        self.assertNotIn("scoring", saved)
        self.assertNotIn("rows", saved)
        self.assertNotIn("cases", saved)
        calls = len(self.runner.calls)
        report = self.lab.report(summary["run_id"])
        self.assertEqual(report["cases"][0], self.pack["cases"][0])
        self.assertEqual(report["scoring"]["version"], "case-credit-v1")
        report["cases"][0]["prompt"] = "caller changed copy"
        report["scoring"]["rule"] = "caller changed rule"
        self.assertEqual(self.lab.report(summary["run_id"])["cases"][0]["prompt"], "public question A")
        self.assertEqual(self.lab.history()[0]["scoring"], service.SCORING)
        self.assertNotIn("cases", self.lab.history()[0])
        self.assertNotIn("cases", summary)
        self.assertTrue(self.lab.compare([summary["run_id"]])["comparable"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.runner.calls), calls)


if __name__ == "__main__":
    unittest.main()
