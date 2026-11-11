"""Run the Vim UI suite inside an isolated real tmux server."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

repo = Path(__file__).resolve().parents[1]
name = 'vim-codex-test-' + str(os.getpid())
with tempfile.TemporaryDirectory() as directory:
    done = Path(directory) / 'done'
    log = Path(directory) / 'vim.log'
    command = ['env', 'CODEX_TEST_DONE=' + str(done), 'vim', '-Nu', 'NONE', '-i', 'NONE',
               '-n', '-es', '-V1' + str(log), '--cmd', "let g:codex_test_backend='tmux'",
               '-S', 'tests/vim/ui.vim']
    # Keep a control pane alive after Vim exits, including on failures.
    subprocess.run(['tmux', '-L', name, 'new-session', '-d', '-s', 'test',
                    '-x', '120', '-y', '40', '-c', str(repo), 'sleep 60'], check=True)
    try:
        subprocess.run(['tmux', '-L', name, 'new-window', '-t', 'test', '-c', str(repo),
                        shlex.join(command)], check=True)
        end = time.monotonic() + 25
        while not done.exists() and time.monotonic() < end:
            time.sleep(.1)
        if not done.exists():
            raise SystemExit('tmux integration failed:\n' + (log.read_text() if log.exists() else 'No Vim log'))
        print('tmux integration: passed')
    finally:
        subprocess.run(['tmux', '-L', name, 'kill-server'], check=False)
