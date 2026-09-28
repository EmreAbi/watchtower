"""Offline, deterministic BBEH import tests; no models, downloads or source execution."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_watchtower_bbeh_pack as builder


def fixtures():
    blobs, sources, examples = {}, [], {}
    for topic, _sha, count in builder.SOURCES:
        rows = [{"input": f"Original {topic} problem {i}.\nReturn its answer.", "target": str(i)} for i in range(count)]
        raw = json.dumps({"canary": builder.CANARY, "examples": rows}).encode("utf-8")
        blobs[builder.source_path(topic)] = raw
        sources.append((topic, hashlib.sha256(raw).hexdigest(), count))
        examples[topic] = rows
    return blobs, tuple(sources), examples


class BBEHImportTests(unittest.TestCase):
    def setUp(self):
        self.blobs, self.sources, self.examples = fixtures()
        self.source_patch = patch.object(builder, "SOURCES", self.sources)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)

    def test_balanced_deterministic_selection_with_original_strings(self):
        pack = builder.build_pack(self.blobs)
        same = builder.build_pack(dict(reversed(list(self.blobs.items()))))
        self.assertEqual(pack, same)
        self.assertEqual(len(pack["cases"]), 40)
        self.assertEqual(pack["capability"], "normalized-answer")
        self.assertEqual(Counter(case["topic"] for case in pack["cases"]), {topic: 8 for topic in self.examples})
        self.assertEqual(pack["quick_case_ids"], [case["id"] for case in pack["cases"][:5]])
        self.assertEqual([case["topic"] for case in pack["cases"][:5]], list(self.examples))
        self.assertEqual(pack["source"]["source_count"], 1000)
        for case in pack["cases"]:
            index = int(case["source_id"].rsplit("/", 1)[1])
            original = self.examples[case["topic"]][index]
            self.assertEqual(case["expected"], {"answer": original["target"]})
            self.assertEqual(case["prompt"], builder.PROMPT_PREFIX + original["input"])
            self.assertEqual(case["capability"], "normalized-answer")

    def test_ranking_cannot_depend_on_targets_or_model_performance(self):
        topic = self.sources[0][0]
        original = self.examples[topic]
        changed = deepcopy(original)
        for row in changed:
            row["target"] = "a different answer"
            row["model_accuracy"] = 1
        self.assertEqual(builder.selected_indices(topic, original), builder.selected_indices(topic, changed))
        independently_ranked = sorted(range(len(original)), key=lambda i: (
            hashlib.sha256(("watchtower-bbeh-subset-v1\n" + builder.REVISION + "\n" + builder.source_path(topic)
                            + "\n" + str(i) + "\n" + original[i]["input"]).encode("utf-8")).hexdigest(), i))[:8]
        self.assertEqual(builder.selected_indices(topic, original), independently_ranked)

    def test_altered_source_bytes_rejected_before_import(self):
        self.blobs[next(iter(self.blobs))] += b"\n"
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            builder.build_pack(self.blobs)

    def _replace_first_source(self, value):
        topic, _sha, count = self.sources[0]
        raw = json.dumps(value).encode("utf-8")
        self.blobs[builder.source_path(topic)] = raw
        builder.SOURCES = ((topic, hashlib.sha256(raw).hexdigest(), count),) + self.sources[1:]

    def test_count_and_canary_must_match(self):
        for invalid in (
            {"canary": "missing notice", "examples": self.examples[self.sources[0][0]]},
            {"canary": builder.CANARY, "examples": self.examples[self.sources[0][0]][:-1]},
        ):
            with self.subTest(invalid_canary=invalid["canary"] != builder.CANARY):
                self._replace_first_source(invalid)
                with self.assertRaises(ValueError):
                    builder.build_pack(self.blobs)

    def test_oversize_unselected_question_fails_instead_of_cherry_picking(self):
        topic = self.sources[0][0]
        rows = deepcopy(self.examples[topic])
        selected = builder.selected_indices(topic, rows)
        index = next(i for i in range(len(rows)) if i not in selected)
        rows[index]["input"] = "x" * builder.MAX_PROMPT_CHARS
        self._replace_first_source({"canary": builder.CANARY, "examples": rows})
        with self.assertRaisesRegex(ValueError, "prompt exceeds"):
            builder.build_pack(self.blobs)

    def test_offline_source_loading_and_check_never_fetch_or_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for topic, _sha, _count in self.sources:
                (root / f"{topic}.json").write_bytes(self.blobs[builder.source_path(topic)])
            output = root / "pack.json"
            with patch.object(builder, "urlopen", side_effect=AssertionError("Network not permitted")):
                self.assertEqual(builder.main(["--source-dir", directory, "--output", str(output)]), 0)
                before = output.read_bytes()
                self.assertEqual(builder.main(["--source-dir", directory, "--output", str(output), "--check"]), 0)
                self.assertEqual(output.read_bytes(), before)
                altered = json.loads(before)
                altered["cases"][0]["expected"]["answer"] = "tampered"
                output.write_text(json.dumps(altered), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "differs"):
                    builder.main(["--source-dir", directory, "--output", str(output), "--check"])

    def test_source_read_has_hard_size_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            topic = self.sources[0][0]
            (Path(directory) / f"{topic}.json").write_bytes(b"x" * (builder.MAX_SOURCE_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "size exceeds"):
                builder.load_sources(directory)


class BundledBBEHTests(unittest.TestCase):
    def test_pinned_bundle_shape_provenance_and_license(self):
        pack = json.loads(builder.DEFAULT_OUTPUT.read_text(encoding="utf-8"))
        self.assertEqual(pack["source"]["revision"], builder.REVISION)
        self.assertEqual(pack["source"]["license"], "CC-BY-4.0")
        self.assertEqual(pack["source"]["source_count"], 1000)
        self.assertEqual(pack["capability"], "normalized-answer")
        self.assertEqual(len(pack["cases"]), 40)
        self.assertEqual(Counter(case["topic"] for case in pack["cases"]), {topic: 8 for topic, _, _ in builder.SOURCES})
        self.assertEqual(len({case["source_id"] for case in pack["cases"]}), 40)
        self.assertEqual(pack["quick_case_ids"], [case["id"] for case in pack["cases"][:5]])
        self.assertLessEqual(max(len(case["prompt"]) for case in pack["cases"]), 16000)
        self.assertTrue(all(set(case) == {"id", "capability", "prompt", "expected", "source_id", "topic"} for case in pack["cases"]))
        self.assertEqual([(item["path"], item["sha256"], item["count"]) for item in pack["source"]["upstream_files"]],
                         [(builder.source_path(topic), digest, count) for topic, digest, count in builder.SOURCES])
        notice = (builder.DEFAULT_OUTPUT.parents[1] / pack["source"]["license_file"]).read_text(encoding="utf-8")
        for required in (builder.CANARY, builder.REVISION, "Copyright 2025 Google LLC", "2502.19187", "2409.10502", "CC-BY-4.0"):
            self.assertIn(required, notice)
        self.assertIn("not an official BBEH", pack["limitations"])


if __name__ == "__main__":
    unittest.main()
