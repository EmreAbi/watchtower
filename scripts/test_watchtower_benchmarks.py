"""Keep versioned Model Lab fixtures portable, and private run data out."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import test_watchtower_package as package_tests
from scripts.watchtower_benchmarks import DEVELOPMENT_FILES, RUNTIME_FILES, validated_benchmark_files


def fixture(root):
    for name in RUNTIME_FILES | DEVELOPMENT_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Model Lab fixture: " + name, encoding="utf-8")


class BenchmarkAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "benchmarks"
        fixture(self.root)

    def test_complete_runtime_only(self):
        self.assertEqual([p.relative_to(self.root).as_posix() for p in validated_benchmark_files(self.root)], sorted(RUNTIME_FILES))

    def test_missing_pack_rejected(self):
        (self.root / "packs/coding-v1.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            validated_benchmark_files(self.root)

    def test_private_data_and_cache_rejected(self):
        for name in ("auth.json", "runs/private/report.json", "__pycache__/service.pyc"):
            with self.subTest(name=name):
                path = self.root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private fixture", encoding="utf-8")
                try:
                    with self.assertRaisesRegex(ValueError, "Unexpected"):
                        validated_benchmark_files(self.root)
                finally:
                    path.unlink()

    def test_linked_pack_directory_rejected(self):
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda p: p == self.root / "packs"):
            with self.assertRaisesRegex(ValueError, "links"):
                validated_benchmark_files(self.root)


class BenchmarkArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = package_tests.WatchtowerPackageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root / "distribution/watchtower/benchmarks"
        fixture(self.root)

    def test_archive_and_manifest_contain_only_runtime(self):
        manifest = self.fixture.build()
        expected = {"benchmarks/" + name for name in RUNTIME_FILES}
        with zipfile.ZipFile(self.fixture.output) as archive:
            self.assertEqual({name for name in archive.namelist() if name.startswith("benchmarks/")}, expected)
            self.assertTrue(expected <= set(manifest["files"]))

    def test_run_data_aborts_archive(self):
        (self.root / "report.json").write_text("private fixture", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Model Lab"):
            self.fixture.build()
        self.assertFalse(self.fixture.output.exists())


if __name__ == "__main__":
    unittest.main()
