set nocompatible
set nomore
set rtp^=.
let g:codex_executable = getcwd() . '/tests/fake_codex.py'
let g:codex_state_dir = tempname()
let g:codex_pane_backend = get(g:, 'codex_test_backend', 'vim')
let $VIM_CODEX_TEST_EXTERNAL_ROOT = getcwd()
runtime plugin/codex.vim
let s:source = bufnr('%')
call setline(1, ['unsaved original', 'second'])

function! WaitFor(expression) abort
  let start = reltime()
  while !eval(a:expression) && reltimefloat(reltime(start)) < 10
    sleep 10m
  endwhile
  call assert_true(eval(a:expression), 'Timed out: ' . a:expression)
endfunction

function! PopupText() abort
  let ids = popup_list()
  return empty(ids) ? '' : join(getbufline(winbufnr(ids[0]), 1, '$'), "\n")
endfunction

function! PermissionVisible() abort
  for id in popup_list()
    if index(getbufline(winbufnr(id), 1, '$'), 'Allow once') >= 0
      return 1
    endif
  endfor
  return 0
endfunction

" Inspecting the current context must not start a bridge or submit a question.
CodexContext
call assert_true(PopupText() =~# '\[No Name\]')
call assert_true(PopupText() =~# 'No context has been provided yet')
call feedkeys("\<CR>", 'xt')
call assert_equal('nofile', &buftype)
call assert_equal(0, &modifiable)
call assert_equal(s:source, codex#Context(0, 0, 0).buffer_id)
hide close

CodexPopAsk LONG_ANSWER
call WaitFor('PopupText() =~# "Answer line 100"')
call WaitFor('popup_getoptions(popup_list()[0]).title !~# "working"')
call assert_true(popup_getoptions(popup_list()[0]).title =~# '\[No Name\]')
call assert_true(popup_getoptions(popup_list()[0]).title =~# '\[+\]')
let s:popup = popup_list()[0]
call assert_equal(s:source, bufnr('%'))
call feedkeys('G', 'xt')
call assert_true(popup_getpos(s:popup).firstline > 1)
call feedkeys('gg', 'xt')
call assert_equal(1, popup_getpos(s:popup).firstline)
call feedkeys("\<CR>", 'xt')
call assert_true(bufname('%') =~# '^codex-answer://')
call assert_equal('nofile', &buftype)
call assert_equal(100, line('$'))
call assert_equal(0, &modifiable)
call assert_equal(s:source, codex#Context(0, 0, 0).buffer_id)
hide close

CodexPopReopen
call WaitFor('PopupText() =~# "Answer line 100"')
call feedkeys('q', 'xt')
call assert_equal([], popup_list())

CodexContext!
call assert_true(join(getline(1, '$'), "\n") =~# 'Context payload (popup)')
call assert_true(join(getline(1, '$'), "\n") =~# 'unsaved original')
hide close

" Real job/channel -> app-server -> MCP subprocess -> Vim edit -> streamed answer.
CodexPopAsk EDIT_BUFFER
call WaitFor('PopupText() =~# "Modified buffer"')
call WaitFor('popup_getoptions(popup_list()[0]).title !~# "working"')
call assert_equal('edited by Codex fixture', getbufline(s:source, 1)[0])
call assert_true(getbufvar(s:source, '&modified'))
call feedkeys("\<Esc>", 'xt')

" Closing the popup does not cancel. Explicit stop does, and keeps its answer.
CodexPopAsk SLOW
call WaitFor('PopupText() =~# "Waiting"')
call feedkeys('q', 'xt')
CodexPopStop
sleep 200m
CodexPopReopen
call WaitFor('PopupText() =~# "Response stopped"')
call feedkeys('q', 'xt')

" Skill-related permission requests must reach Vim and await an explicit choice.
CodexPopAsk REQUEST_APPROVAL
call WaitFor('PermissionVisible()')
call feedkeys("\<CR>", 'xt')
call WaitFor('PopupText() =~# "Permission accepted"')
call WaitFor('popup_getoptions(popup_list()[0]).title !~# "working"')
call feedkeys('q', 'xt')

" Existing hidden buffers save through MCP only when current permissions allow.
call mkdir('tests/.results', 'p')
let s:save_path = getcwd() . '/tests/.results/save-buffer-' . getpid() . '.txt'
call writefile(['before validation'], s:save_path)
let s:save_nr = bufadd(s:save_path)
call bufload(s:save_nr)
execute 'CodexPopAsk SAVE_BUFFER buffer=' . s:save_nr
call WaitFor('PopupText() =~# "Saved for validation"')
call WaitFor('popup_getoptions(popup_list()[0]).title !~# "working"')
call assert_equal(['saved by fixture'], readfile(s:save_path))
call assert_false(getbufvar(s:save_nr, '&modified'))
call assert_equal(s:source, bufnr('%'))
call feedkeys('q', 'xt')

" Declining a required save leaves disk unchanged and does not validate.
execute 'CodexPopAsk SAVE_CONFIRM buffer=' . s:save_nr
call WaitFor('PermissionVisible()')
call feedkeys("\<Esc>", 'xt')
call WaitFor('PopupText() =~# "save declined"')
call WaitFor('popup_getoptions(popup_list()[0]).title !~# "working"')
call assert_equal(['saved by fixture'], readfile(s:save_path))
call assert_true(getbufvar(s:save_nr, '&modified'))
call assert_false(PopupText() =~# 'Saved for validation')
call feedkeys('q', 'xt')
execute 'bwipeout! ' . s:save_nr
call delete(s:save_path)

CodexPopResume
call WaitFor('!empty(popup_list())')
call feedkeys("\<CR>", 'xt')
sleep 200m
call assert_equal([], popup_list())
CodexPopNew
sleep 200m

" A pre-existing CLI conversation must appear in the pane resume picker.
CodexResume
call WaitFor('PopupText() =~# "Existing CLI conversation"')
call assert_true(PopupText() =~# '\[CLI\]')
call feedkeys("\<CR>", 'xt')
if g:codex_pane_backend ==# 'vim' || empty($TMUX)
  call WaitFor('!empty(filter(getbufinfo(), {_, b -> b.name =~# "^codex://"}))')
  let s:terminal = filter(getbufinfo(), {_, b -> b.name =~# '^codex://'})[0].bufnr
  call assert_equal('codex://11111111-1111-4111-8111-111111111111', bufname(s:terminal))
  call assert_true(&l:statusline =~# 'codex#PaneStatusline')
  call assert_true(codex#PaneStatusline() =~# '\[No Name\]', codex#PaneStatusline())
  call assert_equal(s:source, codex#BufferRequest('editor_context', {}, getcwd()).buffer_id)
  stopinsert
  CodexHide
  call assert_equal(-1, bufwinid(s:terminal))
  Codex
  call WaitFor('bufwinid(s:terminal) != -1')
  stopinsert
  CodexNew
  call WaitFor('!bufexists(s:terminal)')
  call WaitFor('!empty(filter(getbufinfo(), {_, b -> b.name =~# "^codex://"}))')
else
  sleep 500m
  call assert_equal(1, winnr('$'))
  call assert_equal(2, str2nr(system('tmux list-panes -F "#{pane_id}" | wc -l')))
  call assert_true(system('tmux list-panes -F "#{@vim_codex_context}"') =~# '\[No Name\]')
  call assert_equal('top', trim(system('tmux show-options -wAv pane-border-status')))
  CodexHide
  call assert_equal(1, str2nr(system('tmux list-panes -F "#{pane_id}" | wc -l')))
  call assert_equal('off', trim(system('tmux show-options -wAv pane-border-status')))
endif

call codex#Shutdown()
call delete(g:codex_state_dir, 'rf')
if !empty(v:errors)
  call writefile(v:errors, '/tmp/vim-codex-ui-errors')
  cquit
endif
if exists('$CODEX_TEST_DONE') && !empty($CODEX_TEST_DONE)
  call writefile(['ok'], $CODEX_TEST_DONE)
endif
qa!
