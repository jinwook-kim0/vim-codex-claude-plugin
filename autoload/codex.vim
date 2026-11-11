let s:script = fnamemodify(resolve(expand('<sfile>:p')), ':h:h') . '/pythonx/codex_bridge.py'
let s:job = v:null
let s:sequence = 0
let s:pending = {}
let s:panes = {}
let s:popups = {}
let s:contexts = {}
let s:source_buffers = {}
let s:changes = {}
let s:last_buffer = -1
let s:source_history = []
let s:instance = string(getpid()) . '-' . localtime()
let s:closing = 0
let s:preview = {}
let s:provided = {}
let s:context_popup = 0
let s:context_timer = -1
let s:context_root = ''
let s:refreshing = 0

function! s:Error(text) abort
  echohl ErrorMsg
  echomsg '[codex] ' . a:text
  echohl None
endfunction

function! s:Root(path) abort
  let directory = empty(a:path) ? getcwd() : fnamemodify(a:path, ':p:h')
  let current = directory
  while 1
    if isdirectory(current . '/.git') || filereadable(current . '/.git')
      return resolve(current)
    endif
    let parent = fnamemodify(current, ':h')
    if parent ==# current
      return resolve(directory)
    endif
    let current = parent
  endwhile
endfunction

function! codex#Track() abort
  if &buftype ==# '' && !get(b:, 'codex_ui', 0) && bufname('%') !~# '^codex\%(-answer\)\?:'
    let s:last_buffer = bufnr('%')
    call filter(s:source_history, 'v:val != bufnr("%") && bufexists(v:val)')
    call add(s:source_history, s:last_buffer)
    let root = s:Root(expand('%:p'))
    let s:source_buffers[root] = bufnr('%')
    " Capture the cursor while this source window still has focus.
    let b:codex_cursor = [line('.'), col('.')]
    if !s:refreshing && (!empty(s:panes) || s:context_popup > 0)
      if s:context_timer != -1
        call timer_stop(s:context_timer)
      endif
      let s:context_timer = timer_start(120, function('s:RefreshContext'))
    endif
  endif
endfunction

function! s:Position(lines, row, column) abort
  let row = min([max([1, a:row]), len(a:lines)])
  let text = a:lines[row - 1]
  let column = min([max([1, a:column]), strlen(text) + 1])
  return {'line': row, 'column': column,
        \ 'utf16_column': strlen(iconv(strpart(text, 0, column - 1), &encoding, 'utf-16le')) / 2 + 1}
endfunction

function! s:Metadata(context) abort
  let result = deepcopy(a:context)
  let result.line_count = len(get(result, 'lines', []))
  for key in ['lines', 'selected_text']
    if has_key(result, key)
      call remove(result, key)
    endif
  endfor
  return result
endfunction

function! codex#ContextSummary(context) abort
  if empty(a:context)
    return 'no source buffer'
  endif
  if has_key(a:context, 'error')
    return 'unavailable: ' . a:context.error
  endif
  let path = get(a:context, 'path', '')
  let root = get(a:context, 'root', '')
  if empty(path)
    let path = '[No Name] #' . get(a:context, 'buffer_id', '?')
  elseif !empty(root) && stridx(path, root . '/') == 0
    let path = strpart(path, strlen(root) + 1)
  else
    let path = fnamemodify(path, ':~')
  endif
  let path = substitute(path, '[\r\n\t]', ' ', 'g')
  let state = get(a:context, 'modified', 0) ? ' [+]' : ''
  if !get(a:context, 'on_disk', 1)
    let state .= ' [new]'
  endif
  let selection = get(a:context, 'selection', v:null)
  if type(selection) == v:t_dict
    if selection.type ==# 'line'
      let scope = 'L' . selection.start.line . '-' . selection.end.line
    elseif selection.type ==# 'block'
      let scope = 'block L' . selection.start.line . '-' . selection.end.line
      if has_key(selection, 'virtual_columns')
        let scope .= ' V' . join(selection.virtual_columns, '-')
      endif
    else
      let scope = printf('L%d:C%d-L%d:C%d', selection.start.line, selection.start.column,
            \ selection.end.line, selection.end.column)
    endif
    if !get(a:context, 'on_disk', 1)
      let scope .= ' + buffer'
    endif
  else
    let scope = 'buffer ' . get(a:context, 'line_count', len(get(a:context, 'lines', []))) . 'L'
  endif
  let cursor = get(a:context, 'cursor', {})
  return path . state . ' | ' . scope . (empty(cursor) ? '' : printf(' @L%d:C%d', cursor.line, cursor.column))
endfunction

function! codex#PaneStatusline() abort
  let root = get(b:, 'codex_project', '')
  return 'Context ready: ' . codex#ContextSummary(get(s:preview, root, {}))
endfunction

