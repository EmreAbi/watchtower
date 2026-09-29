"""Allowlist portable Teams code; never package session or preview data."""

from pathlib import Path


RUNTIME_FILES = {
    "catalog.py", "scaffold.py", "service.py", "integration.py", "view.py", "launcher.py",
    "review_engine.py", "workflow.py",
    "herdr-plugin.toml", "bin/center.cmd", "README.md",
}
MACOS_FILES = {"herdr-plugin.macos.toml", "bin/center"}
DEVELOPMENT_FILES = {
    "test_catalog.py", "test_scaffold.py", "test_service.py", "test_integration.py", "test_view.py",
    "test_workflow.py", "test_review_identity.py",
}


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def validated_teams_files(root: Path) -> list[Path]:
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    if not RUNTIME_FILES <= actual or actual - RUNTIME_FILES - DEVELOPMENT_FILES - MACOS_FILES:
        raise ValueError("Unexpected or missing files in the Teams package")
    if _is_link(root):
        raise ValueError("Teams package cannot contain symbolic links or junctions")
    files = []
    for name in sorted(RUNTIME_FILES):
        path = root / name
        ancestors = [path, *(root / parent for parent in Path(name).parents if parent != Path("."))]
        if any(_is_link(parent) for parent in ancestors) or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Teams package cannot contain symbolic links or junctions")
        files.append(path)
    return files
