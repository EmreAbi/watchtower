"""Deterministic grading of synthetic Model Lab responses.

Generated Python is parsed, never executed or evaluated by Python. A small AST
interpreter handles only bounded pure functions over JSON-like values. It has
no imports, I/O, loops, comprehensions, helpers, recursion, or object traversal.
This measures the supplied examples, not general program correctness or safety.
"""
from __future__ import annotations

import ast
import json
import math
import operator


GRADING_VERSION = "1"
MAX_RESPONSE = 32768
MAX_CODE = 12000
MAX_NODES = 512
MAX_STEPS = 2000
MAX_ITEMS = 4096
_FUNCTIONS = {"len": len, "abs": abs, "min": min, "max": max, "sum": sum,
              "sorted": sorted, "set": set, "round": round, "int": int,
              "str": str, "bool": bool}
_METHODS = {"lower", "upper", "strip", "lstrip", "rstrip", "split", "rsplit",
            "replace", "startswith", "endswith", "count", "join"}
_BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
           ast.Pow: operator.pow}
_COMPARE = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
            ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
            ast.Is: operator.is_, ast.IsNot: operator.is_not,
            ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
_NODES = {ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Return, ast.Assign,
          ast.If, ast.Expr, ast.Constant, ast.Name, ast.Load, ast.Store, ast.List, ast.Tuple,
          ast.Set, ast.Dict, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
          ast.Subscript, ast.Slice, ast.Call, ast.Attribute, ast.keyword,
          ast.And, ast.Or, ast.Not, ast.UAdd, ast.USub, *_BINARY, *_COMPARE}


class RestrictedCode(ValueError):
    pass


class _Returned(Exception):
    def __init__(self, value):
        self.value = value


def _bounded(value, depth=0, budget=None):
    budget = [MAX_ITEMS] if budget is None else budget
    budget[0] -= 1 + (len(value) if type(value) is str else 0)
    if depth > 16 or budget[0] < 0:
        raise RestrictedCode("Value nesting limit")
    if value is None or type(value) is bool:
        return value
    if type(value) is int and value.bit_length() <= 128:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is str and len(value) <= MAX_ITEMS:
        return value
    if type(value) in (list, tuple, set, dict) and len(value) <= MAX_ITEMS:
        members = list(value.items()) if type(value) is dict else value
        for member in members:
            _bounded(member, depth + 1, budget)
        return value
    raise RestrictedCode("Value size or type limit")


