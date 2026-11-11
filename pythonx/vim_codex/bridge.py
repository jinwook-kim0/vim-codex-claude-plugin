"""Per-Vim Codex host, session catalog, IDE provider, and buffer MCP server.

The app-server uses the user's normal Codex home/authentication. Only the remote
TUI client gets a private CODEX_HOME, so /ide cannot capture another editor.
No model request is made until the user submits a question.
"""
import argparse
import concurrent.futures
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import signal
import shutil
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time

from .transport import RPC
from .permissions import WRITE_PROBE

INSTRUCTIONS = """You are connected to Vim. Use list_buffers to distinguish loaded source
buffers (including hidden buffers) from files that are not open in Vim.
For an open buffer, its current contents are authoritative, including unsaved
edits. Use read_buffer and edit_buffer with a fresh changedtick; never bypass
the buffer by changing its disk file. Stale edits must be recalculated.
For a new file or a file not open in Vim, use normal Codex filesystem/shell
tools under the session's current permissions. Do not load it into Vim merely
to read, create, or modify it. Buffer tools deliberately reject unopened files.
When a next step such as testing requires an edited open buffer on disk, call
save_buffer with its current changedtick and explain the reason. It saves
automatically if current permissions allow; otherwise it requests permission
when the policy permits asking. Wait for successful save before running tests;
if save is declined, blocked, or stale, do not claim validation used the edit.
Unnamed buffers need a filename chosen by the user before saving.
Global skills, shell tools, and the user's configured MCP tools remain available.
Do not change permission settings yourself. List edited files/buffers and any
saves in your answer. For CLI questions without inline context, call editor_context first.
"""


def context_text(context):
    """Keep explicit coordinates even when Codex's /ide renderer drops ranges."""
    meta = {k: v for k, v in context.items() if k not in ('lines', 'selected_text')}
    result = '# Vim editor context\n' + json.dumps(meta, ensure_ascii=False) + '\n'
    if context.get('selection'):
        result += '\nSelected text:\n' + context.get('selected_text', '') + '\n'
    if not context.get('selection') or not context.get('on_disk'):
        result += '\nCurrent buffer contents:\n' + '\n'.join(context.get('lines', []))
    return result


def context_metadata(context):
    metadata = copy.deepcopy({k: v for k, v in context.items() if k not in ('lines', 'selected_text')})
    metadata['line_count'] = len(context.get('lines', []))
    return metadata


def project_root(cwd):
    """Match CLI sessions started in a repository's subdirectories as well."""
    if not cwd:
        return None
    directory = Path(os.path.realpath(cwd))
    for candidate in (directory, *directory.parents):
        if (candidate / '.git').exists():
            return str(candidate)
    return str(directory)


def ide_context(context):
    if not context:
        return {'activeFile': None, 'openTabs': []}
    pos = context.get('cursor', {'line': 1, 'column': 1})
    selection = context.get('selection') or {'start': pos, 'end': pos}
    # Prompt coordinates below remain explicitly 1-based byte columns. Native
    # positions serve as a fallback; exact metadata travels in the content too.
    def native(p):
        return {'line': max(0, p['line'] - 1),
                'character': max(0, p.get('utf16_column', p['column']) - 1)}
    path = context.get('path') or context['buffer_uri']
    return {'activeFile': {
        'label': context.get('name') or '[No Name]', 'path': path,
        'selection': {'start': native(selection['start']), 'end': native(selection['end'])},
        'selections': [], 'activeSelectionContent': context_text(context),
    }, 'openTabs': [{'label': context.get('name') or '[No Name]', 'path': path}]}


