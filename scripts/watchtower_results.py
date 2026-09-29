"""Allowlist portable Results code; never package session or preview data."""

from pathlib import Path


RUNTIME_FILES = {
    "bridge.py", "files.py", "session_reader.py", "view.py", "launcher.py",
    "preview.py", "preview_view.py", "status.py",
    "herdr-plugin.toml", "bin/center.cmd", "README.md",
}
MACOS_FILES = {"herdr-plugin.macos.toml", "bin/center"}
DEVELOPMENT_FILES = {
    "test_bridge.py", "test_files.py", "test_session_reader.py", "test_view.py", "test_launcher.py",
    "test_preview.py", "test_preview_view.py", "test_status.py",
}


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def validated_results_files(root: Path) -> list[Path]:
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if not RUNTIME_FILES <= actual or actual - RUNTIME_FILES - DEVELOPMENT_FILES - MACOS_FILES:
        raise ValueError("Unexpected or missing files in the Results package")
    if _is_link(root):
        raise ValueError("Results package cannot contain symbolic links or junctions")
    files = []
    for name in sorted(RUNTIME_FILES):
        path = root / name
        ancestors = [path, *(root / parent for parent in Path(name).parents if parent != Path("."))]
        if any(_is_link(parent) for parent in ancestors) or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Results package cannot contain symbolic links or junctions")
        files.append(path)
    return files
