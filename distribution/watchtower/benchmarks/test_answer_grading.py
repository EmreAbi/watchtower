import json
import unittest

import answer_grading
import packs


def case(answer, kind="exact-answer"):
    return dict(capability=kind, expected={"answer": answer})


class AnswerGradingTests(unittest.TestCase):
    def test_published_references_pass_and_wrong_answers_fail_for_every_case(self):
        count = 0
        for pack_id in packs.PUBLISHED_CAPABILITIES:
            for item in packs.load_pack(pack_id)["cases"]:
                with self.subTest(pack=pack_id, source=item["source_id"]):
                    self.assertTrue(answer_grading.grade(item, json.dumps(item["expected"]))["passed"])
                    self.assertFalse(answer_grading.grade(item, '{"answer":"__deliberately_wrong__"}')["passed"])
                count += 1
        self.assertEqual(count, 120)

    def test_exact_answer_credit_is_binary_and_preserves_meaning(self):
        for response, score in (("(A)", 1), ("  (A)\n", 1), ("(a)", 0), ("A", 0), ("(A) or (B)", 0)):
            with self.subTest(response=response):
                grade = answer_grading.grade(case("(A)"), json.dumps({"answer": response}))
                self.assertEqual(grade["score"], score)
                self.assertEqual(grade["passed"], bool(score))
        self.assertEqual(answer_grading.grade(case("] )"), '{"answer":"] )"}')["score"], 1)
        self.assertEqual(answer_grading.grade(case("] )"), '{"answer":"])"}')["score"], 0)

    def test_numeric_exact_comparison_accepts_equivalent_spelling(self):
        for value in ("1200", "+1200.00", "1,200", "001200", " 1200 "):
            with self.subTest(value=value):
                self.assertTrue(answer_grading.grade(case("1200", "numeric-answer"), json.dumps({"answer": value}))["passed"])
        self.assertTrue(answer_grading.grade(case("-12.5", "numeric-answer"), '{"answer":"-12.500"}')["passed"])
        self.assertFalse(answer_grading.grade(case("1200", "numeric-answer"), '{"answer":"1201"}')["passed"])

    def test_numeric_grader_rejects_units_expressions_bad_commas_and_large_inputs(self):
        for value in ("12,00", "1e3", "1200 dollars", "600+600", "NaN", "Infinity", "１２００", "9" * 300):
            with self.subTest(value=value):
                grade = answer_grading.grade(case("1200", "numeric-answer"), json.dumps({"answer": value}))
                self.assertEqual(grade["score"], 0)
                self.assertFalse(grade["metrics"]["format_valid"])

    def test_json_contract_rejects_extra_duplicate_or_nonstring_answers(self):
        for value in ('{"answer":"A","answer":"(A)"}', '{"answer":"(A)","reason":"anything"}',
                      '{"answer":42}', '{"answer":true}', '{"answer":null}', '["(A)"]',
                      '```json\n{"answer":"(A)"}\n```', '{"answer":NaN}', 'not json'):
            with self.subTest(value=value):
                self.assertFalse(answer_grading.grade(case("(A)"), value)["metrics"]["format_valid"])

    def test_model_text_is_never_executed(self):
        value = '__import__("os").system("unexpected")'
        self.assertFalse(answer_grading.grade(case("safe"), json.dumps({"answer": value}))["passed"])
        self.assertFalse(answer_grading.grade(case("safe"), "x" * 33000)["passed"])

    def test_bbeh_normalization_is_narrow_and_never_accepts_substrings(self):
        reference = case("Alpha, Beta", "normalized-answer")
        self.assertTrue(answer_grading.grade(reference, '{"answer":" alpha ,beta "}')["passed"])
        for value in ("The answer is Alpha, Beta", "Alpha, Betamax", "Alpha Beta", '"Alpha, Beta"'):
            with self.subTest(value=value):
                self.assertFalse(answer_grading.grade(reference, json.dumps({"answer": value}))["passed"])


if __name__ == "__main__":
    unittest.main()
