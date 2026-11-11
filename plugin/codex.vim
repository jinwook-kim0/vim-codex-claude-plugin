if exists('g:loaded_vim_codex')
  finish
endif
let g:loaded_vim_codex = 1
if !has('job') || !has('channel') || !has('terminal') || !has('popupwin')
  echoerr 'vim-codex requires Vim with +job +channel +terminal +popupwin'
  finish
endif

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

command! -range=0 Codex call codex#Command('open', 'pane', '', <range>, <line1>, <line2>)
command! -range=0 -nargs=* CodexAsk call codex#Command('ask', 'pane', <q-args>, <range>, <line1>, <line2>)
command! CodexResume call codex#Resume('pane')
command! CodexNew call codex#Command('new', 'pane', '', 0, 0, 0)
command! CodexHide call codex#Hide()
command! -range=0 -nargs=* CodexPopAsk call codex#Command('ask', 'popup', <q-args>, <range>, <line1>, <line2>)
command! CodexPopResume call codex#Resume('popup')
command! CodexPopNew call codex#Command('new', 'popup', '', 0, 0, 0)
command! CodexPopReopen call codex#Command('reopen', 'popup', '', 0, 0, 0)
command! CodexPopStop call codex#Command('stop', 'popup', '', 0, 0, 0)
command! CodexChanges call codex#Changes()
command! CodexStatus call codex#Status()
command! -bang -range=0 CodexContext call codex#ShowContext(<bang>0, <range>, <line1>, <line2>)

nnoremap <silent> <Plug>(CodexAsk) :CodexAsk<CR>
xnoremap <silent> <Plug>(CodexAsk) :CodexAsk<CR>
nnoremap <silent> <Plug>(CodexPopAsk) :CodexPopAsk<CR>
xnoremap <silent> <Plug>(CodexPopAsk) :CodexPopAsk<CR>

nmap <silent> <Bslash>ca <Plug>(CodexAsk)
xmap <silent> <Bslash>ca <Plug>(CodexAsk)
nmap <silent> <Bslash>cpa <Plug>(CodexPopAsk)
xmap <silent> <Bslash>cpa <Plug>(CodexPopAsk)

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
