set nocompatible
set encoding=utf-8
set nomore
set rtp^=.
runtime plugin/codex.vim
let s:root = getcwd()
let s:temp = tempname()
call mkdir(s:temp, 'p')
call mkdir(s:temp . '/.git')
execute 'cd ' . fnameescape(s:temp)

" A saved file has unsaved content; all reads must see the buffer.
call writefile(['disk original', 'second'], s:temp . '/source.txt')
execute 'edit ' . fnameescape(s:temp . '/source.txt')
call setline(1, 'unsaved first')
let s:context = codex#Context(0, 0, 0)
call assert_equal(['unsaved first', 'second'], s:context.lines)
call assert_true(s:context.on_disk)
call assert_true(s:context.modified)
let s:nr = bufnr('%')
let s:read = codex#BufferRequest('read_buffer', {'buffer_id': s:nr}, s:temp)
call assert_equal('unsaved first', s:read.lines[0])

" A stale edit must fail without altering any text.
call setline(2, 'user typing')
try
  call codex#BufferRequest('edit_buffer', {'buffer_id': s:nr, 'expected_tick': s:read.changedtick,
        \ 'start_line': 1, 'end_line': 1, 'lines': ['wrong']}, s:temp)
  call assert_report('stale edit was accepted')
catch /Stale buffer/
endtry
call assert_equal(['unsaved first', 'user typing'], getline(1, '$'))

" One tool call is one undo operation, preserving the user's prior edits.
let s:read = codex#BufferRequest('read_buffer', {'buffer_id': s:nr}, s:temp)
call codex#BufferRequest('edit_buffer', {'buffer_id': s:nr, 'expected_tick': s:read.changedtick,
      \ 'start_line': 1, 'end_line': 2, 'lines': ['replacement']}, s:temp)
call assert_equal(['replacement'], getline(1, '$'))
call assert_true(&modified)
call assert_equal(['disk original', 'second'], readfile(s:temp . '/source.txt'))
undo
call assert_equal(['unsaved first', 'user typing'], getline(1, '$'))
redo
call assert_equal(['replacement'], getline(1, '$'))

" Unopened files are left to native Codex tools, never imported by a read.
call writefile(['other disk'], s:temp . '/other.txt')
let s:count = len(getbufinfo())
try
  call codex#BufferRequest('read_buffer', {'path': 'other.txt'}, s:temp)
  call assert_report('unopened file was imported into Vim')
catch /not open in Vim/
endtry
call assert_equal(s:count, len(getbufinfo()))
try
  call codex#BufferRequest('edit_buffer', {'path': 'new.txt', 'expected_tick': 0,
        \ 'start_line': 1, 'end_line': 0, 'lines': ['new file']}, s:temp)
  call assert_report('new file was imported into Vim')
catch /not open in Vim/
endtry
call assert_equal(s:count, len(getbufinfo()))
" A user-opened hidden buffer is editable without moving focus or saving.
let s:other_nr = bufadd(s:temp . '/other.txt')
call bufload(s:other_nr)
let s:other = codex#BufferRequest('read_buffer', {'path': 'other.txt'}, s:temp)
let s:window = win_getid()
call codex#BufferRequest('edit_buffer', {'buffer_id': s:other.buffer_id, 'expected_tick': s:other.changedtick,
      \ 'start_line': 1, 'end_line': 1, 'lines': ['other unsaved', 'added']}, s:temp)
call assert_equal(s:nr, bufnr('%'))
call assert_equal(s:window, win_getid())
call assert_equal(['other disk'], readfile(s:temp . '/other.txt'))
call assert_equal(['other unsaved', 'added'], getbufline(s:other.buffer_id, 1, '$'))
call assert_true(getbufvar(s:other.buffer_id, '&modified'))
execute 'hide buffer ' . s:other.buffer_id
undo
call assert_equal(['other disk'], getline(1, '$'))

" Insert at beginning/end and delete all lines.
let s:read = codex#BufferRequest('read_buffer', {}, s:temp)
call codex#BufferRequest('edit_buffer', {'buffer_id': s:read.buffer_id, 'expected_tick': s:read.changedtick,
      \ 'start_line': 1, 'end_line': 0, 'lines': ['inserted']}, s:temp)
call assert_equal('inserted', getbufline(s:read.buffer_id, 1)[0])

" Project-only access remains available as an explicit setting.
let g:codex_access_scope = 'project'
for s:path in ['../outside.txt', '/etc/passwd']
  try
    call codex#BufferRequest('read_buffer', {'path': s:path}, s:temp)
    call assert_report('outside path was accepted: ' . s:path)
  catch /outside the project/
  endtry
