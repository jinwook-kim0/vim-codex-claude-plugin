.PHONY: test test-python test-vim test-tmux test-native test-claude-native
PYTHON ?= python3
VIM_BIN ?= vim

test: test-python test-vim

test-python:
	PYTHONPATH=pythonx $(PYTHON) -m unittest discover -s tests/python -v

test-vim:
	VIM_CODEX_TEST_PYTHON=$(PYTHON) $(VIM_BIN) -Nu NONE -i NONE -n -es -S tests/vim/buffers.vim
	VIM_CODEX_TEST_PYTHON=$(PYTHON) $(VIM_BIN) -Nu NONE -i NONE -n -es -S tests/vim/ui.vim
	VIM_CODEX_TEST_PYTHON=$(PYTHON) $(VIM_BIN) -Nu NONE -i NONE -n -es -S tests/vim/claude.vim

test-tmux:
	VIM_CODEX_TEST_VIM=$(VIM_BIN) VIM_CODEX_TEST_PYTHON=$(PYTHON) $(PYTHON) tests/tmux_smoke.py
	VIM_CODEX_TEST_VIM=$(VIM_BIN) VIM_CODEX_TEST_PYTHON=$(PYTHON) $(PYTHON) tests/tmux_smoke.py claude

test-native:
	$(PYTHON) tests/native_smoke.py

test-claude-native:
	$(PYTHON) tests/claude_native_smoke.py
