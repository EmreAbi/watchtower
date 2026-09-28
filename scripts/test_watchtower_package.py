from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from scripts import package_windows_conpty as conpty
from scripts import watchtower_package as package
from scripts import test_package_windows_conpty as conpty_tests


class WatchtowerPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.stage = Path(self.temporary.name) / "verified"
        self.stage.mkdir()
        product = {
            "name": "Watchtower", "version": "0.1.0-preview.1",
            "repository": "https://github.com/EmreAbi/watchtower",
            "upstream": {"version": "0.9.1", "commit": "a" * 40},
        }
        files = {
            "distribution/watchtower/product.json": json.dumps(product),
            "LICENSE": "Apache License fixture",
            "distribution/watchtower/NOTICE": "Herdr attribution fixture",
            "distribution/watchtower/README.md": "Preview usage fixture",
            "distribution/watchtower/VALIDATION.md": "Preview validation fixture",
            "distribution/watchtower/open-watchtower.cmd": "@echo off\nwatchtower.exe\n",
            "distribution/watchtower/setup-radio.cmd": "@echo off\nwatchtower.exe plugin link radio\n",
            "vendor/libghostty-vt/LICENSE": "Ghostty MIT fixture",
            "vendor/portable-pty/LICENSE.md": "Portable PTY MIT fixture",
        }
        for relative, content in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        dll = b"verified-conpty-fixture"
        self.metadata = {
            "schema_version": 1,
            "package": {"id": "Microsoft.Windows.Console.ConPTY", "version": "test", "sha256": "b" * 64},
            "bundles": {"x86_64": {"files": [{
                "destination": "conpty/conpty.dll",
                "sha256": hashlib.sha256(dll).hexdigest(),
            }]}},
            "notices": [],
        }
        metadata_path = self.root / "packaging/windows/conpty.json"
        metadata_path.parent.mkdir(parents=True)
        metadata_path.write_text(json.dumps(self.metadata), encoding="utf-8")
        (self.stage / "conpty").mkdir()
        (self.stage / "conpty/conpty.dll").write_bytes(dll)
        (self.stage / conpty.MARKER_PATH).write_bytes(conpty.marker_data(self.metadata, "x86_64"))
        (self.stage / "herdr.exe").write_bytes(
            conpty_tests.WindowsConptyPackageTests._pe_with_imports(0x8664, ["KERNEL32.dll"])
        )
        self.provenance = {"commit": "c" * 40, "dirty": False, "commit_timestamp": 1720000000}
        self.output = Path(self.temporary.name) / "watchtower.zip"

    def build(self, output: Path | None = None) -> dict:
        return package.package_preview(
            self.stage, output or self.output, root=self.root,
            provenance=self.provenance,
        )

    def test_portable_archive_has_provenance_licenses_and_identical_entrypoints(self) -> None:
        manifest = self.build()
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(archive.read("watchtower.exe"), archive.read("herdr.exe"))
            self.assertIn("LICENSE", archive.namelist())
            self.assertIn("THIRD-PARTY-NOTICES/libghostty-vt-LICENSE.txt", archive.namelist())
            self.assertEqual(json.loads(archive.read("BUILD-MANIFEST.json")), manifest)
            self.assertEqual(manifest["source"]["commit"], "c" * 40)
            self.assertEqual(manifest["upstream"]["version"], "0.9.1")
            self.assertIsNone(manifest["radio"])
            self.assertEqual(set(manifest["files"]), set(archive.namelist()) - {"BUILD-MANIFEST.json"})
            for relative, digest in manifest["files"].items():
                self.assertEqual(hashlib.sha256(archive.read(relative)).hexdigest(), digest)
        self.assertEqual(
            self.output.with_suffix(".zip.sha256").read_text(),
            f"{package.sha256(self.output)}  watchtower.zip\n",
        )

    def test_identical_input_produces_identical_zip_bytes(self) -> None:
        self.build()
        another = self.output.with_name("another.zip")
        self.build(another)
        self.assertEqual(self.output.read_bytes(), another.read_bytes())

    def test_runtime_tampering_is_rejected_before_archive_creation(self) -> None:
        (self.stage / "conpty/conpty.dll").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_extra_state_cannot_leak_from_verified_stage(self) -> None:
        (self.stage / "auth.json").write_text('{"token":"do-not-copy"}')
        with self.assertRaisesRegex(ValueError, "bundle layout mismatch"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_output_is_not_overwritten(self) -> None:
        self.output.write_bytes(b"keep existing")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.build()
        self.assertEqual(self.output.read_bytes(), b"keep existing")

    def test_dirty_source_is_explicit_in_manifest(self) -> None:
        self.provenance["dirty"] = True
        self.assertTrue(self.build()["source"]["dirty"])

    def test_release_mode_rejects_dirty_or_unknown_source_before_creating_archive(self) -> None:
        for dirty in (True, None, "false", 0):
            with self.subTest(dirty=dirty):
                self.provenance["dirty"] = dirty
                with self.assertRaisesRegex(ValueError, "clean source checkout"):
                    package.package_preview(self.stage, self.output, root=self.root,
                                            provenance=self.provenance, require_clean_source=True)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.output.with_suffix(".zip.sha256").exists())

    def test_release_mode_accepts_clean_source(self) -> None:
        result = package.package_preview(self.stage, self.output, root=self.root,
                                         provenance=self.provenance, require_clean_source=True)
        self.assertIs(result["source"]["dirty"], False)

    def test_full_commit_required_for_provenance(self) -> None:
        self.provenance["commit"] = "unknown"
        with self.assertRaisesRegex(ValueError, "full Git SHA"):
            self.build()

    def test_validated_private_radio_is_included_with_its_revision(self) -> None:
        shutil.copytree(
            package.ROOT / "distribution/watchtower/radio",
            self.root / "distribution/watchtower/radio",
        )
        manifest = self.build()
        self.assertEqual(manifest["radio"]["version"], "0.7.1")
        self.assertEqual(manifest["radio"]["commit"], "bf3d460cd66da9d65d7bb9650bd9dbe8bfb1d906")
        with zipfile.ZipFile(self.output) as archive:
            self.assertIn("radio/bin/radio.cmd", archive.namelist())
            self.assertIn("radio/vendor/AgentRadio/LICENSE", archive.namelist())
            self.assertEqual(
                hashlib.sha256(archive.read("radio/provenance.json")).hexdigest(),
                manifest["files"]["radio/provenance.json"],
            )

    def test_unexpected_radio_state_is_rejected(self) -> None:
        radio_root = self.root / "distribution/watchtower/radio"
        shutil.copytree(package.ROOT / "distribution/watchtower/radio", radio_root)
        (radio_root / "radio.db").write_bytes(b"private runtime state")
        with self.assertRaises((ValueError, RuntimeError)):
            self.build()
        self.assertFalse(self.output.exists())

    def test_accounts_source_is_packaged_without_tests_or_state(self) -> None:
        shutil.copytree(package.ROOT / "distribution/watchtower/accounts",
                        self.root / "distribution/watchtower/accounts")
        shutil.copyfile(package.ROOT / "distribution/watchtower/setup-accounts.cmd",
                        self.root / "distribution/watchtower/setup-accounts.cmd")
        manifest = self.build()
        self.assertIn("accounts/view.py", manifest["files"])
        self.assertIn("accounts/tool_updates.py", manifest["files"])
        self.assertIn("setup-accounts.cmd", manifest["files"])
        self.assertNotIn("accounts/test_backend.py", manifest["files"])
        self.assertNotIn("accounts/test_view.py", manifest["files"])
        self.assertNotIn("accounts/test_tool_updates.py", manifest["files"])

    def test_unexpected_accounts_state_is_rejected(self) -> None:
        root = self.root / "distribution/watchtower/accounts"
        shutil.copytree(package.ROOT / "distribution/watchtower/accounts", root)
        (root / "auth.json").write_text('{"token":"private"}')
        with self.assertRaisesRegex(ValueError, "Accounts package"):
            self.build()
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
