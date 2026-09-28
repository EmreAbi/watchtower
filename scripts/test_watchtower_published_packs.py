"""Offline importer/provenance tests: no providers, downloads or dataset code."""
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from . import build_watchtower_published_packs as importer
except ImportError:
    import build_watchtower_published_packs as importer


def bundled(identifier):
    return json.loads((importer.DEFAULT_OUTPUT / "packs" / f"{identifier}-v1.json").read_text(encoding="utf-8"))


class PublishedPackTests(unittest.TestCase):
    def test_gsm_selection_is_fixed_across_entire_original_test_split(self):
        pack = bundled("gsm8k-subset")
        expected = sorted(range(1319), key=lambda i: (hashlib.sha256(f"watchtower-gsm8k-v1:{i}".encode()).hexdigest(), i))[:40]
        self.assertEqual([int(case["source_id"].rsplit(":", 1)[1]) for case in pack["cases"]], expected)
        self.assertEqual(expected[:5], [1163, 1138, 1168, 610, 108])
        self.assertEqual(pack["quick_case_ids"], [case["id"] for case in pack["cases"][:5]])
        self.assertEqual(pack["source"]["source_count"], 1319)
        self.assertIn("not an official GSM8K leaderboard score", pack["limitations"])

    def test_bbh_selection_balances_five_real_tasks_and_preserves_quick_topics(self):
        pack = bundled("bbh-subset")
        self.assertEqual(Counter(case["topic"] for case in pack["cases"]), {topic: 8 for topic in importer.TOPICS})
        self.assertEqual([case["topic"] for case in pack["cases"][:5]], list(importer.TOPICS))
        self.assertEqual(pack["quick_case_ids"], [case["id"] for case in pack["cases"][:5]])
        self.assertEqual([int(case["source_id"].rsplit(":", 1)[1]) for case in pack["cases"][:5]], [61, 249, 76, 58, 219])
        for topic in importer.TOPICS:
            expected = sorted(range(250), key=lambda i: (hashlib.sha256(f"watchtower-bbh-v1:{topic}:{i}".encode()).hexdigest(), i))[:8]
            selected = [case for case in pack["cases"] if case["topic"] == topic]
            self.assertEqual([int(case["source_id"].rsplit(":", 1)[1]) for case in selected], expected)
        self.assertEqual(pack["source"]["source_count"], 1250)
        self.assertIn(importer.CANARY, pack["provenance"])

    def test_questions_are_copied_exactly_and_gsm_solution_is_never_in_prompt(self):
        rows = [{"question": f"Original question {i}.\nSpacing  and Unicode: café.",
                 "answer": "PRIVATE_REFERENCE_WORKING\n#### 1,200.00"} for i in range(1319)]
        pack = importer.gsm8k_pack(rows)
        for case in pack["cases"]:
            index = int(case["source_id"].rsplit(":", 1)[1])
            self.assertEqual(case["prompt"], rows[index]["question"] + importer.GSM_SUFFIX)
            self.assertNotIn("PRIVATE_REFERENCE_WORKING", case["prompt"])
            self.assertEqual(case["expected"], {"answer": "1200"})
            original = rows[index]["answer"].rsplit("####", 1)[1].strip().replace(",", "")
            self.assertEqual(Decimal(case["expected"]["answer"]), Decimal(original))

    def test_bbh_original_input_and_target_are_unchanged(self):
        data = {topic: {"canary": importer.CANARY, "examples": [
            {"input": f"Original input {topic}:{i}\nExact whitespace  here.", "target": f"TARGET-{i}"} for i in range(250)
        ]} for topic in importer.TOPICS}
        pack = importer.bbh_pack(data)
        for case in pack["cases"]:
            index = int(case["source_id"].rsplit(":", 1)[1])
            original = data[case["topic"]]["examples"][index]
            self.assertEqual(case["prompt"], original["input"] + importer.BBH_SUFFIXES[case["topic"]])
            self.assertEqual(case["expected"], {"answer": original["target"]})
            self.assertNotIn(original["target"], case["prompt"])

    def test_numeric_reference_normalization_preserves_value_and_rejects_bad_grouping(self):
        for original, expected in (("1,234", "1234"), ("-1,234.500", "-1234.5"), ("0.00", "0"), ("-0.0", "0"), ("1200", "1200")):
            self.assertEqual(importer.canonical_number(original), expected)
        for invalid in ("12,34", "1,,234", "1,234,56", "12 dollars", "1/2", "NaN", "1e2"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                importer.canonical_number(invalid)

    def test_provenance_pins_revisions_data_hashes_and_original_mit_notices(self):
        for identifier, prefix, revision in (("gsm8k-subset", "gsm8k", importer.GSM_REVISION), ("bbh-subset", "bbh", importer.BBH_REVISION)):
            pack = bundled(identifier)
            source = pack["source"]
            self.assertEqual(source["revision"], revision)
            self.assertEqual(len(revision), 40)
            self.assertEqual(source["license"], "MIT")
            self.assertTrue(source["subset"])
            self.assertEqual(sum(item["count"] for item in source["upstream_files"]), source["source_count"])
            for entry in source["upstream_files"]:
                matching = [item for item in importer.SOURCES if item["url"] == entry["url"]]
                self.assertEqual(len(matching), 1)
                self.assertEqual(entry, importer._metadata(matching[0]))
                self.assertIn(revision, entry["url"])
            license_bytes = (importer.DEFAULT_OUTPUT / source["license_file"]).read_bytes()
            expected = importer.SOURCE_BY_NAME[f"{prefix}-LICENSE.txt"]["sha256"]
            self.assertEqual(hashlib.sha256(license_bytes).hexdigest(), expected)
            self.assertIn(b"MIT License", license_bytes)
            self.assertIn(b"copyright notice", license_bytes)
            self.assertEqual(len(pack["cases"]), 40)
            self.assertEqual(len({case["source_id"] for case in pack["cases"]}), 40)

    def test_missing_or_changed_cache_fails_offline_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(importer.urllib.request, "urlopen", side_effect=AssertionError("Network forbidden")):
            cache = Path(directory)
            with self.assertRaises(FileNotFoundError):
                importer.build_files(cache)
            first = cache / importer.SOURCES[0]["cache_name"]
            first.write_bytes(b"corrupted source")
            with self.assertRaisesRegex(ValueError, "Pinned source hash"):
                importer.build_files(cache)
            with self.assertRaisesRegex(ValueError, "Pinned source hash"):
                importer.download(cache)
            self.assertEqual(first.read_bytes(), b"corrupted source")

    def test_oversized_download_is_rejected_before_creating_cached_file(self):
        with tempfile.TemporaryDirectory() as directory:
            response = unittest.mock.MagicMock()
            response.__enter__.return_value.read.return_value = b"x" * (importer.MAX_SOURCE_BYTES + 1)
            with patch.object(importer.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(ValueError, "Pinned source hash"):
                    importer.download(Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_immutable_output_conflict_does_not_write_other_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "existing.json").write_bytes(b"original")
            with self.assertRaisesRegex(ValueError, "Frozen output differs"):
                importer.write_files(root, {"new.json": b"new", "existing.json": b"changed"})
            self.assertEqual((root / "existing.json").read_bytes(), b"original")
            self.assertFalse((root / "new.json").exists())
            importer.write_files(root, {"existing.json": b"original"}, check=True)


if __name__ == "__main__":
    unittest.main()
