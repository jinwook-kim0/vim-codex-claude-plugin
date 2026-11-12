function! PopupList() abort
  if exists('*popup_list')
    return popup_list()
  endif
  " Vim 8.2.0000 exposes popup IDs through buffers, before popup_list existed.
  let ids = []
  for buffer in getbufinfo()
    call extend(ids, get(buffer, 'popups', []))
  endfor
  return uniq(sort(ids, 'n'))
endfunction
