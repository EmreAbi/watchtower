"""Binary answer accuracy for pinned published subsets; no generated code execution.

The Watchtower JSON answer envelope is an adapted, zero-shot protocol. Scores
are local subset accuracy, not official full-dataset leaderboard scores.
"""
from decimal import Decimal, InvalidOperation
import json
import re

GRADING_VERSION = "answer-1"
MAX_RESPONSE = 32768
_NUMBER = re.compile(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?\Z", re.ASCII)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _numeric(value):
    value = value.strip()
    if len(value) > 256 or not _NUMBER.fullmatch(value):
        raise ValueError("A plain numeric answer is required")
    return Decimal(value.replace(",", ""))


def grade(case, response):
    """Parse exactly one answer string and award whole-case credit only."""
    def result(correct, valid, detail):
        return dict(score=float(correct), passed=bool(correct), details=detail,
                    metrics=dict(format_valid=valid, exact_match=bool(correct), scoring_kind="accuracy"))
    try:
        if not isinstance(response, str) or len(response) > MAX_RESPONSE:
            raise ValueError()
        answer = json.loads(response, object_pairs_hook=_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(answer, dict) or set(answer) != {"answer"}
                or not isinstance(answer["answer"], str) or len(answer["answer"]) > 4096):
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        return result(False, False, 'Return one JSON object with exactly one string field: "answer".')
    value, expected = answer["answer"].strip(), case["expected"]["answer"].strip()
    if case["capability"] == "numeric-answer":
        try:
            correct = _numeric(value) == _numeric(expected)
        except (ValueError, InvalidOperation):
            return result(False, False, "The answer must be a plain integer or decimal, without units or expressions.")
        detail = "Final numeric answer matches." if correct else "Final numeric answer differs from the reference."
    elif case["capability"] == "normalized-answer":
        # BBEH's source matcher also allows broad fuzzy matches. Deliberately use
        # only these declared normalizations, never substrings or eval/model code.
        normalize = lambda text: re.sub(r"\s*,\s*", ",", text.casefold().strip())
        correct = normalize(value) == normalize(expected)
        detail = "Final answer matches after case/comma-spacing normalization." if correct else "Final answer differs after case/comma-spacing normalization; no fuzzy matching is used."
    elif case["capability"] == "exact-answer":
        correct = value == expected
        detail = "Final answer matches exactly (outer whitespace ignored)." if correct else "Final answer differs from the reference; spelling, case and internal spacing are significant."
    else:
        raise ValueError("Unsupported answer capability")
    return result(correct, True, detail)