function! s:RefreshContext(timer) abort
  if s:closing || s:refreshing
    return
  endif
  let s:context_timer = -1
  let s:refreshing = 1
  try
    let roots = keys(s:panes)
    if s:context_popup > 0 && index(roots, s:context_root) < 0
      call add(roots, s:context_root)
    endif
    for root in roots
      try
        let s:preview[root] = s:Metadata(codex#BufferRequest('editor_context', {}, root))
      catch
        let s:preview[root] = {'error': v:exception}
      endtry
      if has_key(s:panes, root) && has_key(s:panes[root], 'tmux')
        call s:UpdateTmuxContext(root)
      endif
    endfor
    if s:context_popup > 0 && !empty(popup_getpos(s:context_popup))
      call popup_settext(s:context_popup, s:ContextLines(s:context_root, 0))
    endif
    redrawstatus
  finally
    let s:refreshing = 0
  endtry
endfunction

function! s:ContextLines(root, full) abort
  let preview = get(s:preview, a:root, {})
  let lines = ['Vim context — ready for the next request', '', codex#ContextSummary(preview),
        \ 'File: ' . get(preview, 'path', get(preview, 'buffer_uri', 'unavailable')),
        \ 'Workspace: ' . a:root, 'Columns: 1-based byte positions',
        \ 'Buffer access: ' . g:codex_access_scope, '', 'Last provided in this Vim:']
  let provided = get(s:provided, a:root, {})
  if empty(provided)
    call add(lines, '  No context has been provided yet.')
  endif
  for channel in ['pane', 'popup', 'ide', 'tool']
    if !has_key(provided, channel)
      continue
    endif
    let record = provided[channel]
    let stage = index(['pane', 'popup'], channel) >= 0 ? 'question accepted' : 'provided to ' . channel
    call extend(lines, ['', '[' . channel . '] ' . strftime('%H:%M:%S', float2nr(record.at)) . ' · ' . stage,
          \ codex#ContextSummary(record.context)])
    if a:full
      call extend(lines, ['', '--- Context payload (' . channel . ') ---'] + split(record.text, "\n", 1))
    endif
  endfor
  if !a:full
    call extend(lines, ['', 'Enter: inspect provided payloads   q/Esc: close'])
  endif
  return lines
endfunction

function! codex#ShowContext(full, range, first, last) abort
  try
    call codex#Track()
    if a:range || s:last_buffer < 0 || !bufexists(s:last_buffer)
      let context = codex#Context(a:range, a:first, a:last)
    else
      let path = empty(bufname(s:last_buffer)) ? '' : fnamemodify(bufname(s:last_buffer), ':p')
      let context = codex#BufferRequest('editor_context', {}, s:Root(path))
    endif
    let root = context.root
    let s:preview[root] = s:Metadata(context)
    if s:context_popup > 0
      call popup_close(s:context_popup)
    endif
    if a:full
      noautocmd botright vnew
      let b:codex_ui = 1
      setlocal buftype=nofile bufhidden=wipe noswapfile filetype=markdown
      call setline(1, s:ContextLines(root, 1))
      setlocal nomodified readonly nomodifiable
      nnoremap <silent><buffer> q :hide close<CR>
    else
      let s:context_root = root
      let s:context_popup = popup_create(s:ContextLines(root, 0), {
            \ 'pos': 'center', 'title': ' Codex context ', 'border': [], 'padding': [0, 1, 0, 1],
            \ 'maxwidth': max([20, &columns - 8]), 'maxheight': max([3, &lines - 6]),
            \ 'scrollbar': 1, 'mapping': 0, 'filter': function('s:ContextFilter'),
            \ 'callback': function('s:ContextClosed')})
    endif
  catch
    call s:Error(v:exception)
  endtry
endfunction

function! s:ContextClosed(id, result) abort
  if s:context_popup == a:id
    let s:context_popup = 0
  endif
endfunction

function! s:ContextFilter(id, key) abort
  if a:key ==# 'q' || a:key ==# "\<Esc>"
    call popup_close(a:id)
  elseif a:key ==# "\<CR>"
    call codex#ShowContext(1, 0, 0, 0)
  else
    let first = popup_getpos(a:id).firstline
    let delta = index(['j', "\<Down>", "\<C-d>"], a:key) >= 0 ? 1
          \ : (index(['k', "\<Up>", "\<C-u>"], a:key) >= 0 ? -1 : 0)
    call popup_setoptions(a:id, {'firstline': max([1, first + delta])})
  endif
  return 1
endfunction

function! s:Interactive(message) abort
  if a:message.event ==# 'input_request'
    let answers = {}
    call inputsave()
    try
      for question in get(a:message.params, 'questions', [])
        let prompt = question.question . "\n"
        for option in get(question, 'options', [])
          let prompt .= option.label . ': ' . get(option, 'description', '') . "\n"
        endfor
        let answer = get(question, 'isSecret', 0) ? inputsecret(prompt) : input(prompt)
        let answers[question.id] = {'answers': [answer]}
      endfor
    finally
      call inputrestore()
      call ch_sendraw(job_getchannel(s:job), json_encode({'op': 'interactive_reply',
            \ 'request_id': a:message.request_id, 'answers': answers}) . "\n")
    endtry
    return
  endif
  let details = get(a:message.params, 'reason', '')
  let command = get(a:message.params, 'command', '')
  if !empty(command)
    let details .= ' ' . (type(command) == v:t_string ? command : json_encode(command))
  endif
  if has_key(a:message.params, 'permissions')
    let details .= ' ' . json_encode(a:message.params.permissions)
  endif
  call popup_menu(['Allow once', 'Allow for this session', 'Reject'], {
        \ 'title': ' Codex permission: ' . substitute(details, '[\r\n]', ' ', 'g'),
        \ 'maxwidth': max([20, &columns - 8]),
        \ 'callback': function('s:ApprovalChosen', [a:message.request_id])})
endfunction

function! s:ApprovalChosen(request, id, index) abort
  let choices = ['accept', 'acceptForSession', 'decline']
  let decision = a:index >= 1 && a:index <= 3 ? choices[a:index - 1] : 'decline'
  call ch_sendraw(job_getchannel(s:job), json_encode({'op': 'interactive_reply',
        \ 'request_id': a:request, 'decision': decision}) . "\n")
endfunction

function! s:BlockSlice(text, left, right) abort
  let output = ''
  let virtual = 1
  for char in split(a:text, '\zs')
    let width = char ==# "\t" ? &tabstop - ((virtual - 1) % &tabstop) : strdisplaywidth(char)
    if virtual <= a:right && virtual + width - 1 >= a:left
      if char ==# "\t"
        let output .= repeat(' ', min([a:right + 1, virtual + width]) - max([a:left, virtual]))
      else
        let output .= char
      endif
    endif
    let virtual += width
  endfor
  return output
endfunction

" Public for integrations and headless verification. Column metadata is 1-based
" byte positions; utf16_column is supplied separately for native IDE clients.
function! codex#Context(range, first, last) abort
  call codex#Track()
  if s:last_buffer < 0 || !bufloaded(s:last_buffer) || getbufvar(s:last_buffer, '&buftype') !=# '' || getbufvar(s:last_buffer, 'codex_ui', 0)
    let candidates = filter(copy(s:source_history), 'bufloaded(v:val) && getbufvar(v:val, "&buftype") ==# "" && !getbufvar(v:val, "codex_ui", 0)')
    let s:last_buffer = empty(candidates) ? -1 : candidates[-1]
  endif
  let nr = s:last_buffer
  if nr < 0 || !bufexists(nr)
    throw 'No source buffer available'
  endif
  let lines = getbufline(nr, 1, '$')
  let path = empty(bufname(nr)) ? '' : resolve(fnamemodify(bufname(nr), ':p'))
  let root = s:Root(path)
  let s:source_buffers[root] = nr
  let cursor = getbufvar(nr, 'codex_cursor', [1, 1])
  let result = {'buffer_id': nr, 'buffer_uri': 'vim-buffer://' . s:instance . '/' . nr,
        \ 'path': path, 'name': empty(path) ? '[No Name]' : fnamemodify(path, ':t'),
        \ 'root': root, 'on_disk': filereadable(path) ? v:true : v:false,
        \ 'modified': getbufvar(nr, '&modified') ? v:true : v:false,
        \ 'changedtick': getbufvar(nr, 'changedtick'), 'filetype': getbufvar(nr, '&filetype'),
        \ 'endofline': getbufvar(nr, '&endofline') ? v:true : v:false,
        \ 'column_unit': 'byte', 'cursor': s:Position(lines, cursor[0], cursor[1]),
        \ 'lines': lines, 'selection': v:null, 'selected_text': ''}
  let active_visual = nr == bufnr('%') && index(['v', 'V', "\<C-v>"], mode()) >= 0
  if (a:range || active_visual) && nr == bufnr('%')
    let start = active_visual ? getpos('v') : getpos("'<")
    let end = active_visual ? getpos('.') : getpos("'>")
    if start[1] > end[1] || (start[1] == end[1] && start[2] > end[2])
      let [start, end] = [end, start]
    endif
    let kind = active_visual ? mode() : visualmode()
    if !active_visual && (start[1] != a:first || end[1] != a:last || empty(kind) || a:range == 1)
      let start = [0, a:first, 1, 0]
      let end = [0, a:last, 2147483647, 0]
      let kind = 'V'
    endif
    if kind ==# 'V'
      let start[2] = 1
      let end[2] = strlen(lines[end[1] - 1]) + 1
      let selected = lines[start[1] - 1 : end[1] - 1]
      let selection_type = 'line'
    elseif kind ==# "\<C-v>"
      let left = min([virtcol([start[1], start[2]]), virtcol([end[1], end[2]])])
      let right = max([virtcol([start[1], start[2]]), virtcol([end[1], end[2]])])
      if &selection ==# 'exclusive' && right > left
        let right -= 1
      endif
      let selected = map(copy(lines[start[1] - 1 : end[1] - 1]), 's:BlockSlice(v:val, left, right)')
      let selection_type = 'block'
    else
      let selected = copy(lines[start[1] - 1 : end[1] - 1])
      let endtext = lines[end[1] - 1]
      let endbyte = min([end[2] - 1, strlen(endtext)])
      if &selection !=# 'exclusive'
        let endbyte += strlen(matchstr(strpart(endtext, endbyte), '^.'))
      endif
      let selected[-1] = strpart(selected[-1], 0, endbyte)
      let selected[0] = strpart(selected[0], start[2] - 1)
      let end[2] = endbyte + 1
      let selection_type = 'character'
    endif
    let result.selection = {'type': selection_type,
          \ 'start': s:Position(lines, start[1], start[2]),
          \ 'end': s:Position(lines, end[1], end[2]), 'end_exclusive': v:true}
    if selection_type ==# 'block'
      let result.selection.virtual_columns = [left, right]
      let result.selection.end_exclusive = v:false
    endif
    let result.selected_text = join(selected, "\n")
  endif
  let s:contexts[root] = result
  let s:preview[root] = s:Metadata(result)
  return result
endfunction

function! s:Start() abort
  if type(s:job) == v:t_job && job_status(s:job) ==# 'run'
    return
  endif
  if !executable(g:codex_python) || !executable(g:codex_executable)
    throw 'Install Python 3.11+ and Codex CLI, or configure their executable paths'
  endif
  let s:job = job_start([g:codex_python, s:script, 'host'], {
        \ 'in_mode': 'nl', 'out_mode': 'nl', 'err_mode': 'nl',
        \ 'out_cb': function('s:Output'), 'err_cb': function('s:Stderr'),
        \ 'exit_cb': function('s:Exited'), 'stoponexit': 'term'})
  if job_status(s:job) !=# 'run'
    throw 'Could not start the Codex bridge'
  endif
  call s:Send({'op': 'init', 'config': {'codex': exepath(g:codex_executable),
        \ 'python': exepath(g:codex_python), 'script': s:script,
        \ 'state_dir': expand(g:codex_state_dir), 'model': g:codex_model,
        \ 'sandbox_mode': g:codex_sandbox_mode, 'approval_policy': g:codex_approval_policy,
        \ 'extra_args': g:codex_extra_args}}, function('s:Ignore'))
endfunction

function! s:Ignore(result) abort
endfunction

function! s:Send(message, callback) abort
  let s:sequence += 1
  let a:message.id = s:sequence
  let s:pending[string(s:sequence)] = a:callback
  call ch_sendraw(job_getchannel(s:job), json_encode(a:message) . "\n")
endfunction

function! s:Stderr(channel, text) abort
  if !empty(a:text)
    call s:Error(a:text)
  endif
endfunction

function! s:Exited(job, status) abort
  if !s:closing
    let s:pending = {}
    call s:Error('Bridge exited (' . a:status . '); use :CodexStatus to inspect configuration')
  endif
endfunction

function! s:Output(channel, text) abort
  if s:closing
    return
  endif
  try
    let message = json_decode(a:text)
    if has_key(message, 'id')
      let Callback = remove(s:pending, string(message.id))
      if has_key(message, 'error')
        call s:Error(message.error)
      else
        call Callback(message.result)
      endif
    elseif get(message, 'event', '') ==# 'buffer_request'
      let reply = {'op': 'buffer_reply', 'request_id': message.request_id}
      try
        let reply.result = codex#BufferRequest(message.operation, message.arguments, message.root)
      catch
        let reply.error = v:exception
      endtry
      call ch_sendraw(a:channel, json_encode(reply) . "\n")
    elseif get(message, 'event', '') ==# 'error'
      call s:Error(message.message)
    elseif get(message, 'event', '') ==# 'context_provided'
      if !has_key(s:provided, message.root)
        let s:provided[message.root] = {}
      endif
      let s:provided[message.root][message.channel] = message
      call s:RefreshContext(-1)
    elseif index(['approval_request', 'input_request'], get(message, 'event', '')) >= 0
      call s:Interactive(message)
    elseif get(message, 'mode', '') ==# 'popup'
      call s:Answer(message)
    endif
  catch
    call s:Error(v:exception)
  endtry
endfunction

function! codex#Command(op, mode, question, range, first, last) abort
  try
    let context = codex#Context(a:range, a:first, a:last)
    let question = a:question
    if a:op ==# 'reopen' && has_key(s:popups, context.root) && !empty(s:popups[context.root].text)
      let s:popups[context.root].hidden = 0
      call s:ShowPopup(context.root)
      return
    endif
    if a:op ==# 'ask' && empty(question)
      let question = input('Codex: ')
      if empty(question)
        return
      endif
    endif
    if a:mode ==# 'pane' && index(['open', 'new', 'ask'], a:op) >= 0
      let Callback = function('s:OpenPane', [context.root])
    elseif a:op ==# 'reopen'
      let Callback = function('s:Reopen', [context.root])
    else
      let Callback = function('s:PopupSelected', [context.root, a:op])
    endif
    call s:Start()
    call s:Send({'op': a:op, 'mode': a:mode, 'root': context.root,
          \ 'context': context, 'question': question}, Callback)
  catch
    call s:Error(v:exception)
  endtry
endfunction

function! s:System(argv) abort
  " Keep Vim's terminal mode while running noninteractive tmux commands.
  " Otherwise timer-driven calls can leave stray characters until CTRL-L.
  silent let output = system(join(map(copy(a:argv), 'shellescape(v:val)'), ' '))
  return output
endfunction

function! s:OwnTmux(pane) abort
  if !has_key(a:pane, 'tmux') || !executable('tmux') || empty($TMUX)
    return 0
  endif
  let owner = trim(s:System(['tmux', 'display-message', '-p', '-t', a:pane.tmux, '#{@vim_codex_owner}']))
  return !v:shell_error && owner ==# s:instance
endfunction

function! s:UpdateTmuxContext(root) abort
  let pane = s:panes[a:root]
  if !s:OwnTmux(pane)
    if has_key(pane, 'window')
      call s:RestoreTmuxBorder(pane.window)
    endif
    return
  endif
  let summary = codex#ContextSummary(get(s:preview, a:root, {}))
  if get(pane, 'context_summary', '') ==# summary
    return
  endif
  call s:System(['tmux', 'set-option', '-p', '-t', pane.tmux, '@vim_codex_context', summary])
  let window = trim(s:System(['tmux', 'display-message', '-p', '-t', pane.tmux, '#{window_id}']))
  let pane.window = window
  let stored = trim(s:System(['tmux', 'show-options', '-wqv', '-t', window, '@vim_codex_border_saved']))
  if empty(stored)
    let saved = {}
    for name in ['pane-border-status', 'pane-border-format']
      let saved[name] = substitute(s:System(['tmux', 'show-options', '-wAv', '-t', window, name]), '\n$', '', '')
      let saved[name . '-local'] = !empty(trim(s:System(['tmux', 'show-options', '-wq', '-t', window, name])))
    endfor
    let saved.applied_status = saved['pane-border-status'] ==# 'off' ? 'top' : saved['pane-border-status']
    let saved.applied_format = saved['pane-border-format'] . ' #{?@vim_codex_context,| Context ready: ,}#{@vim_codex_context}'
    call s:System(['tmux', 'set-option', '-w', '-t', window, '@vim_codex_border_saved', json_encode(saved)])
    call s:System(['tmux', 'set-option', '-w', '-t', window, 'pane-border-status', saved.applied_status])
    call s:System(['tmux', 'set-option', '-w', '-t', window, 'pane-border-format', saved.applied_format])
  endif
  let pane.context_summary = summary
endfunction

function! s:RestoreTmuxBorder(window) abort
  let active = trim(s:System(['tmux', 'list-panes', '-t', a:window, '-F', '#{@vim_codex_context}']))
  if !empty(active)
    return
  endif
  let stored = trim(s:System(['tmux', 'show-options', '-wqv', '-t', a:window, '@vim_codex_border_saved']))
  if empty(stored)
    return
  endif
  try
    let saved = json_decode(stored)
    for [name, applied] in [['pane-border-status', saved.applied_status], ['pane-border-format', saved.applied_format]]
      let current = substitute(s:System(['tmux', 'show-options', '-wAv', '-t', a:window, name]), '\n$', '', '')
      if current ==# applied
        if saved[name . '-local']
          call s:System(['tmux', 'set-option', '-w', '-t', a:window, name, saved[name]])
        else
          call s:System(['tmux', 'set-option', '-wu', '-t', a:window, name])
        endif
      endif
    endfor
    call s:System(['tmux', 'set-option', '-wu', '-t', a:window, '@vim_codex_border_saved'])
  catch
    call s:Error('Could not restore tmux context border: ' . v:exception)
  endtry
endfunction

function! s:DestroyPane(root) abort
  if !has_key(s:panes, a:root)
    return
  endif
  let pane = remove(s:panes, a:root)
  if s:OwnTmux(pane)
    call s:System(['tmux', 'kill-pane', '-t', pane.tmux])
    if has_key(pane, 'window')
      call s:RestoreTmuxBorder(pane.window)
    endif
  elseif has_key(pane, 'buffer') && bufexists(pane.buffer)
    call job_stop(term_getjob(pane.buffer))
    execute 'silent! bwipeout! ' . pane.buffer
  endif
endfunction

function! s:OpenPane(root, result) abort
  let pane = get(s:panes, a:root, {})
  if get(pane, 'thread_id', '') ==# a:result.thread_id
    if s:OwnTmux(pane)
      call s:RefreshContext(-1)
      call s:System(['tmux', 'select-pane', '-t', pane.tmux])
      return
    elseif has_key(pane, 'buffer') && bufexists(pane.buffer) && job_status(term_getjob(pane.buffer)) ==# 'run'
      let window = bufwinid(pane.buffer)
      if window != -1
        call win_gotoid(window)
      else
        execute 'botright vertical sbuffer ' . pane.buffer
      endif
      return
    endif
  endif
  call s:DestroyPane(a:root)
  let pane = {'thread_id': a:result.thread_id}
  if g:codex_pane_backend ==# 'tmux' && !empty($TMUX) && executable('tmux')
    let command = join(map(copy(a:result.argv), 'shellescape(v:val)'), ' ')
    let output = s:System(['tmux', 'split-window', '-h', '-t', $TMUX_PANE,
          \ '-P', '-F', '#{pane_id}', '-c', a:root, command])
    if !v:shell_error && trim(output) =~# '^%\d\+$'
      let pane.tmux = trim(output)
      call s:System(['tmux', 'set-option', '-p', '-t', pane.tmux, '@vim_codex_owner', s:instance])
      let s:panes[a:root] = pane
      call s:RefreshContext(-1)
      return
    endif
    echomsg '[codex] tmux pane failed; opening Vim split: ' . trim(output)
  endif
  let source = get(s:source_buffers, a:root, s:last_buffer)
  noautocmd botright vnew
  let b:codex_ui = 1
  let pane.buffer = term_start(a:result.argv, {'curwin': 1, 'cwd': a:root,
        \ 'term_name': 'codex://' . a:result.thread_id, 'term_finish': 'close'})
  if pane.buffer <= 0
    close
    call s:Error('Could not open the Codex terminal')
    return
  endif
  let b:codex_ui = 1
  let b:codex_project = a:root
  let s:source_buffers[a:root] = source
  let s:last_buffer = source
  call filter(s:source_history, 'v:val != pane.buffer')
  let &l:statusline = '[Codex] %{codex#PaneStatusline()}'
  let s:panes[a:root] = pane
  call s:RefreshContext(-1)
  startinsert
endfunction

function! codex#Hide() abort
  try
    let root = codex#Context(0, 0, 0).root
    let pane = get(s:panes, root, {})
    if s:OwnTmux(pane)
      call s:DestroyPane(root)
    elseif has_key(pane, 'buffer')
      let window = bufwinid(pane.buffer)
      if window != -1
        call win_execute(window, 'hide close')
      endif
    endif
  catch
    call s:Error(v:exception)
  endtry
endfunction

function! codex#Resume(mode) abort
  try
    let context = codex#Context(0, 0, 0)
    call s:Start()
    call s:Send({'op': 'list', 'mode': a:mode, 'root': context.root},
          \ function('s:SessionList', [context.root, a:mode]))
  catch
    call s:Error(v:exception)
  endtry
endfunction

function! s:SessionList(root, mode, sessions) abort
  if empty(a:sessions)
    echomsg '[codex] No ' . a:mode . ' sessions for ' . a:root
    return
  endif
  let labels = map(copy(a:sessions),
        \ 'strftime("%m-%d %H:%M", float2nr(v:val.updated)) . "  [" . get(v:val, "source", "Vim") . "] " . (empty(v:val.title) ? "[New conversation]" : v:val.title) . "  " . strpart(v:val.id, 0, 8)')
  call popup_menu(labels, {'title': ' Codex ' . a:mode . ' sessions ',
        \ 'maxwidth': max([30, &columns - 8]), 'maxheight': max([3, &lines - 8]),
        \ 'callback': function('s:SessionChosen', [a:root, a:mode, a:sessions])})
endfunction

function! s:SessionChosen(root, mode, sessions, id, index) abort
  if a:index < 1
    return
  endif
  let Callback = a:mode ==# 'pane' ? function('s:OpenPane', [a:root])
        \ : function('s:PopupSelected', [a:root, 'select'])
  call s:Send({'op': 'select', 'mode': a:mode, 'root': a:root,
        \ 'thread_id': a:sessions[a:index - 1].id}, Callback)
endfunction

function! s:PopupSelected(root, op, result) abort
  if index(['select', 'new'], a:op) >= 0
    let old = get(s:popups, a:root, {})
    if get(old, 'id', 0) > 0
      call popup_close(old.id)
    endif
    let s:popups[a:root] = {'id': 0, 'text': '', 'hidden': 1,
          \ 'thread_id': a:result.thread_id, 'busy': 0}
    echomsg '[codex] Popup session selected: ' . a:result.thread_id
  endif
endfunction

function! s:Answer(message) abort
  let root = a:message.root
  if a:message.event ==# 'answer_start'
    let old = get(s:popups, root, {})
    if get(old, 'id', 0) > 0
      call popup_close(old.id)
    endif
    let s:popups[root] = {'id': 0, 'text': 'Waiting for Codex…', 'hidden': 0,
          \ 'thread_id': a:message.thread_id, 'busy': 1, 'context': get(a:message, 'context', {})}
  elseif index(['answer', 'answer_done'], a:message.event) >= 0
    let state = get(s:popups, root, {})
    if get(state, 'thread_id', '') !=# a:message.thread_id
      return
    endif
    let state.text = a:message.text
    let state.busy = a:message.event !=# 'answer_done'
  else
    return
  endif
  let state = s:popups[root]
  if has_key(state, 'answer_buffer') && bufexists(state.answer_buffer)
    call s:SetAnswerBuffer(state.answer_buffer, state.text)
  endif
  if !state.hidden
    call s:ShowPopup(root)
  endif
endfunction

function! s:Reopen(root, result) abort
  let state = get(s:popups, a:root, {})
  if get(state, 'id', 0) > 0
    call popup_close(state.id)
  endif
  let s:popups[a:root] = {'id': 0, 'text': a:result.text, 'hidden': 0,
        \ 'thread_id': a:result.thread_id, 'busy': 0}
  call s:ShowPopup(a:root)
endfunction

function! s:ShowPopup(root) abort
  let state = s:popups[a:root]
  let text = split(state.text, "\n", 1)
  if empty(text)
    let text = ['']
  endif
  let title = ' Codex' . (state.busy ? ' · working' : '') . ' | Enter: expand  q/Esc: close '
  if !empty(get(state, 'context', {}))
    let title = ' Codex' . (state.busy ? ' · working' : '') . ' | ' . codex#ContextSummary(state.context) . ' '
  endif
  if state.id > 0 && !empty(popup_getpos(state.id))
    call popup_settext(state.id, text)
    call popup_setoptions(state.id, {'title': title})
    return
  endif
  let width = max([20, min([100, &columns - 8])])
  let state.id = popup_create(text, {'pos': 'center', 'minwidth': width, 'maxwidth': width,
        \ 'minheight': 3, 'maxheight': max([3, &lines * 2 / 3]), 'border': [],
        \ 'padding': [0, 1, 0, 1], 'scrollbar': 1, 'title': title, 'mapping': 0,
        \ 'filter': function('s:PopupFilter', [a:root]),
        \ 'callback': function('s:PopupClosed', [a:root])})
  call setbufvar(winbufnr(state.id), '&filetype', 'markdown')
endfunction

function! s:PopupClosed(root, id, result) abort
  if has_key(s:popups, a:root) && s:popups[a:root].id == a:id
    let s:popups[a:root].hidden = 1
    let s:popups[a:root].id = 0
  endif
endfunction

function! s:PopupFilter(root, id, key) abort
  if a:key ==# 'q' || a:key ==# "\<Esc>"
    call popup_close(a:id)
  elseif a:key ==# "\<CR>"
    call s:ExpandAnswer(a:root)
  else
    let pos = popup_getpos(a:id)
    let first = pos.firstline
    if a:key ==# 'j' || a:key ==# "\<Down>"
      let first += 1
    elseif a:key ==# 'k' || a:key ==# "\<Up>"
      let first -= 1
    elseif a:key ==# "\<C-d>" || a:key ==# "\<PageDown>"
      let first += max([1, pos.core_height / 2])
    elseif a:key ==# "\<C-u>" || a:key ==# "\<PageUp>"
      let first -= max([1, pos.core_height / 2])
    elseif a:key ==# 'g'
      let first = 1
    elseif a:key ==# 'G'
      let first = len(split(s:popups[a:root].text, "\n", 1)) - pos.core_height + 1
    endif
    call popup_setoptions(a:id, {'firstline': max([1, first])})
  endif
  return 1
endfunction

function! s:SetAnswerBuffer(nr, text) abort
  call setbufvar(a:nr, '&modifiable', 1)
  call setbufvar(a:nr, '&readonly', 0)
  let lines = split(a:text, "\n", 1)
  call setbufline(a:nr, 1, empty(lines) ? [''] : lines)
  if len(getbufline(a:nr, 1, '$')) > len(lines)
    call deletebufline(a:nr, len(lines) + 1, '$')
  endif
  call setbufvar(a:nr, '&modified', 0)
  call setbufvar(a:nr, '&modifiable', 0)
  call setbufvar(a:nr, '&readonly', 1)
endfunction

function! s:ExpandAnswer(root) abort
  let state = s:popups[a:root]
  call popup_close(state.id)
  let nr = get(state, 'answer_buffer', -1)
  if nr > 0 && bufexists(nr)
    execute 'botright vertical sbuffer ' . nr
  else
    noautocmd botright vnew
    let b:codex_ui = 1
    setlocal buftype=nofile bufhidden=hide noswapfile filetype=markdown
    execute 'file codex-answer://' . state.thread_id
    let state.answer_buffer = bufnr('%')
    nnoremap <silent><buffer> q :hide close<CR>
  endif
  call s:SetAnswerBuffer(state.answer_buffer, state.text)
endfunction

function! s:Within(path, root) abort
  return a:path ==# a:root || stridx(a:path, a:root . '/') == 0
endfunction

function! s:Target(arguments, root) abort
  if has_key(a:arguments, 'buffer_id')
    let nr = a:arguments.buffer_id
    if !bufloaded(nr) || getbufvar(nr, '&buftype') !=# '' || getbufvar(nr, 'codex_ui', 0)
      throw 'File is not open in a source buffer; use normal Codex file tools under current permissions'
    endif
    let path = empty(bufname(nr)) ? '' : resolve(fnamemodify(bufname(nr), ':p'))
    if empty(path) && g:codex_access_scope ==# 'project'
      if get(s:source_buffers, a:root, -1) != nr && getbufvar(nr, 'codex_root', '') !=# a:root
        throw 'Unnamed buffer does not belong to this project'
      endif
    elseif !empty(path) && g:codex_access_scope ==# 'project' && !s:Within(path, a:root)
      throw 'Buffer is outside the project root'
    endif
  elseif has_key(a:arguments, 'path') && !empty(a:arguments.path)
    let given = a:arguments.path
    let path = resolve(fnamemodify(given =~# '^/' ? given : a:root . '/' . given, ':p'))
    if isdirectory(path)
      throw 'Path is a directory, not a source file'
    endif
    if g:codex_access_scope ==# 'project' && !s:Within(path, a:root)
      throw 'File is outside the project root'
    endif
    let nr = -1
    for info in getbufinfo()
      if info.loaded && !empty(info.name) && resolve(fnamemodify(info.name, ':p')) ==# path
        let nr = info.bufnr
        break
      endif
    endfor
    if nr < 0
      throw 'File is not open in Vim; use normal Codex file tools under current permissions'
    endif
  else
    let nr = get(s:source_buffers, a:root, -1)
    if nr < 0
      throw 'No source buffer for this project'
    endif
    return s:Target({'buffer_id': nr}, a:root)
  endif
  if !bufloaded(nr) || getbufvar(nr, '&buftype') !=# '' || getbufvar(nr, 'codex_ui', 0)
    throw 'File is not open in a source buffer; use normal Codex file tools under current permissions'
  endif
  call setbufvar(nr, 'codex_root', a:root)
  return nr
endfunction

function! s:DiskState(path) abort
  if empty(a:path) || empty(getftype(a:path))
    return {'path': a:path, 'exists': 0, 'hash': ''}
  endif
  if getftype(a:path) !=# 'file'
    throw 'Save target is not a regular file'
  endif
  return {'path': a:path, 'exists': 1, 'hash': sha256(join(readfile(a:path, 'b'), "\n"))}
endfunction

function! codex#RememberDisk(...) abort
  let nr = a:0 ? a:1 : bufnr('%')
  if !bufloaded(nr) || getbufvar(nr, '&buftype') !=# '' || getbufvar(nr, 'codex_ui', 0)
    return
  endif
  let path = empty(bufname(nr)) ? '' : resolve(fnamemodify(bufname(nr), ':p'))
  try
    call setbufvar(nr, 'codex_disk_state', s:DiskState(path))
  catch
    call setbufvar(nr, 'codex_disk_state', {'path': path, 'error': v:exception})
  endtry
endfunction

function! s:BufferInfo(nr) abort
  let path = empty(bufname(a:nr)) ? '' : resolve(fnamemodify(bufname(a:nr), ':p'))
  if empty(getbufvar(a:nr, 'codex_disk_state', {}))
    call codex#RememberDisk(a:nr)
  endif
  try
    let disk_changed = getbufvar(a:nr, 'codex_disk_state') !=# s:DiskState(path)
  catch
    let disk_changed = 1
  endtry
  return {'buffer_id': a:nr, 'path': path, 'on_disk': filereadable(path) ? v:true : v:false,
        \ 'buffer_uri': 'vim-buffer://' . s:instance . '/' . a:nr,
        \ 'changedtick': getbufvar(a:nr, 'changedtick'),
        \ 'modified': getbufvar(a:nr, '&modified') ? v:true : v:false,
        \ 'disk_changed': disk_changed ? v:true : v:false,
        \ 'lines': getbufline(a:nr, 1, '$')}
endfunction

function! s:SaveBuffer(nr, args) abort
  let info = s:BufferInfo(a:nr)
  if info.changedtick != a:args.expected_tick
    throw 'Stale buffer: content changed while save permission was pending; read it again'
  endif
  if empty(info.path) || info.path !=# get(a:args, 'authorized_path', '')
    throw 'Save target differs from the authorized path'
  endif
  if info.disk_changed
    throw 'File changed on disk since Vim read/saved it; reconcile before saving'
  endif
  if getbufvar(a:nr, '&readonly') || !getbufvar(a:nr, '&modifiable')
    throw 'Buffer is not writable; save was not forced'
  endif
  let origin_window = win_getid()
  let origin_buffer = bufnr('%')
  let origin_view = winsaveview()
  let source = s:last_buffer
  let sources = copy(s:source_buffers)
  let history = copy(s:source_history)
  try
    let target_window = bufwinid(a:nr)
    if target_window != -1
      call win_gotoid(target_window)
    elseif bufnr('%') != a:nr
      execute 'noautocmd keepalt keepjumps hide buffer ' . a:nr
    endif
    " Use Vim's normal write path: encoding, EOL, write guards and user hooks.
    silent keepalt write
    call codex#RememberDisk(a:nr)
  finally
    if win_getid() != origin_window
      call win_gotoid(origin_window)
    endif
    if bufnr('%') != origin_buffer && bufexists(origin_buffer)
      execute 'noautocmd keepalt keepjumps hide buffer ' . origin_buffer
    endif
    call winrestview(origin_view)
    let s:last_buffer = source
    let s:source_buffers = sources
    let s:source_history = history
  endtry
  let result = s:BufferInfo(a:nr)
  let result.saved = !result.modified ? v:true : v:false
  if !result.saved
    let result.message = 'Write completed, but a write hook left new unsaved changes; re-read before validation'
  endif
  echomsg '[codex] Saved buffer: ' . info.path
  return result
endfunction

function! codex#BufferRequest(operation, arguments, root) abort
  if a:operation ==# 'editor_context'
    let nr = get(s:source_buffers, a:root, -1)
    if nr >= 0 && (!bufloaded(nr) || getbufvar(nr, '&buftype') !=# '' || getbufvar(nr, 'codex_ui', 0))
      let candidates = filter(copy(s:source_history), 'bufloaded(v:val) && getbufvar(v:val, "&buftype") ==# "" && !getbufvar(v:val, "codex_ui", 0) && s:Root(empty(bufname(v:val)) ? "" : fnamemodify(bufname(v:val), ":p")) ==# a:root')
      let nr = empty(candidates) ? -1 : candidates[-1]
      let s:source_buffers[a:root] = nr
    endif
    if nr < 0 || !bufexists(nr)
      throw 'No Vim source buffer for this workspace'
    endif
    if bufnr('%') == nr && index(['v', 'V', "\<C-v>"], mode()) >= 0
      return codex#Context(0, 0, 0)
    endif
    let context = deepcopy(get(s:contexts, a:root, {}))
    if empty(context) || context.buffer_id != nr
      let old = s:last_buffer
      let s:last_buffer = nr
      " Context() tracks the current source window; when another project is
      " active, build from the stored source after temporarily disabling Track.
      let context = s:ContextForBuffer(nr, a:root)
      let s:last_buffer = old
    endif
    let tick = getbufvar(nr, 'changedtick')
    if context.changedtick != tick
      let context.selection = v:null
      let context.selected_text = ''
    endif
    let context.lines = getbufline(nr, 1, '$')
    let context.changedtick = tick
    let context.modified = getbufvar(nr, '&modified') ? v:true : v:false
    let context.on_disk = filereadable(context.path) ? v:true : v:false
    let cursor = getbufvar(nr, 'codex_cursor', [1, 1])
    let context.cursor = s:Position(context.lines, cursor[0], cursor[1])
    return context
  elseif a:operation ==# 'list_buffers'
    let buffers = []
    for info in getbufinfo()
      if info.loaded && getbufvar(info.bufnr, '&buftype') ==# '' && !getbufvar(info.bufnr, 'codex_ui', 0)
        let path = empty(info.name) ? '' : resolve(fnamemodify(info.name, ':p'))
        if g:codex_access_scope ==# 'global' || (!empty(path) && s:Within(path, a:root)) || get(s:source_buffers, a:root, -1) == info.bufnr || getbufvar(info.bufnr, 'codex_root', '') ==# a:root
          let item = s:BufferInfo(info.bufnr)
          call remove(item, 'lines')
          call add(buffers, item)
        endif
      endif
    endfor
    return buffers
  elseif index(['read_buffer', 'edit_buffer', 'save_buffer'], a:operation) < 0
    throw 'Unknown buffer operation'
  endif
  let nr = s:Target(a:arguments, a:root)
  if a:operation ==# 'read_buffer'
    return s:BufferInfo(nr)
  elseif a:operation ==# 'save_buffer'
    return s:SaveBuffer(nr, a:arguments)
  endif
  let args = a:arguments
  if getbufvar(nr, 'changedtick') != args.expected_tick
    throw 'Stale buffer: changedtick differs. Read the buffer again and recalculate the edit.'
  endif
  if !getbufvar(nr, '&modifiable') || getbufvar(nr, '&readonly')
    throw 'Buffer is not writable'
  endif
  let lines = getbufline(nr, 1, '$')
  if args.start_line < 1 || args.start_line > len(lines) + 1 || args.end_line < args.start_line - 1 || args.end_line > len(lines)
    throw 'Invalid buffer line range'
  endif
  if type(args.lines) != v:t_list || !empty(filter(copy(args.lines), 'type(v:val) != v:t_string || stridx(v:val, "\n") >= 0'))
    throw 'lines must contain strings without embedded newlines'
  endif
  let before = args.start_line > 1 ? lines[:args.start_line - 2] : []
  let after = args.end_line < len(lines) ? lines[args.end_line:] : []
  let replacement = before + args.lines + after
  if empty(replacement)
    let replacement = ['']
  endif
  if replacement ==# lines
    return s:BufferInfo(nr)
  endif
  let origin_window = win_getid()
  let origin_buffer = bufnr('%')
  let origin_view = winsaveview()
  let target_window = bufwinid(nr)
  try
    if target_window != -1
      call win_gotoid(target_window)
    elseif nr != bufnr('%')
      execute 'noautocmd keepalt keepjumps hide buffer ' . nr
    endif
    " End the previous undo block before this asynchronous edit. Join only the
    " two operations belonging to this edit, so one `u` reverses one tool call.
    let &l:undolevels = &l:undolevels
    call setline(1, replacement)
    if line('$') > len(replacement)
      undojoin
      execute (len(replacement) + 1) . ',$delete _'
    endif
    let &l:modified = 1
  finally
    if win_getid() != origin_window
      call win_gotoid(origin_window)
    endif
    if bufnr('%') != origin_buffer && bufexists(origin_buffer)
      execute 'noautocmd keepalt keepjumps hide buffer ' . origin_buffer
    endif
    call winrestview(origin_view)
  endtry
  if !has_key(s:changes, a:root)
    let s:changes[a:root] = []
  endif
  if index(s:changes[a:root], nr) < 0
    call add(s:changes[a:root], nr)
  endif
  echomsg '[codex] Modified buffer: ' . (empty(bufname(nr)) ? '[No Name] #' . nr : bufname(nr))
  return s:BufferInfo(nr)
endfunction

function! s:ContextForBuffer(nr, root) abort
  let path = empty(bufname(a:nr)) ? '' : resolve(fnamemodify(bufname(a:nr), ':p'))
  let lines = getbufline(a:nr, 1, '$')
  let cursor = getbufvar(a:nr, 'codex_cursor', [1, 1])
  return {'buffer_id': a:nr, 'buffer_uri': 'vim-buffer://' . s:instance . '/' . a:nr,
        \ 'path': path, 'name': empty(path) ? '[No Name]' : fnamemodify(path, ':t'),
        \ 'root': a:root, 'on_disk': filereadable(path) ? v:true : v:false,
        \ 'modified': getbufvar(a:nr, '&modified') ? v:true : v:false,
        \ 'changedtick': getbufvar(a:nr, 'changedtick'), 'filetype': getbufvar(a:nr, '&filetype'),
        \ 'endofline': getbufvar(a:nr, '&endofline') ? v:true : v:false,
        \ 'column_unit': 'byte', 'cursor': s:Position(lines, cursor[0], cursor[1]),
        \ 'lines': lines, 'selection': v:null, 'selected_text': ''}
endfunction

function! codex#Changes() abort
  try
    let root = codex#Context(0, 0, 0).root
    let buffers = filter(copy(get(s:changes, root, [])), 'bufexists(v:val)')
    if empty(buffers)
      echomsg '[codex] No changed buffers in this Vim instance'
      return
    endif
    call popup_menu(map(copy(buffers), '(getbufvar(v:val, "&modified") ? "+ " : "  ") . (empty(bufname(v:val)) ? "[No Name] #" . v:val : bufname(v:val))'),
          \ {'title': ' Codex changed buffers ', 'callback': function('s:ChangeChosen', [buffers])})
  catch
    call s:Error(v:exception)
  endtry
endfunction

function! s:ChangeChosen(buffers, id, index) abort
  if a:index > 0
    execute 'hide buffer ' . a:buffers[a:index - 1]
  endif
endfunction

function! codex#Status() abort
  echomsg '[codex] backend=' . g:codex_pane_backend . ' codex=' . g:codex_executable
        \ . ' python=' . g:codex_python . ' state=' . expand(g:codex_state_dir)
        \ . ' bridge=' . (type(s:job) == v:t_job ? job_status(s:job) : 'not started')
        \ . ' access=' . g:codex_access_scope . ' sandbox_override=' . (empty(g:codex_sandbox_mode) ? 'inherit' : g:codex_sandbox_mode)
        \ . ' approval_override=' . (empty(g:codex_approval_policy) ? 'inherit' : g:codex_approval_policy)
  try
    call codex#Track()
    let path = empty(bufname(s:last_buffer)) ? '' : fnamemodify(bufname(s:last_buffer), ':p')
    echomsg '[codex] Context ready: ' . codex#ContextSummary(codex#BufferRequest('editor_context', {}, s:Root(path)))
  catch
  endtry
endfunction

function! codex#Shutdown() abort
  let s:closing = 1
  if s:context_timer != -1
    call timer_stop(s:context_timer)
    let s:context_timer = -1
  endif
  for root in keys(s:panes)
    call s:DestroyPane(root)
  endfor
  if type(s:job) == v:t_job && job_status(s:job) ==# 'run'
    call ch_sendraw(job_getchannel(s:job), "{\"op\":\"shutdown\"}\n")
    " Give the host time to reap its server and remove private sockets.
    let deadline = reltime()
    while job_status(s:job) ==# 'run' && reltimefloat(reltime(deadline)) < 2.0
      sleep 10m
    endwhile
    if job_status(s:job) ==# 'run'
      call job_stop(s:job)
    endif
  endif
endfunction
