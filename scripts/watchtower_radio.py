#!/usr/bin/env python3
"""Validate the Watchtower Radio source allowlist without starting the runtime."""

from pathlib import Path

RADIO_ROOT = Path(__file__).resolve().parents[1] / "distribution/watchtower/radio"


def validated_radio_files(root: Path = RADIO_ROOT) -> list[Path]:
    adapter = root / "watchtower_radio.py"
    namespace = {"__file__": str(adapter), "__name__": "watchtower_radio_adapter"}
    # No import cache may be written into the exact, checksummed bundle.
    exec(compile(adapter.read_bytes(), str(adapter), "exec"), namespace)
    return namespace["verified_files"](root)


if __name__ == "__main__":
    print(f"Validated {len(validated_radio_files())} private Radio files")
