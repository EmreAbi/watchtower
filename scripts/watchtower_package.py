#!/usr/bin/env python3
"""Wrap an already verified ConPTY stage as a Watchtower portable archive."""

from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

try:
    from . import package_windows_conpty as conpty
    from .watchtower_radio import validated_radio_files
except ImportError:
    import package_windows_conpty as conpty
    from watchtower_radio import validated_radio_files


ROOT = Path(__file__).resolve().parent.parent
PRODUCT = ROOT / "distribution/watchtower/product.json"


def sha256(path: Path) -> str:
    return conpty.sha256_file(path)


def source_state(root: Path) -> dict:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, encoding="utf-8"
        ).strip()

    commit = git("rev-parse", "HEAD")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("source commit must be a full Git SHA")
    return {
        "commit": commit,
        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
        "commit_timestamp": int(git("show", "-s", "--format=%ct", "HEAD")),
    }


def package_preview(
    verified_stage: Path,
    output: Path,
    *,
    root: Path = ROOT,
    provenance: dict | None = None,
) -> dict:
    """Copy only allowlisted application files; never copy settings or sessions."""
    product = json.loads((root / "distribution/watchtower/product.json").read_text())
    metadata_path = root / "packaging/windows/conpty.json"
    metadata = conpty.load_metadata(metadata_path)
    conpty.validate_stage(metadata_path, "x86_64", verified_stage)
    source = provenance if provenance is not None else source_state(root)
    if not re.fullmatch(r"[a-f0-9]{40}", source["commit"]):
        raise ValueError("source commit must be a full Git SHA")
    if output.exists() or output.with_suffix(output.suffix + ".sha256").exists():
        raise ValueError("output already exists; choose a new artifact path")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="watchtower-package-", dir=output.parent) as temp:
        stage = Path(temp) / "stage"
        stage.mkdir()
        for relative in sorted(conpty.expected_stage_files(metadata, "x86_64")):
            source_file = verified_stage / relative
            if source_file.is_symlink():
                raise ValueError(f"symbolic links are not packaged: {relative}")
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_file, destination)
        shutil.copyfile(stage / "herdr.exe", stage / "watchtower.exe")
        notices = {
            "LICENSE": "LICENSE",
            "distribution/watchtower/NOTICE": "NOTICE",
            "distribution/watchtower/README.md": "README.md",
            "distribution/watchtower/VALIDATION.md": "VALIDATION.md",
            "distribution/watchtower/open-watchtower.cmd": "open-watchtower.cmd",
            "distribution/watchtower/setup-radio.cmd": "setup-radio.cmd",
            "vendor/libghostty-vt/LICENSE": "THIRD-PARTY-NOTICES/libghostty-vt-LICENSE.txt",
            "vendor/portable-pty/LICENSE.md": "THIRD-PARTY-NOTICES/portable-pty-LICENSE.txt",
        }
        for relative, destination in notices.items():
            (stage / destination).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, stage / destination)
        radio_root = root / "distribution/watchtower/radio"
        radio_provenance = None
        if radio_root.exists():
            for path in validated_radio_files(radio_root):
                destination = stage / "radio" / path.relative_to(radio_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
            radio_provenance = json.loads((stage / "radio/provenance.json").read_text(encoding="utf-8"))
        files = {
            path.relative_to(stage).as_posix(): sha256(path)
            for path in sorted(stage.rglob("*"))
            if path.is_file()
        }
        manifest = {
            "schema_version": 1,
            "product": product["name"],
            "version": product["version"],
            "target": "x86_64-pc-windows-msvc",
            "source": {"repository": product["repository"], **source},
            "upstream": product["upstream"],
            "conpty": {
                "version": metadata["package"]["version"],
                "package_sha256": metadata["package"]["sha256"],
            },
            "radio": {
                key: radio_provenance[key]
                for key in ("upstream", "version", "commit", "license")
            } if radio_provenance else None,
            "files": files,
        }
        (stage / "BUILD-MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        stamp = datetime.datetime.fromtimestamp(
            max(source["commit_timestamp"], 315532800), datetime.timezone.utc
        ).timetuple()[:6]
        archive_path = Path(temp) / "preview.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), stamp)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o100644 << 16
                    archive.writestr(info, path.read_bytes())
        archive_hash = sha256(archive_path)
        # Exclusive creation avoids silently replacing an existing preview.
        with output.open("xb") as destination, archive_path.open("rb") as source_file:
            shutil.copyfileobj(source_file, destination)
        with output.with_suffix(output.suffix + ".sha256").open("x", encoding="ascii") as checksum:
            checksum.write(f"{archive_hash}  {output.name}\n")
        return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verified-stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = package_preview(args.verified_stage, args.output)
    print(json.dumps({"output": str(args.output), "version": manifest["version"], "source": manifest["source"]}))


if __name__ == "__main__":
    main()
