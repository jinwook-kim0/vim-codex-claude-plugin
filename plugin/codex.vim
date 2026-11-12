if exists('g:loaded_vim_codex')
  finish
endif
let g:loaded_vim_codex = 1
if !has('job') || !has('channel') || !has('terminal') || !has('popupwin')
  echoerr 'vim-codex requires Vim with +job +channel +terminal +popupwin'
  finish
endif

let g:codex_provider = get(g:, 'codex_provider', 'codex')
let g:claude_executable = get(g:, 'claude_executable', 'claude')
let g:claude_model = get(g:, 'claude_model', '')
let g:claude_permission_mode = get(g:, 'claude_permission_mode', '')
let g:claude_extra_args = get(g:, 'claude_extra_args', [])
let g:codex_pane_backend = get(g:, 'codex_pane_backend', 'tmux')
let g:codex_executable = get(g:, 'codex_executable', 'codex')
let g:codex_python = get(g:, 'codex_python', 'python3')
let g:codex_model = get(g:, 'codex_model', '')
let g:codex_extra_args = get(g:, 'codex_extra_args', [])
let g:codex_access_scope = get(g:, 'codex_access_scope', 'global')
let g:codex_sandbox_mode = get(g:, 'codex_sandbox_mode', '')
let g:codex_approval_policy = get(g:, 'codex_approval_policy', '')
let g:codex_state_dir = get(g:, 'codex_state_dir',
      \ empty($XDG_STATE_HOME) ? expand('~/.local/state/vim-codex') : $XDG_STATE_HOME . '/vim-codex')

for [s:name, s:provider] in [['Codex', 'codex'], ['Claude', 'claude']]
  let s:dispatch = 'call codex#Dispatch(' . string(s:provider) . ', '
  execute 'command! -range=0 ' . s:name . ' ' . s:dispatch . "'Command', ['open', 'pane', '', <range>, <line1>, <line2>])"
  execute 'command! -range=0 -nargs=* ' . s:name . 'Ask ' . s:dispatch . "'Command', ['ask', 'pane', <q-args>, <range>, <line1>, <line2>])"
  execute 'command! ' . s:name . 'Resume ' . s:dispatch . "'Resume', ['pane'])"
  execute 'command! ' . s:name . 'New ' . s:dispatch . "'Command', ['new', 'pane', '', 0, 0, 0])"
  execute 'command! ' . s:name . 'Hide ' . s:dispatch . "'Hide', [])"
  execute 'command! -range=0 -nargs=* ' . s:name . 'PopAsk ' . s:dispatch . "'Command', ['ask', 'popup', <q-args>, <range>, <line1>, <line2>])"
  execute 'command! ' . s:name . 'PopResume ' . s:dispatch . "'Resume', ['popup'])"
  execute 'command! ' . s:name . 'PopNew ' . s:dispatch . "'Command', ['new', 'popup', '', 0, 0, 0])"
  execute 'command! ' . s:name . 'PopReopen ' . s:dispatch . "'Command', ['reopen', 'popup', '', 0, 0, 0])"
  execute 'command! ' . s:name . 'PopStop ' . s:dispatch . "'Command', ['stop', 'popup', '', 0, 0, 0])"
  execute 'command! ' . s:name . 'Changes ' . s:dispatch . "'Changes', [])"
  execute 'command! ' . s:name . 'Status ' . s:dispatch . "'Status', [])"
  execute 'command! -bang -range=0 ' . s:name . 'Context ' . s:dispatch . "'ShowContext', [<bang>0, <range>, <line1>, <line2>])"
  execute 'command! -bang ' . s:name . 'Help call codex#Help(<bang>0)'
endfor
unlet s:name s:provider s:dispatch

nnoremap <silent> <Plug>(CodexAsk) :CodexAsk<CR>
xnoremap <silent> <Plug>(CodexAsk) :CodexAsk<CR>
nnoremap <silent> <Plug>(CodexPopAsk) :CodexPopAsk<CR>
xnoremap <silent> <Plug>(CodexPopAsk) :CodexPopAsk<CR>

nnoremap <silent> <Plug>(ClaudeAsk) :ClaudeAsk<CR>
xnoremap <silent> <Plug>(ClaudeAsk) :ClaudeAsk<CR>
nnoremap <silent> <Plug>(ClaudePopAsk) :ClaudePopAsk<CR>
xnoremap <silent> <Plug>(ClaudePopAsk) :ClaudePopAsk<CR>

" These shortcuts use the selected provider, including Visual ranges.
nnoremap <silent> <Bslash>ca :call codex#Command('ask', 'pane', '', 0, 0, 0)<CR>
xnoremap <silent> <Bslash>ca :<C-U>call codex#Command('ask', 'pane', '', 1, line("'<"), line("'>"))<CR>
nnoremap <silent> <Bslash>cpa :call codex#Command('ask', 'popup', '', 0, 0, 0)<CR>
xnoremap <silent> <Bslash>cpa :<C-U>call codex#Command('ask', 'popup', '', 1, line("'<"), line("'>"))<CR>
nnoremap <silent> <Bslash>ch :call codex#Help(0)<CR>
xnoremap <silent> <Bslash>ch :<C-U>call codex#Help(0)<CR>

augroup vim_codex
  autocmd!
  autocmd BufEnter,CursorMoved,CursorMovedI,TextChanged,TextChangedI * call codex#Track()
  autocmd BufWritePost * call codex#Track()
  autocmd BufReadPost,BufWritePost * call codex#RememberDisk()
  autocmd VimLeavePre * call codex#Shutdown()
  if exists('##ModeChanged')
    autocmd ModeChanged * call codex#Track()
  endif
augroup END
call codex#Track()
