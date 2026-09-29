"""Native Apple Silicon build; no cross-compile or user's runtime modifications."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import posixpath
import subprocess
import sys

try:
    from .watchtower_package_macos import package_preview, ROOT
    from .watchtower_package import source_state
except ImportError:
    from watchtower_package_macos import package_preview, ROOT
    from watchtower_package import source_state


def build_environment(root: str, env: dict[str, str]) -> dict[str, str]:
    flags = env['CARGO_ENCODED_RUSTFLAGS'].split('\x1f') if env.get('CARGO_ENCODED_RUSTFLAGS') else env.get('RUSTFLAGS', '').split()
    home = env.get('HOME', '')
    for path, alias in ((home, '/user'), (env.get('CARGO_HOME') or posixpath.join(home, '.cargo'), '/cargo'),
                        (env.get('RUSTUP_HOME') or posixpath.join(home, '.rustup'), '/rustup'), (root, '/watchtower')):
        if path and posixpath.isabs(path):
            flags.append(f'--remap-path-prefix={posixpath.normpath(path)}={alias}')
    return {**env, 'CARGO_ENCODED_RUSTFLAGS': '\x1f'.join(flags),
            'CARGO_PROFILE_RELEASE_DEBUG': '0', 'CARGO_PROFILE_RELEASE_STRIP': 'debuginfo',
            'MACOSX_DEPLOYMENT_TARGET': '15.0'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'target/watchtower-artifacts-macos')
    parser.add_argument('--require-clean-source', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine() not in ('arm64', 'aarch64'):
        raise SystemExit('Build on an Apple Silicon Mac; this command does not cross-compile.')
    state = source_state(ROOT)
    if args.require_clean_source and state['dirty']:
        raise SystemExit('Release packaging requires a clean source checkout')
    env = build_environment(str(ROOT), dict(os.environ))
    subprocess.run(['cargo', 'build', '--release', '--locked', '--features', 'watchtower', '--target', 'aarch64-apple-darwin'], cwd=ROOT, env=env, check=True)
    binary = Path(env.get('CARGO_TARGET_DIR', ROOT / 'target')) / 'aarch64-apple-darwin/release/herdr'
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(binary)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(binary)], check=True)
    dependencies = subprocess.check_output(['/usr/bin/otool', '-L', str(binary)], text=True).splitlines()[1:]
    for line in dependencies:
        path = line.strip().split(' (', 1)[0]
        if not path.startswith(('/usr/lib/', '/System/Library/')):
            raise SystemExit('Non-system dynamic dependency: ' + path)
    product = json.loads((ROOT / 'distribution/watchtower/product.json').read_text())
    version = subprocess.check_output([str(binary), '--version'], text=True).split()
    if version[:2] != ['watchtower', product['version']]:
        raise SystemExit('Executable is not the expected Watchtower version')
    archive = args.output_dir.resolve() / f"watchtower-{product['version']}-{state['commit'][:12]}-macos-aarch64.tar.gz"
    manifest = package_preview(binary, archive, require_clean_source=args.require_clean_source)
    print(json.dumps({'output': str(archive), 'source': manifest['source']}))


if __name__ == '__main__':
    main()