class Catalog:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, root TEXT NOT NULL, mode TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
                    answer TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS selected (
                    root TEXT, mode TEXT, id TEXT, PRIMARY KEY (root, mode));
            ''')
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def current(self, root, mode):
        with self.connect() as db:
            row = db.execute('SELECT id FROM selected WHERE root=? AND mode=?', (root, mode)).fetchone()
            return row['id'] if row else None

    def choose(self, root, mode, session_id):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO selected VALUES (?,?,?)', (root, mode, session_id))

    def add(self, root, mode, session_id):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO sessions (id,root,mode,updated) VALUES (?,?,?,?)',
                       (session_id, root, mode, time.time()))
        self.choose(root, mode, session_id)

    def get(self, session_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            return dict(row) if row else None

    def update(self, session_id, **fields):
        if not fields or not set(fields) <= {'title', 'answer'}:
            return
        with self.connect() as db:
            db.execute('UPDATE sessions SET ' + ','.join(k + '=?' for k in fields)
                       + ',updated=? WHERE id=?', (*fields.values(), time.time(), session_id))

    def discard_empty(self, session_id):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE id=? AND title='' AND answer=''", (session_id,))
            db.execute('DELETE FROM selected WHERE id=?', (session_id,))

    def sessions(self, root, mode):
        with self.connect() as db:
            return [dict(r) for r in db.execute(
                'SELECT * FROM sessions WHERE root=? AND mode=? ORDER BY updated DESC', (root, mode))]


def tool(name, description, properties, required=()):
    return {'name': name, 'description': description,
            'inputSchema': {'type': 'object', 'properties': properties,
                            'required': list(required), 'additionalProperties': False},
            'annotations': {'readOnlyHint': name not in ('edit_buffer', 'save_buffer'),
                            'destructiveHint': False, 'openWorldHint': False}}


TARGET = {'buffer_id': {'type': 'integer', 'description': 'Vim buffer number, when known.'},
          'path': {'type': 'string', 'description': 'Absolute or workspace-relative path of an already loaded Vim buffer.'}}
TOOLS = [
    tool('editor_context', 'Read current Vim editor context, cursor, selection, and unsaved contents.', {}),
    tool('list_buffers', 'List accessible source buffers and their changedticks in the connected Vim.', {}),
    tool('read_buffer', 'Read an already loaded Vim buffer, including unsaved edits. '
         'Never opens files. For an unopened file, use normal Codex file tools. '
         'Use returned changedtick for edits and saves.', TARGET),
    tool('edit_buffer', 'Replace a 1-based inclusive line range in a Vim buffer without saving. '
         'Insert before start_line by setting end_line=start_line-1. Use a fresh read changedtick. '
         'Changes are undoable in Vim. Never retry stale edits without re-reading.',
         {**TARGET, 'expected_tick': {'type': 'integer'},
          'start_line': {'type': 'integer', 'minimum': 1},
          'end_line': {'type': 'integer', 'minimum': 0},
          'lines': {'type': 'array', 'items': {'type': 'string'}}},
         ('expected_tick', 'start_line', 'end_line', 'lines')),
    tool('save_buffer', 'Save a named, already loaded Vim buffer when a next step needs it on disk. '
         'Checks live Codex write permissions; requests approval only if needed and permitted. '
         'Rejects stale buffers or changed disk files. Wait for success before testing.',
         {**TARGET, 'expected_tick': {'type': 'integer'},
          'reason': {'type': 'string', 'minLength': 1}}, ('expected_tick', 'reason')),
]


class ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class BufferHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.request.settimeout(35)
        try:
            data = json.loads(self.rfile.readline(8 * 1024 * 1024))
            if not secrets.compare_digest(str(data.get('token', '')), self.server.host.token):
                raise ValueError('Invalid Vim connection token')
            if data['operation'] == 'save_buffer':
                reply = self.server.host.save_buffer(data.get('arguments', {}), data['root'], data.get('session_key'))
            else:
                reply = self.server.host.buffer_request(data['operation'], data.get('arguments', {}), data['root'])
            value = {'result': reply}
        except Exception as error:
            value = {'error': str(error)}
        self.wfile.write((json.dumps(value, ensure_ascii=False) + '\n').encode())
        if 'result' in value and data['operation'] in ('editor_context', 'read_buffer'):
            self.server.host.context_provided(data['root'], 'tool', reply,
                                              text=json.dumps(reply, ensure_ascii=False))


def socket_exact(stream, length):
    data = bytearray()
    while len(data) < length:
        chunk = stream.recv(length - len(data))
        if not chunk:
            raise EOFError('Incomplete IDE request')
        data.extend(chunk)
    return bytes(data)


class IDEHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            length = struct.unpack('<I', socket_exact(self.request, 4))[0]
            if length > 1024 * 1024:
                return
            message = json.loads(socket_exact(self.request, length))
            root = os.path.realpath(message.get('params', {}).get('workspaceRoot', ''))
            if message.get('method') != 'ide-context':
                response = {'resultType': 'error', 'error': 'no-handler-for-request'}
            else:
                # Read on demand so direct typing in the CLI sees fresh buffers.
                context = self.server.host.buffer_request('editor_context', {}, root)
                response = {'resultType': 'success', 'result': {'ideContext': ide_context(context)}}
            response.update(type='response', requestId=message.get('requestId'), method='ide-context')
            payload = json.dumps(response, ensure_ascii=False).encode()
            self.request.sendall(struct.pack('<I', len(payload)) + payload)
            if response['resultType'] == 'success':
                self.server.host.context_provided(root, 'ide', context)
        except (OSError, ValueError, EOFError, TimeoutError, RuntimeError):
            return


class ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class Host:
    def __init__(self, config, emit):
        self.config = config
        self.emit = emit
        self.runtime = Path(tempfile.mkdtemp(prefix='vim-codex-'))
        self.catalog = Catalog(Path(config['state_dir']) / 'sessions.sqlite3')
        self.token = secrets.token_hex(32)
        self.buffer_pending = {}
        self.buffer_lock = threading.Lock()
        self.interactive_pending = {}
        self.tool_sessions = {}
        self.save_grants = {}
        self.save_locks = {}
        self.server_lock = threading.Lock()
        self.operation_locks = {}
        self.contexts = {}
        self.loaded = {}
        self.turns = {}
        self.messages = {}
        self.rpc = None
        self.process = None
        self.stopping = False
        self.buffer_server = ThreadingTCPServer(('127.0.0.1', 0), BufferHandler)
        self.buffer_server.host = self
        threading.Thread(target=self.buffer_server.serve_forever, daemon=True).start()
        self.client_home = Path(config['state_dir']) / 'clients' / self.runtime.name
        (self.client_home / 'ipc').mkdir(parents=True, mode=0o700)
        user_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser().resolve()
        for name in ('skills', 'plugins', 'rules', 'AGENTS.md', 'AGENTS.override.md', 'instructions.md'):
            if (user_home / name).exists():
                (self.client_home / name).symlink_to(user_home / name)
        self.ide_server = ThreadingUnixServer(str(self.client_home / 'ipc/ipc.sock'), IDEHandler)
        self.ide_server.host = self
        threading.Thread(target=self.ide_server.serve_forever, daemon=True).start()

    def context_provided(self, root, channel, context, thread_id=None, text=None):
        metadata = context_metadata(context)
        metadata.setdefault('root', root)
        self.emit({'event': 'context_provided', 'root': root, 'channel': channel,
                   'context': metadata, 'text': context_text(context) if text is None else text,
                   'thread_id': thread_id, 'at': time.time()})

    def buffer_request(self, operation, arguments, root):
        request_id = secrets.token_hex(12)
        event, slot = threading.Event(), {}
        with self.buffer_lock:
            self.buffer_pending[request_id] = (event, slot)
        try:
            self.emit({'event': 'buffer_request', 'request_id': request_id,
                       'operation': operation, 'arguments': arguments, 'root': root})
            if not event.wait(25):
                raise TimeoutError('Vim did not answer the buffer request (is Vim busy?)')
            if 'error' in slot:
                raise RuntimeError(slot['error'])
            return slot['result']
        finally:
            with self.buffer_lock:
                self.buffer_pending.pop(request_id, None)

    def buffer_reply(self, message):
        with self.buffer_lock:
            pending = self.buffer_pending.get(message['request_id'])
            if pending:
                pending[1].update(message)
                pending[0].set()

    def start_server(self):
        with self.server_lock:
            if self.rpc:
                return
            endpoint = self.runtime / 'server.sock'
            log = self.runtime / 'server.log'
            argv = [self.config['codex'], 'app-server', '--listen', 'unix://' + str(endpoint)]
            # Retain normal skill, shell, plugin and MCP availability. Source
            # edits still go through Vim; permission requests are shown in Vim.
            overrides = {key: value for key, value in {
                'sandbox_mode': self.config.get('sandbox_mode'),
                'approval_policy': self.config.get('approval_policy')}.items() if value}
            for key, value in overrides.items():
                argv += ['-c', key + '=' + json.dumps(value)]
            argv += self.config.get('extra_args', [])
            env = os.environ.copy()
            for key in ('CODEX_THREAD_ID', 'CODEX_SESSION_ID', 'CODEX_TOOL_CALL_ID',
                        'CODEX_PERMISSION_PROFILE', 'CODEX_CI'):
                env.pop(key, None)
            with log.open('wb') as stderr:
                self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=stderr, env=env)
            deadline = time.monotonic() + 15
            while not endpoint.exists():
                if self.process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Codex app-server failed to start: ' + log.read_text()[-3000:])
                time.sleep(.03)
            self.rpc = RPC(endpoint, self.notification, self.server_request)
            self.rpc.call('initialize', {
                'clientInfo': {'name': 'vim_codex', 'version': '0.1.0'},
                'capabilities': {'experimentalApi': True}})
            self.rpc.ws.send({'method': 'initialized'})

    def server_request(self, message):
        threading.Thread(target=self.interactive_request, args=(message,), daemon=True).start()

    def interactive_request(self, message):
        method = message['method']
        if not (method.endswith('/requestApproval') or method == 'tool/requestUserInput'):
            self.rpc.ws.send({'id': message['id'], 'error': {
                'code': -32601, 'message': 'Unsupported Vim host request: ' + method}})
            return
        request_id = secrets.token_hex(12)
        event, slot = threading.Event(), {}
        with self.buffer_lock:
            self.interactive_pending[request_id] = (event, slot)
        try:
            self.emit({'event': 'input_request' if method == 'tool/requestUserInput' else 'approval_request',
                       'request_id': request_id, 'method': method, 'params': message.get('params', {})})
            event.wait()
            decision = slot.get('decision', 'decline')
        finally:
            with self.buffer_lock:
                self.interactive_pending.pop(request_id, None)
        if method == 'item/permissions/requestApproval':
            result = {'permissions': message.get('params', {}).get('permissions', {})
                      if decision in ('accept', 'acceptForSession') else {},
                      'scope': 'session' if decision == 'acceptForSession' else 'turn'}
        elif method.endswith('/requestApproval'):
            result = {'decision': decision}
        else:
            result = {'answers': slot.get('answers', {})}
        if not self.stopping:
            self.rpc.ws.send({'id': message['id'], 'result': result})

    def interactive_reply(self, message):
        with self.buffer_lock:
            pending = self.interactive_pending.get(message['request_id'])
            if pending:
                pending[1].update(message)
                pending[0].set()

    def current_permissions(self, thread_id):
        # Read live thread settings without applying startup overrides. This
        # observes /permissions changes made in the native CLI after opening.
        reply = self.rpc.call('thread/resume', {'threadId': thread_id, 'excludeTurns': True})
        if 'sandbox' not in reply or 'approvalPolicy' not in reply:
            raise RuntimeError('Codex did not return the current save permissions')
        return {key: reply.get(key) for key in
                ('sandbox', 'approvalPolicy', 'activePermissionProfile', 'approvalsReviewer', 'cwd')}

    def write_allowed(self, path, permissions):
        profile = permissions.get('activePermissionProfile') or {}
        custom_profile = bool(profile.get('id') and not profile['id'].startswith(':'))
        if permissions['sandbox'].get('type') == 'readOnly' and not custom_profile:
            return False
        params = {'command': [self.config['python'], '-c', WRITE_PROBE, path],
                  'cwd': permissions['cwd'], 'timeoutMs': 15000}
        if custom_profile:
            params['permissionProfile'] = profile['id']
        else:
            params['sandboxPolicy'] = permissions['sandbox']
        result = self.rpc.call('command/exec', params)
        if result.get('exitCode') != 0:
            raise RuntimeError('Could not check Codex write permission: ' + result.get('stderr', '')[-1500:])
        probe = json.loads(result['stdout'])
        if probe['path'] != path:
            raise RuntimeError('Save target changed during the permission check')
        if not probe['allowed'] and not probe.get('permission_denied'):
            raise RuntimeError('Cannot save this path: ' + probe.get('message', 'write probe failed'))
        return probe['allowed']

    def request_save_permission(self, path, reason):
        request_id = secrets.token_hex(12)
        event, slot = threading.Event(), {}
        with self.buffer_lock:
            self.interactive_pending[request_id] = (event, slot)
        try:
            self.emit({'event': 'approval_request', 'request_id': request_id,
                       'method': 'vim/save_buffer',
                       'params': {'reason': reason, 'command': 'Save Vim buffer: ' + path}})
            event.wait()
            return slot.get('decision', 'decline')
        finally:
            with self.buffer_lock:
                self.interactive_pending.pop(request_id, None)

    def save_buffer(self, arguments, root, session_key):
        session = self.tool_sessions.get(session_key)
        if not session or session[0] != root:
            raise ValueError('Save request is not bound to a Codex session')
        if type(arguments.get('expected_tick')) is not int or not isinstance(arguments.get('reason'), str) or not arguments['reason'].strip():
            raise ValueError('Saving requires expected_tick and a reason')
        target = {key: arguments[key] for key in ('buffer_id', 'path') if key in arguments}
        info = self.buffer_request('read_buffer', target, root)
        if info['changedtick'] != arguments['expected_tick']:
            raise ValueError('Stale buffer: read it again before saving')
        if info.get('disk_changed'):
            raise ValueError('File changed on disk; reconcile before saving')
        if not info.get('path'):
            raise ValueError('Unnamed buffer: ask the user to choose a filename before saving')
        if not info.get('modified', True) and info.get('on_disk'):
            return dict(info, saved=True, already_saved=True)
        path = os.path.realpath(info['path'])
        with self.server_lock:
            lock = self.save_locks.setdefault(path, threading.Lock())
        with lock:
            permissions = self.current_permissions(session[1])
            fingerprint = json.dumps(permissions, sort_keys=True)
            grant_key = (session[1], path)
            if grant_key in self.save_grants and self.save_grants[grant_key] != fingerprint:
                self.save_grants.pop(grant_key)
            granted = self.save_grants.get(grant_key) == fingerprint
            if not granted:
                granted = self.write_allowed(path, permissions)
            decision = None
            if not granted:
                policy = permissions['approvalPolicy']
                may_ask = policy in ('on-request', 'untrusted') if isinstance(policy, str) else bool(
                    policy.get('granular', {}).get('sandbox_approval', False))
                if not may_ask:
                    raise PermissionError('Current Codex permissions deny saving and do not allow a prompt')
                decision = self.request_save_permission(path, arguments['reason'])
                if decision not in ('accept', 'acceptForSession'):
                    raise PermissionError('Buffer save declined; disk is unchanged. Do not validate the unsaved edit.')
            if json.dumps(self.current_permissions(session[1]), sort_keys=True) != fingerprint:
                raise PermissionError('Codex permissions changed while saving; retry under the new settings')
            # Vim rechecks changedtick, disk conflicts, target identity and its
            # own write options, then performs a normal :write with user hooks.
            result = self.buffer_request('save_buffer', {
                'buffer_id': info['buffer_id'], 'expected_tick': arguments['expected_tick'],
                'authorized_path': path}, root)
            if decision == 'acceptForSession':
                self.save_grants[grant_key] = fingerprint
            return result

    def thread_config(self, root, session_key=None):
        server = {
            'command': self.config['python'],
            'args': [self.config['script'], 'mcp'],
            'env': {'VIM_CODEX_PORT': str(self.buffer_server.server_address[1]),
                    'VIM_CODEX_TOKEN': self.token, 'VIM_CODEX_ROOT': root,
                    'VIM_CODEX_SESSION': session_key or ''},
            'enabled': True, 'required': True, 'tool_timeout_sec': 600,
        }
        # A dotted override adds only our server, preserving global MCP config
        # including optional values and credentials without round-tripping it.
        return {'mcp_servers.vim_buffers': server}

    def sessions(self, root, mode):
        cached = {row['id']: row for row in self.catalog.sessions(root, mode)}
        if mode != 'pane':
            return list(cached.values())
        self.start_server()
        cursor = None
        seen_cursors = set()
        while True:
            params = {'limit': 100, 'sortKey': 'updated_at',
                      'sourceKinds': ['cli', 'vscode', 'exec', 'appServer', 'unknown']}
            if cursor:
                params['cursor'] = cursor
            page = self.rpc.call('thread/list', params)
            for thread in page.get('data', []):
                if project_root(thread.get('cwd')) != root or thread.get('parentThreadId'):
                    continue
                session_id = thread['id']
                known = self.catalog.get(session_id)
                # Popup sessions remain independent even though Codex also
                # exposes them in its general saved-thread inventory.
                if known and (known['root'], known['mode']) != (root, mode):
                    continue
                title = thread.get('name') or thread.get('preview') or ''
                if known and known['title'] and title.startswith('Vim pane:'):
                    title = known['title']
                cached[session_id] = {
                    'id': session_id, 'root': root, 'mode': mode,
                    'title': ' '.join(title.split())[:160],
                    'updated': max(thread.get('updatedAt') or 0, known['updated'] if known else 0),
                    'answer': known['answer'] if known else '',
                    'source': 'Vim' if known else {
                        'cli': 'CLI', 'vscode': 'IDE', 'exec': 'Exec',
                    }.get(thread.get('source'), 'Codex'),
                }
            cursor = page.get('nextCursor')
            if not cursor:
                break
            if cursor in seen_cursors:
                raise RuntimeError('Codex returned a repeated session-list cursor')
            seen_cursors.add(cursor)
        return sorted(cached.values(), key=lambda row: row['updated'], reverse=True)

    def ensure_thread(self, root, mode, force=False, session_id=None):
        self.start_server()
        current = session_id or (None if force else self.catalog.current(root, mode))
        if current and current in self.loaded:
            if self.loaded[current] != (root, mode):
                raise ValueError('Session does not belong to this project/mode')
            self.catalog.choose(root, mode, current)
            return current
        session_key = secrets.token_hex(24)
        params = {'cwd': root, 'developerInstructions': INSTRUCTIONS,
                  'config': self.thread_config(root, session_key)}
        if self.config.get('sandbox_mode'):
            params['sandbox'] = self.config['sandbox_mode']
        if self.config.get('approval_policy'):
            params['approvalPolicy'] = self.config['approval_policy']
        if self.config.get('model'):
            params['model'] = self.config['model']
        if current:
            record = self.catalog.get(current)
            imported_title = None
            if record is None and session_id and mode == 'pane':
                thread = self.rpc.call('thread/read', {'threadId': current, 'includeTurns': False})['thread']
                if project_root(thread.get('cwd')) != root or thread.get('parentThreadId'):
                    raise ValueError('Session does not belong to this project')
                imported_title = ' '.join((thread.get('name') or thread.get('preview') or '').split())[:160]
            elif not record or (record['root'], record['mode']) != (root, mode):
                raise ValueError('Session does not belong to this project/mode')
            params.update(threadId=current, excludeTurns=True)
            try:
                response = self.rpc.call('thread/resume', params)
            except RuntimeError as error:
                # Codex does not persist a rollout until the first turn. There
                # is no conversation to recover for an untouched blank thread.
                if record and 'no rollout found' in str(error) and not record['title'] and not record['answer']:
                    self.catalog.discard_empty(current)
                    return self.ensure_thread(root, mode, force=True)
                raise
        else:
            params.update(ephemeral=False)
            response = self.rpc.call('thread/start', params)
        thread_id = response['thread']['id']
        if not current:
            # Starting an empty thread alone does not create a durable rollout.
            # Naming it materializes it without submitting a model turn, which
            # also makes it resumable by the separate native TUI connection.
            self.rpc.call('thread/name/set', {'threadId': thread_id,
                'name': 'Vim ' + mode + ': ' + (Path(root).name or root)})
        self.loaded[thread_id] = (root, mode)
        self.tool_sessions[session_key] = (root, thread_id)
        self.catalog.add(root, mode, thread_id)
        if current and imported_title is not None:
            self.catalog.update(thread_id, title=imported_title)
        return thread_id

    def pane_info(self, root, thread_id):
        # Only the TUI's discovery home is private. Its remote server retains
        # normal auth, model settings, and durable sessions in the real home.
        argv = ['env', '-u', 'CODEX_THREAD_ID', '-u', 'CODEX_SESSION_ID',
                '-u', 'CODEX_TOOL_CALL_ID', '-u', 'CODEX_PERMISSION_PROFILE', '-u', 'CODEX_CI',
                'CODEX_HOME=' + str(self.client_home), self.config['codex'],
                '--remote', 'unix://' + str(self.runtime / 'server.sock'),
                '--cd', root, 'resume', thread_id]
        return {'thread_id': thread_id, 'argv': argv}

    def operation(self, message):
        if self.stopping:
            raise RuntimeError('Vim host is shutting down')
        op = message['op']
        root = os.path.realpath(message.get('root', os.getcwd()))
        mode = message.get('mode', 'pane')
        if mode not in ('pane', 'popup'):
            raise ValueError('Invalid session mode')
        if 'context' in message:
            self.contexts[root] = message['context']
        if op == 'list':
            return self.sessions(root, mode)
        if op == 'reopen':
            record = self.catalog.get(self.catalog.current(root, mode))
            if not record or not record['answer']:
                raise ValueError('No saved answer for this popup session')
            return {'thread_id': record['id'], 'text': record['answer']}
        if op == 'stop':
            thread_id = self.catalog.current(root, mode)
            if thread_id in self.turns:
                self.rpc.call('turn/interrupt', {'threadId': thread_id, 'turnId': self.turns[thread_id]})
            return {}
        key = (root, mode)
        with self.server_lock:
            lock = self.operation_locks.setdefault(key, threading.Lock())
        with lock:
            old_id = self.catalog.current(root, mode)
            if op in ('new', 'select') and old_id in self.turns:
                raise ValueError('Stop the current response before switching sessions')
            thread_id = self.ensure_thread(root, mode, force=op == 'new',
                                           session_id=message.get('thread_id'))
            if op == 'ask':
                if thread_id in self.turns:
                    raise ValueError('A response is already running; stop it or wait')
                question = message.get('question', '').strip()
                if not question:
                    raise ValueError('Question must not be empty')
                text = context_text(message['context']) + '\n\n## User question\n' + question
                self.messages[thread_id] = {}
                self.catalog.update(thread_id, title=question[:160])
                self.emit({'event': 'answer_start', 'root': root, 'mode': mode,
                           'thread_id': thread_id, 'context': context_metadata(message['context'])})
                self.turns[thread_id] = ''
                try:
                    response = self.rpc.call('turn/start', {'threadId': thread_id,
                        'input': [{'type': 'text', 'text': text}]})
                except Exception:
                    self.turns.pop(thread_id, None)
                    self.emit({'event': 'answer_done', 'root': root, 'mode': mode,
                               'thread_id': thread_id, 'text': '[Failed to start response]', 'status': 'failed'})
                    raise
                # Notifications may arrive before the RPC response.
                if self.turns.get(thread_id) == '':
                    self.turns[thread_id] = response['turn']['id']
                self.context_provided(root, mode, message['context'], thread_id)
            return self.pane_info(root, thread_id) if mode == 'pane' else {'thread_id': thread_id}

    def notification(self, method, params):
        thread_id = params.get('threadId')
        if method == 'bridge/disconnected':
            if not self.stopping:
                self.emit({'event': 'error', 'message': params['message']})
            return
        if thread_id not in self.loaded:
            return
        root, mode = self.loaded[thread_id]
        base = {'root': root, 'mode': mode, 'thread_id': thread_id}
        if method == 'turn/started':
            self.turns[thread_id] = params['turn']['id']
            self.messages[thread_id] = {}
        elif method == 'item/agentMessage/delta':
            messages = self.messages.setdefault(thread_id, {})
            item_id = params['itemId']
            messages[item_id] = messages.get(item_id, '') + params.get('delta', '')
            self.emit(dict(base, event='answer', text='\n\n'.join(messages.values())))
        elif method == 'item/completed' and params.get('item', {}).get('type') == 'agentMessage':
            item = params['item']
            self.messages.setdefault(thread_id, {})[item['id']] = item.get('text', '')
        elif method == 'turn/completed':
            text = '\n\n'.join(self.messages.get(thread_id, {}).values())
            turn = params['turn']
            if turn.get('error'):
                text += '\n\nError: ' + turn['error'].get('message', str(turn['error']))
            if turn.get('status') == 'interrupted':
                text += '\n\n[Response stopped]'
            if not text:
                text = '[No text response]'
            self.catalog.update(thread_id, answer=text)
            self.emit(dict(base, event='answer_done', text=text, status=turn.get('status')))
            self.turns.pop(thread_id, None)
        elif method == 'error' and not params.get('willRetry', False):
            error = params.get('error', {})
            self.emit(dict(base, event='error', message=error.get('message', str(error))))

    def close(self):
        self.stopping = True
        with self.buffer_lock:
            for event, slot in self.buffer_pending.values():
                slot['error'] = 'Vim host is shutting down'
                event.set()
            for event, slot in self.interactive_pending.values():
                slot['decision'] = 'decline'
                event.set()
        if self.rpc:
            self.rpc.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.buffer_server.shutdown()
        self.buffer_server.server_close()
        self.ide_server.shutdown()
        self.ide_server.server_close()
        shutil.rmtree(self.runtime, ignore_errors=True)
        shutil.rmtree(self.client_home, ignore_errors=True)


def mcp_main():
    """MCP stdio adapter: the model never receives the private Vim token."""
    for line in sys.stdin:
        message = json.loads(line)
        if 'id' not in message:
            continue
        response = {'jsonrpc': '2.0', 'id': message['id']}
        method = message.get('method')
        if method == 'initialize':
            response['result'] = {'protocolVersion': message.get('params', {}).get('protocolVersion', '2024-11-05'),
                'capabilities': {'tools': {}}, 'serverInfo': {'name': 'vim_buffers', 'version': '0.1.0'}}
        elif method == 'ping':
            response['result'] = {}
        elif method == 'tools/list':
            response['result'] = {'tools': TOOLS}
        elif method == 'tools/call':
            try:
                params = message['params']
                if params['name'] not in {t['name'] for t in TOOLS}:
                    raise ValueError('Unknown buffer tool')
                request = {'token': os.environ['VIM_CODEX_TOKEN'], 'root': os.environ['VIM_CODEX_ROOT'],
                           'session_key': os.environ.get('VIM_CODEX_SESSION'),
                           'operation': params['name'], 'arguments': params.get('arguments', {})}
                timeout = 600 if params['name'] == 'save_buffer' else 30
                with socket.create_connection(('127.0.0.1', int(os.environ['VIM_CODEX_PORT'])), timeout=timeout) as stream:
                    stream.sendall((json.dumps(request) + '\n').encode())
                    reply = json.loads(stream.makefile('rb').readline(16 * 1024 * 1024))
                if 'error' in reply:
                    raise RuntimeError(reply['error'])
                response['result'] = {'content': [{'type': 'text', 'text': json.dumps(reply['result'], ensure_ascii=False)}]}
            except Exception as error:
                response['result'] = {'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}
        else:
            response['error'] = {'code': -32601, 'message': 'Method not found'}
        print(json.dumps(response, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['host', 'mcp'])
    args = parser.parse_args()
    if args.command == 'mcp':
        mcp_main()
        return
    lock = threading.Lock()
    def emit(value):
        with lock:
            print(json.dumps(value, ensure_ascii=False), flush=True)
    host = None
    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
    def execute(message):
        try:
            emit({'id': message['id'], 'result': host.operation(message)})
        except Exception as error:
            emit({'id': message['id'], 'error': str(error)})
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if message['op'] == 'init':
                host = Host(message['config'], emit)
                emit({'id': message['id'], 'result': {'ready': True}})
            elif message['op'] == 'buffer_reply':
                host.buffer_reply(message)
            elif message['op'] == 'interactive_reply':
                host.interactive_reply(message)
            elif message['op'] == 'shutdown':
                break
            else:
                executor.submit(execute, message)
    finally:
        if host:
            host.close()
        executor.shutdown(wait=False, cancel_futures=True)


if __name__ == '__main__':
    main()
