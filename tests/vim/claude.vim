set nocompatible
set nomore
set encoding=utf-8
set rtp^=.
source tests/vim/helpers.vim
let g:codex_provider = 'claude'
let g:codex_python = empty($VIM_CODEX_TEST_PYTHON) ? 'python3' : $VIM_CODEX_TEST_PYTHON
let g:claude_executable = getcwd() . '/tests/fake_claude.py'
let g:codex_state_dir = tempname()
let $CLAUDE_CONFIG_DIR = g:codex_state_dir . '/native'
let g:codex_pane_backend = get(g:, 'codex_test_backend', 'vim')
runtime plugin/codex.vim
let s:source = bufnr('%')
let s:source_window = win_getid()
call setline(1, ['unsaved original', '선택', 'third'])

function! WaitFor(expression) abort
  let start = reltime()
  while !eval(a:expression) && reltimefloat(reltime(start)) < 12
    sleep 10m
  endwhile
  call assert_true(eval(a:expression), 'Timed out: ' . a:expression)
endfunction

function! PopupText() abort
  let text = []
  for id in PopupList()
    if get(popup_getpos(id), 'visible', 0)
      call extend(text, getbufline(winbufnr(id), 1, '$'))
    endif
  endfor
  return join(text, "\n")
endfunction

function! ClosePopups() abort
  call WaitFor('!Working()')
  for id in PopupList()
    call popup_close(id)
  endfor
endfunction

function! Working() abort
  for id in PopupList()
    if popup_getoptions(id).title =~# 'working'
      return 1
    endif
  endfor
  return 0
endfunction

function! PaneText() abort
  if exists('s:tmux_pane')
    silent let text = system('tmux capture-pane -p -t ' . shellescape(s:tmux_pane))
    return text
  endif
  if exists('s:terminal') && bufexists(s:terminal)
    return join(map(range(1, term_getsize(s:terminal)[0]), 'term_getline(s:terminal, v:val)'), "\n")
  endif
  return ''
endfunction

function! VimFocus() abort
  if exists('s:tmux_pane')
    silent call system('tmux select-pane -t ' . shellescape($TMUX_PANE))
  else
    stopinsert
    call win_gotoid(s:source_window)
  endif
endfunction

function! NativeQuestion(text) abort
  if exists('s:tmux_pane')
    silent call system('tmux send-keys -t ' . shellescape(s:tmux_pane) . ' -l ' . shellescape(a:text))
    silent call system('tmux send-keys -t ' . shellescape(s:tmux_pane) . ' Enter')
  else
    call term_sendkeys(s:terminal, a:text . "\r")
  endif
endfunction

function! OwnedTmuxPanes() abort
  silent let panes = split(system('tmux list-panes -F "#{pane_id}:#{@vim_codex_owner}"'), "\n")
  return filter(panes, 'v:val =~# "^%[0-9]\\+:."')
endfunction

