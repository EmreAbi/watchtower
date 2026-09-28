"""Process-local Rust release flags; do not alter a developer's Cargo config.

Rust remapping covers compiler-generated paths, not arbitrary strings or native
dependencies. The completed archive still needs the release privacy scan.
"""
from __future__ import annotations

import argparse
import json
import ntpath
import os
from pathlib import Path


def build_environment(root: str | Path, environ: dict[str, str] | None = None) -> dict[str, str]:
    env = {key.upper(): value for key, value in (os.environ if environ is None else environ).items()}
    # Cargo gives the encoded variable precedence, then whitespace-split RUSTFLAGS.
    # Retain explicit caller flags and reassert the required static CRT afterwards.
    encoded = env.get("CARGO_ENCODED_RUSTFLAGS")
    flags = encoded.split("\x1f") if encoded else env.get("RUSTFLAGS", "").split()
    flags += ["-C", "target-feature=+crt-static", "-C", "link-arg=/PDBALTPATH:%_PDB%"]
    user = env.get("USERPROFILE")
    cargo = env.get("CARGO_HOME") or (ntpath.join(user, ".cargo") if user else None)
    rustup = env.get("RUSTUP_HOME") or (ntpath.join(user, ".rustup") if user else None)
    # rustc uses the last matching map. Put the most specific repository map last.
    for source, replacement in ((user, "/user"), (cargo, "/cargo"), (rustup, "/rustup"), (str(root), "/watchtower")):
        if not source:
            continue
        absolute = ntpath.normpath(ntpath.abspath(source))
        for spelling in dict.fromkeys((absolute, absolute.replace("\\", "/"))):
            flags.append(f"--remap-path-prefix={spelling}={replacement}")
    return {
        "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(flags),
        "CARGO_PROFILE_RELEASE_DEBUG": "0",
        "CARGO_PROFILE_RELEASE_STRIP": "debuginfo",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(build_environment(args.root.resolve())))


if __name__ == "__main__":
    main()
