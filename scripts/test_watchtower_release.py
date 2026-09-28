"""Release boundary checks with fabricated paths/packages, no live runtime."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from scripts.watchtower_release import build_environment
from scripts.watchtower_smoke_windows import SmokeFailure, verify_package_version


ROOT = Path(__file__).resolve().parent.parent


class ReleaseFlagsTest(unittest.TestCase):
    def test_personal_roots_are_remapped_in_both_windows_spellings(self):
        settings = build_environment(r"C:\Users\Example User\work\Watchtower", {
            "USERPROFILE": r"C:\Users\Example User",
            "CARGO_HOME": r"D:\Build Tools\cargo",
            "RUSTUP_HOME": r"D:\Build Tools\rustup",
        })
        flags = settings["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
        for source, destination in (
            (r"C:\Users\Example User", "/user"), (r"D:\Build Tools\cargo", "/cargo"),
            (r"D:\Build Tools\rustup", "/rustup"), (r"C:\Users\Example User\work\Watchtower", "/watchtower"),
        ):
            for spelling in (source, source.replace("\\", "/")):
                self.assertIn(f"--remap-path-prefix={spelling}={destination}", flags)
        self.assertTrue(flags[-1].endswith("=/watchtower"))
        self.assertIn("target-feature=+crt-static", flags)
        self.assertIn("link-arg=/PDBALTPATH:%_PDB%", flags)
        self.assertEqual(settings["CARGO_PROFILE_RELEASE_DEBUG"], "0")
        self.assertEqual(settings["CARGO_PROFILE_RELEASE_STRIP"], "debuginfo")

    def test_default_rust_homes_and_case_insensitive_windows_environment(self):
        flags = build_environment("C:/repo", {"userprofile": "C:/Example"})["CARGO_ENCODED_RUSTFLAGS"]
        self.assertIn("--remap-path-prefix=C:/Example/.cargo=/cargo", flags)
        self.assertIn("--remap-path-prefix=C:/Example/.rustup=/rustup", flags)

    def test_encoded_caller_flags_win_and_are_not_split_at_spaces(self):
        env = {"CARGO_ENCODED_RUSTFLAGS": "--cfg\x1ffeature=\"synthetic value\"", "RUSTFLAGS": "--unused"}
        original = env.copy()
        flags = build_environment("C:/repo", env)["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
        self.assertEqual(flags[:2], ["--cfg", 'feature="synthetic value"'])
        self.assertNotIn("--unused", flags)
        self.assertEqual(env, original)

    def test_whitespace_rustflags_are_preserved_before_required_release_flags(self):
        flags = build_environment("C:/repo", {"RUSTFLAGS": "-D warnings\n-C target-feature=-crt-static"})["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
        self.assertEqual(flags[:4], ["-D", "warnings", "-C", "target-feature=-crt-static"])
        self.assertGreater(flags.index("target-feature=+crt-static"), flags.index("target-feature=-crt-static"))

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows build script boundary")
    def test_failed_cargo_build_restores_callers_process_environment(self):
        # Run only the build-script boundary: a mock Cargo function records flags
        # then fails before compilation, package download, or runtime execution.
        with tempfile.TemporaryDirectory(prefix="watchtower-build-test-") as directory:
            temp = Path(directory)
            task = temp / "repo with spaces"
            (task / "scripts").mkdir(parents=True)
            (task / "distribution/watchtower").mkdir(parents=True)
            for name in ("watchtower_build_windows.ps1", "watchtower_release.py"):
                shutil.copyfile(ROOT / "scripts" / name, task / "scripts" / name)
            (task / "distribution/watchtower/product.json").write_text('{"version":"0.0.0-preview.9"}')
            harness = temp / "harness.ps1"
            harness.write_text(r'''
param([string]$TaskRoot)
$ErrorActionPreference = 'Stop'
$env:CARGO_ENCODED_RUSTFLAGS = '--cfg' + [char]31 + 'fixture'
$env:CARGO_PROFILE_RELEASE_DEBUG = '2'
Remove-Item Env:CARGO_PROFILE_RELEASE_STRIP -ErrorAction SilentlyContinue
function cargo {
    $env:CARGO_ENCODED_RUSTFLAGS | Set-Content -LiteralPath (Join-Path $TaskRoot 'flags.txt') -Encoding utf8
    $global:LASTEXITCODE = 17
}
$failed = $false
try { & (Join-Path $TaskRoot 'scripts/watchtower_build_windows.ps1') } catch { $failed = $_.Exception.Message -like '*exit code 17*' }
@{ failed=$failed; flags=$env:CARGO_ENCODED_RUSTFLAGS; debug=$env:CARGO_PROFILE_RELEASE_DEBUG; strip=$env:CARGO_PROFILE_RELEASE_STRIP } | ConvertTo-Json -Compress
''', encoding="utf-8")
            result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness), str(task)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["failed"], result.stderr)
            self.assertEqual(report["flags"], "--cfg\x1ffixture")
            self.assertEqual(report["debug"], "2")
            self.assertIsNone(report["strip"])
            used = (task / "flags.txt").read_text(encoding="utf-8-sig")
            self.assertIn("=/watchtower", used)
            self.assertIn("link-arg=/PDBALTPATH:%_PDB%", used)


class PackageVersionTest(unittest.TestCase):
    def test_smoke_uses_manifest_version_and_rejects_prefix_collisions(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "watchtower.exe"
            (binary.parent / "BUILD-MANIFEST.json").write_text(json.dumps({"product": "Watchtower", "version": "9.8.7-preview.19"}))
            self.assertEqual(verify_package_version(binary, "watchtower 9.8.7-preview.19 (Herdr 0.9.1)"), "9.8.7-preview.19")
            for value in ("watchtower 9.8.7-preview.190", "watchtower 0.1.0-preview.1", "herdr 9.8.7-preview.19"):
                with self.subTest(value=value), self.assertRaises(SmokeFailure):
                    verify_package_version(binary, value)

    def test_missing_invalid_or_different_product_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "watchtower.exe"
            with self.assertRaises(SmokeFailure):
                verify_package_version(binary, "watchtower 1.0.0")
            for manifest in ({}, {"product": "Herdr", "version": "1.0.0"}, {"product": "Watchtower", "version": 1},
                             {"product": "Watchtower", "version": "1.0.0\nextra"}):
                (binary.parent / "BUILD-MANIFEST.json").write_text(json.dumps(manifest))
                with self.subTest(manifest=manifest), self.assertRaises(SmokeFailure):
                    verify_package_version(binary, "watchtower 1.0.0")

    def test_product_and_compiled_identity_agree(self):
        product = json.loads((ROOT / "distribution/watchtower/product.json").read_text())
        source = (ROOT / "src/distro.rs").read_text(encoding="utf-8")
        self.assertEqual(re.search(r'pub const VERSION: &str = "([^"]+)";', source).group(1), product["version"])

    def test_manual_ci_installs_the_same_pinned_dependencies_as_runtime_setup(self):
        setup = ast.parse((ROOT / "distribution/watchtower/accounts/setup.py").read_text(encoding="utf-8"))
        requirements = {node.value for node in ast.walk(setup)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "==" in node.value}
        workflow = (ROOT / ".github/workflows/watchtower-preview.yml").read_text(encoding="utf-8")
        self.assertEqual(requirements, set(re.findall(r"\b(?:textual|Pillow|pypdfium2)==[0-9.]+", workflow)))
        self.assertIn("@('accounts', 'results', 'teams', 'benchmarks')", workflow)
        self.assertIn("-RequireCleanSource", workflow)


if __name__ == "__main__":
    unittest.main()
