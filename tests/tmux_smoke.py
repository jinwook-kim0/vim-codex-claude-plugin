"""Run the Vim UI suite inside an isolated real tmux server."""
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

repo = Path(__file__).resolve().parents[1]
name = 'vim-codex-test-' + str(os.getpid())
with tempfile.TemporaryDirectory() as directory:
    done = Path(directory) / 'done'
    log = Path(directory) / 'vim.log'
    command = ['env', 'CODEX_TEST_DONE=' + str(done), os.environ.get('VIM_CODEX_TEST_VIM', 'vim'), '-Nu', 'NONE', '-i', 'NONE',
               '-n', '-es', '-V1' + str(log), '--cmd', "let g:codex_test_backend='tmux'",
               '-S', 'tests/vim/' + (sys.argv[1] if len(sys.argv) > 1 else 'ui') + '.vim']
    # Keep a control pane alive after Vim exits, including on failures.
    subprocess.run(['tmux', '-L', name, 'new-session', '-d', '-s', 'test',
                    '-x', '120', '-y', '40', '-c', str(repo), 'sleep 60'], check=True)
    try:
        pane = subprocess.run(['tmux', '-L', name, 'new-window', '-P', '-F', '#{pane_id}',
                              '-t', 'test', '-c', str(repo), shlex.join(command)],
                              check=True, capture_output=True, text=True).stdout.strip()
        end = time.monotonic() + 25
        while not done.exists() and time.monotonic() < end:
            time.sleep(.1)
        if not done.exists():
            screen = subprocess.run(['tmux', '-L', name, 'capture-pane', '-p', '-S', '-', '-t', pane],
                                    capture_output=True, text=True).stdout
            raise SystemExit('tmux integration failed:\n' +
                             (log.read_text() if log.exists() else 'No Vim log') + '\n' + screen)
        print('tmux integration: passed')
    finally:
        subprocess.run(['tmux', '-L', name, 'kill-server'], check=False)
