"""Allowlist portable Accounts code; never package profile/auth/runtime state."""
from pathlib import Path

RUNTIME_FILES = {
    "account_reader.py", "backend.py", "integration.py", "launcher.py",
    "view.py", "browser_login.py", "tool_updates.py", "models.py", "gemini_context.py", "opencode_context.py", "opencode-hooks/tui.js", "setup.py", "herdr-plugin.toml", "bin/center.cmd", "README.md",
    "switch_service.py", "switch_runtime.py", "switch_process.py", "switch_launch.py",
}
DEVELOPMENT_FILES = {"test_backend.py", "test_view.py", "test_models.py", "test_browser_login.py", "test_tool_updates.py", "test_gemini_context.py", "test_opencode_context.py", "test_switch_service.py", "test_switch_runtime.py", "test_switch_process.py", "test_switch_launch.py"}


def validated_accounts_files(root: Path) -> list[Path]:
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if not RUNTIME_FILES <= actual or actual - RUNTIME_FILES - DEVELOPMENT_FILES:
        raise ValueError("Unexpected or missing files in the Accounts package")
    files = []
    for name in sorted(RUNTIME_FILES):
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("Accounts package cannot contain symbolic links")
        files.append(path)
    return files
