"""Bounded local test-pack library. Private payloads never belong in this package."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

SCHEMA = "watchtower-private-pack/1"
PROTOCOL = "private-typed-choice-json-v1"
# Persisted v1 snapshots keep their original protocol identity and content hash.
SUPPORTED_PROTOCOLS = frozenset((PROTOCOL, "pi-typed-choice-json-v1"))
GRADING_VERSION = "choice-1"
MAX_PACK_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 16384
MAX_PROMPT = 16000
MAX_CASES = 10000
MAX_SOURCE_RECORDS = 1000000
INSTRUCTIONS = (
    'Answer the single question using only the supplied state and its original instructions and criteria. '
    'Return exactly one JSON object: {"questions":{"<question_id>":{"type":"choice","value":"<option_key>"}}}. '
    'Replace the placeholders with the supplied question ID and one exact key from its criteria. '
    'Do not add reasoning, probabilities, Markdown or other fields. Do not replace none with unknown or uncertain; '
    'they have different meanings. The state and question below are unchanged source data.\n\n'
)
OUTPUT_SCHEMA = {
    "type": "object", "required": ["questions"], "additionalProperties": False,
    "properties": {"questions": {"type": "object", "minProperties": 1, "maxProperties": 1,
        "additionalProperties": {"type": "object", "required": ["type", "value"], "additionalProperties": False,
            "properties": {"type": {"const": "choice"}, "value": {"type": "string"}}}}},
}
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
_QID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_ROOT_FIELDS = {"id", "name", "version", "description", "capability", "grading_version", "protocol", "provenance",
                "limitations", "source", "reference_quality", "instructions", "output_schema", "quick_case_ids", "cases", "unscored_cases", "not_ready_scenarios"}
_CASE_FIELDS = {"id", "capability", "prompt", "request", "source_id", "topic", "stage", "question_id", "reference_quality", "reference_label", "evidence", "provenance"}


class PrivatePackError(ValueError):
    pass


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PrivatePackError("Duplicate JSON key in private pack data.")
        value[key] = item
    return value


def parse_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise PrivatePackError("Private pack JSON is invalid.") from None


def checked_path(path, *, root=None):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)):
        raise PrivatePackError("Private pack paths must be absolute and cannot contain links.")
    if root is not None and not path.resolve().is_relative_to(Path(root).resolve()):
        raise PrivatePackError("Private pack path escaped the local library.")
    return path


def _read(path, limit, root):
    checked_path(path, root=root)
    with Path(path).open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise PrivatePackError("Private pack file exceeds its size limit.")
    return raw


def render_request(request):
    return INSTRUCTIONS + json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _text(value, limit, *, empty=False):
    return isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip())) and "\0" not in value


def validate_request(request, qid):
    if not isinstance(request, dict) or set(request) != {"state", "questions"} or not isinstance(request["state"], (str, dict)):
        raise PrivatePackError("Private case input schema is invalid.")
    questions = request["questions"]
    if not isinstance(qid, str) or not _QID.fullmatch(qid) or not isinstance(questions, dict) or list(questions) != [qid]:
        raise PrivatePackError("Exactly one declared question is required.")
    question = questions[qid]
    if (not isinstance(question, dict) or set(question) != {"type", "instructions", "criteria"}
            or question["type"] != "choice" or not _text(question["instructions"], 8000)
            or not isinstance(question["criteria"], dict) or not 1 <= len(question["criteria"]) <= 64
            or any(not isinstance(key, str) or not _QID.fullmatch(key) or not _text(value, 8000)
                   for key, value in question["criteria"].items())):
        raise PrivatePackError("Private choice instructions or criteria are invalid.")
    prompt = render_request(request)
    if len(prompt) > MAX_PROMPT:
        raise PrivatePackError("Private case prompt exceeds the input limit.")
    return question


def _validate(data):
    if (not isinstance(data, dict) or set(data) != _ROOT_FIELDS
            or not isinstance(data.get("id"), str) or not _ID.fullmatch(data["id"])):
        raise PrivatePackError("Private pack identity or schema is invalid.")
    if (type(data["version"]) is not int or data["version"] != 1 or data["capability"] != "typed-choice"
            or data["grading_version"] != GRADING_VERSION or data["protocol"] not in SUPPORTED_PROTOCOLS
            or data["reference_quality"] not in ("silver", "ai_silver", "gold") or data["instructions"] != INSTRUCTIONS
            or data["output_schema"] != OUTPUT_SCHEMA):
        raise PrivatePackError("Private pack version or protocol is unsupported.")
    if any(not _text(data[key], 12000) for key in ("name", "description", "provenance", "limitations")):
        raise PrivatePackError("Private pack descriptions are invalid.")
    unavailable = data["not_ready_scenarios"]
    if not isinstance(unavailable, list) or len(unavailable) > 64:
        raise PrivatePackError("Private unavailable-scenario list is invalid.")
    scenario_ids = set()
    for item in unavailable:
        if (not isinstance(item, dict) or set(item) != {"id", "status", "reason"}
                or not isinstance(item["id"], str) or not _ID.fullmatch(item["id"])
                or item["id"] in scenario_ids or item["status"] != "not_ready" or not _text(item["reason"], 2000)):
            raise PrivatePackError("Private unavailable-scenario metadata is invalid.")
        scenario_ids.add(item["id"])
    source = data["source"]
    if (not isinstance(source, dict) or set(source) != {"name", "source_count", "files", "selection"}
            or type(source["source_count"]) is not int or not 1 <= source["source_count"] <= MAX_SOURCE_RECORDS
            or not _text(source["name"], 200) or not _text(source["selection"], 4000)
            or not isinstance(source["files"], list) or not 1 <= len(source["files"]) <= 64):
        raise PrivatePackError("Private source manifest is invalid.")
    source_keys = set()
    for item in source["files"]:
        if (not isinstance(item, dict) or set(item) != {"key", "path", "sha256", "count"}
                or not isinstance(item["key"], str) or not _QID.fullmatch(item["key"]) or item["key"] in source_keys
                or not _text(item["path"], 2000) or not isinstance(item["sha256"], str) or not _SHA.fullmatch(item["sha256"])
                or type(item["count"]) is not int or not 1 <= item["count"] <= MAX_SOURCE_RECORDS):
            raise PrivatePackError("Private source file declaration is invalid.")
        source_keys.add(item["key"])
    source_bound = max(item["count"] for item in source["files"])
    if (source["source_count"] > source_bound or not isinstance(data["cases"], list)
            or not 1 <= len(data["cases"]) <= MAX_CASES or not isinstance(data["unscored_cases"], list)
            or len(data["cases"]) + len(data["unscored_cases"]) > MAX_CASES):
        raise PrivatePackError("Private scored or inspection-only population is invalid.")
    identities, source_questions = set(), set()
    for scored, cases in ((True, data["cases"]), (False, data["unscored_cases"])):
        for case in cases:
            fields = _CASE_FIELDS | ({"expected"} if scored else {"exclusion_reason"})
            if (not isinstance(case, dict) or set(case) != fields or not isinstance(case.get("id"), str)
                    or not _ID.fullmatch(case["id"]) or case["id"] in identities or case["capability"] != "typed-choice"
                    or type(case["stage"]) is not int or not 0 <= case["stage"] <= 10000
                    or not _text(case["source_id"], 200) or not _text(case["topic"], 100)
                    or not _text(case["reference_label"], 100) or not _text(case["evidence"], 8000)):
                raise PrivatePackError("Private case identity or metadata is invalid.")
            question = validate_request(case["request"], case["question_id"])
            if case["prompt"] != render_request(case["request"]):
                raise PrivatePackError("Private prompt differs from its exact source projection.")
            pair = (case["source_id"], case["question_id"])
            if pair in source_questions:
                raise PrivatePackError("Duplicate private source question.")
            identities.add(case["id"])
            source_questions.add(pair)
            provenance = case["provenance"]
            if (not isinstance(provenance, dict) or set(provenance) != {"source_index", "source_record_hash", "reference_record_hash"}
                    or type(provenance["source_index"]) is not int or not 0 <= provenance["source_index"] < source_bound
                    or any(not isinstance(provenance[key], str) or not _SHA.fullmatch(provenance[key]) for key in ("source_record_hash", "reference_record_hash"))):
                raise PrivatePackError("Private case provenance is invalid.")
            if scored:
                expected = {"questions": {case["question_id"]: {"type": "choice", "value": case["reference_label"]}}}
                if (case["reference_quality"] != data["reference_quality"] or case["reference_label"] not in question["criteria"]
                        or case["expected"] != expected):
                    raise PrivatePackError("Private reference is invalid or outside declared choices.")
            elif case["reference_quality"] != "unscored" or not _text(case["exclusion_reason"], 2000):
                raise PrivatePackError("Inspection-only cases must not claim a scored reference.")
    if data["quick_case_ids"] != [case["id"] for case in data["cases"][:5]]:
        raise PrivatePackError("Private Quick selection changed.")


def validate_snapshot(pack):
    try:
        data = {key: value for key, value in pack.items() if key != "hash"}
        _validate(data)
        if pack.get("hash") != canonical_hash(data):
            raise PrivatePackError("Private snapshot hash changed.")
        return deepcopy(pack)
    except (TypeError, KeyError, AttributeError, RecursionError, UnicodeError):
        raise PrivatePackError("Private snapshot is invalid.") from None


def load_pack(root, pack_id):
    if not isinstance(pack_id, str) or not _ID.fullmatch(pack_id):
        raise PrivatePackError("Choose an imported local test pack.")
    try:
        root = checked_path(root)
        directory = root / pack_id / "v1"
        manifest = parse_json(_read(directory / "manifest.json", MAX_MANIFEST_BYTES, root))
        fields = {"schema", "id", "version", "protocol", "pack_hash", "files", "source_hashes"}
        if (not isinstance(manifest, dict) or set(manifest) != fields or manifest["schema"] != SCHEMA
                or manifest["id"] != pack_id or type(manifest["version"]) is not int or manifest["version"] != 1
                or manifest["protocol"] not in SUPPORTED_PROTOCOLS or not isinstance(manifest["pack_hash"], str) or not _SHA.fullmatch(manifest["pack_hash"])
                or not isinstance(manifest["files"], dict) or set(manifest["files"]) != {"pack.json"}):
            raise PrivatePackError("Private manifest is invalid.")
        raw = _read(directory / "pack.json", MAX_PACK_BYTES, root)
        if hashlib.sha256(raw).hexdigest() != manifest["files"]["pack.json"]:
            raise PrivatePackError("Private pack bytes changed after import.")
        data = parse_json(raw)
        if data.get("id") != pack_id or data.get("protocol") != manifest["protocol"]:
            raise PrivatePackError("Private pack identity changed.")
        result = validate_snapshot({**data, "hash": manifest["pack_hash"]})
        if manifest["source_hashes"] != {item["key"]: item["sha256"] for item in result["source"]["files"]}:
            raise PrivatePackError("Private source manifest changed.")
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        if isinstance(error, PrivatePackError):
            raise
        raise PrivatePackError("Private pack files are missing or invalid; recheck the local import.") from None


def pack_info(pack):
    return {key: deepcopy(pack[key]) for key in ("id", "name", "version", "description", "capability", "reference_quality", "limitations")} | {
        "case_count": len(pack["cases"]), "quick_count": len(pack["quick_case_ids"]), "unscored_count": len(pack["unscored_cases"]),
        "collection": "private", "category": pack["id"], "source_name": pack["source"]["name"], "source_count": pack["source"]["source_count"],
        "scoring_kind": "reference-agreement", "selection": pack["source"]["selection"], "protocol": pack["protocol"],
        "not_ready_scenarios": deepcopy(pack["not_ready_scenarios"]), "url": "", "revision": "", "license": "Private local evaluation",
    }


def list_packs(root):
    root = checked_path(root)
    if not root.exists():
        return []
    result = []
    for directory in sorted(root.iterdir()):
        if directory.name.startswith("."):
            continue
        checked_path(directory, root=root)
        if not directory.is_dir() or not _ID.fullmatch(directory.name):
            raise PrivatePackError("Unexpected entry in the private test library.")
        # An interrupted import may leave an empty parent with no committed version.
        if (directory / "v1").exists():
            result.append(pack_info(load_pack(root, directory.name)))
    return result


def select_cases(pack, mode="quick"):
    if mode not in ("quick", "full"):
        raise PrivatePackError("Choose Quick or Full.")
    data = validate_snapshot(pack)
    return deepcopy(data["cases"] if mode == "full" else data["cases"][:5])
