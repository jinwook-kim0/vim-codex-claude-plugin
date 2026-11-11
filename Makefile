.PHONY: test test-python test-vim test-tmux test-native

test: test-python test-vim

test-python:
	PYTHONPATH=pythonx python3 -m unittest discover -s tests/python -v

test-vim:
	vim -Nu NONE -i NONE -n -es -S tests/vim/buffers.vim
	vim -Nu NONE -i NONE -n -es -S tests/vim/ui.vim

test-tmux:
	python3 tests/tmux_smoke.py

test-native:
	python3 tests/native_smoke.py
