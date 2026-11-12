"""Initialize a real Claude CLI without submitting a user/model turn."""
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pythonx'))
from vim_codex.claude import ClaudeHost, ClaudeStream

executable = shutil.which(os.environ.get('CLAUDE_EXECUTABLE', 'claude'))
if not executable:
    raise SystemExit('Install Claude Code or set CLAUDE_EXECUTABLE to its path')

with tempfile.TemporaryDirectory(prefix='vim-claude-native-') as directory:
    root = Path(directory)
    os.environ['CLAUDE_CONFIG_DIR'] = str(root / 'home')
    skill = root / 'home/skills/vim-smoke-skill/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: vim-smoke-skill\ndescription: Offline Vim smoke test skill.\n---\n'
                     'This is a discovery fixture; never invoke a model.\n', encoding='utf-8')
    host = ClaudeHost({'claude': executable, 'python': sys.executable,
        'script': str(Path(__file__).resolve().parents[1] / 'pythonx/codex_bridge.py'),
        'state_dir': str(root / 'state')}, lambda event: None)
    try:
        sid = host.ensure_thread(directory, 'popup')
        stream = ClaudeStream(host, directory, sid)
        host.streams[sid] = stream
        assert stream.ready.wait(45), 'Claude CLI initialize timed out'
        assert not stream.init_error, stream.init_error
        assert not stream.closed, 'Claude CLI exited during initialization'
        commands = stream.initialize_response.get('commands', [])
        assert any(c.get('name') == 'vim-smoke-skill' for c in commands), 'Global skill was not discovered'
        assert not stream.awaiting_result, 'Smoke test must not submit a user turn'
        print('Native Claude CLI initialize and global skill discovery: passed (no model turn)')
    finally:
        host.close()
