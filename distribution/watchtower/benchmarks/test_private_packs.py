"""Synthetic private fixtures only: no domain rows or copied source examples."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import private_packs

# Deliberately tiny, invented populations; no private evaluation metadata.
FIXTURE_SPECS = {
    "private-decisions": {"stage": 1, "count": 9, "unscored": 2, "quality": "silver"},
    "private-selection": {"stage": 2, "count": 3, "unscored": 0, "quality": "ai_silver"},
    "private-quantity": {"stage": 3, "count": 6, "unscored": 0, "quality": "silver"},
    "private-evidence": {"stage": 4, "count": 7, "unscored": 1, "quality": "gold"},
}
NOT_READY_FIXTURE = [{"id": "future-test", "status": "not_ready", "reason": "No synthetic reference set yet."}]


def make_pack(pack_id="private-decisions"):
    """Shared hermetic fixture for service tests; never opens actual private sources."""
    spec = FIXTURE_SPECS[pack_id]
    cases, excluded = [], []
    total = spec["count"] + spec["unscored"]
    for index in range(total):
        scored = index < spec["count"]
        label = "product" if spec["stage"] == 1 else "none"
        if not scored:
            label = "unresolved"
        qid = "fixture_choice"
        choices = {"product": "Synthetic product", "service": "Synthetic service", "uncertain": "Synthetic uncertainty"} if spec["stage"] == 1 else {
            "none": "No supplied candidate", "unknown": "Insufficient supplied evidence"}
        request = {"state": "SYNTHETIC INPUT " + str(index), "questions": {qid: {
            "type": "choice", "instructions": "Choose a synthetic fixture option.", "criteria": choices}}}
        case = {"id": f"fixture-{index:04d}", "capability": "typed-choice", "prompt": private_packs.render_request(request),
                "request": request, "source_id": f"synthetic-source-{index:04d}", "topic": "synthetic-fixture", "stage": spec["stage"],
                "question_id": qid, "reference_quality": spec["quality"] if scored else "unscored", "reference_label": label,
                "evidence": "PRIVATE_REFERENCE_EVIDENCE_DO_NOT_SEND_93f2",
                "provenance": {"source_index": index,
                               "source_record_hash": "a" * 64, "reference_record_hash": "b" * 64}}
        if scored:
            case["expected"] = {"questions": {qid: {"type": "choice", "value": label}}}
            cases.append(case)
        else:
            case["exclusion_reason"] = "Synthetic unresolved row has no scored reference."
            excluded.append(case)
    keys = ("inputs", "references")
    return {
        "id": pack_id, "name": "Synthetic " + pack_id, "version": 1, "description": "Synthetic test fixture only.",
        "capability": "typed-choice", "grading_version": private_packs.GRADING_VERSION, "protocol": private_packs.PROTOCOL,
        "provenance": "Synthetic source fixtures, never domain data.", "limitations": "Synthetic silver fixture; no gold accuracy claim.",
        "source": {"name": "Synthetic private test", "source_count": total, "selection": "Synthetic fixture order.",
                   "files": [{"key": key, "path": key + ".jsonl", "sha256": "a" * 64 if key == "inputs" else "b" * 64, "count": total} for key in keys]},
        "reference_quality": spec["quality"], "instructions": private_packs.INSTRUCTIONS,
        "output_schema": deepcopy(private_packs.OUTPUT_SCHEMA), "not_ready_scenarios": deepcopy(NOT_READY_FIXTURE),
        "quick_case_ids": [case["id"] for case in cases[:5]], "cases": cases, "unscored_cases": excluded,
    }


def install_fixture(root, pack_id="private-decisions", pack=None):
    """Write an immutable-shape synthetic fixture at an explicit temporary library."""
    pack = deepcopy(pack if pack is not None else make_pack(pack_id))
    pack.pop("hash", None)
    directory = Path(root) / pack["id"] / "v1"
    directory.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(pack, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    manifest = {"schema": private_packs.SCHEMA, "id": pack["id"], "version": 1, "protocol": pack["protocol"],
                "pack_hash": private_packs.canonical_hash(pack), "files": {"pack.json": hashlib.sha256(raw).hexdigest()},
                "source_hashes": {item["key"]: item["sha256"] for item in pack["source"]["files"]}}
    (directory / "pack.json").write_bytes(raw)
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {**pack, "hash": manifest["pack_hash"]}


class PrivatePackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_roundtrip_all_private_packs_and_bounded_quick_without_catalog_payloads(self):
        for pack_id, spec in FIXTURE_SPECS.items():
            with self.subTest(pack=pack_id):
                original = install_fixture(self.root, pack_id)
                loaded = private_packs.load_pack(self.root, pack_id)
                self.assertEqual(loaded, original)
                self.assertEqual(len(private_packs.select_cases(loaded, "full")), spec["count"])
                self.assertEqual(len(private_packs.select_cases(loaded)), min(5, spec["count"]))
                self.assertEqual(len(loaded["unscored_cases"]), spec["unscored"])
                self.assertTrue(all("expected" not in case for case in loaded["unscored_cases"]))
                info = private_packs.pack_info(loaded)
                self.assertEqual(info["collection"], "private")
                self.assertEqual(info["scoring_kind"], "reference-agreement")
                for marker in ("SYNTHETIC INPUT", "PRIVATE_REFERENCE_EVIDENCE", "expected", "request"):
                    self.assertNotIn(marker, json.dumps(info))
        self.assertEqual(len(private_packs.list_packs(self.root)), 4)

    def test_legacy_snapshot_keeps_protocol_identity_and_hash(self):
        pack = make_pack()
        pack["id"] = "pi-legacy-fixture"
        pack["protocol"] = "pi-typed-choice-json-v1"
        original = install_fixture(self.root, pack=pack)
        before = {p: p.read_bytes() for p in self.root.rglob("*.json")}
        self.assertEqual(private_packs.load_pack(self.root, pack["id"]), original)
        self.assertEqual(private_packs.list_packs(self.root)[0]["id"], pack["id"])
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*.json")})

    def test_new_identity_and_arbitrary_declared_source_metadata(self):
        pack = make_pack()
        pack["id"] = "private-new-scenario"
        pack["source"]["files"][0]["path"] = "local-only/input.jsonl"
        pack["source"]["files"][0]["sha256"] = "c" * 64
        original = install_fixture(self.root, pack=pack)
        self.assertEqual(private_packs.load_pack(self.root, pack["id"]), original)
        self.assertEqual(private_packs.list_packs(self.root)[0]["id"], pack["id"])

    def test_source_manifest_and_protocol_mismatch_fail_closed(self):
        for field, value in (("source_hashes", {"inputs": "c" * 64}), ("protocol", "pi-typed-choice-json-v1")):
            with self.subTest(field=field):
                pack = install_fixture(self.root)
                path = self.root / pack["id"] / "v1/manifest.json"
                manifest = json.loads(path.read_bytes())
                manifest[field] = value
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(private_packs.PrivatePackError):
                    private_packs.load_pack(self.root, pack["id"])

    def test_absent_library_is_read_only_and_returns_empty(self):
        absent = self.root / "absent"
        self.assertEqual(private_packs.list_packs(absent), [])
        self.assertFalse(absent.exists())

    def test_snapshot_and_selection_are_detached_and_exclude_unscored(self):
        pack = install_fixture(self.root)
        cases = private_packs.select_cases(pack)
        cases[0]["request"]["state"] = "changed by caller"
        self.assertNotEqual(cases[0], pack["cases"][0])
        selected_ids = {case["id"] for case in private_packs.select_cases(pack, "full")}
        self.assertFalse(selected_ids & {case["id"] for case in pack["unscored_cases"]})
        pack["cases"][0]["evidence"] = "tampered"
        with self.assertRaises(private_packs.PrivatePackError):
            private_packs.validate_snapshot(pack)

    def test_traversal_links_unknown_names_and_versions_rejected(self):
        install_fixture(self.root)
        for value in ("../private-decisions", "private-unknown", "coding", None, []):
            with self.subTest(value=value), self.assertRaises(private_packs.PrivatePackError):
                private_packs.load_pack(self.root, value)
        with self.assertRaises(private_packs.PrivatePackError):
            private_packs.list_packs(Path("relative"))
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path.name == "v1"):
            with self.assertRaises(private_packs.PrivatePackError):
                private_packs.load_pack(self.root, "private-decisions")
        path = self.root / "private-decisions/v1/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["version"] = 2
        path.write_text(json.dumps(manifest))
        with self.assertRaises(private_packs.PrivatePackError):
            private_packs.load_pack(self.root, "private-decisions")

    def test_byte_hash_duplicate_json_and_size_limits_fail_closed(self):
        install_fixture(self.root)
        directory = self.root / "private-decisions/v1"
        pack_file = directory / "pack.json"
        pack_file.write_bytes(pack_file.read_bytes() + b" ")
        with self.assertRaises(private_packs.PrivatePackError):
            private_packs.load_pack(self.root, "private-decisions")
        install_fixture(self.root)
        manifest_file = directory / "manifest.json"
        raw = manifest_file.read_text()
        manifest_file.write_text(raw[:-1] + ',"id":"private-decisions"}')
        with self.assertRaises(private_packs.PrivatePackError):
            private_packs.load_pack(self.root, "private-decisions")
        install_fixture(self.root)
        with patch.object(private_packs, "MAX_PACK_BYTES", 20):
            with self.assertRaises(private_packs.PrivatePackError):
                private_packs.load_pack(self.root, "private-decisions")

    def test_invalid_reference_source_pin_duplicate_case_and_unscored_expected_rejected(self):
        changes = [lambda p: p["cases"][0]["expected"]["questions"]["fixture_choice"].update(value="missing-option"),
                   lambda p: p["source"]["files"][0].update(sha256="not-a-digest"),
                   lambda p: p["cases"].__setitem__(1, deepcopy(p["cases"][0])),
                   lambda p: p["unscored_cases"][0].update(expected={"invented": "gold"}),
                   lambda p: p["cases"][0].update(prompt="different input")]
        for change in changes:
            pack = make_pack()
            change(pack)
            install_fixture(self.root, pack=pack)
            with self.subTest(change=change), self.assertRaises(private_packs.PrivatePackError):
                private_packs.load_pack(self.root, "private-decisions")


if __name__ == "__main__":
    unittest.main()
