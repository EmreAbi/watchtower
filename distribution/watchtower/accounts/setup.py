"""Explicitly install the Accounts UI into a private virtual environment."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

# Isolated Python (-I) omits the script directory; trust only bundled modules.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend import default_home
from launcher import clean_env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Verify an existing private UI runtime without installing anything")
    args = parser.parse_args(argv)
    env = clean_env()
    root = Path(default_home()) / "state" / "accounts" / "venv"
    executable = root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if args.check:
        if not executable.is_file():
            print("Accounts runtime is not installed. Run setup-accounts.", file=sys.stderr)
            return 1
        return subprocess.run(
            [str(executable), "-I", "-B", "-c", "import textual.app, PIL, pypdfium2"],
            env=env,
        ).returncode
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