class PureFunction:
    def __init__(self, source, name="solve"):
        if not isinstance(source, str) or len(source) > MAX_CODE:
            raise RestrictedCode("Code size limit")
        tree = ast.parse(source, mode="exec")
        nodes = list(ast.walk(tree))
        if len(nodes) > MAX_NODES or any(type(node) not in _NODES for node in nodes):
            raise RestrictedCode("Unsupported syntax or syntax size limit")
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise RestrictedCode("Return exactly one function")
        self.function = tree.body[0]
        arguments = self.function.args
        if (self.function.name != name or self.function.decorator_list or arguments.defaults
                or arguments.kw_defaults or arguments.kwonlyargs or arguments.vararg or arguments.kwarg):
            raise RestrictedCode("Unsupported function signature")
        self.arguments = [item.arg for item in arguments.posonlyargs + arguments.args]
        if len(self.arguments) != len(set(self.arguments)) or set(self.arguments) & set(_FUNCTIONS):
            raise RestrictedCode("Duplicate arguments or shadowed builtins")
        for node in nodes:
            if isinstance(node, ast.FunctionDef) and node is not self.function:
                raise RestrictedCode("Helper functions are unsupported")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS:
                    pass
                elif isinstance(node.func, ast.Attribute) and node.func.attr in _METHODS:
                    pass
                else:
                    raise RestrictedCode("Unsupported function call")
                names = [keyword.arg for keyword in node.keywords]
                if len(names) != len(set(names)) or any(name != "reverse" for name in names):
                    raise RestrictedCode("Unsupported call keyword")
            if isinstance(node, ast.Attribute) and node.attr not in _METHODS:
                raise RestrictedCode("Object traversal is unsupported")
            if isinstance(node, ast.Assign) and (len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name)):
                raise RestrictedCode("Only local name assignment is supported")
            if isinstance(node, ast.Assign) and node.targets[0].id in _FUNCTIONS:
                raise RestrictedCode("Shadowing builtins is unsupported")

    def call(self, args, kwargs=None):
        kwargs = kwargs or {}
        if (not isinstance(args, list) or not isinstance(kwargs, dict) or len(args) > len(self.arguments)
                or set(kwargs) - set(self.arguments) or set(kwargs) & set(self.arguments[:len(args)])):
            raise RestrictedCode("Invalid function arguments")
        self.scope = dict(zip(self.arguments, args))
        self.scope.update(kwargs)
        if set(self.scope) != set(self.arguments):
            raise RestrictedCode("Missing function arguments")
        _bounded(self.scope)
        self.steps = 0
        try:
            self._block(self.function.body)
        except _Returned as returned:
            return _bounded(returned.value)
        return None

    def _step(self):
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise RestrictedCode("Evaluation step limit")

    def _block(self, nodes):
        for node in nodes:
            self._step()
            if isinstance(node, ast.Return):
                raise _Returned(self._expression(node.value) if node.value else None)
            if isinstance(node, ast.Assign):
                self.scope[node.targets[0].id] = self._expression(node.value)
            elif isinstance(node, ast.If):
                self._block(node.body if self._expression(node.test) else node.orelse)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                pass  # Function docstrings have no effect.
            else:
                raise RestrictedCode("Unsupported statement")

    def _expression(self, node):
        self._step()
        if isinstance(node, ast.Constant):
            result = node.value
        elif isinstance(node, ast.Name):
            result = self.scope[node.id]
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            values = [self._expression(item) for item in node.elts]
            result = values if isinstance(node, ast.List) else tuple(values) if isinstance(node, ast.Tuple) else set(values)
        elif isinstance(node, ast.Dict):
            result = {self._expression(k): self._expression(v) for k, v in zip(node.keys, node.values)}
        elif isinstance(node, ast.UnaryOp):
            value = self._expression(node.operand)
            result = (not value) if isinstance(node.op, ast.Not) else -value if isinstance(node.op, ast.USub) else +value
        elif isinstance(node, ast.BinOp):
            left, right = self._expression(node.left), self._expression(node.right)
            if isinstance(node.op, ast.Pow) and (type(right) not in (int, float) or abs(right) > 16):
                raise RestrictedCode("Exponent limit")
            if isinstance(node.op, ast.Mult):
                for sequence, count in ((left, right), (right, left)):
                    if isinstance(sequence, (str, list, tuple)) and (type(count) is not int or len(sequence) * max(count, 0) > MAX_ITEMS):
                        raise RestrictedCode("Sequence size limit")
            # String formatting is outside this pure completion subset.
            if isinstance(node.op, ast.Mod) and isinstance(left, str):
                raise RestrictedCode("String formatting is unsupported")
            result = _BINARY[type(node.op)](left, right)
        elif isinstance(node, ast.BoolOp):
            result = self._expression(node.values[0])
            for value in node.values[1:]:
                if (isinstance(node.op, ast.And) and not result) or (isinstance(node.op, ast.Or) and result):
                    break
                result = self._expression(value)
        elif isinstance(node, ast.Compare):
            left = self._expression(node.left)
            result = True
            for operation, operand in zip(node.ops, node.comparators):
                right = self._expression(operand)
                if not _COMPARE[type(operation)](left, right):
                    result = False
                    break
                left = right
        elif isinstance(node, ast.IfExp):
            result = self._expression(node.body if self._expression(node.test) else node.orelse)
        elif isinstance(node, ast.Subscript):
            value = self._expression(node.value)
            if isinstance(node.slice, ast.Slice):
                index = slice(*(self._expression(part) if part else None
                                for part in (node.slice.lower, node.slice.upper, node.slice.step)))
            else:
                index = self._expression(node.slice)
            result = value[index]
        elif isinstance(node, ast.Call):
            arguments = [self._expression(arg) for arg in node.args]
            keywords = {key.arg: self._expression(key.value) for key in node.keywords}
            if isinstance(node.func, ast.Name):
                result = _FUNCTIONS[node.func.id](*arguments, **keywords)
            else:
                receiver = self._expression(node.func.value)
                if type(receiver) is not str:
                    raise RestrictedCode("Methods are supported only on strings")
                if node.func.attr == "replace" and len(arguments) >= 2 and isinstance(arguments[1], str):
                    if (len(receiver) + 1) * len(arguments[1]) > MAX_ITEMS:
                        raise RestrictedCode("Replacement size limit")
                if node.func.attr == "join" and arguments and isinstance(arguments[0], (str, list, tuple, set)):
                    if sum(len(value) for value in arguments[0]) + len(receiver) * len(arguments[0]) > MAX_ITEMS:
                        raise RestrictedCode("Join size limit")
                result = getattr(receiver, node.func.attr)(*arguments, **keywords)
        else:
            raise RestrictedCode("Unsupported expression")
        return _bounded(result)


