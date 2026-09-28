import json
import re
import unittest
from unittest.mock import patch

from grading import grade, PureFunction, RestrictedCode
from packs import load_pack


class GradingTests(unittest.TestCase):
    def test_all_80_references_pass_their_real_checks(self):
        count = 0
        for pack_id in ("structured-decisions", "coding", "code-review", "debugging"):
            for case in load_pack(pack_id)["cases"]:
                response = ({"code": case["expected"]["reference_code"]} if pack_id in ("coding", "debugging")
                            else case["expected"])
                with self.subTest(pack=pack_id, case=case["id"]):
                    result = grade(case, json.dumps(response))
                    self.assertTrue(result["passed"], result)
                    self.assertEqual(result["score"], 1)
                count += 1
        self.assertEqual(count, 80)

    def test_every_debugging_bug_fails_at_least_one_hidden_example(self):
        for case in load_pack("debugging")["cases"]:
            bug = re.search(r"Faulty code:\n(.*?)\nReturn exactly JSON", case["prompt"], re.S).group(1)
            with self.subTest(case=case["id"]):
                result = grade(case, json.dumps({"code": bug}))
                self.assertFalse(result["passed"])
                self.assertLess(result["score"], 1)

    def test_structured_exact_field_types_and_fixed_reason_enums(self):
        case = load_pack("structured-decisions")["cases"][0]
        for delta in ({"priority": False}, {"priority": 0.0}, {"priority": "0"},
                      {"reason": "It seems routine"}, {"action": "APPROVE"}):
            response = dict(case["expected"], **delta)
            result = grade(case, json.dumps(response))
            self.assertFalse(result["passed"])
            self.assertEqual(result["score"], 2 / 3)
        result = grade(case, json.dumps(dict(case["expected"], commentary="extra")))
        self.assertEqual(result["score"], 0)

    def test_structured_unknown_and_missing_data_controls(self):
        cases = load_pack("structured-decisions")["cases"]
        reasons = {case["expected"]["reason"] for case in cases}
        self.assertEqual(reasons, {"routine", "insufficient_data", "unverified", "risk_limit", "manual_check"})
        self.assertGreaterEqual(sum(case["expected"]["reason"] == "insufficient_data" for case in cases), 5)
        self.assertEqual(cases[3]["expected"]["reason"], "unverified")  # Precedence beats high risk.
        self.assertEqual(cases[4]["expected"]["action"], "approve")  # Exact boundary.

    def test_review_precision_recall_false_alarms_and_clean_controls(self):
        cases = load_pack("code-review")["cases"]
        multiple = cases[-1]
        first = multiple["expected"]["findings"][0]
        result = grade(multiple, json.dumps({"findings": [first]}))
        self.assertEqual(result["metrics"]["precision"], 1)
        self.assertEqual(result["metrics"]["recall"], 0.5)
        self.assertAlmostEqual(result["score"], 2 / 3)
        false_alarm = {"line": 999, "category": "logic"}
        result = grade(multiple, json.dumps({"findings": multiple["expected"]["findings"] + [false_alarm]}))
        self.assertEqual(result["metrics"]["false_positives"], 1)
        self.assertEqual(result["metrics"]["recall"], 1)
        self.assertFalse(result["passed"])
        single = cases[0]
        result = grade(single, json.dumps({"findings": single["expected"]["findings"] * 2}))
        self.assertEqual(result["metrics"]["precision"], 0.5)
        clean = [case for case in cases if not case["expected"]["findings"]]
        self.assertEqual(len(clean), 4)
        for case in clean:
            self.assertEqual(grade(case, '{"findings":[]}')["score"], 1)
            self.assertEqual(grade(case, json.dumps({"findings": [false_alarm]}))["score"], 0)

    def test_bad_json_and_schema_do_not_crash_or_expose_hidden_tests(self):
        for pack_id in ("structured-decisions", "coding", "code-review", "debugging"):
            case = load_pack(pack_id)["cases"][0]
            for text in ("not-json", "[]", "null", "{}", "```json\n{}\n```", "x" * 40000):
                with self.subTest(pack=pack_id, response=text[:12]):
                    result = grade(case, text)
                    self.assertFalse(result["passed"])
                    self.assertGreaterEqual(result["score"], 0)
                    self.assertNotIn("reference_code", json.dumps(result))
                    self.assertNotIn("\"args\"", json.dumps(result))

    def test_wrong_behavior_is_not_accepted_by_code_text_matching(self):
        case = load_pack("coding")["cases"][0]
        result = grade(case, json.dumps({"code": 'def solve(value, low, high):\n    return value'}))
        self.assertFalse(result["passed"])
        self.assertGreater(result["metrics"]["tests_passed"], 0)
        self.assertLess(result["metrics"]["tests_passed"], result["metrics"]["tests_total"])
        equivalent = 'def solve(value, low, high):\n    if value < low:\n        return low\n    if value > high:\n        return high\n    return value'
        self.assertTrue(grade(case, json.dumps({"code": equivalent}))["passed"])

    def test_generated_code_is_never_execed_evaled_or_given_io(self):
        case = load_pack("coding")["cases"][0]
        malicious = [
            'import os\ndef solve(value, low, high): return 0',
            'def solve(value, low, high): return __import__("os").system("echo unsafe")',
            'def solve(value, low, high): return open("private.txt").read()',
            'def solve(value, low, high): return value.__class__.__base__.__subclasses__()',
            'def solve(value, low, high):\n    while True:\n        value += 1',
            'def solve(value, low, high): return [x for x in value]',
            'def solve(value, low, high): return solve(value, low, high)',
            'def solve(value, low, high): return eval("1+1")',
        ]
        with patch("builtins.exec", side_effect=AssertionError("exec forbidden")), patch("builtins.eval", side_effect=AssertionError("eval forbidden")):
            for code in malicious:
                with self.subTest(code=code[:35]):
                    result = grade(case, json.dumps({"code": code}))
                    self.assertFalse(result["passed"])
                    self.assertFalse(result["metrics"]["supported_code"])
            self.assertTrue(grade(case, json.dumps({"code": case["expected"]["reference_code"]}))["passed"])

    def test_resource_explosions_rejected_with_bounded_interpretation(self):
        bodies = ['return "x" * 1000000000', 'return 2 ** 1000000000',
                  'a = [0] * 1000\nreturn [a] * 1000',
                  'a = "x" * 2000\nreturn a.join(a)',
                  'a = "x" * 2000\nreturn a.replace("", a)']
        for body in bodies:
            code = 'def solve():\n' + '\n'.join('    ' + line for line in body.splitlines())
            with self.subTest(body=body), self.assertRaises(RestrictedCode):
                PureFunction(code).call([])

    def test_shadowed_builtins_cannot_pass_with_non_python_call_semantics(self):
        for code in ('def solve(values):\n    len = 4\n    return len(values)',
                     'def solve(len):\n    return len([1, 2])'):
            with self.subTest(code=code), self.assertRaises(RestrictedCode):
                PureFunction(code)

    def test_duplicate_call_keywords_cannot_earn_behavioral_credit(self):
        case = load_pack("coding")["cases"][4]
        code = ('def solve(values):\n'
                '    items = sorted(set(values), reverse=True, reverse=True)\n'
                '    return items[1] if len(items) >= 2 else None')
        result = grade(case, json.dumps({"code": code}))
        self.assertEqual(result["score"], 0)
        self.assertFalse(result["metrics"]["supported_code"])


if __name__ == "__main__":
    unittest.main()