endfor
let g:codex_access_scope = 'global'
let s:outside = tempname()
call writefile(['outside original'], s:outside)
call bufload(bufadd(s:outside))
let s:outside_read = codex#BufferRequest('read_buffer', {'path': s:outside}, s:temp)
call codex#BufferRequest('edit_buffer', {'buffer_id': s:outside_read.buffer_id,
      \ 'expected_tick': s:outside_read.changedtick, 'start_line': 1, 'end_line': 1,
      \ 'lines': ['outside unsaved']}, s:temp)
call assert_equal(['outside unsaved'], getbufline(s:outside_read.buffer_id, 1, '$'))
call assert_equal(['outside original'], readfile(s:outside))
call assert_true(index(map(codex#BufferRequest('list_buffers', {}, s:temp), 'v:val.buffer_id'), s:outside_read.buffer_id) >= 0)
call delete(s:outside)

" Save uses Vim's normal write path and keeps undo, encoding and source focus.
execute 'hide buffer ' . s:nr
let s:read = codex#BufferRequest('read_buffer', {'buffer_id': s:nr}, s:temp)
let s:save = codex#BufferRequest('save_buffer', {'buffer_id': s:nr,
      \ 'expected_tick': s:read.changedtick, 'authorized_path': s:read.path}, s:temp)
call assert_true(s:save.saved)
call assert_equal(['replacement'], readfile(s:temp . '/source.txt'))
call assert_false(&modified)
undo
call assert_equal(['unsaved first', 'user typing'], getline(1, '$'))
redo
call assert_equal(['replacement'], getline(1, '$'))

" A save may not overwrite user edits made during permission confirmation.
let s:read = codex#BufferRequest('read_buffer', {'buffer_id': s:nr}, s:temp)
call setline(1, 'typing while waiting')
try
  call codex#BufferRequest('save_buffer', {'buffer_id': s:nr,
        \ 'expected_tick': s:read.changedtick, 'authorized_path': s:read.path}, s:temp)
  call assert_report('stale save was accepted')
catch /Stale buffer/
endtry
call assert_equal(['replacement'], readfile(s:temp . '/source.txt'))

" External disk edits cannot be silently replaced, even when save is allowed.
call writefile(['external process'], s:temp . '/source.txt')
let s:read = codex#BufferRequest('read_buffer', {'buffer_id': s:nr}, s:temp)
call assert_true(s:read.disk_changed)
try
  call codex#BufferRequest('save_buffer', {'buffer_id': s:nr,
        \ 'expected_tick': s:read.changedtick, 'authorized_path': s:read.path}, s:temp)
  call assert_report('external disk edit was overwritten')
catch /changed on disk/
endtry
call assert_equal(['external process'], readfile(s:temp . '/source.txt'))

" Unloading a buffer makes it a normal disk file again.
execute 'silent bunload! ' . s:other_nr
call assert_false(bufloaded(s:other_nr))
try
  call codex#BufferRequest('read_buffer', {'buffer_id': s:other_nr}, s:temp)
  call assert_report('unloaded buffer was implicitly loaded')
catch /not open in a source buffer/
endtry
call assert_false(bufloaded(s:other_nr))

" Exact multibyte character selection, coordinates, and full unnamed buffer.
enew!
call setline(1, ['가나다abc', 'second row'])
set selection=inclusive
call cursor(1, 4)
normal! v2l
execute "normal! \<Esc>"
let s:selected = codex#Context(2, 1, 1)
call assert_equal('나다a', s:selected.selected_text)
call assert_equal(4, s:selected.selection.start.column)
call assert_equal(2, s:selected.selection.start.utf16_column)
call assert_false(s:selected.on_disk)
call assert_equal(['가나다abc', 'second row'], s:selected.lines)
call assert_equal(v:null, codex#Context(0, 0, 0).selection)

" /ide can read a selection still active in Vim without an Ex command first.
call cursor(1, 1)
normal! vl
let s:live = codex#BufferRequest('editor_context', {}, s:temp)
call assert_equal('가나', s:live.selected_text)
execute "normal! \<Esc>"

normal! ggVj
execute "normal! \<Esc>"
let s:selected = codex#Context(2, 1, 2)
call assert_equal('line', s:selected.selection.type)
call assert_equal("가나다abc\nsecond row", s:selected.selected_text)

" Block selection handles tab-expanded columns.
call setline(1, ["\tabc", '123456789'])
set tabstop=4
call cursor(1, 1)
execute "normal! \<C-v>j2l\<Esc>"
let s:selected = codex#Context(2, 1, 2)
call assert_equal('block', s:selected.selection.type)
call assert_true(len(split(s:selected.selected_text, "\n", 1)) == 2)

let s:source = bufnr('%')
new
setlocal buftype=nofile
let b:codex_ui = 1
call assert_equal(s:source, codex#Context(0, 0, 0).buffer_id)

execute 'cd ' . fnameescape(s:root)
call delete(s:temp, 'rf')
if !empty(v:errors)
  call writefile(v:errors, '/tmp/vim-codex-buffer-errors')
  cquit
endif
qa!
