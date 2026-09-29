"""Materialize verified macOS plugin assets; never copy mutable user state."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

if __package__:
    from .watchtower_accounts import MACOS_FILES as ACCOUNTS_MACOS, validated_accounts_files
    from .watchtower_results import MACOS_FILES as RESULTS_MACOS, validated_results_files
    from .watchtower_teams import MACOS_FILES as TEAMS_MACOS, validated_teams_files
    from .watchtower_benchmarks import MACOS_FILES as BENCHMARKS_MACOS, validated_benchmark_files
    from .watchtower_radio import validated_radio_files
else:
    from watchtower_accounts import MACOS_FILES as ACCOUNTS_MACOS, validated_accounts_files
    from watchtower_results import MACOS_FILES as RESULTS_MACOS, validated_results_files
    from watchtower_teams import MACOS_FILES as TEAMS_MACOS, validated_teams_files
    from watchtower_benchmarks import MACOS_FILES as BENCHMARKS_MACOS, validated_benchmark_files
    from watchtower_radio import validated_radio_files

PLUGINS = {
    "accounts": (validated_accounts_files, ACCOUNTS_MACOS),
    "results": (validated_results_files, RESULTS_MACOS),
    "teams": (validated_teams_files, TEAMS_MACOS),
    "benchmarks": (validated_benchmark_files, BENCHMARKS_MACOS),
}
LAUNCHERS = ("open-watchtower", "setup-radio", "setup-accounts")


def _safe_file(path: Path, root: Path) -> Path:
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Missing or escaped macOS asset: {path.name}")
    current = path
    while True:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ValueError("macOS plugin assets cannot contain links")
        if current == root:
            break
        if current.parent == current:
            raise ValueError("macOS plugin asset escaped its source directory")
        current = current.parent
    return path


def _copy(source: Path, destination: Path, *, executable: bool = False) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(0o755 if executable else 0o644)


def stage_plugins(root: Path, stage: Path) -> dict:
    """Copy reviewed source allowlists into a fresh package stage.

    root is the repository root; stage may already contain binaries and
    notices, but none of these plugin directories or shell entrypoints. Stable
    pane/action IDs are preserved by selecting a checked-in macOS manifest.
    Returned Radio provenance authenticates the staged adapter; all upstream
    runtime bytes retain their original pinned hashes.
    """
    root, stage = Path(root).absolute(), Path(stage).absolute()
    source_root = root / "distribution/watchtower"
    for destination in (*PLUGINS, "radio", *LAUNCHERS):
        if (stage / destination).exists() or (stage / destination).is_symlink():
            raise ValueError(f"macOS plugin stage is not fresh: {destination}")
    if stage.is_symlink() or getattr(stage, "is_junction", lambda: False)():
        raise ValueError("macOS plugin stage cannot be a link")

    # Validate every source before writing anything to the stage.
    copies = []
    for name, (validate, macos_files) in PLUGINS.items():
        source = source_root / name
        common = validate(source)
        for file in common:
            relative = file.relative_to(source).as_posix()
            _safe_file(file, root)
            if relative == "herdr-plugin.toml" or relative.endswith(".cmd"):
                continue
            copies.append((file, stage / name / relative, False))
        for relative in sorted(macos_files):
            file = _safe_file(source / relative, root)
            target = "herdr-plugin.toml" if relative == "herdr-plugin.macos.toml" else relative
            copies.append((file, stage / name / target, relative.startswith("bin/")))

    radio = source_root / "radio"
    validated_radio_files(radio)
    provenance = json.loads((radio / "provenance.json").read_text(encoding="utf-8"))
    for relative in provenance["files"]:
        file = _safe_file(radio / relative, root)
        if relative == "herdr-plugin.toml" or relative.endswith(".cmd"):
            continue
        target = "herdr-plugin.toml" if relative == "herdr-plugin.macos.toml" else relative
        copies.append((file, stage / "radio" / target, relative.startswith("bin/")))
    for relative in LAUNCHERS:
        copies.append((_safe_file(source_root / relative, root), stage / relative, True))
    for source, destination, executable in copies:
        _copy(source, destination, executable=executable)

    staged_radio = stage / "radio"
    files = {
        file.relative_to(staged_radio).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
        for file in sorted(staged_radio.rglob("*")) if file.is_file()
    }
    for name in provenance["upstream_files"]:
        if files.get(name) != provenance["files"].get(name):
            raise ValueError("macOS staging changed the pinned Radio runtime")
    provenance["files"] = files
    (staged_radio / "provenance.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    validated_radio_files(staged_radio)
    return provenance
