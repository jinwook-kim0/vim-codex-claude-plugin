"""Claude Code's native terminal and bidirectional print-mode transport.

Only stdlib APIs available in Python 3.9 are used. Authentication, managed
settings, skills, MCP servers and permissions belong to the native CLI.
"""
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

from .bridge import Host, INSTRUCTIONS, context_text, context_metadata, project_root


INSTRUCTIONS = INSTRUCTIONS.replace('Codex', 'Claude') + """
Authorization for Vim MCP tools follows Claude's current permissions. Invoke
save_buffer only when a next step needs the edited buffer on disk. Do not
replace a failed save by writing the same open file through another tool.
"""


def normalize_session_id(value):
    """Session IDs are also filenames; never accept arbitrary path components."""
    return str(uuid.UUID(value))


def private_json(path, value):
    with path.open('w', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=False)


class ClaudeStream:
    """One persistent SDK control channel per popup conversation."""
    def __init__(self, host, root, thread_id):
        self.host, self.root, self.thread_id = host, root, thread_id
        self.write_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.ready = threading.Event()
        self.init_error = None
        self.initialize_response = {}
        self.closed = False
        self.stopped = False
        self.blocks = {}
        self.completed = []
        self.awaiting_result = False
        self.init_id = secrets.token_hex(12)
        self.log_path = host.runtime / (thread_id + '.stderr')
        argv = host.argv(root, thread_id) + [
            '--print', '--verbose', '--output-format', 'stream-json',
            '--input-format', 'stream-json', '--include-partial-messages',
            '--permission-prompt-tool', 'stdio']
        with self.log_path.open('wb') as stderr:
            self.process = subprocess.Popen(argv, cwd=root, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=stderr, encoding='utf-8',
                bufsize=1, start_new_session=True)
        threading.Thread(target=self.read, daemon=True).start()
        try:
            self.send({'type': 'control_request', 'request_id': self.init_id,
                       'request': {'subtype': 'initialize', 'hooks': {}}})
        except OSError:
            self.close()
            raise

    def send(self, value):
        with self.write_lock:
            if self.closed:
                raise RuntimeError('Claude stream is closed; ask again to resume')
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
            self.process.stdin.flush()

    def ask(self, text):
        self.blocks, self.completed = {}, []
        self.awaiting_result = True
        deadline = time.monotonic() + 60
        while not self.ready.wait(.1):
            if self.stopped:
                self.finish('[Response stopped]', 'interrupted')
                return False
            if time.monotonic() > deadline:
                raise TimeoutError('Claude initialization timed out; check CLI login and MCP configuration')
        if self.stopped:
            self.finish('[Response stopped]', 'interrupted')
            return False
        if self.init_error:
            raise RuntimeError(self.init_error)
        self.send({'type': 'user', 'session_id': self.thread_id,
                   'parent_tool_use_id': None,
                   'message': {'role': 'user', 'content': text}})
        return True

    def text(self):
        partial = ''.join(self.blocks[key] for key in sorted(self.blocks))
        return '\n\n'.join(self.completed + ([partial] if partial else []))

    def event(self, event, **fields):
        self.host.emit(dict(event=event, root=self.root, mode='popup',
                            thread_id=self.thread_id, **fields))

    def finish(self, text, status):
        if not self.awaiting_result:
            return
        self.awaiting_result = False
        self.host.catalog.update(self.thread_id, answer=text)
        self.host.turns.pop(self.thread_id, None)
        self.event('answer_done', text=text, status=status)

    def read(self):
        error = None
        try:
            for line in self.process.stdout:
                if not line.strip():
                    continue
                message = json.loads(line)
                kind = message.get('type')
                if kind == 'control_response':
                    response = message.get('response', {})
                    if response.get('request_id') == self.init_id:
                        self.initialize_response = response.get('response', {})
                        if response.get('subtype') == 'error':
                            self.init_error = response.get('error', 'Claude initialization failed')
                        self.ready.set()
                elif kind == 'control_request':
                    self.control_request(message)
                elif kind == 'control_cancel_request':
                    self.cancel_request(message.get('request_id'))
                elif message.get('parent_tool_use_id'):
                    # Subagent messages must not replace the main answer.
                    continue
                elif kind == 'stream_event':
                    event = message.get('event', {})
                    index = event.get('index', 0)
                    if event.get('type') == 'message_start':
                        self.blocks = {}
                    elif event.get('type') == 'content_block_start':
                        block = event.get('content_block', {})
                        if block.get('type') == 'text':
                            self.blocks[index] = block.get('text', '')
                    elif (event.get('type') == 'content_block_delta'
                          and event.get('delta', {}).get('type') == 'text_delta'):
                        self.blocks[index] = self.blocks.get(index, '') + event['delta'].get('text', '')
                        self.event('answer', text=self.text())
                elif kind == 'assistant':
                    content = message.get('message', {}).get('content', [])
                    text = '\n'.join(b.get('text', '') for b in content if b.get('type') == 'text')
                    if text:
                        self.completed.append(text)
                    self.blocks = {}
                    self.event('answer', text=self.text())
                elif kind == 'result':
                    self.cancel_all()
                    text = message.get('result') or self.text()
                    if message.get('is_error') or message.get('subtype', 'success') != 'success':
                        details = '\n'.join(str(e) for e in message.get('errors', []))
                        text = '\n\n'.join(t for t in (text, details) if t) or 'Claude request failed'
                        status = 'failed'
                    else:
                        status = 'completed'
                    if self.stopped:
                        text = (text or '') + '\n\n[Response stopped]'
                        status = 'interrupted'
                    self.finish(text or '[No text response]', status)
        except Exception as exc:
            error = str(exc)
        finally:
            self.closed = True
            self.cancel_all()
            if not self.host.stopping:
                details = self.log_path.read_text(encoding='utf-8', errors='replace')[-2000:]
                self.init_error = error or details or 'Claude CLI exited before returning a result'
                self.finish((self.text() + '\n\n' + self.init_error).strip(), 'failed')
            self.ready.set()

    def control_request(self, message):
        raw_id = message['request_id']
        nonce = secrets.token_hex(12)
        event, slot = threading.Event(), {}
        with self.pending_lock:
            if raw_id in self.pending:
                return
            self.pending[raw_id] = (nonce, event, slot)
        with self.host.buffer_lock:
            self.host.interactive_pending[nonce] = (event, slot)
        request = message.get('request', {})
        tool = request.get('tool_name', '')
        params = request.get('input', {})
        is_question = tool == 'AskUserQuestion'
        if request.get('subtype') != 'can_use_tool':
            slot['error'] = 'Unsupported Claude control request: ' + str(request.get('subtype'))
            event.set()
        else:
            questions = [dict(q, id=str(i)) for i, q in enumerate(params.get('questions', []))]
            self.host.emit({'event': 'input_request' if is_question else 'approval_request',
                'request_id': nonce, 'thread_id': self.thread_id,
                'method': 'claude/can_use_tool', 'native': False,
                'allow_session': bool(request.get('permission_suggestions')),
                'params': {'questions': questions} if is_question else {
                    'reason': tool, 'command': json.dumps(params, ensure_ascii=False)}})
        threading.Thread(target=self.control_reply,
                         args=(raw_id, request, nonce, event, slot), daemon=True).start()

    def control_reply(self, raw_id, request, nonce, event, slot):
        try:
            event.wait()
            if slot.get('cancelled') or self.closed:
                return
            params = request.get('input', {})
            decision = slot.get('decision', 'decline')
            if request.get('tool_name') == 'AskUserQuestion' and 'answers' in slot:
                answers = {q['question']: ', '.join(slot['answers'].get(str(i), {}).get('answers', []))
                           for i, q in enumerate(params.get('questions', []))}
                result = ({'behavior': 'allow', 'updatedInput': dict(params, answers=answers)}
                          if answers and all(value.strip() for value in answers.values()) else
                          {'behavior': 'deny', 'message': 'Question cancelled in Vim'})
            elif decision in ('accept', 'acceptForSession'):
                result = {'behavior': 'allow', 'updatedInput': params}
                if decision == 'acceptForSession':
                    result['updatedPermissions'] = [dict(update, destination='session')
                        for update in request.get('permission_suggestions', [])]
            else:
                result = {'behavior': 'deny', 'message': 'Declined in Vim'}
            response = {'subtype': 'success', 'request_id': raw_id, 'response': result}
            if 'error' in slot:
                response = {'subtype': 'error', 'request_id': raw_id, 'error': slot['error']}
            self.send({'type': 'control_response', 'response': response})
        except (OSError, RuntimeError, ValueError):
            pass
        finally:
            with self.pending_lock:
                self.pending.pop(raw_id, None)
            with self.host.buffer_lock:
                self.host.interactive_pending.pop(nonce, None)
            self.host.emit({'event': 'interactive_resolved', 'request_id': nonce})

    def cancel_request(self, raw_id):
        with self.pending_lock:
            pending = self.pending.get(raw_id)
            if pending:
                nonce, event, slot = pending
                slot['cancelled'] = True
                event.set()
                self.host.emit({'event': 'interactive_resolved', 'request_id': nonce})

    def cancel_all(self):
        with self.pending_lock:
            pending_ids = list(self.pending)
        for raw_id in pending_ids:
            self.cancel_request(raw_id)

    def stop(self):
        self.stopped = True
        self.cancel_all()
        if self.ready.is_set() and not self.closed:
            self.send({'type': 'control_request', 'request_id': secrets.token_hex(12),
                       'request': {'subtype': 'interrupt'}})

    def close(self):
        self.closed = True
        self.cancel_all()
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


