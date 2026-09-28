"""Strict typed-choice reference agreement; no source data or generated code."""
import json

GRADING_VERSION = "choice-1"
MAX_RESPONSE = 32768


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def grade(case, response):
    def result(correct, valid, detail):
        return dict(score=float(correct), passed=bool(correct), details=detail,
                    metrics=dict(format_valid=valid, reference_match=bool(correct), scoring_kind="reference-agreement"))
    try:
        if not isinstance(response, str) or len(response) > MAX_RESPONSE:
            raise ValueError()
        value = json.loads(response, object_pairs_hook=_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        qid = case["question_id"]
        if not isinstance(value, dict) or set(value) != {"questions"}:
            raise ValueError()
        questions = value["questions"]
        if not isinstance(questions, dict) or set(questions) != {qid}:
            raise ValueError()
        answer = questions[qid]
        if (not isinstance(answer, dict) or set(answer) != {"type", "value"}
                or answer["type"] != "choice" or not isinstance(answer["value"], str)
                or answer["value"] not in case["request"]["questions"][qid]["criteria"]):
            raise ValueError()
    except (ValueError, TypeError, KeyError, RecursionError):
        return result(False, False, "Return only the requested question ID with type 'choice' and one exact declared option key.")
    correct = value == case["expected"]
    return result(correct, True, "Choice matches the silver reference." if correct else
                  "Choice differs from the silver reference; none, unknown and uncertain are distinct options.")
