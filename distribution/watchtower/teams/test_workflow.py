import json
from contextlib import closing
import os
import sqlite3
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from review_engine import ReviewError, ReviewSystem
from workflow import Workflow


class LocalEngine(ReviewSystem):
    role = 'lead'

    def identity(self, config):
        return dict(role=self.role, workspace='w1', pane='w1:p-' + self.role, session='session-' + self.role)

    def notify(self, *args, **kwargs):
        self.last_notification = (args, kwargs)
        return 'sent'


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {'HERDR_WORKSPACE_ID': 'w1'})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.team = self.root / 'team-a'
        self.team.mkdir()
        self.config = {'workspace_id': 'w1', 'roles': {k: {} for k in ('lead', 'worker', 'reviewer')},
                       'review': {'lead': 'lead', 'local_reviewer': 'reviewer', 'global_reviewer': None,
                                  'global_mode': 'on_demand', 'required': True}}
        self.save(self.team / 'config.json', self.config)
        self.engine = LocalEngine(self.root)
        self.flow = Workflow(self.team, self.engine)
        self.begin = self.root / 'begin.json'
        self.scope = {'task': 'Answer the question', 'acceptance_criteria': ['Evidence supports the answer']}
        self.save(self.begin, self.scope)
        self.output = self.root / 'output.md'
        self.output.write_text('Evidence and answer', encoding='utf-8')
        self.report = self.root / 'report.md'
        self.report.write_text('Checked against the criteria and evidence.', encoding='utf-8')
        self.request = self.root / 'request.json'
        self.save(self.request, dict(self.scope, summary='Answered', artifacts=[{'path': str(self.output)}]))

    def save(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def submitted(self):
        self.flow.begin('one', self.begin)
        self.engine.role = 'worker'
        return self.flow.submit('one', self.request)['digest']

    def test_one_active_task_and_owner_only(self):
        self.engine.role = 'worker'
        with self.assertRaisesRegex(ReviewError, 'task owner'):
            self.flow.begin('one', self.begin)
        self.engine.role = 'lead'
        self.flow.begin('one', self.begin)
        with self.assertRaisesRegex(ReviewError, 'active task'):
            self.flow.begin('two', self.begin)

    def test_complete_requires_independent_exact_fresh_pass(self):
        sha = self.submitted()
        self.engine.role = 'lead'
        with self.assertRaisesRegex(ReviewError, 'PENDING_LOCAL'):
            self.flow.complete('one', sha, 'Done')
        self.engine.role = 'worker'
        with self.assertRaisesRegex(ReviewError, 'independent reviewer'):
            self.flow.result('one', sha, 'PASS', self.report)
        self.engine.role = 'reviewer'
        self.assertEqual(self.flow.result('one', sha, 'PASS', self.report)['state'], 'PASS')
        self.assertEqual(self.flow.status('one')['state'], 'READY_TO_COMPLETE')
        self.engine.role = 'lead'
        with self.assertRaisesRegex(ReviewError, 'STALE'):
            self.flow.complete('one', '0' * 64, 'Done')
        self.assertEqual(self.flow.complete('one', sha, 'Done')['state'], 'COMPLETED')
        self.output.write_text('Changed after approval', encoding='utf-8')
        self.assertEqual(self.flow.status('one')['state'], 'STALE')

    def test_changed_deliverable_cannot_reuse_pass(self):
        sha = self.submitted()
        self.engine.role = 'reviewer'
        self.flow.result('one', sha, 'PASS', self.report)
        self.output.write_text('New output', encoding='utf-8')
        self.engine.role = 'lead'
        with self.assertRaisesRegex(ReviewError, 'STALE'):
            self.flow.complete('one', sha, 'Done')

    def test_changes_required_needs_new_revision_and_reports_are_immutable(self):
        sha = self.submitted()
        self.engine.role = 'reviewer'
        self.flow.result('one', sha, 'CHANGES_REQUIRED', self.report)
        with self.assertRaisesRegex(ReviewError, 'different decision'):
            self.flow.result('one', sha, 'PASS', self.report)
        self.engine.role = 'worker'
        self.output.write_text('Corrected evidence', encoding='utf-8')
        newsha = self.flow.submit('one', self.request)['digest']
        self.assertNotEqual(sha, newsha)
        self.engine.role = 'reviewer'
        self.assertEqual(self.flow.result('one', newsha, 'PASS', self.report)['state'], 'PASS')

    def test_unconfigured_escalation_blocks_instead_of_silent_pass(self):
        sha = self.submitted()
        self.engine.role = 'reviewer'
        result = self.flow.result('one', sha, 'PASS', self.report, require_global=True)
        self.assertEqual(result['state'], 'BLOCKED')
        args, _ = self.engine.last_notification
        self.assertEqual(args[3], 'w1:lead')
        self.assertIn('REVIEW_BLOCKED', args[4])
        self.engine.role = 'lead'
        with self.assertRaisesRegex(ReviewError, 'BLOCKED'):
            self.flow.complete('one', sha, 'Done')
        self.output.write_text('new revision', encoding='utf-8')
        with self.assertRaisesRegex(ReviewError, 'Unresolved global review'):
            self.flow.submit('one', self.request)

    def test_no_self_submission_or_scope_weakening(self):
        self.flow.begin('one', self.begin)
        self.engine.role = 'reviewer'
        with self.assertRaisesRegex(ReviewError, 'cannot submit'):
            self.flow.submit('one', self.request)
        self.engine.role = 'worker'
        self.save(self.request, dict(self.scope, acceptance_criteria=['Just answer'], summary='Changed',
                                     artifacts=[{'path': str(self.output)}]))
        with self.assertRaisesRegex(ReviewError, 'recorded scope'):
            self.flow.submit('one', self.request)

    def test_quick_task_needs_no_review_or_fake_approval(self):
        self.config['review'].update(required=False, lead='assistant', local_reviewer=None)
        self.config['roles'] = {'assistant': {}}
        self.save(self.team / 'config.json', self.config)
        self.engine.role = 'assistant'
        self.flow.begin('one', self.begin)
        result = self.flow.complete('one', None, 'Delivered image')
        self.assertEqual(result['state'], 'COMPLETED')
        self.assertFalse(result['independent_review'])
        self.assertFalse((self.team / 'reviews').exists())

    def test_real_identity_fails_closed_outside_live_team(self):
        actual = Workflow(self.team)
        with self.assertRaises(ReviewError):
            actual.begin('one', self.begin)
        self.assertFalse((self.team / 'work-items').exists())

    def test_identity_matches_ledger_session_workspace_pane_and_role_directory(self):
        radio = self.root / 'radio'
        radio.mkdir()
        with closing(sqlite3.connect(radio / 'radio.db')) as conn:
            conn.execute('CREATE TABLE handles(name, session_ref, workspace, pane_workspace, agent_session, agent)')
            conn.execute('INSERT INTO handles VALUES(?,?,?,?,?,?)',
                         ('reviewer', 'herdr:w1:p2', 'w1', 'w1', 'fixture-session', 'codex'))
            conn.commit()
        config = dict(self.config, radio_home=str(radio), herdr='fixture-watchtower')
        config['roles']['reviewer']['cwd'] = str(self.team)
        pane = dict(pane_id='w1:p2', workspace_id='w1', label='reviewer', agent='codex',
                    agent_session={'value': 'fixture-session'}, cwd=str(self.team))
        env = dict(HERDR_ENV='1', HERDR_PANE_ID='w1:p2', HERDR_WORKSPACE_ID='w1',
                   RADIO_HOME=str(radio), RADIO_HANDLE='reviewer', RADIO_JOINED_SCOPE='w1',
                   CODEX_THREAD_ID='fixture-session')
        engine = ReviewSystem(self.root)
        with patch.dict(os.environ, env), patch('review_engine.run', return_value=SimpleNamespace(
                stdout=json.dumps({'result': {'pane': pane}}).encode())) as run:
            self.assertEqual(engine.identity(config)['role'], 'reviewer')
            self.assertEqual(run.call_args.args[0], ['fixture-watchtower', 'pane', 'get', 'w1:p2'])
            for key, value in [('CODEX_THREAD_ID', 'another-session'), ('RADIO_HANDLE', 'lead'),
                               ('RADIO_JOINED_SCOPE', 'w2'), ('HERDR_WORKSPACE_ID', 'w2')]:
                with self.subTest(key=key), patch.dict(os.environ, {key: value}), self.assertRaises(ReviewError):
                    engine.identity(config)
            with closing(sqlite3.connect(radio / 'radio.db')) as conn:
                conn.execute("UPDATE handles SET agent_session='old-session'")
                conn.commit()
            with self.assertRaises(ReviewError):
                engine.identity(config)


if __name__ == '__main__':
    unittest.main()
