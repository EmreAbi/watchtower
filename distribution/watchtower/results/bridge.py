"""Read one bound agent session; explicit user actions use public Watchtower APIs."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading

from files import action as output_action, local_file
from session_reader import CodexSessionReader
from status import describe_status


class ResultsError(ValueError):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


QUICK_PREFIX = (
    '[Watchtower: Quick request]\n'
    'For a standalone image, writing, translation or question, complete it directly and briefly. '
    'Do not create a work item, delegate or request project review just for this simple output. '
    'Use only the tools and context required by the request. For code changes, databases, '
    'deployments or substantive project work, keep the existing project workflow and scope. '
    'All permission and safety boundaries still apply. Return the actual output and a short answer.\n\n'
)


def cli(args, env=None):
    env = dict(os.environ if env is None else env)
    binary = Path(env.get('HERDR_BIN_PATH', ''))
    socket = env.get('HERDR_SOCKET_PATH', '')
    if (not binary.is_absolute() or not binary.is_file()
            or binary.stem.lower() not in ('watchtower', 'herdr') or not Path(socket).is_absolute()):
        raise ResultsError('Open Results from a Watchtower agent pane.')
    try:
        result = subprocess.run([str(binary), *args], env=env, capture_output=True,
            text=True, encoding='utf-8', timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        response = json.loads(result.stdout)
        if result.returncode or 'error' in response or not isinstance(response.get('result'), dict):
            raise ResultsError('Watchtower rejected the action. Nothing will be retried automatically.')
        return response['result']
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, ResultsError):
            raise
        raise ResultsError('Watchtower is unavailable. Check Terminal before retrying.') from None


class ResultsService:
    def __init__(self, env=None, api=None, reader=None):
        self.env = dict(os.environ if env is None else env)
        self.api = api or (lambda args: cli(args, self.env))
        self.reader = reader or CodexSessionReader()
        try:
            context = json.loads(self.env.get('HERDR_PLUGIN_CONTEXT_JSON', '{}'))
        except ValueError:
            context = {}
        self.pane_id = self.env.get('WATCHTOWER_RESULTS_PANE') or context.get('focused_pane_id') or self.env.get('HERDR_PANE_ID')
        if not re.fullmatch(r'w\d+:p\d+', self.pane_id or ''):
            raise ResultsError('Focus an agent pane, then open Results.')
        state = self.env.get('HERDR_PLUGIN_STATE_DIR')
        if not state or not Path(state).is_absolute():
            raise ResultsError('Results state directory is unavailable.')
        self.state = Path(state)
        self.original = self._pane()
        self.identity = self._identity(self.original)
        if not self.identity[0]:
            raise ResultsError('This pane has no saved agent session. Start an agent first.')
        self.home = self._home(self.original)
        self.files = {}
        self.lock = threading.RLock()
        self.pending = None
        self.selected_turn = None
        self.turns = {}

    def _pane(self):
        pane = self.api(['pane', 'get', self.pane_id]).get('pane', {})
        if pane.get('pane_id') != self.pane_id:
            raise ResultsError('The original pane is no longer available.')
        return pane

    @staticmethod
    def _identity(pane):
        session = pane.get('agent_session') or {}
        return (session.get('value'), session.get('agent'), pane.get('cwd'), pane.get('workspace_id'))

    def _verified(self):
        pane = self._pane()
        if self._identity(pane) != self.identity:
            raise ResultsError('The pane session changed. Close Results and open it again.')
        if self._home(pane) != self.home:
            raise ResultsError('The account binding changed. Close Results and open it again.')
        return pane

    def _home(self, pane):
        default = Path(self.env.get('CODEX_HOME') or Path.home() / '.codex')
        radio = self.env.get('RADIO_HOME')
        if not radio or not Path(radio).is_absolute() or not (Path(radio) / 'radio.db').is_file():
            return default.resolve()
        try:
            with closing(sqlite3.connect((Path(radio) / 'radio.db').as_uri() + '?mode=ro', uri=True, timeout=2)) as conn:
                rows = conn.execute('SELECT h.agent_session,h.agent,h.account,a.provider,a.home, '
                    "CASE WHEN json_valid(a.env) THEN json_extract(a.env,'$.CODEX_HOME') END "
                    'FROM handles h LEFT JOIN accounts a ON a.name=h.account WHERE h.session_ref=?',
                    ('herdr:' + pane['pane_id'],)).fetchall()
            if len(rows) > 1:
                raise ResultsError('Ambiguous Radio pane binding.')
            if rows:
                sid, provider, account, account_provider, home, override = rows[0]
                if sid != self._identity(pane)[0] or provider != self._identity(pane)[1]:
                    raise ResultsError('Radio and pane session identity do not match.')
                if account:
                    if account_provider != provider or not home or not Path(home).is_absolute():
                        raise ResultsError('The bound account home is unavailable.')
                    default = Path(override or home)
            if not default.is_absolute():
                raise ResultsError('The bound account home is invalid.')
            return default.resolve()
        except sqlite3.Error:
            raise ResultsError('Radio bindings could not be verified.') from None

    def snapshot(self):
        with self.lock:
            pane = self._verified()
            sid, provider, cwd, _ = self.identity
            data = self.reader.read(sid, self.home, cwd=cwd, provider=provider)
            state = pane.get('agent_status', 'unknown')
            supported = bool(data.get('provider_supported'))
            if data.get('state') == 'running' and state in ('idle', 'done'):
                state = 'working'
            if self.pending is not None:
                previous_task, request = self.pending
                if data.get('task') == previous_task:
                    data = dict(data, task=dict(previous_task, request=request), messages=[], artifacts=[], activity=[])
                    if state in ('idle', 'done'):
                        state = 'working'
                else:
                    self.pending = None
            status = describe_status(pane, data, pending=self.pending is not None)
            history = [turn for turn in data.get('history', []) if turn.get('key')]
            self.turns = {turn['key']: turn for turn in history}
            history_notice = ''
            if self.selected_turn is not None and self.selected_turn not in self.turns:
                self.selected_turn = None
                history_notice = 'That task is outside the retained history. Showing the latest task.'
            display = self.turns[self.selected_turn] if self.selected_turn else data
            final = '\n\n'.join(m.get('text', '') for m in display.get('messages', []) if m.get('kind') == 'final')
            # Never present the previous turn as the result of a submitted one.
            self.files = {str(f['id']): f for f in display.get('artifacts', [])}
            activity = list(display.get('activity', []))
            activity.extend({'kind': 'commentary', 'text': message.get('text', ''),
                             'timestamp': message.get('timestamp')}
                            for message in display.get('messages', []) if message.get('kind') == 'commentary')
            activity.sort(key=lambda item: item.get('timestamp') or '')
            agent = {'label': pane.get('label') or provider or 'Agent', 'provider': provider,
                'state': state, 'supports_results': supported, 'supports_input': supported,
                'can_send': supported and not data.get('error') and status['can_send'] and self.selected_turn is None,
                'blocked': state == 'blocked', 'message': data.get('error') or ''}
            agent.update(status_code=status['code'], status_label=status['label'], status_detail=status['detail'])
            payload = {'agent': agent, 'request': display.get('task', {}).get('request', ''),
                'result': final, 'activity': activity, 'files': list(self.files.values()),
                'display_key': display.get('key'),
                'display_state': display.get('state'),
                'selected_turn': self.selected_turn, 'history_notice': history_notice,
                'history_truncated': bool(data.get('truncated')),
                'turns': [dict(key=turn['key'], request=turn.get('task', {}).get('request', '').removeprefix(QUICK_PREFIX),
                              timestamp=turn.get('task', {}).get('timestamp'), state=turn.get('state'))
                          for turn in reversed(history)]}
            if payload['request'].startswith(QUICK_PREFIX):
                payload['request'] = payload['request'][len(QUICK_PREFIX):]
            if state == 'blocked':
                agent['message'] = 'Agent needs input. Use Terminal to inspect and answer the prompt.'
            elif status['code'] == 'interrupted':
                agent['message'] = 'This turn was interrupted. Use Terminal to inspect its state.'
            elif any(item.get('kind') == 'error' for item in activity):
                agent['message'] = 'The agent reported an error. Open Activity or Terminal for details.'
            payload['revision'] = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
            return payload

    def select_turn(self, key=None):
        with self.lock:
            self._verified()
            if key is not None and key not in self.turns:
                raise ResultsError('That task is no longer in the retained history. Refresh Results.')
            self.selected_turn = key
            self.files = {}

    def preview(self, file_id, *, columns=72, rows=24, page=0):
        from preview import preview
        with self.lock:
            self._verified()
            record = self.files.get(str(file_id))
            if not record:
                raise ResultsError('Select an output from the displayed task.')
            try:
                return preview(record['path'], columns=columns, rows=rows, page=page)
            except (OSError, ValueError) as exc:
                raise ResultsError(str(exc)) from None

    def send(self, text, mode='quick'):
        if not isinstance(text, str) or not text.strip() or len(text) > 16000 or '\0' in text:
            raise ResultsError('Enter a request of up to 16,000 characters.')
        if mode not in ('quick', 'project'):
            raise ResultsError('Choose Quick request or Project task.')
        with self.lock:
            if self.selected_turn is not None:
                raise ResultsError('Return to Latest task before sending a new request.')
            if self.pending is not None:
                raise ResultsError('The previous request is awaiting confirmation. Use Terminal to check it.')
            pane = self._verified()
            if self.identity[1] != 'codex' or pane.get('agent') != 'codex' or pane.get('agent_status') not in ('idle', 'done'):
                raise ResultsError('The agent is not ready. Use Terminal; no request was sent.')
            request = (QUICK_PREFIX if mode == 'quick' else '') + text
            before = self.reader.read(self.identity[0], self.home, cwd=self.identity[2], provider=self.identity[1])
            if not describe_status(pane, before)['can_send']:
                raise ResultsError('The session is busy or could not be verified. Use Terminal; no request was sent.')
            pane = self._verified()
            if pane.get('agent') != 'codex' or pane.get('agent_status') not in ('idle', 'done'):
                raise ResultsError('The agent is no longer ready. No request was sent.')
            self.api(['agent', 'prompt', self.pane_id, request])
            self.pending = (before.get('task', {}), text)
            self.files = {}
            return 'Request sent to ' + (pane.get('label') or self.pane_id) + '.'

    def file_action(self, file_id, action):
        with self.lock:
            self._verified()
            record = self.files.get(str(file_id))
            if not record:
                raise ResultsError('Select an output from the displayed task.')
            try:
                return output_action(local_file(record['path']), action, self.state / 'previews')
            except (OSError, ValueError) as exc:
                raise ResultsError(str(exc)) from None

    def load_preference(self):
        try:
            data = json.loads((self.state / 'view.json').read_text(encoding='utf-8'))
            value = data.get('tab', 'results') if isinstance(data, dict) else 'results'
            return value if value in ('results', 'activity', 'outputs') else 'results'
        except (OSError, ValueError):
            return 'results'

    def save_preference(self, tab):
        if tab not in ('results', 'activity', 'outputs'):
            return
        self.state.mkdir(parents=True, exist_ok=True)
        path = self.state / 'view.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'tab': tab}), encoding='utf-8')
        os.replace(temporary, path)
