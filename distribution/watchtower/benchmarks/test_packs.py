from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import packs


class PackTests(unittest.TestCase):
    def test_all_four_frozen_versions_and_fixed_quick_cases(self):
        summaries = [item for item in packs.list_packs() if item["collection"] == "watchtower"]
        self.assertEqual([item["id"] for item in summaries], ["structured-decisions", "coding", "code-review", "debugging"])
        for item in summaries:
            with self.subTest(pack=item["id"]):
                self.assertEqual((item["case_count"], item["quick_count"], item["version"]), (20, 5, 1))
                data = packs.load_pack(item["id"])
                self.assertEqual(data["hash"], packs.MANIFEST[item["id"]][1])
                self.assertEqual(len(packs.select_cases(data, "full")), 20)
                self.assertEqual([case["id"] for case in packs.select_cases(data)], data["quick_case_ids"])
                self.assertIn("synthetic", data["provenance"])
                self.assertIn("not general", data["limitations"])

    def test_published_subsets_have_pinned_sources_balanced_quick_and_no_catalog_answers(self):
        published = [item for item in packs.list_packs() if item["collection"] == "published-subset"]
        self.assertEqual([item["id"] for item in published], ["gsm8k-subset", "bbh-subset", "bbeh-subset"])
        self.assertEqual(sum(item["case_count"] for item in packs.list_packs()), 200)
        for info in published:
            with self.subTest(pack=info["id"]):
                pack = packs.load_pack(info["id"])
                self.assertEqual((info["case_count"], info["quick_count"]), (40, 5))
                self.assertEqual(info["scoring_kind"], "accuracy")
                self.assertEqual(len(pack["source"]["revision"]), 40)
                self.assertEqual(len({case["source_id"] for case in pack["cases"]}), 40)
                self.assertTrue((packs.PACK_ROOT.parent / pack["source"]["license_file"]).is_file())
                self.assertNotIn("expected", info)
                self.assertNotIn("cases", info)
                self.assertIn("subset", info["limitations"].lower())
                if info["id"] != "gsm8k-subset":
                    self.assertEqual(len({case["topic"] for case in packs.select_cases(pack)}), 5)

    def test_source_reference_and_case_metadata_changes_invalidate_published_pack(self):
        original = packs.load_pack("gsm8k-subset")
        del original["hash"]
        changes = [lambda p: p["source"].update(revision="0" * 40),
                   lambda p: p["source"].update(selection="a different sample"),
                   lambda p: p["cases"][0].update(source_id="wrong origin"),
                   lambda p: p["cases"][0]["expected"].update(answer="999")]
        with tempfile.TemporaryDirectory() as temp, patch.object(packs, "PACK_ROOT", Path(temp).resolve()):
            for change in changes:
                data = deepcopy(original)
                change(data)
                (Path(temp).resolve() / "gsm8k-subset-v1.json").write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(change=change), self.assertRaises(packs.PackError):
                    packs.load_pack("gsm8k-subset")

    def test_public_projection_is_pure_and_cannot_leak_private_reference_fields(self):
        for pack in packs.list_packs():
            for case in packs.load_pack(pack["id"])["cases"]:
                original = deepcopy(case)
                case["expected"]["private_marker"] = "DO_NOT_SEND_REFERENCE_7b832"
                case["hidden_extra"] = "DO_NOT_SEND_EXAMPLES_819ab"
                request = packs.public_request(case)
                self.assertEqual(request, original["prompt"])
                self.assertNotIn("DO_NOT_SEND", request)
                self.assertIn("JSON", request)

    def test_selection_returns_detached_snapshots(self):
        data = packs.load_pack("coding")
        selected = packs.select_cases(data)
        selected[0]["expected"]["tests"][0]["result"] = "changed"
        self.assertNotEqual(selected[0], data["cases"][0])
        self.assertEqual(packs.load_pack("coding")["cases"][0], data["cases"][0])

    def test_in_place_semantic_changes_rejected_for_prompts_references_and_selection(self):
        original = packs.load_pack("coding")
        del original["hash"]
        changes = [lambda data: data["cases"][0].update(prompt="Modified public request"),
                   lambda data: data["cases"][0]["expected"]["tests"][0].update(result=999),
                   lambda data: data["cases"][0]["expected"].update(reference_code="def solve(): return 999"),
                   lambda data: data["quick_case_ids"].reverse(),
                   lambda data: data.update(grading_version="2")]
        with tempfile.TemporaryDirectory() as temp, patch.object(packs, "PACK_ROOT", Path(temp).resolve()):
            for change in changes:
                data = deepcopy(original)
                change(data)
                (Path(temp).resolve() / "coding-v1.json").write_text(json.dumps(data), encoding="utf-8")
                with self.subTest(change=change), self.assertRaises(packs.PackError):
                    packs.load_pack("coding")

    def test_snapshot_tampering_and_invalid_identifiers_modes_rejected(self):
        data = packs.load_pack("coding")
        data["cases"][0]["expected"]["tests"][0]["result"] = 999
        with self.assertRaises(packs.PackError):
            packs.select_cases(data)
        for value in ("../coding", "unknown", None, []):
            with self.subTest(value=value), self.assertRaises(packs.PackError):
                packs.load_pack(value)
        with self.assertRaises(packs.PackError):
            packs.select_cases(packs.load_pack("coding"), "random")


if __name__ == "__main__":
    unittest.main()
