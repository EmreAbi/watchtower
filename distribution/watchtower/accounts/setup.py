"""Explicitly install the Accounts UI into a private virtual environment."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from backend import default_home
from launcher import clean_env


def main():
    env = clean_env()
    root = Path(default_home()) / "state" / "accounts" / "venv"
    executable = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not executable.is_file():
        result = subprocess.run([sys.executable, "-I", "-m", "venv", str(root)], env=env)
        if result.returncode:
            return result.returncode
    # Pin the version exercised by the headless UI tests. Nothing installs when
    # opening Accounts, linking the plugin, or starting Watchtower.
    return subprocess.run([str(executable), "-I", "-m", "pip", "install",
                           "--disable-pip-version-check", "--only-binary=:all:",
                           "textual==8.2.8", "Pillow==12.3.0", "pypdfium2==5.13.0"], env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
