"""Start the template chooser or consume an explicit agent launch ticket."""
import argparse
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--launch', action='store_true')
    args = parser.parse_args()
    if args.launch:
        from integration import main as launch_agent
        return launch_agent()
    env = dict(os.environ)
    for key in list(env):
        if key.upper() in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
            env.pop(key)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    try:
        spec = importlib.util.spec_from_file_location('teams_ui_runtime', ROOT.parent / 'accounts/launcher.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        python = next((path for path in module.runtime_candidates(env) if module.supports_textual(path, env)), None)
        if python is None:
            raise ValueError('Accounts UI runtime is unavailable.')
        return subprocess.run([str(python), '-B', str(ROOT / 'view.py')], env=env, cwd=ROOT).returncode
    except (OSError, ValueError, subprocess.SubprocessError):
        print('Watchtower Teams needs the Accounts UI runtime. Run setup-accounts.cmd first.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
