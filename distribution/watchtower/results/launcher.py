"""Results plugin launch/actions. Opening the view never starts an agent."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def runtime(env):
    home = Path(env.get('WATCHTOWER_HOME') or Path.home() / '.watchtower')
    suffix = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
    candidates = [home / 'state/accounts/venv' / suffix]
    radio = env.get('RADIO_HOME')
    if radio and Path(radio).is_absolute():
        candidates.append(Path(radio) / 'venv' / suffix)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        result = subprocess.run([str(candidate), '-I', '-B', '-c', 'import textual.app,markdown_it'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, timeout=8, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode == 0:
            return candidate
    raise ValueError('Results needs the Accounts UI runtime. Run setup-accounts.cmd first.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--file', action='store_true')
    parser.add_argument('--render-file', action='store_true')
    args = parser.parse_args()
    env = dict(os.environ)
    for key in list(env):
        if key.upper() in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
            env.pop(key)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    try:
        context = json.loads(env.get('HERDR_PLUGIN_CONTEXT_JSON', '{}'))
        if args.open:
            from bridge import cli
            pane = context.get('focused_pane_id') or env.get('HERDR_PANE_ID')
            if not pane:
                raise ValueError('Focus an agent pane before opening Results.')
            cli(['plugin', 'pane', 'open', '--plugin', 'watchtower-results', '--entrypoint', 'center',
                 '--env', 'WATCHTOWER_RESULTS_PANE=' + pane,
                 '--focus'], env)
            return 0
        if args.render_file:
            from files import action, local_file
            state = env.get('HERDR_PLUGIN_STATE_DIR', '')
            if not Path(state).is_absolute():
                raise ValueError('Results preview state is unavailable.')
            path = local_file(context.get('clicked_url', ''))
            action(path, 'preview', Path(state) / 'previews')
            return 0
        python = runtime(env)
        if args.file:
            command = [str(python), '-I', '-B', str(ROOT / 'launcher.py'), '--render-file']
        else:
            command = [str(python), '-B', str(ROOT / 'view.py')]
        return subprocess.run(command, env=env, cwd=ROOT).returncode
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print('Watchtower Results: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
