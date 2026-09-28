"""Real frozen published packs through the service, with synthetic answers only.

No account runtime or provider CLI is constructed. References are available to
this local test double; the service must send it only the frozen public prompt.
"""
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import service


PUBLISHED = ("gsm8k-subset", "bbh-subset", "bbeh-subset")
ASSIGNMENTS = [dict(profile_id="one", model="synthetic/a"),
               dict(profile_id="two", model="synthetic/b")]


class PublishedRunner:
    """Return known fixture answers; never import or invoke a real provider."""

    def __init__(self, packs):
        self.cases = {case["prompt"]: case for pack in packs for case in pack["cases"]}
        self.calls = []
        self.overrides = {}

    def profiles(self):
        return dict(profiles=[dict(id="one", label="Fixture One", provider="codex"),
                              dict(id="two", label="Fixture Two", provider="codex")],
                    default_profile="one")

    def models(self, profile_id):
        return dict(models=[dict(id="synthetic/a", label="A"),
                            dict(id="synthetic/b", label="B")])

    def run(self, profile_id, model, prompt, timeout, cancel_event):
        self.calls.append(dict(profile_id=profile_id, model=model, prompt=prompt, timeout=timeout))
        case = self.cases[prompt]
        expected = case["expected"]["answer"]
        if case["capability"] == "numeric-answer":
            # Numeric equality must accept grouping and equivalent decimals.
            answer = format(Decimal(expected), ",.2f")
        elif case["capability"] == "normalized-answer":
            # Only the declared case/comma normalization, not fuzzy matching.
            answer = "  " + expected.swapcase().replace(",", " ,  ") + "\n"
        else:
            answer = "\n " + expected + " \n"
        answer = self.overrides.get((model, case["id"]), answer)
        return dict(status="ok", text=json.dumps({"answer": answer}), elapsed_ms=12,
                    adapter="synthetic-published-fixture", provider_version="fixture-1",
                    conditions={"execution_kind": "synthetic", "tools_enabled": False},
                    usage={"input_tokens": 3, "output_tokens": 2})


class PublishedServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.packs = {pack_id: service.packs.load_pack(pack_id) for pack_id in PUBLISHED}
        self.runner = PublishedRunner(self.packs.values())
        self.lab = service.LabService(self.runner, self.home)

    def prepare(self, pack_id="gsm8k-subset", **overrides):
        args = dict(pack_id=pack_id, mode="quick", assignments=ASSIGNMENTS,
                    repeats=1, timeout_seconds=60, request_limit=10)
        args.update(overrides)
        return self.lab.prepare(**args)

    def run_pack(self, pack_id="gsm8k-subset", **overrides):
        plan = self.prepare(pack_id, **overrides)
        summary = self.lab.run(plan["token"])
        self.assertEqual(summary["status"], "completed", summary.get("error"))
        return plan, summary, self.lab.report(summary["run_id"])

    def test_each_published_capability_uses_real_answer_grader_and_its_hash(self):
        answer_hash = hashlib.sha256(Path(service.answer_grading.__file__).read_bytes()).hexdigest()
        legacy_hash = hashlib.sha256(Path(service.grading.__file__).read_bytes()).hexdigest()
        for pack_id in PUBLISHED:
            with self.subTest(pack_id=pack_id), \
                    patch.object(service.answer_grading, "grade", wraps=service.answer_grading.grade) as grader, \
                    patch.object(service.grading, "grade", side_effect=AssertionError("Wrong grader")) as legacy:
                plan, summary, report = self.run_pack(pack_id)
                self.assertEqual(plan["conditions"]["grader_hash"], answer_hash)
                self.assertNotEqual(plan["conditions"]["grader_hash"], legacy_hash)
                self.assertEqual(plan["conditions"]["grading_version"], "answer-1")
                self.assertEqual(grader.call_count, 10)
                legacy.assert_not_called()
                self.assertEqual({call.args[0]["capability"] for call in grader.call_args_list},
                                 {self.packs[pack_id]["capability"]})
                self.assertEqual(summary["completed"], 10)
                self.assertEqual([row["benchmark_score"] for row in report["rows"]], [100, 100])
                self.assertTrue(all(result["score"] == 1 and result["passed"] for result in report["results"]))
                self.assertTrue(all(result["metrics"]["scoring_kind"] == "accuracy" for result in report["results"]))

    def test_original_packs_keep_legacy_grader_hash_version_and_twenty_cases(self):
        legacy_hash = hashlib.sha256(Path(service.grading.__file__).read_bytes()).hexdigest()
        for pack_id in ("coding", "code-review", "debugging", "structured-decisions"):
            with self.subTest(pack_id=pack_id):
                plan = self.prepare(pack_id, mode="full", request_limit=40)
                self.assertEqual(plan["conditions"]["grader_hash"], legacy_hash)
                self.assertEqual(plan["conditions"]["grading_version"], "1")
                self.assertEqual(len(plan["conditions"]["case_ids"]), 20)
                self.assertEqual(plan["pack"]["collection"], "watchtower")
                self.assertEqual(plan["pack"]["scoring_kind"], "case-credit")
        self.assertFalse(self.lab.root.exists())
        self.assertEqual(self.runner.calls, [])

    def test_real_pack_budgets_are_ten_eighty_and_reject_one_hundred_sixty(self):
        for pack_id in PUBLISHED:
            with self.subTest(pack_id=pack_id):
                self.assertEqual(self.prepare(pack_id)["request_count"], 10)
                self.assertEqual(self.prepare(pack_id, mode="full", request_limit=80)["request_count"], 80)
                with self.assertRaises(service.LabError):
                    self.prepare(pack_id, mode="full", repeats=2, request_limit=120)
                with self.assertRaises(service.LabError):
                    self.prepare(pack_id, mode="full", request_limit=79)
        self.assertFalse(self.lab.root.exists())
        self.assertEqual(self.runner.calls, [])

    def test_full_subset_executes_exactly_eighty_public_prompts(self):
        plan, summary, report = self.run_pack("bbeh-subset", mode="full", request_limit=80)
        expected_prompts = Counter({case["prompt"]: 2 for case in self.packs["bbeh-subset"]["cases"]})
        self.assertEqual(Counter(call["prompt"] for call in self.runner.calls), expected_prompts)
        self.assertEqual(len(self.runner.calls), 80)
        self.assertEqual((summary["request_count"], summary["completed"]), (80, 80))
        for row in report["rows"]:
            self.assertEqual((row["passed"], row["total"], row["answered"]), (40, 40, 40))
            self.assertEqual(row["benchmark_score"], 100)
        self.assertNotIn("cases", plan["pack"])
        self.assertNotIn("expected", json.dumps(plan))
        self.assertTrue(all(isinstance(call["prompt"], str) for call in self.runner.calls))
        # The entire argument equals the frozen prompt: references/source metadata
        # cannot be appended as a hidden grading hint even if their words occur
        # legitimately inside a public question.
        for call in self.runner.calls:
            case = self.runner.cases[call["prompt"]]
            self.assertEqual(call["prompt"], service.packs.public_request(case))
            self.assertEqual(call["prompt"], case["prompt"])

    def test_binary_accuracy_counts_wrong_answers_without_partial_or_execution_error(self):
        pack = self.packs["bbh-subset"]
        first = service.packs.select_cases(pack, "quick")[0]
        # BBH exact answers are case-sensitive, unlike BBEH normalization.
        self.assertNotEqual(first["expected"]["answer"], first["expected"]["answer"].lower())
        self.runner.overrides[("synthetic/b", first["id"])] = first["expected"]["answer"].lower()
        _, summary, report = self.run_pack("bbh-subset")
        by_model = {row["model"]: row for row in summary["rows"]}
        wrong = by_model["synthetic/b"]
        self.assertEqual((wrong["benchmark_score"], wrong["quality_score"]), (80, 80))
        self.assertEqual((wrong["answered"], wrong["wrong"], wrong["execution_errors"]), (5, 1, 0))
        self.assertEqual({result["score"] for result in report["results"]}, {0, 1})
        self.assertTrue(all(result["status"] == "ok" for result in report["results"]))
        compared = self.lab.compare([summary["run_id"]])
        self.assertTrue(compared["comparable"])
        self.assertEqual(compared["verdict"]["kind"], "winner")
        self.assertEqual(compared["verdict"]["targets"][0]["model"], "synthetic/a")
        self.assertEqual(compared["scoring"]["kind"], "accuracy")

    def test_saved_source_metadata_and_report_accuracy_are_read_only_and_detached(self):
        plan, summary, report = self.run_pack("gsm8k-subset")
        directory = self.lab.root / summary["run_id"]
        paths = (directory / "report.json", directory / "pack.json")
        before = {path: path.read_bytes() for path in paths}
        saved = json.loads(before[paths[0]])
        source = self.packs["gsm8k-subset"]["source"]
        for info in (plan["pack"], summary["pack_info"], saved["pack_info"], report["pack_info"]):
            self.assertEqual(info["collection"], "published-subset")
            self.assertEqual(info["scoring_kind"], "accuracy")
            self.assertEqual(info["source_name"], source["name"])
            for key in ("url", "revision", "license", "selection", "source_count"):
                self.assertEqual(info[key], source[key])
            self.assertEqual((info["case_count"], info["quick_count"]), (40, 5))
        self.assertEqual(report["scoring"]["version"], "binary-accuracy-v1")
        self.assertIn("correct answers", report["scoring"]["rule"])
        self.assertIn("not an official", report["scoring"]["explanation"].lower())
        first = report["cases"][0]
        original = self.packs["gsm8k-subset"]["cases"][0]
        for key in ("id", "prompt", "expected", "source_id", "topic"):
            self.assertEqual(first[key], original[key])
        first["expected"]["answer"] = "mutated public result"
        self.assertEqual(self.lab.report(summary["run_id"])["cases"][0]["expected"], original["expected"])
        history = self.lab.history()
        self.assertEqual(history[0]["scoring"]["kind"], "accuracy")
        self.assertEqual(history[0]["rows"][0]["benchmark_score"], 100)
        self.assertNotIn("cases", history[0])
        self.assertTrue(self.lab.compare([summary["run_id"]])["comparable"])
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_different_published_subsets_cannot_be_ranked_together(self):
        _, one, _ = self.run_pack("gsm8k-subset")
        _, two, _ = self.run_pack("bbh-subset")
        compared = self.lab.compare([one["run_id"], two["run_id"]])
        self.assertFalse(compared["comparable"])
        self.assertIn("Pack version/hash", compared["reason"])
        self.assertEqual(compared["rows"], [])
        self.assertNotIn("verdict", compared)
        self.assertTrue(self.lab.compare([one["run_id"]])["comparable"])
        self.assertTrue(self.lab.compare([two["run_id"]])["comparable"])

    def test_same_pack_quick_and_full_subsets_cannot_be_ranked_together(self):
        _, quick, _ = self.run_pack("gsm8k-subset", assignments=ASSIGNMENTS[:1])
        _, full, _ = self.run_pack("gsm8k-subset", mode="full", assignments=ASSIGNMENTS[:1], request_limit=40)
        self.assertEqual(quick["pack_hash"], full["pack_hash"])
        self.assertNotEqual(quick["conditions"]["case_ids"], full["conditions"]["case_ids"])
        compared = self.lab.compare([quick["run_id"], full["run_id"]])
        self.assertFalse(compared["comparable"])
        self.assertNotIn("verdict", compared)


if __name__ == "__main__":
    unittest.main()
