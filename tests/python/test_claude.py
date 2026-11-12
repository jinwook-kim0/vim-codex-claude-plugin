import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from vim_codex.claude import ClaudeHost
from test_bridge import context, REPO


class ClaudeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vim claude 한글 ')
        self.root = self.temp.name
        (Path(self.root) / '.git').mkdir()
        self.env = patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': self.root + '/native'})
        self.env.start()
        self.events = []
        self.config = {'claude': str(REPO / 'tests/fake_claude.py'), 'python': sys.executable,
            'script': str(REPO / 'pythonx/codex_bridge.py'), 'state_dir': self.root + '/state'}
        self.host = ClaudeHost(self.config, self.events.append)
        self.context = context(self.root)

    def tearDown(self):
        self.host.close()
        self.env.stop()
        self.temp.cleanup()

    def ask(self, question, mode='popup', **fields):
        return self.host.operation(dict(op='ask', mode=mode, root=self.root,
            context=self.context, question=question, **fields))['thread_id']

    def wait(self, event):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            matches = [e for e in self.events if e.get('event') == event]
            if matches:
                return matches[-1]
            time.sleep(.01)
        self.fail('Timed out waiting for ' + event + ': ' + str(self.events))

    def test_stream_context_resume_and_mode_isolation(self):
        sid = self.ask('first')
        answer = self.wait('answer_done')
        self.assertEqual('Claude answer: first', answer['text'])
        self.assertNotIn('firstfirst', answer['text'])
        provided = self.wait('context_provided')
        self.assertIn('before\n선택\nafter', provided['text'])
        self.assertEqual('popup', provided['channel'])
        pane = self.host.operation({'op': 'open', 'root': self.root})
        self.assertNotEqual(pane['thread_id'], sid)
        self.assertNotIn('--remote', pane['argv'])
        self.assertNotIn('--permission-mode', pane['argv'])
        self.assertNotIn('--strict-mcp-config', pane['argv'])
        self.assertEqual([pane['thread_id']], [s['id'] for s in self.host.sessions(self.root, 'pane')])
        self.host.close()
        self.host = ClaudeHost(self.config, self.events.append)
        self.assertEqual(sid, self.host.catalog.current(self.root, 'popup'))
        self.events.clear()
        self.assertEqual(sid, self.ask('second'))
        self.assertEqual('Claude answer: second', self.wait('answer_done')['text'])
        self.assertIn('--resume', self.host.argv(self.root, sid))

    def test_native_history_import_filters_project_and_popup(self):
        base = Path(self.root) / 'native/projects/fixture'
        base.mkdir(parents=True)
        sid, other = str(uuid.uuid4()), str(uuid.uuid4())
        for key, root in ((sid, self.root), (other, '/another/project')):
            (base / (key + '.jsonl')).write_text(json.dumps({'type': 'user', 'cwd': root,
                'message': {'content': 'from terminal'}}) + '\n' + json.dumps({
                    'type': 'custom-title', 'customTitle': 'CLI title'}) + '\n', encoding='utf-8')
        rows = self.host.sessions(self.root, 'pane')
        self.assertEqual([sid], [r['id'] for r in rows])
        self.assertEqual('CLI title', rows[0]['title'])
        result = self.host.operation({'op': 'select', 'root': self.root, 'thread_id': sid})
        self.assertIn('--resume', self.host.argv(self.root, result['thread_id'], pane=True))
        for mode, key in (('popup', sid), ('pane', other), ('pane', '../bad')):
            with self.assertRaises(ValueError):
                self.host.ensure_thread(self.root, mode, session_id=key)

    def test_permission_requires_user_decision_and_cancel_is_not_a_denial(self):
        sid = self.ask('REQUEST_APPROVAL')
        request = self.wait('approval_request')
        self.assertFalse(request['native'])
        self.assertIn(sid, self.host.turns)
        self.assertFalse(any(e['event'] == 'answer_done' for e in self.events))
        self.host.interactive_reply({'request_id': request['request_id'], 'decision': 'decline'})
        self.assertEqual('Permission declined', self.wait('answer_done')['text'])
        self.events.clear()
        self.ask('CANCEL_APPROVAL')
        self.assertEqual('Cancelled permission', self.wait('answer_done')['text'])
        self.wait('interactive_resolved')
        self.events.clear()
        self.ask('after cancellation')
        self.assertEqual('Claude answer: after cancellation', self.wait('answer_done')['text'])

    def test_stop_and_error_recovery(self):
        sid = self.ask('SLOW')
        self.wait('answer')
        with self.assertRaises(ValueError):
            self.ask('must wait')
        with self.assertRaises(ValueError):
            self.host.operation({'op': 'new', 'mode': 'popup', 'root': self.root})
        self.host.operation({'op': 'stop', 'mode': 'popup', 'root': self.root})
        self.assertIn('[Response stopped]', self.wait('answer_done')['text'])
        self.events.clear()
        self.ask('FAIL')
        self.assertEqual('failed', self.wait('answer_done')['status'])
        self.assertIn('Fixture failure', self.wait('answer_done')['text'])
        self.events.clear()
        self.ask('CRASH')
        self.assertEqual('failed', self.wait('answer_done')['status'])
        self.assertIn('Fixture CLI crashed', self.wait('answer_done')['text'])
        self.events.clear()
        self.assertEqual(sid, self.ask('after restart'))
        self.assertEqual('Claude answer: after restart', self.wait('answer_done')['text'])

    def test_stop_during_initialization_does_not_send_the_question(self):
        results = []
        with patch.dict(os.environ, {'VIM_CLAUDE_TEST_INIT_DELAY': '2'}):
            worker = threading.Thread(target=lambda: results.append(self.ask('must not be sent')))
            worker.start()
            self.wait('answer_start')
            self.host.operation({'op': 'stop', 'mode': 'popup', 'root': self.root})
            self.assertEqual('[Response stopped]', self.wait('answer_done')['text'])
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(results))
        self.assertFalse(any(e['event'] == 'context_provided' for e in self.events))

    def test_user_question_answers_and_cancel(self):
        self.ask('ASK_USER')
        request = self.wait('input_request')
        self.assertEqual('Choose a path', request['params']['questions'][0]['question'])
        self.host.interactive_reply({'request_id': request['request_id'],
                                    'answers': {'0': {'answers': ['B']}}})
        self.assertEqual('Question answer: B', self.wait('answer_done')['text'])
        self.events.clear()
        self.ask('ASK_USER')
        request = self.wait('input_request')
        self.host.interactive_reply({'request_id': request['request_id'],
                                    'answers': {'0': {'answers': ['']}}})
        self.assertEqual('Question cancelled', self.wait('answer_done')['text'])

    def test_native_pane_hook_uses_captured_selection_and_defers_permission(self):
        sid = self.ask('native question', mode='pane')
        hook = self.host.hook({'hook_event_name': 'UserPromptSubmit', 'prompt': 'native question'}, self.root, sid)
        self.assertIn('선택', hook['hookSpecificOutput']['additionalContext'])
        self.assertIn('"line": 2', hook['hookSpecificOutput']['additionalContext'])
        with self.assertRaises(ValueError):
            self.ask('busy', mode='pane', pane_alive=True)
        self.host.hook({'hook_event_name': 'Stop'}, self.root, sid)
        self.assertEqual(sid, self.ask('next question', mode='pane', pane_alive=True))
        results = []
        waiter = threading.Thread(target=lambda: results.append(self.host.hook({
            'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash',
            'tool_input': {'command': 'pwd'}}, self.root, sid)))
        waiter.start()
        request = self.wait('approval_request')
        self.assertTrue(request['defer_to_native'])
        self.host.interactive_reply({'request_id': request['request_id'], 'decision': 'native'})
        waiter.join(2)
        self.assertEqual([{}], results)

    def test_save_is_bound_and_rechecks_tick_conflict_and_filename(self):
        sid = self.host.ensure_thread(self.root, 'popup')
        info = {'buffer_id': 1, 'path': self.root + '/a.py', 'changedtick': 4,
                'modified': True, 'on_disk': True, 'disk_changed': False}
        writes = []
        def request(op, args, root):
            if op == 'read_buffer':
                return info.copy()
            writes.append(args)
            return {'saved': True}
        self.host.buffer_request = request
        args = {'buffer_id': 1, 'expected_tick': 4, 'reason': 'test'}
        key = self.host.keys[sid]
        with self.assertRaises(ValueError):
            self.host.tool_request('save_buffer', args, self.root, 'wrong-key')
        for field, value in (('changedtick', 5), ('disk_changed', True), ('path', '')):
            original = info[field]
            info[field] = value
            with self.assertRaises(ValueError):
                self.host.tool_request('save_buffer', args, self.root, key)
            info[field] = original
        self.assertEqual([], writes)
        self.assertTrue(self.host.tool_request('save_buffer', args, self.root, key)['saved'])
        self.assertEqual(os.path.realpath(info['path']), writes[0]['authorized_path'])
