import hashlib
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest

from scripts.watchtower_package_macos import package_preview, validate_binary, ROOT
from scripts.watchtower_build_macos import build_environment


class MacPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'binary'
        self.binary.write_bytes(struct.pack('<8I', 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0))
        self.source = {'commit': 'b' * 40, 'dirty': False, 'commit_timestamp': 1720000000}

    def test_package_layout_hashes_permissions_and_no_windows_entrypoints(self):
        archive = self.root / 'preview.tar.gz'
        manifest = package_preview(self.binary, archive, provenance=self.source, require_clean_source=True)
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            names = {item.name for item in members}
            self.assertEqual(names, set(manifest['files']) | {'BUILD-MANIFEST.json'})
            self.assertTrue(all(item.isfile() and not item.name.startswith('/') and '..' not in Path(item.name).parts for item in members))
            self.assertFalse(any(name.endswith(('.cmd', '.exe')) for name in names))
            for name, digest in manifest['files'].items():
                self.assertEqual(hashlib.sha256(tar.extractfile(name).read()).hexdigest(), digest)
            for name in ('watchtower', 'open-watchtower', 'setup-radio', 'setup-accounts', 'accounts/bin/center'):
                self.assertEqual(tar.getmember(name).mode, 0o755)
            self.assertEqual(tar.extractfile('watchtower').read(), tar.extractfile('herdr').read())
        self.assertEqual(manifest['target'], 'aarch64-apple-darwin')
        self.assertEqual(archive.with_name(archive.name + '.sha256').read_text().split()[0], hashlib.sha256(archive.read_bytes()).hexdigest())
        second = self.root / 'second.tar.gz'
        package_preview(self.binary, second, provenance=self.source)
        self.assertEqual(archive.read_bytes(), second.read_bytes())

    def test_wrong_binary_or_dirty_source_refused_without_artifact(self):
        archive = self.root / 'bad.tar.gz'
        for data in (b'MZ' + bytes(30), struct.pack('<8I', 0xFEEDFACF, 0x01000007, 0, 2, 0, 0, 0, 0),
                     struct.pack('<8I', 0xFEEDFACF, 0x0100000C, 0, 6, 0, 0, 0, 0)):
            self.binary.write_bytes(data)
            with self.assertRaises(ValueError):
                package_preview(self.binary, archive, provenance=self.source)
            self.assertFalse(archive.exists())
        self.binary.write_bytes(struct.pack('<8I', 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0))
        with self.assertRaisesRegex(ValueError, 'clean source'):
            package_preview(self.binary, archive, provenance={**self.source, 'dirty': True}, require_clean_source=True)
        self.assertFalse(archive.exists())

    def test_release_flags_remap_posix_roots_without_windows_linker_flags(self):
        original = {'HOME': '/Users/example', 'RUSTFLAGS': '-D warnings'}
        result = build_environment('/Users/example/My Projects/watchtower', original)
        self.assertEqual(original, {'HOME': '/Users/example', 'RUSTFLAGS': '-D warnings'})
        self.assertIn('--remap-path-prefix=/Users/example/My Projects/watchtower=/watchtower', result['CARGO_ENCODED_RUSTFLAGS'].split('\x1f'))
        self.assertNotIn('crt-static', result['CARGO_ENCODED_RUSTFLAGS'])
        self.assertNotIn('PDB', result['CARGO_ENCODED_RUSTFLAGS'])
        self.assertEqual(result['MACOSX_DEPLOYMENT_TARGET'], '15.0')


if __name__ == '__main__':
    unittest.main()
