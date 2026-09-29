"""Generic local import tests using invented fixtures; no private source metadata."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import import_watchtower_private_tests as importer

_fixture_spec = importlib.util.spec_from_file_location("synthetic_private_pack_fixture", importer.REPO_ROOT / "distribution/watchtower/benchmarks/test_private_packs.py")
_fixture = importlib.util.module_from_spec(_fixture_spec)
with patch.dict(sys.modules, {"private_packs": importer.private_packs}):
    _fixture_spec.loader.exec_module(_fixture)


class PrivateImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.source = self.root / "sources"
        self.source.mkdir()
        self.manifest = self.source / "import.json"
        self.packs = [_fixture.make_pack(identifier) for identifier in _fixture.FIXTURE_SPECS]
        entries = []
        for pack in self.packs:
            raw = (json.dumps(pack, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            name = pack["id"] + ".json"
            (self.source / name).write_bytes(raw)
            entries.append({"path": name, "sha256": hashlib.sha256(raw).hexdigest()})
        self.declaration = {"schema": importer.IMPORT_SCHEMA, "packs": entries}
        self.write_manifest()

    def write_manifest(self):
        self.manifest.write_text(json.dumps(self.declaration), encoding="utf-8")

    def test_explicit_manifest_preserves_all_cases_and_reference_separation(self):
        self.assertEqual(importer.read_packs(self.manifest), self.packs)
        for pack in importer.read_packs(self.manifest):
            for case in pack["cases"]:
                self.assertEqual(case["prompt"], importer.private_packs.render_request(case["request"]))
                self.assertNotIn(case["evidence"], case["prompt"])
            self.assertTrue(all("expected" not in case for case in pack["unscored_cases"]))
        self.assertFalse(self.home.exists())

    def test_hash_mismatch_and_duplicate_json_keys_are_rejected(self):
        path = self.source / self.declaration["packs"][0]["path"]
        raw = path.read_bytes()
        path.write_bytes(raw + b" ")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            importer.read_packs(self.manifest)
        duplicate = raw.rstrip()[:-1] + b',"id":"duplicate"}'
        path.write_bytes(duplicate)
        self.declaration["packs"][0]["sha256"] = hashlib.sha256(duplicate).hexdigest()
        self.write_manifest()
        with self.assertRaises(ValueError):
            importer.read_packs(self.manifest)

    def test_paths_must_be_explicit_contained_relative_files(self):
        for name in ("../outside.json", "/absolute.json", "C:/outside.json", "C:outside.json", "folder\\file.json", "./file.json", "folder//file.json"):
            with self.subTest(name=name):
                self.declaration["packs"][0]["path"] = name
                self.write_manifest()
                with self.assertRaises(ValueError):
                    importer.read_packs(self.manifest)
        with self.assertRaises(ValueError):
            importer.read_packs(Path("relative.json"))

    def test_invalid_manifest_duplicate_pack_and_unknown_protocol_fail(self):
        self.declaration["packs"].append(deepcopy(self.declaration["packs"][0]))
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            importer.read_packs(self.manifest)
        self.declaration["schema"] = "unknown-schema"
        self.write_manifest()
        with self.assertRaises(ValueError):
            importer.read_packs(self.manifest)
        pack = deepcopy(self.packs[0])
        pack["protocol"] = "unknown-protocol"
        with self.assertRaises(ValueError):
            importer.pack_bytes_and_manifest(pack)

    def test_reserved_bundled_ids_rejected_by_check_and_install_before_writes(self):
        for identifier in importer.bundled_packs.MANIFEST:
            with self.subTest(identifier=identifier):
                pack = deepcopy(self.packs[0])
                pack["id"] = identifier
                raw = json.dumps(pack).encode("utf-8")
                (self.source / "collision.json").write_bytes(raw)
                self.declaration["packs"] = [{"path": "collision.json", "sha256": hashlib.sha256(raw).hexdigest()}]
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, "reserved"):
                    importer.main(["--source", str(self.manifest), "--check"])
                # Direct callers and mixed requests also validate all entries before mkdir.
                with self.assertRaisesRegex(ValueError, "reserved"):
                    importer.install_packs(self.home, [self.packs[1], pack])
                self.assertFalse(self.home.exists())

    def test_size_and_symlink_limits_fail_without_destination_creation(self):
        with patch.object(importer.private_packs, "MAX_PACK_BYTES", 10):
            with self.assertRaises(ValueError):
                importer.read_packs(self.manifest)
        target = self.source / self.declaration["packs"][0]["path"]
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == target):
            with self.assertRaises(ValueError):
                importer.read_packs(self.manifest)
        self.assertFalse(self.home.exists())

    def test_check_is_read_only_and_does_not_need_a_home(self):
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.source.iterdir()}
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(importer.main(["--source", str(self.manifest), "--check"]), 0)
        self.assertFalse(json.loads(output.getvalue())["installed"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.source.iterdir()})
        self.assertFalse(self.home.exists())

    def test_install_roundtrips_order_and_identical_reimport_never_overwrites(self):
        packs = self.packs
        importer.install_packs(self.home, packs)
        root = self.home / "state/benchmarks/packs"
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*.json")}
        importer.install_packs(self.home, packs)
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.rglob("*.json")})
        for pack in packs:
            loaded = importer.private_packs.load_pack(root, pack["id"])
            self.assertEqual(loaded["cases"][0]["prompt"], pack["cases"][0]["prompt"])
            self.assertEqual(list(loaded["cases"][0]["request"]["questions"]), list(pack["cases"][0]["request"]["questions"]))

    def test_all_conflicts_checked_before_new_pack_is_written(self):
        packs = self.packs
        importer.install_packs(self.home, [packs[1]])
        changed = deepcopy(packs[1])
        changed["description"] = "Different reviewed content would require a new version."
        with self.assertRaisesRegex(ValueError, "differs"):
            importer.install_packs(self.home, [packs[0], changed])
        root = self.home / "state/benchmarks/packs"
        self.assertFalse((root / packs[0]["id"]).exists())
        self.assertEqual(importer.private_packs.load_pack(root, packs[1]["id"])["hash"], importer.private_packs.canonical_hash(packs[1]))

    def test_failed_staged_write_never_publishes_partial_version(self):
        pack = self.packs[1]
        original = Path.write_bytes
        def fail_manifest(path, data):
            if path.name == "manifest.json":
                raise OSError("Synthetic disk failure")
            return original(path, data)
        with patch.object(Path, "write_bytes", autospec=True, side_effect=fail_manifest), self.assertRaises(OSError):
            importer.install_packs(self.home, [pack])
        root = self.home / "state/benchmarks/packs"
        self.assertFalse((root / pack["id"] / "v1").exists())
        self.assertEqual(list(root.rglob(".pending-*")), [])
        self.assertFalse((root / ".import.lock").exists())
        importer.install_packs(self.home, [pack])
        self.assertEqual(importer.private_packs.load_pack(root, pack["id"])["id"], pack["id"])

    def test_repository_destination_and_linked_home_are_rejected(self):
        pack = self.packs[1]
        with self.assertRaisesRegex(ValueError, "repository"):
            importer.install_packs(importer.REPO_ROOT, [pack])
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == self.home):
            with self.assertRaises(ValueError):
                importer.install_packs(self.home, [pack])
        self.assertFalse(self.home.exists())


if __name__ == "__main__":
    unittest.main()
