"""Validate an extracted Mac package with two disposable servers and real PTYs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from watchtower_smoke_windows import require, wait_until, isolated_environment, verify_package_version


class Instance:
    def __init__(self, binary, home, label, extra=None):
        self.binary, self.home, self.label = binary, home, label
        home.mkdir()
        self.env = isolated_environment(home, label)
        self.env.update(extra or {})
        self.env['SHELL'] = '/bin/zsh'
        self.env['PATH'] = str(binary.parent) + os.pathsep + self.env['PATH']
        self.process = None
        self.log = (home / 'server.log').open('wb')

    def run(self, *args, json_output=False):
        result = subprocess.run([str(self.binary), *args], env=self.env, cwd=self.home,
                                capture_output=True, text=True, timeout=20)
        require(result.returncode == 0, f'{self.label}: {args[:3]}: {result.stderr[-1500:]}')
        data = json.loads(result.stdout) if json_output else result.stdout
        if isinstance(data, dict):
            require(not data.get('error'), str(data))
        return data

    def start(self):
        for name in ('radio', 'accounts', 'results', 'teams', 'benchmarks'):
            self.run('plugin', 'link', str(self.binary.parent / name), json_output=True)
        self.run('config', 'check')
        self.process = subprocess.Popen([str(self.binary), 'server'], env=self.env, cwd=self.home,
                                        stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
        def ready():
            result = self.run('status', 'server', '--json', json_output=True)
            if result.get('running'):
                require(result['session'] == f'smoke-{self.label}', 'Wrong runtime session')
                require(result['pid'] == self.process.pid, 'Server identity differs')
                self.status = result
                return True
        wait_until(ready, f'{self.label} server startup')
        self.pane = self.run('workspace', 'create', '--cwd', str(self.home), '--label', f'SMOKE-{self.label}', json_output=True)['result']['root_pane']['pane_id']
        def relay_ready():
            for path in (self.home / 'state/radio').rglob('relay-runtime-*.json'):
                record = json.loads(path.read_text())
                if record.get('socket') == self.status['socket']:
                    self.relay_path, self.relay = path, record
                    return True
        wait_until(relay_ready, f'{self.label} Radio startup')

    def stop(self):
        self.run('server', 'stop')
        self.process.wait(timeout=20)
        wait_until(lambda: not list((self.home / 'state/radio').rglob('relay-runtime-*.json')), 'Radio shutdown', timeout=25)

    def cleanup(self):
        try:
            if self.process is not None and self.process.poll() is None:
                try:
                    self.stop()
                except Exception:
                    # Signal only the process group created by this retained child.
                    if self.process.poll() is None:
                        os.killpg(self.process.pid, signal.SIGTERM)
                        try:
                            self.process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            if self.process.poll() is None:
                                os.killpg(self.process.pid, signal.SIGKILL)
                            self.process.wait(timeout=10)
            # A detached supervisor must notice host exit even after early failure.
            wait_until(lambda: not list((self.home / 'state/radio').rglob('relay-runtime-*.json')),
                       'owned Radio cleanup', timeout=25)
        finally:
            self.log.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    require(sys.platform == 'darwin', 'Run this smoke on macOS')
    binary = args.exe.resolve()
    # Keep Unix socket paths below the platform length limit, even on Actions.
    temp = Path(tempfile.mkdtemp(prefix='wt-smoke-', dir='/tmp')).resolve()
    instances = []
    report = {'ok': False, 'checks': []}
    try:
        manifest = json.loads((binary.parent / 'BUILD-MANIFEST.json').read_text())
        for name, digest in manifest['files'].items():
            require(hashlib.sha256((binary.parent / name).read_bytes()).hexdigest() == digest, 'Package hash mismatch: ' + name)
        a = Instance(binary, temp / 'a', 'a')
        instances.append(a)
        report['version'] = a.run('--version').strip()
        verify_package_version(binary, report['version'])
        a.start()
        b = Instance(binary, temp / 'b', 'b', {'HERDR_SOCKET_PATH': a.status['socket'],
                     'HERDR_PANE_ID': a.pane, 'HERDR_SESSION': 'smoke-a',
                     'RADIO_HOME': str(a.home / 'state/radio'), 'RADIO_HANDLE': 'foreign'})
        instances.append(b)
        b.start()
        require(a.status['socket'] != b.status['socket'], 'Sockets not isolated')
        require(a.relay['relay_pid'] != b.relay['relay_pid'], 'Radio not isolated')
        report['checks'].append('all five Mac manifests link; two homes, sockets and Radio relays stay separate')
        b.run('pane', 'report-metadata', b.pane, '--source', 'watchtower-smoke', '--token', 'team_role=CONTROL', '--token', 'team_target=Disposable Mac smoke')
        pane = b.run('pane', 'get', b.pane, json_output=True)['result']['pane']
        require(pane['tokens']['team_role'] == 'CONTROL', 'Role metadata lost')
        marker = 'WATCHTOWER_MAC_PTY_OK'
        b.run('pane', 'run', b.pane, 'printf "%s%s\\n" WATCHTOWER_MAC_ PTY_OK')
        wait_until(lambda: marker in [line.strip() for line in b.run('pane', 'read', b.pane, '--source', 'recent-unwrapped', '--lines', '40', '--format', 'text').splitlines()], 'real zsh PTY output')
        report['checks'].append('role metadata and real zsh PTY output')
        b.stop()
        require(a.run('status', 'server', '--json', json_output=True)['running'], 'Stopping B stopped A')
        a.stop()
        report['checks'].append('independent graceful server and Radio shutdown')
        report['ok'] = True
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        for item in instances:
            item.log.flush()
            report.setdefault('logs', {})[item.label] = (item.home / 'server.log').read_text(errors='replace')[-3000:]
    finally:
        for item in reversed(instances):
            try:
                item.cleanup()
            except Exception as exc:
                report['ok'] = False
                report.setdefault('cleanup_errors', []).append(str(exc))
        if report['ok']:
            shutil.rmtree(temp)
        else:
            report['disposable_root'] = str(temp)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