class ClaudeHost(Host):
    def __init__(self, config, emit):
        config = dict(config, provider='claude', state_dir=str(Path(config['state_dir']) / 'claude'))
        super().__init__(config, emit)
        self.streams = {}
        self.keys = {}
        self.pane_state = {}
        self.pane_questions = {}
        self.claude_home = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))).expanduser()

    def native_sessions(self, root):
        result = {}
        for path in (self.claude_home / 'projects').glob('*/*.jsonl'):
            try:
                sid = normalize_session_id(path.stem)
                cwd, title, custom_title = None, '', ''
                with path.open(encoding='utf-8') as stream:
                    for line in stream:
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue  # A currently running CLI can have a partial last record.
                        if not isinstance(item, dict):
                            continue
                        if item.get('isSidechain'):
                            continue
                        if item.get('cwd'):
                            cwd = item['cwd']
                        if item.get('type') == 'custom-title':
                            custom_title = item.get('customTitle', '')
                        if item.get('type') == 'summary':
                            title = item.get('summary') or title
                        if not title and item.get('type') == 'user':
                            content = item.get('message', {}).get('content', '')
                            if isinstance(content, list):
                                content = '\n'.join(b.get('text', '') for b in content
                                                    if isinstance(b, dict) and b.get('type') == 'text')
                            if isinstance(content, str) and not content.startswith('<'):
                                title = content
                if project_root(cwd) != root:
                    continue
                result[sid] = {'id': sid, 'root': root, 'mode': 'pane',
                    'title': ' '.join((custom_title or title).split())[:160],
                    'updated': path.stat().st_mtime, 'answer': '', 'source': 'CLI'}
            except (OSError, ValueError, TypeError):
                continue
        return result

    def persisted(self, thread_id):
        return any((self.claude_home / 'projects').glob('*/' + thread_id + '.jsonl'))

    def sessions(self, root, mode):
        cached = {row['id']: dict(row, source='Vim') for row in self.catalog.sessions(root, mode)}
        if mode == 'pane':
            for sid, row in self.native_sessions(root).items():
                known = self.catalog.get(sid)
                if known and (known['root'], known['mode']) != (root, mode):
                    continue
                if sid not in cached:
                    cached[sid] = row
                else:
                    cached[sid]['updated'] = max(cached[sid]['updated'], row['updated'])
        return sorted(cached.values(), key=lambda row: row['updated'], reverse=True)

    def ensure_thread(self, root, mode, force=False, session_id=None):
        current = session_id or (None if force else self.catalog.current(root, mode))
        if current:
            current = normalize_session_id(current)
            known = self.catalog.get(current)
            if known:
                if (known['root'], known['mode']) != (root, mode):
                    raise ValueError('Session does not belong to this project/mode')
            elif mode != 'pane' or current not in self.native_sessions(root):
                raise ValueError('Session does not belong to this project')
        else:
            current = str(uuid.uuid4())
        self.loaded[current] = (root, mode)
        self.catalog.add(root, mode, current)
        if current not in self.keys:
            key = self.keys[current] = secrets.token_hex(24)
            self.tool_sessions[key] = (root, current)
            self.write_config(root, current, key)
        return current

    def write_config(self, root, thread_id, key):
        env = {'VIM_CODEX_PORT': str(self.buffer_server.server_address[1]),
               'VIM_CODEX_TOKEN': self.token, 'VIM_CODEX_ROOT': root,
               'VIM_CODEX_SESSION': key, 'VIM_CODEX_PROVIDER': 'claude'}
        private_json(self.runtime / (thread_id + '.mcp.json'), {'mcpServers': {'vim_buffers': {
            'type': 'stdio', 'command': self.config['python'],
            'args': [self.config['script'], 'mcp'], 'env': env}}})
        hook_argv = ['env'] + [k + '=' + v for k, v in env.items()] + [
            self.config['python'], self.config['script'], 'claude-hook']
        hooks = {name: [{'hooks': [{'type': 'command', 'command': shlex.join(hook_argv),
                                  'timeout': 600 if name == 'PermissionRequest' else 30}]}]
                 for name in ('SessionStart', 'UserPromptSubmit', 'Stop', 'PermissionRequest', 'PostToolUse')}
        private_json(self.runtime / (thread_id + '.settings.json'), {'hooks': hooks})

    def argv(self, root, thread_id, pane=False):
        argv = [self.config['claude'], '--resume' if self.persisted(thread_id) else '--session-id', thread_id,
                '--append-system-prompt', INSTRUCTIONS,
                '--mcp-config', str(self.runtime / (thread_id + '.mcp.json'))]
        if pane:
            argv += ['--settings', str(self.runtime / (thread_id + '.settings.json'))]
        if self.config.get('model'):
            argv += ['--model', self.config['model']]
        if self.config.get('permission_mode'):
            argv += ['--permission-mode', self.config['permission_mode']]
        return argv + self.config.get('extra_args', [])

    def pane_info(self, root, thread_id, question=''):
        argv = self.argv(root, thread_id, pane=True)
        if question:
            argv += ['--', question]
        # tmux's server environment can predate Vim (custom endpoints, login,
        # CLAUDE_CONFIG_DIR). A private launch file carries Vim's environment
        # without placing credentials in a shell command or tmux option.
        path = self.runtime / (thread_id + '.launch.json')
        private_json(path, {'argv': argv, 'cwd': root, 'env': dict(os.environ)})
        return {'thread_id': thread_id, 'argv': [self.config['python'], self.config['script'],
                'claude-pane', str(path)], 'paste_text': question}

    def operation(self, message):
        if self.stopping:
            raise RuntimeError('Vim host is shutting down')
        op, mode = message['op'], message.get('mode', 'pane')
        root = os.path.realpath(message.get('root', os.getcwd()))
        if mode not in ('pane', 'popup'):
            raise ValueError('Invalid session mode')
        if 'context' in message:
            self.contexts[root] = message['context']
        if op == 'list':
            return self.sessions(root, mode)
        current = self.catalog.current(root, mode)
        if op == 'reopen':
            record = self.catalog.get(current)
            if not record or not record['answer']:
                raise ValueError('No saved answer for this popup session')
            return {'thread_id': current, 'text': record['answer']}
        if op == 'stop':
            if current in self.turns and current in self.streams:
                self.streams[current].stop()
            return {}
        if op == 'pane_ask_failed':
            self.pane_questions.pop(current, None)
            self.pane_state[current] = 'ready'
            return {}
        with self.server_lock:
            lock = self.operation_locks.setdefault((root, mode), threading.Lock())
        with lock:
            current = self.catalog.current(root, mode)
            if op in ('new', 'select') and current in self.turns:
                raise ValueError('Stop the current response before switching sessions')
            if (op in ('new', 'select') and mode == 'pane' and message.get('pane_alive')
                    and self.pane_state.get(current) in ('busy', 'permission')):
                raise ValueError('Claude pane is busy; finish or interrupt its response before switching sessions')
            thread_id = self.ensure_thread(root, mode, force=op == 'new', session_id=message.get('thread_id'))
            if op in ('new', 'select') and current != thread_id and current in self.streams:
                self.streams.pop(current).close()
            if op == 'ask':
                question = message.get('question', '').strip()
                if not question:
                    raise ValueError('Question must not be empty')
                if any(ord(c) < 32 and c not in '\n\t' for c in question):
                    raise ValueError('Question contains terminal control characters')
                if mode == 'pane':
                    if message.get('pane_alive') and self.pane_state.get(thread_id) != 'ready':
                        raise ValueError('Claude pane is starting, busy, or awaiting permission; finish it before asking again')
                    self.pane_questions[thread_id] = (question, message['context'])
                    self.pane_state[thread_id] = 'busy'
                    self.catalog.update(thread_id, title=question[:160])
                    # The native UserPromptSubmit hook supplies the captured context.
                    path = self.runtime / (thread_id + '.paste')
                    path.write_text(question, encoding='utf-8')
                    os.chmod(path, 0o600)
                    return dict(self.pane_info(root, thread_id, question), paste_path=str(path))
                if thread_id in self.turns:
                    raise ValueError('A response is already running; stop it or wait')
                stream = self.streams.get(thread_id)
                if not stream or stream.closed:
                    if stream:
                        stream.close()
                    stream = self.streams[thread_id] = ClaudeStream(self, root, thread_id)
                stream.stopped = False
                self.turns[thread_id] = 'running'
                self.emit({'event': 'answer_start', 'root': root, 'mode': mode,
                           'thread_id': thread_id, 'context': context_metadata(message['context'])})
                try:
                    submitted = stream.ask(INSTRUCTIONS + '\n' + context_text(message['context'])
                                           + '\n\n## User question\n' + question)
                except Exception:
                    self.turns.pop(thread_id, None)
                    stream.close()
                    self.emit({'event': 'answer_done', 'root': root, 'mode': mode,
                               'thread_id': thread_id, 'text': '[Failed to start Claude response]', 'status': 'failed'})
                    raise
                self.catalog.update(thread_id, title=question[:160])
                if submitted:
                    self.context_provided(root, mode, message['context'], thread_id)
            return self.pane_info(root, thread_id) if mode == 'pane' else {'thread_id': thread_id}

    def tool_request(self, operation, arguments, root, session_key):
        session = self.tool_sessions.get(session_key)
        if not session or session[0] != root:
            raise ValueError('Request is not bound to a Claude session')
        if operation == '_claude_hook':
            return self.hook(arguments, root, session[1])
        return super().tool_request(operation, arguments, root, session_key)

    def hook(self, data, root, thread_id):
        event = data.get('hook_event_name')
        if event in ('SessionStart', 'Stop'):
            self.pane_state[thread_id] = 'ready'
        elif event == 'PostToolUse':
            self.pane_state[thread_id] = 'busy'
        elif event == 'UserPromptSubmit':
            self.pane_state[thread_id] = 'busy'
            pending = self.pane_questions.pop(thread_id, None)
            context = (pending[1] if pending and pending[0] == data.get('prompt') else
                       self.buffer_request('editor_context', {}, root))
            self.context_provided(root, 'pane', context, thread_id)
            return {'hookSpecificOutput': {'hookEventName': event,
                                           'additionalContext': INSTRUCTIONS + '\n' + context_text(context)}}
        elif event == 'PermissionRequest':
            self.pane_state[thread_id] = 'permission'
            nonce = secrets.token_hex(12)
            ready, slot = threading.Event(), {}
            with self.buffer_lock:
                self.interactive_pending[nonce] = (ready, slot)
            try:
                self.emit({'event': 'approval_request', 'request_id': nonce,
                    'thread_id': thread_id, 'native': True, 'defer_to_native': True,
                    'allow_session': False, 'method': 'claude/PermissionRequest',
                    'params': {'reason': data.get('tool_name', ''),
                               'command': json.dumps(data.get('tool_input', {}), ensure_ascii=False)}})
                if not ready.wait(570):
                    return {}  # Let Claude show its own native permission prompt.
                decision = slot.get('decision', 'decline')
                if decision == 'native':
                    return {}
                result = {'behavior': 'allow'} if decision == 'accept' else {
                    'behavior': 'deny', 'message': 'Declined in Vim'}
                return {'hookSpecificOutput': {'hookEventName': event, 'decision': result}}
            finally:
                with self.buffer_lock:
                    self.interactive_pending.pop(nonce, None)
                self.emit({'event': 'interactive_resolved', 'request_id': nonce})
        return {}

    def save_buffer(self, arguments, root, session_key):
        # Claude invokes this only after its native MCP permission decision.
        # An allow rule for edit_buffer never grants save_buffer permission.
        session = self.tool_sessions.get(session_key)
        if not session or session[0] != root:
            raise ValueError('Save request is not bound to a Claude session')
        if type(arguments.get('expected_tick')) is not int or not str(arguments.get('reason', '')).strip():
            raise ValueError('Saving requires expected_tick and a reason')
        info = self.buffer_request('read_buffer', {k: arguments[k] for k in ('buffer_id', 'path') if k in arguments}, root)
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
            return self.buffer_request('save_buffer', {'buffer_id': info['buffer_id'],
                'expected_tick': arguments['expected_tick'], 'authorized_path': path}, root)

    def close(self):
        self.stopping = True
        for stream in list(self.streams.values()):
            stream.close()
        super().close()