def _equal(actual, expected):
    if type(expected) is float:
        return type(actual) in (int, float) and math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9)
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_equal(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_equal(actual[key], value) for key, value in expected.items())
    return actual == expected


def _result(score, passed, details, metrics):
    return {"score": max(0.0, min(float(score), 1.0)), "passed": bool(passed), "details": details, "metrics": metrics}


def grade(case, response):
    """Grade standard JSON without printing hidden inputs, references or code."""
    try:
        if not isinstance(response, str) or len(response) > MAX_RESPONSE:
            raise ValueError()
        answer = json.loads(response, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(answer, dict):
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        return _result(0, False, "Response must be one bounded JSON object.", {"format_valid": False})
    capability = case["capability"]
    expected = case["expected"]
    if capability == "structured-decisions":
        fields = {"action": str, "priority": int, "reason": str}
        if set(answer) != set(fields):
            return _result(0, False, "Decision schema fields do not match.", {"format_valid": False})
        matches = sum(type(answer[key]) is typ and answer[key] == expected[key] for key, typ in fields.items())
        return _result(matches / len(fields), matches == len(fields), f"{matches}/{len(fields)} decision fields correct.",
                       {"format_valid": all(type(answer[key]) is typ for key, typ in fields.items()), "fields_correct": matches, "fields_total": len(fields)})
    if capability == "code-review":
        findings = answer.get("findings")
        if set(answer) != {"findings"} or not isinstance(findings, list) or len(findings) > 100:
            return _result(0, False, "Expected a findings array.", {"format_valid": False})
        if any(not isinstance(item, dict) or set(item) != {"line", "category"}
               or type(item["line"]) is not int or item["line"] <= 0 or not isinstance(item["category"], str) for item in findings):
            return _result(0, False, "Each finding needs a positive integer line and category.", {"format_valid": False})
        reference = {(item["line"], item["category"]) for item in expected["findings"]}
        reported = {(item["line"], item["category"]) for item in findings}
        correct = len(reference & reported)
        false_alarms = len(findings) - correct  # Duplicate claims count against precision.
        precision = correct / len(findings) if findings else 1.0
        recall = correct / len(reference) if reference else 1.0
        score = (2 * precision * recall / (precision + recall) if precision + recall else 0) if reference else float(not findings)
        return _result(score, correct == len(reference) and not false_alarms,
                       f"{correct} expected findings matched; {false_alarms} false alarms.",
                       {"format_valid": True, "precision": precision, "recall": recall, "true_positives": correct,
                        "false_positives": false_alarms, "false_negatives": len(reference) - correct})
    if capability in ("coding", "debugging"):
        if set(answer) != {"code"} or not isinstance(answer.get("code"), str):
            return _result(0, False, "Expected a code string.", {"format_valid": False})
        tests = expected["tests"]
        try:
            program = PureFunction(answer["code"], expected["function"])
        except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError):
            return _result(0, False, "Code is outside the supported pure-function subset.",
                           {"format_valid": True, "supported_code": False, "tests_passed": 0, "tests_total": len(tests)})
        passed = 0
        for test in tests:
            try:
                passed += _equal(program.call(test["args"], test.get("kwargs")), test["result"])
            except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError, OverflowError, RecursionError):
                pass
        return _result(passed / len(tests), passed == len(tests), f"{passed}/{len(tests)} hidden examples passed.",
                       {"format_valid": True, "supported_code": True, "tests_passed": passed, "tests_total": len(tests)})
    raise ValueError("Unknown benchmark capability")
