#!/usr/bin/env python3
"""Offline Claude CLI fixture: stream control, real MCP subprocesses and hooks."""
import json
import codecs
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

args = sys.argv[1:]


def option(name, default=None):
    return args[args.index(name) + 1] if name in args else default


sid = option('--resume') or option('--session-id') or str(uuid.uuid4())
mcp = json.loads(Path(option('--mcp-config')).read_text())['mcpServers']['vim_buffers']
native = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects' / 'fixture' / (sid + '.jsonl')
native.parent.mkdir(parents=True, exist_ok=True)
with native.open('a') as f:
    f.write(json.dumps({'type': 'user', 'cwd': os.getcwd(), 'sessionId': sid,
                        'message': {'content': 'Fixture conversation'}}) + '\n')


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def tool(name, arguments):
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
         'params': {'name': name, 'arguments': arguments}}]
    result = subprocess.run([mcp['command']] + mcp['args'],
        env=dict(os.environ, **mcp['env']), input='\n'.join(json.dumps(r) for r in requests) + '\n',
        capture_output=True, text=True, check=True, timeout=20)
    reply = json.loads(result.stdout.splitlines()[-1])['result']
    if reply.get('isError'):
        raise RuntimeError(reply['content'][0]['text'])
    return json.loads(reply['content'][0]['text'])


def finish(text):
    emit({'type': 'stream_event', 'event': {'type': 'content_block_start',
          'index': 0, 'content_block': {'type': 'text', 'text': ''}}})
    emit({'type': 'stream_event', 'event': {'type': 'content_block_delta',
          'index': 0, 'delta': {'type': 'text_delta', 'text': text[:8]}}})
    emit({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}})
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text, 'session_id': sid})


pending = {}
if '--print' in args:
    for line in sys.stdin:
        request = json.loads(line)
        if request['type'] == 'control_request':
            subtype = request['request']['subtype']
            if subtype == 'initialize':
                time.sleep(float(os.environ.get('VIM_CLAUDE_TEST_INIT_DELAY', 0)))
            emit({'type': 'control_response', 'response': {'subtype': 'success',
                  'request_id': request['request_id'], 'response': {}}})
            if subtype == 'interrupt':
                emit({'type': 'result', 'subtype': 'success', 'result': ''})
        elif request['type'] == 'user':
            text = request['message']['content']
            question = text.rsplit('## User question\n', 1)[-1]
            if question == 'SLOW':
                emit({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Waiting'}]}})
            elif question == 'ASK_USER':
                raw_id = str(uuid.uuid4())
                pending[raw_id] = question
                emit({'type': 'control_request', 'request_id': raw_id, 'request': {
                    'subtype': 'can_use_tool', 'tool_name': 'AskUserQuestion', 'input': {
                        'questions': [{'question': 'Choose a path', 'header': 'Path',
                            'multiSelect': False, 'options': [{'label': 'A', 'description': 'First'},
                                                            {'label': 'B', 'description': 'Second'}]}]}}})
            elif question in ('REQUEST_APPROVAL', 'SAVE_BUFFER', 'CANCEL_APPROVAL'):
                raw_id = str(uuid.uuid4())
                pending[raw_id] = question
                emit({'type': 'control_request', 'request_id': raw_id, 'request': {
                    'subtype': 'can_use_tool', 'tool_name': 'mcp__vim_buffers__save_buffer'
                        if question == 'SAVE_BUFFER' else 'Bash',
                    'input': {'command': 'echo test'},
                    'permission_suggestions': [{'type': 'addRules', 'rules': [{'toolName': 'Bash',
                        'ruleContent': 'echo test'}], 'behavior': 'allow', 'destination': 'session'}]}})
                if question == 'CANCEL_APPROVAL':
                    emit({'type': 'control_cancel_request', 'request_id': raw_id})
                    finish('Cancelled permission')
            elif question == 'EDIT_BUFFER':
                context = tool('editor_context', {})
                current = tool('read_buffer', {'buffer_id': context['buffer_id']})
                tool('edit_buffer', {'buffer_id': current['buffer_id'],
                    'expected_tick': current['changedtick'], 'start_line': 1, 'end_line': 1,
                    'lines': ['edited by Claude fixture']})
                finish('Modified buffer')
            elif question == 'LONG_ANSWER':
                finish('\n'.join('Claude answer line ' + str(n) for n in range(1, 101)))
            elif question == 'FAIL':
                emit({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                      'errors': ['Fixture failure']})
            elif question == 'CRASH':
                print('Fixture CLI crashed', file=sys.stderr, flush=True)
                raise SystemExit(1)
            else:
                finish('Claude answer: ' + question)
        elif request['type'] == 'control_response':
            reply = request['response']
            question = pending.pop(reply['request_id'], None)
            if question == 'CANCEL_APPROVAL':
                raise RuntimeError('The host replied to a cancelled control request')
            allowed = reply.get('response', {}).get('behavior') == 'allow'
            if question == 'ASK_USER':
                answer = reply.get('response', {}).get('updatedInput', {}).get('answers', {}).get('Choose a path', '')
                finish('Question answer: ' + answer if allowed else 'Question cancelled')
            elif question == 'SAVE_BUFFER' and allowed:
                context = tool('editor_context', {})
                current = tool('read_buffer', {'buffer_id': context['buffer_id']})
                saved = tool('save_buffer', {'buffer_id': current['buffer_id'],
                    'expected_tick': current['changedtick'], 'reason': 'Run the verifier'})
                finish('Saved buffer' if saved.get('saved') else 'Save failed')
            else:
                finish('Permission accepted' if allowed else 'Permission declined')
else:
    settings = json.loads(Path(option('--settings')).read_text())

    def hook(event, **fields):
        command = settings['hooks'][event][0]['hooks'][0]['command']
        result = subprocess.run(command, shell=True, input=json.dumps(dict(
            hook_event_name=event, session_id=sid, cwd=os.getcwd(), **fields)),
            text=True, capture_output=True, timeout=20)
        if result.returncode:
            print(result.stderr, flush=True)
            return {}
        return json.loads(result.stdout)

    def query(question):
        result = hook('UserPromptSubmit', prompt=question)
        context = result.get('hookSpecificOutput', {}).get('additionalContext', '')
        print('\r\nClaude pane question: ' + question + '\r\nContext received: ' + str(bool(context)), flush=True)
        if question == 'REQUEST_APPROVAL':
            result = hook('PermissionRequest', tool_name='Bash', tool_input={'command': 'echo test'})
            decision = result.get('hookSpecificOutput', {}).get('decision', {}).get('behavior')
            print('\r\nPermission ' + (decision or 'native'), flush=True)
        hook('Stop')
        print('\r\nClaude pane idle', flush=True)

    hook('SessionStart')
    import tty
    tty.setraw(sys.stdin.fileno())
    print('\x1b[?2004hClaude pane ready', flush=True)
    if '--' in args:
        query(args[args.index('--') + 1])
    buffer = ''
    pasted = False
    decoder = codecs.getincrementaldecoder('utf-8')()
    while True:
        char = decoder.decode(os.read(sys.stdin.fileno(), 1))
        buffer += char
        if buffer.endswith('\x1b[200~'):
            pasted = True
        elif buffer.endswith('\x1b[201~'):
            pasted = False
        elif char == '\r' and not pasted:
            query(re.sub(r'\x1b\[(?:200|201)~', '', buffer[:-1]))
            buffer = ''