def hook_main():
    """A per-process hook, added with --settings without editing user settings."""
    data = json.load(sys.stdin)
    request = {'token': os.environ['VIM_CODEX_TOKEN'], 'root': os.environ['VIM_CODEX_ROOT'],
               'session_key': os.environ['VIM_CODEX_SESSION'],
               'operation': '_claude_hook', 'arguments': data}
    try:
        with socket.create_connection(('127.0.0.1', int(os.environ['VIM_CODEX_PORT'])), timeout=600) as stream:
            stream.sendall((json.dumps(request) + '\n').encode())
            reply = json.loads(stream.makefile('rb').readline(16 * 1024 * 1024))
        if 'error' in reply:
            raise RuntimeError(reply['error'])
        print(json.dumps(reply['result'], ensure_ascii=False))
    except Exception:
        if data.get('hook_event_name') == 'UserPromptSubmit':
            # Do not silently send a question without its promised Vim context.
            print('Vim context is unavailable. Reopen the Claude pane from Vim.', file=sys.stderr)
            raise SystemExit(2)
        print('{}')  # Permission hooks fall back to Claude's own prompt.


def pane_main(path):
    with open(path, encoding='utf-8') as stream:
        launch = json.load(stream)
    env = dict(launch['env'])
    # The actual pane owns its terminal and tmux identity.
    for name in ('TMUX', 'TMUX_PANE', 'TERM', 'TERM_PROGRAM', 'TERM_PROGRAM_VERSION', 'LINES', 'COLUMNS'):
        env.pop(name, None)
        if name in os.environ:
            env[name] = os.environ[name]
    os.chdir(launch['cwd'])
    os.execvpe(launch['argv'][0], launch['argv'], env)
