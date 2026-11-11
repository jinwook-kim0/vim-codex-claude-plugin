import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from vim_codex.bridge import Catalog, Host, context_text, ide_context, socket_exact

REPO = Path(__file__).resolve().parents[2]


def context(root, on_disk=False):
    return {'root': root, 'buffer_id': 7, 'buffer_uri': 'vim-buffer://test/7',
            'path': str(Path(root) / 'a.py') if on_disk else '', 'name': '[No Name]',
            'on_disk': on_disk, 'changedtick': 4, 'lines': ['before', '선택', 'after'],
            'cursor': {'line': 2, 'column': 1, 'utf16_column': 1},
            'selection': {'type': 'line', 'start': {'line': 2, 'column': 1},
                          'end': {'line': 2, 'column': 7}}, 'selected_text': '선택'}


class ContextTests(unittest.TestCase):
    def test_saved_selection_omits_other_lines_but_retains_coordinates(self):
        c = context('/project', True)
        text = context_text(c)
        self.assertNotIn('before', text)
        self.assertNotIn('after', text)
        self.assertIn('선택', text)
        self.assertIn('"line": 2', text)
        self.assertIn('/project/a.py', text)

    def test_unnamed_selection_includes_entire_buffer(self):
        text = context_text(context('/project'))
        self.assertIn('before\n선택\nafter', text)
        self.assertIn('vim-buffer://test/7', text)

    def test_native_ide_keeps_coordinates_in_selection_content(self):
        c = context('/project')
        ide = ide_context(c)
        self.assertEqual('vim-buffer://test/7', ide['activeFile']['path'])
        self.assertEqual(1, ide['activeFile']['selection']['start']['line'])
        self.assertIn('"cursor"', ide['activeFile']['activeSelectionContent'])
        self.assertIn('"selection"', ide['activeFile']['activeSelectionContent'])


