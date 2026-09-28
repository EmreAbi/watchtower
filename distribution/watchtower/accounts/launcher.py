#!/usr/bin/env python3
"""Launch Accounts in its private runtime; never install dependencies on open."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def clean_env(environ=None) -> dict[str, str]:
    env = dict(os.environ if environ is None else environ)
    for key in tuple(env):
        if key.upper() in {"PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"}:
            env.pop(key)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def runtime_candidates(env: dict[str, str]) -> list[Path]:
    # The launcher starts with -I. Add only this bundled module directory so
    # the UI, setup and backend share the same portable/default state root.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from backend import AccountError, default_home
    try:
        home = default_home(env)
    except AccountError as exc:
        raise ValueError("Choose an absolute Watchtower account state directory.") from exc
    suffix = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    candidates = [home / "state" / "accounts" / "venv" / suffix]
    radio_home = Path(env.get("RADIO_HOME", ""))
    if radio_home.is_absolute():
        candidates.append(radio_home / "venv" / suffix)
    return candidates


def supports_textual(python: Path, env: dict[str, str]) -> bool:
    if not python.is_file():
        return False
    try:
        result = subprocess.run(
            [str(python), "-I", "-B", "-c", "import textual.app"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args(argv)
    try:
        env = clean_env()
        candidates = runtime_candidates(env)
        if args.launch:
            # The one-use launch request is validated by integration.py. This
            # branch never needs or loads a UI dependency.
            command = [sys.executable, "-B", str(ROOT / "integration.py"), "--run"]
        else:
            python = next((path for path in candidates if supports_textual(path, env)), None)
            if python is None:
                raise ValueError("Accounts UI is not set up. Run setup-accounts.cmd, then open Accounts again.")
            command = [str(python), "-B", str(ROOT / "view.py")]
        return subprocess.run(command, cwd=ROOT, env=env).returncode
    except (OSError, ValueError) as exc:
        print(f"Watchtower Accounts: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
