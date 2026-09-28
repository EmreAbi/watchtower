"""Launch Model Lab in the installed Accounts runtime; never install on open."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def ui_environment(source=None):
    """Use the native pane's color support without changing the parent shell."""
    env = dict(os.environ if source is None else source)
    for key in list(env):
        if key.upper() in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "NO_COLOR",
                           "TERM", "COLORTERM", "TEXTUAL_COLOR_SYSTEM"):
            env.pop(key)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Model Lab runs in a Watchtower truecolor pane. In particular, a server
    # launched by another CLI may inherit NO_COLOR and silently gray every theme.
    env.update(TERM="xterm-256color", COLORTERM="truecolor", TEXTUAL_COLOR_SYSTEM="truecolor")
    return env


def main():
    env = ui_environment()
    try:
        spec = importlib.util.spec_from_file_location("model_lab_ui_runtime", ROOT.parent / "accounts/launcher.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        python = next((path for path in module.runtime_candidates(env) if module.supports_textual(path, env)), None)
        if python is None:
            raise ValueError("Accounts runtime unavailable")
        return subprocess.run([str(python), "-B", str(ROOT / "view.py")], env=env, cwd=ROOT).returncode
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Model Lab needs the Accounts UI runtime. Run setup-accounts.cmd first.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