class CatalogTests(unittest.TestCase):
    def test_project_mode_isolation_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sessions.sqlite3'
            c = Catalog(path)
            c.add('/a', 'pane', 'a1')
            c.add('/a', 'popup', 'a2')
            c.add('/b', 'pane', 'b1')
            c.update('a2', answer='long answer', title='question')
            c = Catalog(path)
            self.assertEqual('a1', c.current('/a', 'pane'))
            self.assertEqual('a2', c.current('/a', 'popup'))
            self.assertEqual('b1', c.current('/b', 'pane'))
            self.assertEqual(['a2'], [x['id'] for x in c.sessions('/a', 'popup')])
            self.assertEqual('long answer', c.get('a2')['answer'])
            self.assertEqual(0o600, path.stat().st_mode & 0o777)


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='vim codex 한글 ')
        self.root = self.temp.name
        (Path(self.root) / '.git').mkdir()
        self.events = []
        self.lock = threading.Lock()
        self.context = context(self.root)
        self.config = {'codex': str(REPO / 'tests/fake_codex.py'),
                       'python': sys.executable, 'script': str(REPO / 'pythonx/codex_bridge.py'),
                       'state_dir': str(Path(self.root) / 'state')}
        self.host = Host(self.config, self.emit)

    def tearDown(self):
        self.host.close()
        self.temp.cleanup()

    def emit(self, event):
        with self.lock:
            self.events.append(event)
        if event.get('event') == 'buffer_request':
            self.host.buffer_reply({'request_id': event['request_id'], 'result': self.context})

    def operation(self, op, mode='popup', **kwargs):
        return self.host.operation(dict(op=op, root=self.root, mode=mode, **kwargs))

    def wait(self, predicate):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for fixture event')

    def test_stream_resume_and_cross_mode_isolation(self):
        result = self.operation('ask', question='hello', context=self.context)
        thread_id = result['thread_id']
        self.wait(lambda: any(e.get('event') == 'answer_done' for e in self.events))
        self.assertEqual('Fixture answer: hello', self.operation('reopen')['text'])
        pane = self.operation('open', 'pane')
        self.assertNotEqual(thread_id, pane['thread_id'])
        self.assertEqual('resume', pane['argv'][-2])
        self.assertEqual(pane['thread_id'], pane['argv'][-1])
        self.assertIn('CODEX_HOME=' + str(self.host.client_home), pane['argv'])
        self.assertEqual(thread_id, self.operation('open')['thread_id'])
        self.host.close()
        self.host = Host(self.config, self.emit)
        self.assertEqual(thread_id, self.operation('open')['thread_id'])
        self.assertEqual('Fixture answer: hello', self.operation('reopen')['text'])
        self.assertEqual(pane['thread_id'], self.operation('open', 'pane')['thread_id'])

    def test_stop_and_reject_session_switch_during_turn(self):
        result = self.operation('ask', question='SLOW', context=self.context)
        thread_id = result['thread_id']
        self.wait(lambda: bool(self.host.turns.get(thread_id)))
        with self.assertRaisesRegex(ValueError, 'Stop'):
            self.operation('new')
        with self.assertRaisesRegex(ValueError, 'already running'):
            self.operation('ask', question='second', context=self.context)
        self.operation('stop')
        self.wait(lambda: thread_id not in self.host.turns)
        self.assertIn('Response stopped', self.operation('reopen')['text'])
        self.assertNotEqual(thread_id, self.operation('new')['thread_id'])

    def test_native_ide_roundtrip_reads_fresh_buffer(self):
        self.operation('open')
        self.context['lines'][0] = 'fresh unsaved'
        request = {'type': 'request', 'requestId': 'test', 'version': 0,
                   'method': 'ide-context', 'params': {'workspaceRoot': self.root}}
        with socket.socket(socket.AF_UNIX) as stream:
            stream.connect(str(self.host.client_home / 'ipc/ipc.sock'))
            payload = json.dumps(request).encode()
            stream.sendall(struct.pack('<I', len(payload)) + payload)
            length = struct.unpack('<I', socket_exact(stream, 4))[0]
            response = json.loads(socket_exact(stream, length))
        self.assertEqual('success', response['resultType'])
        self.assertIn('fresh unsaved', response['result']['ideContext']['activeFile']['activeSelectionContent'])

    def test_mcp_authentication_rejects_wrong_token(self):
        with socket.create_connection(self.host.buffer_server.server_address) as stream:
            stream.sendall(b'{"token":"wrong","operation":"list_buffers"}\n')
            reply = json.loads(stream.makefile('rb').readline())
        self.assertIn('Invalid Vim connection token', reply['error'])
        self.assertFalse(any(e.get('event') == 'buffer_request' for e in self.events))

    def test_mcp_stdio_initialization_and_tool_discovery(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        ]
        result = subprocess.run([sys.executable, self.config['script'], 'mcp'],
            input='\n'.join(json.dumps(x) for x in requests) + '\n', text=True, capture_output=True, check=True)
        replies = [json.loads(x) for x in result.stdout.splitlines()]
        self.assertEqual(2, len(replies))
        self.assertEqual('2024-11-05', replies[0]['result']['protocolVersion'])
        self.assertEqual({'editor_context', 'read_buffer', 'edit_buffer', 'save_buffer', 'list_buffers'},
                         {t['name'] for t in replies[1]['result']['tools']})

    def external_thread(self, cwd=None, source='cli', title='CLI conversation', updated=100, parent=None):
        self.host.start_server()
        return self.host.rpc.call('thread/start', {
            'cwd': cwd or self.root, 'fixtureSource': source,
            'fixtureName': title, 'fixtureUpdated': updated, 'fixtureParent': parent,
        })['thread']['id']

    def test_existing_cli_sessions_are_listed_and_can_be_adopted(self):
        original = self.external_thread(title='Existing terminal conversation')
        self.assertIsNone(self.host.catalog.get(original))
        pane_before = self.host.catalog.current(self.root, 'pane')
        listed = self.operation('list', 'pane')
        row = next(t for t in listed if t['id'] == original)
        self.assertEqual('CLI', row['source'])
        self.assertEqual('Existing terminal conversation', row['title'])
        self.assertEqual(pane_before, self.host.catalog.current(self.root, 'pane'))
        self.assertIsNone(self.host.catalog.get(original), 'Browsing must not import or select sessions')
        resumed = self.operation('select', 'pane', thread_id=original)
        self.assertEqual(original, resumed['thread_id'], 'Must resume the original ID, not fork')
        self.assertEqual(original, self.host.catalog.current(self.root, 'pane'))
        self.assertEqual('Existing terminal conversation', self.host.catalog.get(original)['title'])
        self.assertEqual(original, self.operation('open', 'pane')['thread_id'])

    def test_cli_listing_paginates_and_filters_project_and_popup(self):
        child = Path(self.root) / 'src'
        child.mkdir()
        other = Path(self.root) / 'other-project'
        (other / '.git').mkdir(parents=True)
        expected = {self.external_thread(updated=1), self.external_thread(cwd=str(child), updated=2),
                    self.external_thread(source='vscode', updated=3),
                    self.external_thread(source='exec', updated=4)}
        foreign = self.external_thread(cwd=str(other), updated=5)
        subagent = self.external_thread(parent='parent', updated=6)
        popup = self.operation('open')['thread_id']
        pane = self.operation('open', 'pane')['thread_id']
        listed = self.operation('list', 'pane')
        self.assertEqual(expected | {pane}, {t['id'] for t in listed})
        self.assertNotIn(foreign, {t['id'] for t in listed})
        self.assertNotIn(subagent, {t['id'] for t in listed})
        self.assertNotIn(popup, {t['id'] for t in listed})
        self.assertEqual([popup], [t['id'] for t in self.operation('list')])
        self.assertEqual(len(listed), len({t['id'] for t in listed}), 'No duplicated catalog entries')

    def test_external_selection_revalidates_project_and_mode(self):
        other = Path(self.root) / 'other-project'
        (other / '.git').mkdir(parents=True)
        foreign = self.external_thread(cwd=str(other))
        with self.assertRaisesRegex(ValueError, 'project'):
            self.operation('select', 'pane', thread_id=foreign)
        original = self.external_thread()
        with self.assertRaisesRegex(ValueError, 'mode'):
            self.operation('select', 'popup', thread_id=original)
        popup = self.operation('open')['thread_id']
        with self.assertRaisesRegex(ValueError, 'mode'):
            self.operation('select', 'pane', thread_id=popup)

    def test_provided_context_is_an_immutable_question_snapshot(self):
        self.operation('ask', question='hello', context=self.context)
        self.wait(lambda: any(e.get('event') == 'context_provided' for e in self.events))
        record = next(e for e in self.events if e.get('event') == 'context_provided')
        self.assertEqual('popup', record['channel'])
        self.assertNotIn('lines', record['context'])
        self.assertEqual(3, record['context']['line_count'])
        self.context['lines'][0] = 'new user edits'
        self.context['selection']['start']['line'] = 99
        self.assertIn('before\n선택\nafter', record['text'])
        self.assertEqual(2, record['context']['selection']['start']['line'])

    def test_permission_requests_wait_for_the_user_instead_of_auto_declining(self):
        replies = []
        self.host.rpc = SimpleNamespace(ws=SimpleNamespace(send=replies.append), close=lambda: None)
        self.host.server_request({'id': 'approval-1', 'method': 'item/commandExecution/requestApproval',
                                  'params': {'command': 'read global skill helper'}})
        self.wait(lambda: any(e.get('event') == 'approval_request' for e in self.events))
        record = next(e for e in self.events if e.get('event') == 'approval_request')
        self.assertEqual([], replies)
        self.host.interactive_reply({'request_id': record['request_id'], 'decision': 'accept'})
        self.wait(lambda: bool(replies))
        self.assertEqual({'id': 'approval-1', 'result': {'decision': 'accept'}}, replies[0])


if __name__ == '__main__':
    unittest.main()
