#!/usr/bin/env python3
"""Deterministic Codex protocol fixture; never contacts a model or network API."""
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import socketserver
import struct
import subprocess
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pythonx'))
from vim_codex.transport import WebSocket


class Handler(socketserver.BaseRequestHandler):
    def setup(self):
        data = b''
        while not data.endswith(b'\r\n\r\n'):
            data += self.request.recv(1)
        key = next(line.split(b':', 1)[1].strip() for line in data.split(b'\r\n')
                   if line.lower().startswith(b'sec-websocket-key:'))
        accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        self.request.sendall(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n'
                             b'Connection: Upgrade\r\nSec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        self.ws = object.__new__(WebSocket)
        self.ws.sock = self.request
        self.ws.lock = threading.Lock()
        self.threads = {}
        if os.environ.get('VIM_CODEX_TEST_EXTERNAL_ROOT'):
            self.threads['11111111-1111-4111-8111-111111111111'] = {
                'cwd': os.environ['VIM_CODEX_TEST_EXTERNAL_ROOT'],
                'fixtureSource': 'cli', 'fixtureName': 'Existing CLI conversation',
                'fixtureUpdated': 100,
            }
        self.interrupted = set()
        self.approvals = {}

    def metadata(self, thread_id):
        p = self.threads[thread_id]
        return {'id': thread_id, 'cwd': p.get('cwd'),
                'name': p.get('fixtureName'), 'preview': p.get('fixturePreview', ''),
                'source': p.get('fixtureSource', 'appServer'),
                'updatedAt': p.get('fixtureUpdated', 1),
                'parentThreadId': p.get('fixtureParent')}

    def send(self, value):
        data = json.dumps(value).encode()
        if len(data) < 126:
            header = bytes([0x81, len(data)])
        else:
            header = b'\x81\x7e' + struct.pack('!H', len(data))
        with self.ws.lock:
            self.request.sendall(header + data)

    def notify(self, method, params):
        self.send({'method': method, 'params': params})

    def tool(self, thread_id, name, arguments):
        config = self.threads[thread_id]['config'].get('mcp_servers.vim_buffers')
        env = dict(os.environ, **config['env'])
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2024-11-05'}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}}]
        p = subprocess.run([config['command'], *config['args']], env=env, text=True,
                           input='\n'.join(json.dumps(x) for x in requests) + '\n',
                           capture_output=True, timeout=10, check=True)
        result = json.loads(p.stdout.splitlines()[-1])['result']
        if result.get('isError'):
            raise RuntimeError(result)
        return json.loads(result['content'][0]['text'])

    def answer(self, thread_id, turn_id, text):
        try:
            self.notify('turn/started', {'threadId': thread_id, 'turn': {'id': turn_id}})
            if 'SLOW' in text:
                return
            if 'SAVE_BUFFER' in text or 'SAVE_CONFIRM' in text:
                buffer_id = int(re.search(r'buffer=(\d+)', text).group(1))
                read = self.tool(thread_id, 'read_buffer', {'buffer_id': buffer_id})
                confirm = 'SAVE_CONFIRM' in text
                edited = self.tool(thread_id, 'edit_buffer', {'buffer_id': buffer_id,
                    'expected_tick': read['changedtick'], 'start_line': 1, 'end_line': 1,
                    'lines': ['unsaved validation edit' if confirm else 'saved by fixture']})
                if confirm:
                    self.threads[thread_id]['sandbox'] = 'read-only'
                saved = self.tool(thread_id, 'save_buffer', {'buffer_id': buffer_id,
                    'expected_tick': edited['changedtick'], 'reason': 'Save before running validation'})
                if not saved.get('saved'):
                    raise RuntimeError('Save did not finish; validation was not run')
                answer = 'Saved for validation'
            elif 'REQUEST_APPROVAL' in text or 'NATIVE_RESOLVE' in text:
                request_id = 'permission-' + turn_id
                event, slot = threading.Event(), {'threadId': thread_id}
                self.approvals[request_id] = (event, slot)
                self.send({'id': request_id, 'method': 'item/commandExecution/requestApproval',
                           'params': {'threadId': thread_id, 'command': 'fixture skill helper',
                                      'reason': 'Skill helper needs permission'}})
                if 'NATIVE_RESOLVE' in text and not event.wait(.5):
                    # Stand in for the other native CLI client answering first.
                    slot['result'] = {'decision': 'accept'}
                    self.notify('serverRequest/resolved', {'requestId': request_id, 'threadId': thread_id})
                    event.set()
                if not event.wait(10):
                    raise TimeoutError('Permission fixture timed out')
                answer = 'Permission accepted' if slot.get('result', {}).get('decision') == 'accept' else 'Permission declined'
            elif 'EDIT_BUFFER' in text:
                read = self.tool(thread_id, 'read_buffer', {})
                edit = self.tool(thread_id, 'edit_buffer', {
                    'buffer_id': read['buffer_id'], 'expected_tick': read['changedtick'],
                    'start_line': 1, 'end_line': 1, 'lines': ['edited by Codex fixture']})
                answer = 'Modified buffer #' + str(edit['buffer_id'])
            elif 'LONG_ANSWER' in text:
                answer = '\n'.join('Answer line ' + str(i) for i in range(1, 101))
            else:
                answer = 'Fixture answer: ' + text.split('## User question\n')[-1]
            for chunk in [answer[:10], answer[10:]]:
                self.notify('item/agentMessage/delta', {'threadId': thread_id, 'turnId': turn_id,
                    'itemId': 'answer-' + turn_id, 'delta': chunk})
                time.sleep(.02)
            self.notify('item/completed', {'threadId': thread_id, 'turnId': turn_id,
                'item': {'id': 'answer-' + turn_id, 'type': 'agentMessage', 'text': answer}})
            self.notify('turn/completed', {'threadId': thread_id, 'turn': {'id': turn_id, 'status': 'completed'}})
        except Exception as error:
            self.notify('turn/completed', {'threadId': thread_id, 'turn': {
                'id': turn_id, 'status': 'failed', 'error': {'message': str(error)}}})

    def handle(self):
        try:
            while True:
                message = self.ws.receive()
                if 'id' not in message:
                    continue
                if 'method' not in message:
                    pending = self.approvals.get(message['id'])
                    if pending:
                        pending[1].update(message)
                        self.notify('serverRequest/resolved', {'requestId': message['id'],
                                    'threadId': pending[1]['threadId']})
                        pending[0].set()
                    continue
                method, params = message['method'], message.get('params', {})
                after = None
                if method == 'initialize':
                    result = {'userAgent': 'fixture'}
                elif method in ('thread/start', 'thread/resume'):
                    thread_id = params.get('threadId', str(uuid.uuid4()))
                    self.threads.setdefault(thread_id, {}).update(params)
                    p = self.threads[thread_id]
                    mode = p.get('sandbox', 'workspace-write')
                    result = {'thread': {'id': thread_id}, 'cwd': p.get('cwd'),
                        'sandbox': {'type': {'read-only': 'readOnly', 'workspace-write': 'workspaceWrite',
                                             'danger-full-access': 'dangerFullAccess'}[mode]},
                        'approvalPolicy': p.get('approvalPolicy', 'on-request'), 'approvalsReviewer': 'user'}
                elif method == 'command/exec':
                    result = {'exitCode': 0, 'stdout': json.dumps({'allowed': True,
                              'path': params['command'][-1]}), 'stderr': ''}
                elif method == 'thread/name/set':
                    self.threads[params['threadId']]['fixtureName'] = params['name']
                    result = {}
                elif method == 'thread/list':
                    threads = [self.metadata(tid) for tid in self.threads]
                    threads = [t for t in threads if t['source'] in params.get('sourceKinds', [t['source']])]
                    threads.sort(key=lambda t: t['updatedAt'], reverse=True)
                    offset = int(params.get('cursor') or 0)
                    end = offset + min(params.get('limit', 100), 2)
                    result = {'data': threads[offset:end], 'nextCursor': str(end) if end < len(threads) else None}
                elif method == 'thread/read':
                    if params['threadId'] not in self.threads:
                        self.send({'id': message['id'], 'error': {'code': -32600, 'message': 'Thread not found'}})
                        continue
                    result = {'thread': self.metadata(params['threadId'])}
                elif method == 'turn/start':
                    turn_id = str(uuid.uuid4())
                    result = {'turn': {'id': turn_id, 'status': 'inProgress'}}
                    after = (params['threadId'], turn_id, params['input'][0]['text'])
                elif method == 'turn/interrupt':
                    result = {}
                    self.notify('turn/completed', {'threadId': params['threadId'],
                        'turn': {'id': params['turnId'], 'status': 'interrupted'}})
                else:
                    result = {}
                self.send({'id': message['id'], 'result': result})
                if after:
                    threading.Thread(target=self.answer, args=after, daemon=True).start()
        except (EOFError, OSError):
            pass


if __name__ == '__main__':
    if 'app-server' in sys.argv:
        endpoint = sys.argv[sys.argv.index('--listen') + 1][7:]
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
        with Server(endpoint, Handler) as server:
            server.serve_forever()
    else:
        # A stand-in terminal client. Its argv is checked by Vim/tmux tests.
        print('Codex fixture terminal client', flush=True)
        signal.pause()
