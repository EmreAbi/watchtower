"""Package a native Apple Silicon executable and allowlisted portable runtimes."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import struct
import tarfile
import tempfile

try:
    from .watchtower_package import ROOT, source_state
    from .watchtower_macos_plugins import stage_plugins
except ImportError:
    from watchtower_package import ROOT, source_state
    from watchtower_macos_plugins import stage_plugins


def validate_binary(binary: Path) -> None:
    if binary.is_symlink() or not binary.is_file():
        raise ValueError('Expected a regular Apple Silicon executable')
    header = binary.read_bytes()[:32]
    if len(header) != 32 or struct.unpack('<III', header[:12]) != (0xFEEDFACF, 0x0100000C, 0):
        # CPU subtype may carry capability bits; the architecture and file type
        # are the contract, not a particular compiler's subtype encoding.
        if len(header) != 32 or struct.unpack('<II', header[:8]) != (0xFEEDFACF, 0x0100000C):
            raise ValueError('Expected a 64-bit arm64 Mach-O executable')
    if struct.unpack('<I', header[12:16])[0] != 2:
        raise ValueError('Mach-O file is not an executable')


def package_preview(binary: Path, output: Path, *, root: Path = ROOT,
                    provenance: dict | None = None, require_clean_source: bool = False) -> dict:
    validate_binary(binary)
    product = json.loads((root / 'distribution/watchtower/product.json').read_text())
    source = source_state(root) if provenance is None else provenance
    if not re.fullmatch('[a-f0-9]{40}', source.get('commit', '')):
        raise ValueError('Full Git SHA required')
    if require_clean_source and source.get('dirty') is not False:
        raise ValueError('Release packaging requires a clean source checkout')
    sidecar = output.with_name(output.name + '.sha256')
    if output.exists() or sidecar.exists():
        raise ValueError('Output already exists')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.macos-package-', dir=output.parent) as temp:
        stage = Path(temp) / 'stage'
        stage.mkdir()
        for name in ('watchtower', 'herdr'):
            shutil.copyfile(binary, stage / name)
        notices = {
            'LICENSE': 'LICENSE',
            'distribution/watchtower/NOTICE': 'NOTICE',
            'distribution/watchtower/MACOS.md': 'README.md',
            'distribution/watchtower/VALIDATION.md': 'VALIDATION.md',
            'vendor/libghostty-vt/LICENSE': 'THIRD-PARTY-NOTICES/libghostty-vt-LICENSE.txt',
            'vendor/portable-pty/LICENSE.md': 'THIRD-PARTY-NOTICES/portable-pty-LICENSE.txt',
        }
        for source_name, destination in notices.items():
            path = root / source_name
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Package source cannot be a symbolic link')
            (stage / destination).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, stage / destination)
        radio = stage_plugins(root, stage)
        files = {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(stage.rglob('*')) if p.is_file()}
        manifest = {
            'schema_version': 1, 'product': product['name'], 'version': product['version'],
            'target': 'aarch64-apple-darwin', 'minimum_macos': '15.0',
            'signing': 'ad-hoc; not Developer ID signed or notarized',
            'source': {'repository': product['repository'], **source},
            'upstream': product['upstream'],
            'radio': {key: radio[key] for key in ('upstream', 'version', 'commit', 'license')},
            'files': files,
        }
        (stage / 'BUILD-MANIFEST.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        packed = Path(temp) / 'preview.tar.gz'
        with packed.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(stage.rglob('*')):
                    if not path.is_file():
                        continue
                    relative = path.relative_to(stage).as_posix()
                    info = tarfile.TarInfo(relative)
                    data = path.read_bytes()
                    info.size, info.mtime = len(data), source['commit_timestamp']
                    info.mode = 0o755 if relative in {'watchtower', 'herdr', 'open-watchtower', 'setup-radio', 'setup-accounts'} or '/bin/' in relative else 0o644
                    archive.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(packed.read_bytes()).hexdigest()
        with output.open('xb') as target, packed.open('rb') as incoming:
            shutil.copyfileobj(incoming, target)
        with sidecar.open('x', encoding='ascii') as stream:
            stream.write(f'{digest}  {output.name}\n')
    return manifest