ClaudePopAsk LONG_ANSWER
call WaitFor('PopupText() =~# "Claude answer line 100"')
call WaitFor('popup_getoptions(PopupList()[0]).title !~# "working"')
call assert_true(popup_getoptions(PopupList()[0]).title =~# 'Claude')
let s:popup = PopupList()[0]
call feedkeys('G', 'xt')
call assert_true(popup_getpos(s:popup).firstline > 1)
call feedkeys("\<CR>", 'xt')
call assert_equal('nofile', &buftype)
call assert_equal(0, &modifiable)
call assert_equal(100, line('$'))
call assert_match('^claude-answer://', bufname('%'))
call assert_equal(s:source, codex#Context(0, 0, 0).buffer_id)
hide close

ClaudePopReopen
call WaitFor('PopupText() =~# "Claude answer line 100"')
call feedkeys('q', 'xt')
call assert_equal([], PopupList())

" The same source metadata/selection rules apply to Claude.
call setpos("'<", [0, 2, 1, 0])
call setpos("'>", [0, 2, 7, 0])
2,2ClaudePopAsk selection question
call WaitFor('PopupText() =~# "Claude answer: selection question"')
call ClosePopups()
ClaudeContext!
call assert_match('선택', join(getline(1, '$'), "\n"))
call assert_match('"line": 2', join(getline(1, '$'), "\n"))
call assert_match('unsaved original', join(getline(1, '$'), "\n"))
hide close

ClaudePopAsk EDIT_BUFFER
call WaitFor('PopupText() =~# "Modified buffer"')
call assert_equal('edited by Claude fixture', getbufline(s:source, 1)[0])
call assert_true(getbufvar(s:source, '&modified'))
call ClosePopups()

ClaudePopAsk REQUEST_APPROVAL
call WaitFor('PopupText() =~# "Allow once"')
call feedkeys('j' . "\<CR>", 'xt')
call WaitFor('PopupText() =~# "Permission accepted"')
call ClosePopups()

ClaudePopAsk CANCEL_APPROVAL
call WaitFor('PopupText() =~# "Cancelled permission"')
call assert_false(PopupText() =~# 'Allow once')
call ClosePopups()

ClaudePopAsk SLOW
call WaitFor('PopupText() =~# "Waiting"')
call feedkeys("\<Esc>", 'xt')
ClaudePopStop
sleep 100m
ClaudePopReopen
call WaitFor('PopupText() =~# "Response stopped"')
call ClosePopups()

" Native MCP authorization gates saving; rejection leaves disk unchanged.
let s:file = tempname()
execute 'file ' . fnameescape(s:file)
write
call setline(1, 'unsaved save candidate')
ClaudePopAsk SAVE_BUFFER
call WaitFor('PopupText() =~# "Allow once"')
call feedkeys('jj' . "\<CR>", 'xt')
call WaitFor('PopupText() =~# "Permission declined"')
call assert_equal('edited by Claude fixture', readfile(s:file)[0])
call assert_true(&modified)
call ClosePopups()
ClaudePopAsk SAVE_BUFFER
call WaitFor('PopupText() =~# "Allow once"')
call feedkeys("\<CR>", 'xt')
call WaitFor('PopupText() =~# "Saved buffer"')
call assert_equal('unsaved save candidate', readfile(s:file)[0])
call assert_false(&modified)
call ClosePopups()

ClaudeAsk FIRST_PANE 한글 'quotes' "$HOME"
if g:codex_pane_backend ==# 'tmux' && !empty($TMUX)
  call WaitFor('len(OwnedTmuxPanes()) == 1')
  let s:owned = OwnedTmuxPanes()
  call assert_equal(1, len(s:owned))
  let s:tmux_pane = split(s:owned[0], ':')[0]
else
  call WaitFor('&buftype ==# "terminal"')
  let s:terminal = bufnr('%')
endif
call WaitFor('PaneText() =~# "Claude pane idle"')
call assert_match('FIRST_PANE', PaneText())
call assert_match('한글', PaneText())
call assert_match('\$HOME', PaneText())
call assert_match('Context received: True', PaneText())
call VimFocus()
ClaudeAsk SECOND_PANE 한글
call WaitFor('PaneText() =~# "SECOND_PANE"')
call assert_match('한글', PaneText())
sleep 150m
call VimFocus()

" Native permission hook waits in Vim while Vim has focus.
call NativeQuestion('REQUEST_APPROVAL')
call WaitFor('PopupText() =~# "Allow once"')
call assert_false(PopupText() =~# 'Allow for this session')
call feedkeys("\<CR>", 'xt')
call WaitFor('PaneText() =~# "Permission allow"')
if exists('s:tmux_pane')
  silent call system('tmux select-pane -t ' . shellescape(s:tmux_pane))
  call NativeQuestion('REQUEST_APPROVAL')
  call WaitFor('PaneText() =~# "Permission native"')
  call assert_false(PopupText() =~# 'Allow once')
  call VimFocus()
endif

ClaudeHide
Claude
call VimFocus()
ClaudePopResume
call WaitFor('!empty(PopupList())')
call assert_match('Claude popup sessions', popup_getoptions(PopupList()[0]).title)
call feedkeys("\<CR>", 'xt')
sleep 100m

" Provider commands cannot accidentally mix sessions after initialization.
let s:guard = execute('CodexPopAsk SHOULD_NOT_SEND')
call assert_match('restart Vim to change provider', s:guard)
call assert_match('permission_mode_override=inherit', execute('ClaudeStatus'))

call codex#Shutdown()
call delete(s:file)
call delete(g:codex_state_dir, 'rf')
if len(v:errors)
  call writefile(v:errors, '/tmp/vim-claude-ui-errors')
  cquit
endif
if !empty($CODEX_TEST_DONE)
  call writefile(['passed'], $CODEX_TEST_DONE)
endif
qa!
