"""Results packaging tests use temporary source trees and synthetic binaries."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import test_watchtower_package as package_tests
from scripts.watchtower_results import DEVELOPMENT_FILES, RUNTIME_FILES, validated_results_files


def write_results_fixture(root: Path, *, development: bool = True) -> None:
    for name in RUNTIME_FILES | (DEVELOPMENT_FILES if development else set()):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Results fixture: " + name, encoding="utf-8")


class ResultsAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "results"
        write_results_fixture(self.root)

    def test_only_complete_runtime_allowlist_is_returned_in_stable_order(self):
        actual = [path.relative_to(self.root).as_posix() for path in validated_results_files(self.root)]
        self.assertEqual(actual, sorted(RUNTIME_FILES))
        self.assertTrue(DEVELOPMENT_FILES.isdisjoint(actual))

    def test_runtime_only_source_is_supported(self):
        root = Path(self.temporary.name) / "runtime-only"
        write_results_fixture(root, development=False)
        self.assertEqual(len(validated_results_files(root)), len(RUNTIME_FILES))

    def test_missing_runtime_file_is_rejected(self):
        (self.root / "session_reader.py").unlink()
        with self.assertRaisesRegex(ValueError, "missing files in the Results package"):
            validated_results_files(self.root)

    def test_unexpected_state_preview_or_cache_is_rejected(self):
        for relative in ("auth.json", "sessions/run.jsonl", "previews/report.html", "view.json", "__pycache__/view.pyc"):
            with self.subTest(relative=relative):
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private fixture", encoding="utf-8")
                try:
                    with self.assertRaisesRegex(ValueError, "Results package"):
                        validated_results_files(self.root)
                finally:
                    path.unlink()

    def test_symbolic_link_is_rejected_even_when_target_is_inside_source(self):
        target = self.root / "view.py"
        with patch.object(Path, "is_symlink", autospec=True, side_effect=lambda path: path == target):
            with self.assertRaisesRegex(ValueError, "symbolic links"):
                validated_results_files(self.root)

    def test_linked_bin_directory_is_rejected(self):
        with patch("scripts.watchtower_results._is_link", side_effect=lambda path: path == self.root / "bin"):
            with self.assertRaisesRegex(ValueError, "symbolic links or junctions"):
                validated_results_files(self.root)


class ResultsArchiveTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing validated ConPTY fixture without running its tests twice.
        self.fixture = package_tests.WatchtowerPackageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root / "distribution/watchtower/results"
        write_results_fixture(self.root)

    def test_archive_and_manifest_include_runtime_results_only(self):
        manifest = self.fixture.build()
        expected = {"results/" + name for name in RUNTIME_FILES}
        with zipfile.ZipFile(self.fixture.output) as archive:
            actual = {name for name in archive.namelist() if name.startswith("results/")}
            self.assertEqual(actual, expected)
            self.assertTrue(expected <= set(manifest["files"]))
            self.assertFalse(any("results/" + name in archive.namelist() for name in DEVELOPMENT_FILES))
            self.assertEqual(archive.read("results/bin/center.cmd"), b"Results fixture: bin/center.cmd")

    def test_private_results_state_aborts_archive_creation(self):
        (self.root / "session.jsonl").write_text("private runtime fixture", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Results package"):
            self.fixture.build()
        self.assertFalse(self.fixture.output.exists())


if __name__ == "__main__":
    unittest.main()
