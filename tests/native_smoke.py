"""Check real Codex app-server, MCP discovery and CLI /ide, without a model turn."""
import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pythonx'))
from vim_codex.bridge import Host

repo = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='vim-codex-native-test-') as directory:
    root = Path(directory) / 'project'
    root.mkdir()
    (root / '.git').mkdir()
    # Isolate all Codex history and trust settings from the user's real home.
    auth = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'auth.json'
    os.environ['CODEX_HOME'] = str(Path(directory) / 'server-home')
    server_home = Path(os.environ['CODEX_HOME'])
    server_home.mkdir()
    if auth.is_file():
        (server_home / 'auth.json').symlink_to(auth)
    import json
    trust = '[projects.' + json.dumps(str(root)) + ']\ntrust_level = "trusted"\n'
    (server_home / 'config.toml').write_text(trust)
    global_skill = server_home / 'skills/native-global-skill'
    global_skill.mkdir(parents=True)
    (global_skill / 'SKILL.md').write_text('---\nname: native-global-skill\ndescription: A global smoke-test skill.\n---\nRead the current editor context.\n')
    with (server_home / 'config.toml').open('a') as stream:
        stream.write('\n[mcp_servers.existing_user_server]\ncommand = ' + json.dumps(sys.executable)
                     + '\nargs = ' + json.dumps([str(repo / 'pythonx/codex_bridge.py'), 'mcp']) + '\n')
    # A legacy CLI history fixture, written before any plugin/backend starts.
    # It contains a prior user message but never submits a real model request.
    external = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    timestamp = now.isoformat().replace('+00:00', 'Z')
    rollout = server_home / 'sessions' / now.strftime('%Y/%m/%d')
    rollout.mkdir(parents=True)
    rollout = rollout / ('rollout-' + now.strftime('%Y-%m-%dT%H-%M-%S-') + external + '.jsonl')
    previous_question = 'Existing terminal conversation'
    events = [
        {'timestamp': timestamp, 'type': 'session_meta', 'payload': {
            'id': external, 'timestamp': timestamp, 'cwd': str(root),
            'originator': 'codex_cli_rs', 'cli_version': '0.166.0',
            'source': 'cli', 'model_provider': 'openai'}},
        {'timestamp': timestamp, 'type': 'event_msg', 'payload': {
            'type': 'user_message', 'message': previous_question,
            'images': [], 'local_images': [], 'text_elements': []}},
        {'timestamp': timestamp, 'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': previous_question}]}},
    ]
    rollout.write_text(''.join(json.dumps(event) + '\n' for event in events))
    received = []
    context = {'buffer_id': 1, 'buffer_uri': 'vim-buffer://smoke/1', 'path': '',
               'name': '[No Name]', 'lines': ['native IDE unsaved text'], 'on_disk': False,
               'selection': None, 'cursor': {'line': 1, 'column': 1}, 'root': str(root)}
    host = None
    def emit(event):
        if event.get('event') == 'buffer_request':
            received.append(event)
            host.buffer_reply({'request_id': event['request_id'], 'result': context})
    config = {'codex': shutil.which('codex'), 'python': sys.executable,
                 'script': str(repo / 'pythonx/codex_bridge.py'),
                 'state_dir': str(Path(directory) / 'state')}
    host = Host(config, emit)
    (host.client_home / 'config.toml').write_text(trust)
    process = None
    master = None
    try:
        assert host.catalog.get(external) is None
        listed = host.operation({'op': 'list', 'mode': 'pane', 'root': str(root)})
        assert any(row['id'] == external and row['source'] == 'CLI' for row in listed), 'Saved CLI conversation must be discoverable'
        assert host.catalog.current(str(root), 'pane') is None, 'Listing must not select a session'
        result = host.operation({'op': 'select', 'mode': 'pane', 'root': str(root), 'thread_id': external})
        assert result['thread_id'] == external, 'Import must keep the original conversation ID'
        assert (host.client_home / 'skills/native-global-skill/SKILL.md').is_file()
        skills = host.rpc.call('skills/list', {'cwds': [str(root)], 'forceReload': True})
        assert any(skill['name'] == 'native-global-skill' for entry in skills['data']
                   for skill in entry['skills']), 'Global skills must be discoverable'
        history_before = host.rpc.call('thread/turns/list', {'threadId': external})
        status = host.rpc.call('mcpServerStatus/list', {'threadId': result['thread_id']})
        buffer_server = next(s for s in status['data'] if s['name'] == 'vim_buffers')
        assert buffer_server['runtimeStatus'] == 'connected', buffer_server
        assert 'edit_buffer' in buffer_server['tools'], buffer_server
        assert any(s['name'] == 'existing_user_server' and s['runtimeStatus'] == 'connected'
                   for s in status['data']), 'Existing global MCP servers must remain enabled'
        assert not any('features.shell_tool=false' in arg for arg in result['argv'])
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 35, 120, 0, 0))
        process = subprocess.Popen(result['argv'], stdin=slave, stdout=slave, stderr=slave,
                                   cwd=root, env=dict(os.environ, TERM='xterm-256color'))
        os.close(slave)
        deadline = time.monotonic() + 15
        output = bytearray()
        ide_sent = False
        submit_at = None
        started = time.monotonic()
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([master], [], [], .1)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                output.extend(chunk)
                if b'\x1b[6n' in chunk:
                    os.write(master, b'\x1b[1;1R')
            if time.monotonic() - started > 3 and not ide_sent:
                if b'Sign in with' in output:
                    raise AssertionError('Native smoke requires an existing codex login')
                os.write(master, b'/ide on')
                submit_at = time.monotonic() + .3
                ide_sent = True
            if submit_at is not None and time.monotonic() >= submit_at:
                os.write(master, b'\r')
                submit_at = None
            if received and b'IDE context is on' in output:
                break
        if not received:
            import re
            diagnostic = re.sub(r'\x1b\[[0-9;?<>]*[A-Za-z]', '', output[-2000:].decode(errors='replace'))
            raise AssertionError('CLI did not request Vim context:\n' + diagnostic)
        assert any(e['operation'] == 'editor_context' for e in received)
        assert not host.turns, 'The smoke test must not submit a model turn'
        history = host.rpc.call('thread/turns/list', {'threadId': result['thread_id']})
        assert len(history['data']) == len(history_before['data']), 'The smoke test must not create any model turns'
        process.terminate()
        process.wait(timeout=5)
        process = None
        host.close()
        host = Host(config, emit)
        resumed = host.operation({'op': 'open', 'mode': 'pane', 'root': str(root)})
        assert resumed['thread_id'] == result['thread_id'], 'Session must survive a host restart'
        print('Native CLI history, global skill/MCP, /ide and restart/resume: passed (no model turn)')
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if master is not None:
            os.close(master)
        host.close()
