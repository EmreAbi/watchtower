import json
import unittest

import choice_grading


def case(expected="none"):
    return {"question_id": "selection", "request": {"questions": {"selection": {"criteria": {
        "candidate": "Synthetic candidate", "none": "No supplied candidate", "unknown": "Insufficient evidence", "uncertain": "Uncertain classification"}}}},
        "expected": {"questions": {"selection": {"type": "choice", "value": expected}}}}


def response(value, qid="selection", kind="choice"):
    return json.dumps({"questions": {qid: {"type": kind, "value": value}}})


class ChoiceGradingTests(unittest.TestCase):
    def test_exact_declared_choice_has_binary_silver_agreement(self):
        for expected in ("candidate", "none", "unknown", "uncertain"):
            for actual in ("candidate", "none", "unknown", "uncertain"):
                with self.subTest(expected=expected, actual=actual):
                    grade = choice_grading.grade(case(expected), response(actual))
                    self.assertEqual(grade["score"], float(expected == actual))
                    self.assertEqual(grade["passed"], expected == actual)
                    self.assertTrue(grade["metrics"]["format_valid"])
                    self.assertEqual(grade["metrics"]["scoring_kind"], "reference-agreement")

    def test_strict_type_question_keys_values_and_old_protocol_are_rejected(self):
        invalid = [response("None"), response(" none "), response(None), response(0), response("missing"),
                   response("none", qid="unexpected"), response("none", kind="ordinal"),
                   '{"answers":{"selection":{"type":"choice","choice":"none"}}}',
                   '{"questions":{"selection":{"type":"choice","value":"none","probabilities":{}}}}',
                   '{"questions":{"selection":{"type":"choice","value":"none"},"extra":{"type":"choice","value":"none"}}}',
                   '{"questions":{"selection":{"type":"choice","value":"none"}},"reasoning":"extra"}',
                   '```json\n' + response("none") + '\n```', "not JSON", "x" * 33000]
        for value in invalid:
            with self.subTest(value=value[:100]):
                grade = choice_grading.grade(case(), value)
                self.assertEqual(grade["score"], 0)
                self.assertFalse(grade["metrics"]["format_valid"])

    def test_duplicate_keys_at_any_depth_never_receive_credit(self):
        for text in ('{"questions":{},"questions":{"selection":{"type":"choice","value":"none"}}}',
                     '{"questions":{"selection":{"type":"choice","value":"candidate","value":"none"}}}',
                     '{"questions":{"selection":{"type":"choice","value":"none"},"selection":{"type":"choice","value":"none"}}}'):
            with self.subTest(text=text):
                self.assertFalse(choice_grading.grade(case(), text)["metrics"]["format_valid"])

    def test_executable_or_nested_payloads_are_only_rejected_text(self):
        for value in ('__import__("os").system("unexpected")', {"type": "choice"}, [], True):
            self.assertEqual(choice_grading.grade(case(), response(value))["score"], 0)


if __name__ == "__main__":
    unittest.main()
