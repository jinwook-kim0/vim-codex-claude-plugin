#!/usr/bin/env python3
"""Entry point kept outside the package for Vim job_start()."""
import sys

if sys.version_info < (3, 9):
    raise SystemExit('vim-codex-claude requires Python 3.9 or later')

from vim_codex.bridge import main

if __name__ == '__main__':
    main()
