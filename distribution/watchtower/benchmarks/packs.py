"""Frozen Model Lab packs; never customer/production records.

Original packs have 20 cases; published subsets have 40. Each has five Quick cases.
The manifest pins canonical JSON including prompts, hidden examples/references,
quick selection, provenance and grading version. Changing those requires a new
version and manifest entry. Formatting-only JSON changes preserve its identity.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


PACK_ROOT = Path(__file__).resolve().parent / "packs"
GRADING_VERSION = "1"
ANSWER_GRADING_VERSION = "answer-1"
PUBLISHED_CAPABILITIES = {
    "gsm8k-subset": "numeric-answer",
    "bbh-subset": "exact-answer",
    "bbeh-subset": "normalized-answer",
}
MANIFEST = {
    "structured-decisions": (1, "d38b9c09bbfd08332bed7709adf8e11eafff95a56da78dec34eb3255931df215"),
    "coding": (1, "63e4c77db75ae1ae447bd3b40e338bf74da735a5ba3fff293a03cc467e3cd8c2"),
    "code-review": (1, "b4d18331a28b4cf8962baf4f589c15ce7b6e68f8a59773bffb2bbe1936a85c80"),
    "debugging": (1, "576e3e0142b88bf433651a590b626a9923d6bde5cd8842497c0c0e5cddab6498"),
    "gsm8k-subset": (1, "3d0c1030db7dd0c163721233091ba1fbb41a2cb7f5d72dbcf87c6d242b7bc900"),
    "bbh-subset": (1, "ca51c0dafba7fa3572240e7694212a31e61dd869d4fb11db1ba6ed0593fc1de8"),
    "bbeh-subset": (1, "e601e04a53e36c12a5ad8e48cbb2c3a5c3bd86d49949c172074fba5756b9750f"),
}
_CATEGORIES = {"boundary", "null-handling", "mutation", "security", "resource", "arithmetic", "logic"}
_CASE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_ROOT_FIELDS = {"id", "name", "version", "description", "capability", "grading_version", "provenance",
                "limitations", "quick_case_ids", "cases"}
_SOURCE_FIELDS = {"name", "url", "revision", "license", "license_file", "selection", "source_count", "upstream_files", "subset"}


class PackError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _validate(data, pack_id):
    published = pack_id in PUBLISHED_CAPABILITIES
    if not isinstance(data, dict) or set(data) != _ROOT_FIELDS | ({"source"} if published else set()):
        raise PackError("Benchmark pack schema is invalid.")
    capability = PUBLISHED_CAPABILITIES.get(pack_id, pack_id)
    if (data["id"] != pack_id or data["capability"] != capability
            or type(data["version"]) is not int or data["version"] != MANIFEST[pack_id][0]
            or data["grading_version"] != (ANSWER_GRADING_VERSION if published else GRADING_VERSION)):
        raise PackError("Benchmark identity or grading version is invalid.")
    for field in ("name", "description", "provenance", "limitations"):
        if not isinstance(data[field], str) or not data[field].strip():
            raise PackError("Benchmark metadata is incomplete.")
    cases = data["cases"]
    count = 40 if published else 20
    if not isinstance(cases, list) or len(cases) != count:
        raise PackError(f"This frozen benchmark must contain exactly {count} cases.")
    if published:
        source = data["source"]
        if (not isinstance(source, dict) or set(source) != _SOURCE_FIELDS or source["subset"] is not True
                or type(source["source_count"]) is not int or source["source_count"] < count
                or any(not isinstance(source[key], str) or not source[key].strip()
                       for key in ("name", "url", "revision", "license", "license_file", "selection"))
                or not re.fullmatch(r"[a-f0-9]{40}", source["revision"])
                or not source["url"].startswith("https://")
                or not re.fullmatch(r"licenses/[a-zA-Z0-9_-]+\.txt", source["license_file"])):
            raise PackError("Published benchmark source metadata is invalid.")
        files = source["upstream_files"]
        if not isinstance(files, list) or not 1 <= len(files) <= 30:
            raise PackError("Published source hashes are missing.")
        for file in files:
            if (not isinstance(file, dict) or set(file) != {"path", "url", "sha256", "count"}
                    or not isinstance(file["path"], str) or not file["path"]
                    or not isinstance(file["url"], str) or not file["url"].startswith("https://")
                    or source["revision"] not in file["url"]
                    or not isinstance(file["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", file["sha256"])
                    or type(file["count"]) is not int or file["count"] < 1):
                raise PackError("Published source file metadata is invalid.")
    identifiers = []
    for case in cases:
        if (not isinstance(case, dict) or set(case) != {"id", "capability", "prompt", "expected"} | ({"source_id", "topic"} if published else set())
                or not isinstance(case["id"], str) or not _CASE_ID.fullmatch(case["id"])
                or case["capability"] != capability or not isinstance(case["prompt"], str)
                or not case["prompt"].strip() or len(case["prompt"]) > 16000):
            raise PackError("Benchmark case schema is invalid.")
        identifiers.append(case["id"])
        expected = case["expected"]
        if not isinstance(expected, dict):
            raise PackError("Benchmark reference schema is invalid.")
        if published:
            if (any(not isinstance(case[key], str) or not case[key].strip() or len(case[key]) > 200
                    for key in ("source_id", "topic"))
                    or set(expected) != {"answer"} or not isinstance(expected["answer"], str)
                    or not expected["answer"].strip() or len(expected["answer"]) > 4096):
                raise PackError("Published answer reference is invalid.")
            if capability == "numeric-answer" and not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", expected["answer"]):
                raise PackError("Numeric answer reference is invalid.")
        elif pack_id == "structured-decisions":
            if (set(expected) != {"action", "priority", "reason"}
                    or expected["action"] not in {"approve", "reject", "review"}
                    or type(expected["priority"]) is not int or expected["priority"] not in (0, 1, 2)
                    or expected["reason"] not in {"routine", "insufficient_data", "unverified", "risk_limit", "manual_check"}):
                raise PackError("Structured decision reference is invalid.")
        elif pack_id == "code-review":
            if set(expected) != {"findings"} or not isinstance(expected["findings"], list):
                raise PackError("Review reference is invalid.")
            for finding in expected["findings"]:
                if (not isinstance(finding, dict) or set(finding) != {"line", "category"}
                        or type(finding["line"]) is not int or finding["line"] <= 0
                        or finding["category"] not in _CATEGORIES):
                    raise PackError("Review finding reference is invalid.")
        else:
            if (set(expected) != {"function", "tests", "reference_code"} or expected["function"] != "solve"
                    or not isinstance(expected["reference_code"], str) or len(expected["reference_code"]) > 12000
                    or not isinstance(expected["tests"], list) or not 5 <= len(expected["tests"]) <= 20):
                raise PackError("Pure-function reference is invalid.")
            for test in expected["tests"]:
                if (not isinstance(test, dict) or not {"args", "result"} <= set(test)
                        or set(test) - {"args", "kwargs", "result"} or not isinstance(test["args"], list)
                        or ("kwargs" in test and not isinstance(test["kwargs"], dict))):
                    raise PackError("Hidden example schema is invalid.")
    quick = data["quick_case_ids"]
    if (len(set(identifiers)) != count or not isinstance(quick, list) or len(quick) != 5
            or quick != identifiers[:5]):
        raise PackError("Fixed quick selection or case IDs are invalid.")
    if published and len({case["source_id"] for case in cases}) != count:
        raise PackError("Published source IDs must be unique.")


def load_pack(pack_id):
    if not isinstance(pack_id, str) or pack_id not in MANIFEST:
        raise PackError("Choose a built-in benchmark pack.")
    version, pinned = MANIFEST[pack_id]
    try:
        with (PACK_ROOT / f"{pack_id}-v{version}.json").open("rb") as stream:
            raw = stream.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise ValueError()
        data = json.loads(raw.decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _validate(data, pack_id)
        actual = _hash(data)
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise PackError("The bundled benchmark pack is missing or invalid; repair this installation.") from None
    if actual != pinned:
        raise PackError("The frozen benchmark pack changed. Use a new version instead of editing v1.")
    return {**data, "hash": actual}


def pack_info(pack):
    """Public, lightweight catalog data; excludes answers and full case prompts."""
    source = pack.get("source", {})
    category = {"gsm8k-subset": "math-reasoning", "bbh-subset": "reasoning", "bbeh-subset": "advanced-reasoning"}.get(
        pack["id"], pack.get("capability", pack["id"]))
    return {field: deepcopy(pack[field]) for field in ("id", "name", "version", "description", "capability") if field in pack} | {
        "case_count": len(pack["cases"]), "quick_count": len(pack["quick_case_ids"]),
        "collection": "published-subset" if source else "watchtower", "category": category,
        "source_name": source.get("name", "Watchtower"), "url": source.get("url", ""),
        "revision": source.get("revision", ""), "license": source.get("license", ""),
        "selection": source.get("selection", "Fixed original Watchtower cases"),
        "source_count": source.get("source_count", len(pack["cases"])), "limitations": pack.get("limitations", ""),
        "scoring_kind": "accuracy" if source else "case-credit",
    }


def list_packs():
    result = []
    for pack_id in MANIFEST:
        pack = load_pack(pack_id)
        result.append(pack_info(pack))
    return result


def select_cases(pack, mode="quick"):
    if mode not in ("quick", "full"):
        raise PackError("Choose quick or full benchmark mode.")
    try:
        data = {key: value for key, value in pack.items() if key != "hash"}
        _validate(data, data["id"])
        if _hash(data) != MANIFEST[data["id"]][1] or pack["hash"] != MANIFEST[data["id"]][1]:
            raise ValueError()
    except (AttributeError, ValueError, KeyError, TypeError):
        raise PackError("The selected benchmark snapshot does not match its frozen version.") from None
    cases = pack["cases"] if mode == "full" else [case for case in pack["cases"] if case["id"] in pack["quick_case_ids"]]
    return deepcopy(cases)


def public_request(case):
    """Pure projection: only the public prompt, never references or examples."""
    prompt = case.get("prompt") if isinstance(case, dict) else None
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise PackError("Benchmark prompt is invalid.")
    return prompt
