import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

from vim_codex.bridge import Host


class SavePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = self.temp.name
        self.events = []
        self.calls = []
        self.saves = []
        self.decision = 'accept'
        self.on_approval = None
        self.info = {'buffer_id': 3, 'path': str(Path(self.root) / 'open.py'),
                     'changedtick': 5, 'modified': True, 'on_disk': True, 'disk_changed': False}
        self.permissions = {'cwd': self.root, 'sandbox': {'type': 'workspaceWrite', 'writableRoots': [self.root]},
                            'approvalPolicy': 'on-request', 'activePermissionProfile': {'id': ':workspace'},
                            'approvalsReviewer': 'user'}
        self.host = Host({'codex': 'unused', 'python': sys.executable, 'script': 'unused',
                          'state_dir': str(Path(self.root) / 'state')}, self.emit)
        self.host.rpc = SimpleNamespace(call=self.rpc, close=lambda: None)
        self.host.tool_sessions['bound-session'] = (self.root, 'thread-1')

    def tearDown(self):
        self.host.close()
        self.temp.cleanup()

    def rpc(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'thread/resume':
            self.assertEqual({'threadId': 'thread-1', 'excludeTurns': True}, params)
            return copy.deepcopy(self.permissions)
        if method == 'command/exec':
            return {'exitCode': 0, 'stdout': json.dumps({'allowed': True, 'path': self.info['path']}), 'stderr': ''}
        self.fail('Unexpected RPC: ' + method)

    def emit(self, event):
        self.events.append(event)
        if event.get('event') == 'buffer_request':
            reply = {'request_id': event['request_id']}
            if event['operation'] == 'read_buffer':
                reply['result'] = copy.deepcopy(self.info)
            elif event['operation'] == 'save_buffer':
                if self.info['changedtick'] != event['arguments']['expected_tick']:
                    reply['error'] = 'Stale buffer while waiting for save'
                else:
                    self.saves.append(event['arguments'])
                    self.info['modified'] = False
                    reply['result'] = dict(self.info, saved=True)
            else:
                self.fail('Unexpected buffer operation')
            self.host.buffer_reply(reply)
        elif event.get('event') == 'approval_request':
            if self.on_approval:
                self.on_approval()
            self.host.interactive_reply({'request_id': event['request_id'], 'decision': self.decision})

    def save(self):
        return self.host.save_buffer({'buffer_id': 3, 'expected_tick': 5, 'reason': 'Run validation'},
                                     self.root, 'bound-session')

    def requests(self):
        return [e for e in self.events if e.get('event') == 'approval_request']

    def test_allowed_write_saves_without_a_prompt_even_with_never_policy(self):
        self.permissions['approvalPolicy'] = 'never'
        self.assertTrue(self.save()['saved'])
        self.assertEqual([], self.requests())
        self.assertEqual(self.info['path'], self.saves[0]['authorized_path'])
        probe = next(p for m, p in self.calls if m == 'command/exec')
        self.assertEqual(self.permissions['sandbox'], probe['sandboxPolicy'])

    def test_read_only_policy_asks_then_saves(self):
        self.permissions['sandbox'] = {'type': 'readOnly'}
        self.assertTrue(self.save()['saved'])
        self.assertEqual(1, len(self.requests()))

    def test_decline_does_not_write(self):
        self.permissions['sandbox'] = {'type': 'readOnly'}
        self.decision = 'decline'
        with self.assertRaisesRegex(PermissionError, 'declined'):
            self.save()
        self.assertEqual([], self.saves)

    def test_never_and_granular_rejection_do_not_prompt_or_write(self):
        self.permissions['sandbox'] = {'type': 'readOnly'}
        for policy in ['never', {'granular': {'sandbox_approval': False}}]:
            self.permissions['approvalPolicy'] = policy
            with self.assertRaisesRegex(PermissionError, 'do not allow a prompt'):
                self.save()
        self.assertEqual([], self.requests())
        self.assertEqual([], self.saves)

    def test_permission_changes_are_observed_and_session_grants_are_invalidated(self):
        self.permissions['sandbox'] = {'type': 'readOnly'}
        self.decision = 'acceptForSession'
        self.save()
        self.info['modified'] = True
        self.save()
        self.assertEqual(1, len(self.requests()))
        self.info['modified'] = True
        self.permissions['approvalPolicy'] = 'never'
        with self.assertRaises(PermissionError):
            self.save()
        self.assertEqual(2, len(self.saves))
        self.permissions['approvalPolicy'] = 'on-request'
        self.save()
        self.assertEqual(2, len(self.requests()), 'Returning to old permissions must not revive a stale grant')

    def test_edits_or_permission_changes_while_waiting_cancel_the_save(self):
        self.permissions['sandbox'] = {'type': 'readOnly'}
        self.on_approval = lambda: self.info.update(changedtick=6)
        with self.assertRaisesRegex(RuntimeError, 'Stale buffer'):
            self.save()
        self.info['changedtick'] = 5
        self.on_approval = lambda: self.permissions.update(approvalPolicy='never')
        with self.assertRaisesRegex(PermissionError, 'permissions changed'):
            self.save()
        self.assertEqual([], self.saves)

    def test_custom_permission_profile_is_used_for_the_native_probe(self):
        self.permissions['activePermissionProfile'] = {'id': 'custom-project'}
        self.save()
        probe = next(p for m, p in self.calls if m == 'command/exec')
        self.assertEqual('custom-project', probe['permissionProfile'])
        self.assertNotIn('sandboxPolicy', probe)

    def test_save_requires_a_bound_session_and_fresh_named_buffer(self):
        with self.assertRaisesRegex(ValueError, 'not bound'):
            self.host.save_buffer({'buffer_id': 3, 'expected_tick': 5, 'reason': 'test'}, self.root, 'wrong')
        self.info['path'] = ''
        with self.assertRaisesRegex(ValueError, 'Unnamed'):
            self.save()
        self.assertEqual([], self.saves)


if __name__ == '__main__':
    unittest.main()
