# vim-codex

터미널 Vim에서 Codex CLI pane과 답변 popup을 사용합니다. 저장하지 않은
수정과 이름 없는 버퍼도 컨텍스트로 전달합니다. 열린 파일은 버퍼에서 편집하고,
새 파일과 열려 있지 않은 파일은 Codex가 현재 권한에 따라 직접 처리합니다.

Linux, Vim 9.1 (`+terminal +popupwin +job +channel`), Python 3.11 이상,
Codex CLI 0.166.0을 기준으로 구현했습니다. Python 외부 패키지와 fzf는 필요하지
않습니다. tmux는 선택 사항입니다. Codex 로그인은 기존 `codex login` 설정을 사용합니다.

## 설치

Vim 기본 package 기능으로 이 저장소를 설치할 수 있습니다.

```sh
mkdir -p ~/.vim/pack/local/start
ln -s /home/mono/work/vim-codex-plugin ~/.vim/pack/local/start/vim-codex
```

현재 Vim에서만 시험하려면 다음을 실행합니다.

```vim
:set runtimepath^=/home/mono/work/vim-codex-plugin
:runtime plugin/codex.vim
```

## 사용

```vim
:Codex
:CodexAsk 이 함수의 오류를 찾아서 수정해줘
:CodexPopAsk 이 코드가 하는 일을 설명해줘
```

Visual 모드에서 영역을 선택한 뒤 `:`를 누르고 `CodexAsk` 또는
`CodexPopAsk`를 입력하면 선택 영역을 전달합니다. `:'<,'>` 범위는 유지합니다.
질문을 생략하면 질문 입력줄이 나타납니다.

| 명령 | 동작 |
|---|---|
| `:Codex` | pane 열기, 프로젝트의 선택된 pane 대화 이어가기 |
| `:CodexAsk {질문}` | 컨텍스트와 질문 전송, pane이 없으면 열기 |
| `:CodexResume` | 터미널에서 사용한 기존 Codex 대화를 포함해 프로젝트 세션 선택 후 열기 |
| `:CodexNew` | pane용 새 대화 시작 |
| `:CodexHide` | pane 닫기, 대화 기록 유지 |
| `:CodexPopAsk {질문}` | 독립된 popup 세션에 질문, 답변을 도착하는 대로 표시 |
| `:CodexPopResume` | 프로젝트의 popup용 세션 선택만 수행 |
| `:CodexPopNew` | popup용 새 대화 선택 |
| `:CodexPopReopen` | 현재 popup 세션의 마지막 답변 다시 열기 |
| `:CodexPopStop` | 진행 중인 popup 응답 중단 |
| `:CodexChanges` | 이번 Vim에서 Codex가 변경한 버퍼 목록, Enter로 이동 |
| `:CodexStatus` | 실행 파일, pane 설정, bridge 상태 표시 |
| `:CodexContext` | 현재 파일·선택 범위·미저장 상태와 마지막 전달 내역 확인 |
| `:CodexContext!` | 실제 제공했던 컨텍스트 본문을 읽기 전용 split으로 확인 |

Vim 명령 이름 규칙에 따라 대문자로 시작하고 하이픈을 사용하지 않습니다.
일반 모드와 Visual 모드 모두 `\ca`는 `CodexAsk`, `\cpa`는
`CodexPopAsk`의 질문 입력줄을 엽니다. 이 바인딩의 `\`는 `mapleader` 설정과
관계없는 실제 백슬래시 키입니다. 다른 키를 추가하려면 다음과 같이 설정합니다.

```vim
nmap <leader>ca <Plug>(CodexAsk)
xmap <leader>ca <Plug>(CodexAsk)
nmap <leader>cpa <Plug>(CodexPopAsk)
xmap <leader>cpa <Plug>(CodexPopAsk)
```

Popup은 `j/k`, 방향키, `Ctrl-d/u`, `PageDown/Up`, `gg/G`로 스크롤합니다.
`Enter`는 답변을 검색·선택·복사 가능한 Vim vertical split으로 펼칩니다.
`q`와 `Esc`는 popup만 닫습니다. 응답 생성은 계속되며, 중단은
`:CodexPopStop`을 사용합니다. 펼친 답변 버퍼에서는 `q`로 창을 닫습니다.

Pane은 Codex의 기본 CLI 화면입니다. 새 대화와 대화 선택은 Vim 명령
`:CodexNew`, `:CodexResume`으로 관리해야 플러그인이 선택한 세션과 일치합니다.
CLI의 `/new`나 `/resume`으로 직접 전환한 세션은 플러그인 목록과 동기화하지 않습니다.

## 컨텍스트 표시

Vim pane의 상태줄과 tmux pane의 테두리에 다음과 같이 표시합니다.

```text
Context ready: src/top.sv [+] | L12-24 @L24:C8
```

`[+]`는 미저장 수정, `[new]`는 디스크에 없는 버퍼입니다. 일반 실행은
`buffer 180L`, 영역 선택은 줄·열 범위를 표시합니다. 이 표시는 다음 요청에
제공할 Vim 컨텍스트의 미리보기입니다. 다른 파일로 이동하거나 내용을 수정하면
갱신됩니다. tmux 테두리가 꺼져 있으면 해당 window에만 표시줄을 켜며,
마지막 Codex pane을 닫을 때 기존 테두리 설정을 복구합니다.

답변 popup의 제목에는 그 질문에 붙인 파일·범위를 표시합니다.
`:CodexContext`는 현재 미리보기와 마지막 전달 내역을 구분합니다.
`Enter` 또는 `:CodexContext!`로 해당 컨텍스트 본문을 펼칠 수 있습니다.
확인 명령은 질문을 보내거나 선택 영역을 초기화하지 않습니다.

전달 내역은 `pane`/`popup` 질문 접수, `/ide`의 CLI 컨텍스트 조회,
버퍼 도구의 파일 읽기를 각각 기록합니다. `/ide status`도 조회에 해당하므로
IDE에 제공됐다는 기록만으로 모델 요청이 발생했다고 표시하지 않습니다.
본문은 현재 Vim에서 마지막으로 제공한 스냅샷이며, 이후 편집해도 바뀌지 않습니다.
이 내역은 Vim을 종료하면 사라집니다. Codex의 전체 대화 기록, 셸로 읽은 모든 파일,
CLI 내부의 길이 제한 적용 후 모델 입력 전체를 보여주는 기능은 아닙니다.

## 컨텍스트와 버퍼 편집

마지막으로 편집하던 소스 버퍼를 기억합니다. Codex terminal, 답변 버퍼,
popup 또는 세션 선택창으로 포커스를 옮겨도 해당 소스 버퍼를 사용합니다.

* 일반 실행: 버퍼 전체, 경로 또는 버퍼 URI, 파일 형식, 커서 위치.
* 저장된 파일에서 영역 선택: 선택 텍스트, 경로, 시작·끝 위치, 커서 위치.
* 디스크에 없는 버퍼에서 영역 선택: 위 정보와 버퍼 전체.

이름을 붙였지만 아직 저장하지 않은 새 파일도 디스크에 없는 버퍼로 취급합니다.
줄·열 번호는 1부터 시작하며 기본 열 번호의 단위는 바이트입니다.
UTF-16 열 번호도 별도로 제공합니다. 문자·줄·블록 선택을 지원합니다.
명령 실행 시 수집한 컨텍스트가 질문과 함께 전달됩니다. 예전 Visual 영역은
일반 모드 명령에 자동으로 다시 붙이지 않습니다.

파일 편집은 다음과 같이 구분합니다.

| 대상 | 편집과 저장 |
|---|---|
| 이미 열린 버퍼(숨겨진 loaded 버퍼 포함) | 현재 버퍼 내용을 읽고 버퍼에 편집. 미저장 내용도 포함 |
| 새 파일 또는 Vim에 열려 있지 않은 파일 | Codex의 일반 파일·셸 도구로 생성·수정. 현재 세션 권한에 따라 실행 또는 승인 요청 |
| `:badd`만 했거나 unload한 파일 | 미열림 파일로 처리. 읽거나 편집하려고 자동으로 load하지 않음 |

버퍼용 도구는 `editor_context`, `list_buffers`, `read_buffer`, `edit_buffer`,
`save_buffer`입니다. `read_buffer`/`edit_buffer`는 열린 버퍼만 처리하며,
새 파일이나 미열림 파일을 버퍼에 가져오지 않습니다. `:CodexChanges`는 이번
Vim에서 편집한 버퍼를 표시합니다. 디스크 파일의 직접 변경은 Codex 결과에서 확인합니다.

열린 버퍼는 기본적으로 편집만 하고, 검증·빌드 등 다음 단계에서 디스크의 최신
내용이 필요하면 Codex가 이유를 붙여 `save_buffer`를 호출합니다.

* 세션의 현재 권한으로 해당 경로에 쓸 수 있으면 자동 저장합니다.
* 권한이 부족하고 승인 요청이 허용된 정책이면 Vim에서 한 번 허용·해당 파일에
  대해 세션 동안 허용·거절을 선택합니다.
* 승인을 거절하거나, 권한도 없고 요청도 허용되지 않으면 저장하지 않습니다.
  Codex는 미저장 변경으로 검증을 완료했다고 보고하지 않도록 안내받습니다.

쓰기 권한은 시작 시 설정을 추측하지 않고 현재 세션에서 조회합니다. Codex의
실제 sandbox/이름 있는 permission profile 안에서 파일을 변경하지 않는 쓰기
권한 검사를 실행합니다. 따라서 CLI의 `/permissions` 변경도 반영합니다.
명시적으로 허용한 파일별 세션 저장 권한은 permission 설정이 바뀌면 다시 확인합니다.

저장은 Vim의 정상 `:write`를 사용하므로 파일 인코딩·개행과 사용자 쓰기 hook을
유지합니다. 읽은 뒤 버퍼가 바뀌었거나, 디스크가 외부에서 변경됐거나, 승인한
파일 경로가 달라지면 저장을 거부합니다. `readonly`도 강제로 해제하지 않습니다.
쓰기 hook이 다시 버퍼를 수정하면 미저장 상태를 반환하므로 재확인 후 검증해야 합니다.
직접 `:w`/`:wa`를 사용할 수도 있으며, 이름 없는 버퍼는 먼저 저장할 이름을 정해야 합니다.

프로젝트 경계는 가장 가까운 `.git` 디렉터리 또는 파일입니다. Git 프로젝트가
아니면 소스 파일의 디렉터리, 이름 없는 버퍼는 현재 작업 디렉터리를 사용합니다.
기본적으로 프로젝트 밖에 열린 버퍼와 심볼릭 링크 대상도 편집할 수 있습니다.
상대 경로는 세션의 프로젝트를 기준으로 해석합니다. 버퍼 도구의 접근을 프로젝트
안으로 제한하려면 `g:codex_access_scope = 'project'`로 설정합니다.

## `/ide` 연결

Pane CLI에서 `/ide on`을 실행하면 같은 Vim 인스턴스에서 최신 컨텍스트를
받습니다. `/ide status`로 상태를 확인할 수 있습니다. `:CodexAsk`와
`:CodexPopAsk`는 `/ide` 활성화 여부와 관계없이 컨텍스트를 직접 전달합니다.
CLI에 직접 질문할 때도 최신 버퍼를 도구로 읽도록 지침을 제공합니다.

각 Vim 인스턴스에 별도 app-server와 IDE 소켓을 둡니다. CLI 화면의
`CODEX_HOME`만 임시로 분리하며, backend는 기존 Codex 인증·모델 설정과
기록 저장소를 사용합니다. 기존 VS Code/Cursor의 IPC 소켓은 변경하지 않습니다.
CLI의 폴더 신뢰 확인은 Codex 기본 동작을 따릅니다.

`/ide` IPC와 app-server는 Codex 버전에 따라 바뀔 수 있습니다. 0.166.0에서
실제 MCP 도구 연결과 CLI `/ide` 연결을 확인하는 별도 테스트를 제공합니다.

## 설정

플러그인을 로드하기 전에 vimrc에 설정합니다.

```vim
" tmux 안에서는 오른쪽 tmux pane, 밖에서는 Vim vertical split (기본값).
let g:codex_pane_backend = 'tmux'
" 항상 Vim vertical split을 사용하려면 'vim'.

let g:codex_executable = 'codex'
let g:codex_python = 'python3'
let g:codex_model = ''             " 빈 값이면 Codex 기본 설정
let g:codex_extra_args = []        " app-server에 전달할 추가 CLI 인수 목록
let g:codex_access_scope = 'global' " 외부의 열린 버퍼도 접근. 제한하려면 'project'.
let g:codex_sandbox_mode = ''       " 기본: Codex 설정/재개한 세션의 권한을 상속
let g:codex_approval_policy = ''    " 기본: Codex 설정/재개한 세션의 승인 정책을 상속
" 명시적으로 제한할 경우:
" let g:codex_sandbox_mode = 'read-only'
" let g:codex_approval_policy = 'on-request'
" 기본 상태 경로: $XDG_STATE_HOME/vim-codex 또는 ~/.local/state/vim-codex
" let g:codex_state_dir = expand('~/.local/state/vim-codex')
```

tmux pane 생성에 실패하면 이유를 표시하고 Vim split으로 전환합니다.
Popup의 Enter로 여는 답변 창은 이 설정과 관계없이 Vim split입니다.

Pane과 popup은 프로젝트별로 각각 선택된 세션을 기억합니다. `:CodexResume`은
Codex의 저장된 대화도 조회하므로 터미널 CLI에서 시작한 세션을 이어갈 수 있습니다.
같은 Git 프로젝트의 하위 디렉터리에서 시작한 세션도 포함합니다. 목록에는
`CLI`, `IDE`, `Vim` 등의 출처를 표시합니다. 선택한 기존 세션에는 Vim 버퍼 도구와
편집 정책을 연결하며, 원래 세션 ID와 대화 기록을 유지합니다. 조회만 해서는
현재 선택된 대화를 바꾸지 않습니다. Popup 목록은 popup용 세션만 표시합니다.
Vim을 종료해도 선택과 마지막 답변을
보관하며, 다시 열면 해당 대화를 이어갑니다. Codex가 아직 저장하지 않은,
질문이 한 번도 없었던 빈 세션은 재실행 시 새 빈 세션으로 대체합니다.

전역 skill과 그 지원 파일, 셸 도구, 기존 사용자 MCP 및 plugin 설정을 유지합니다.
분리한 CLI home에서도 원래 Codex home의 skill·plugin·규칙을 참조합니다.
기존 설정 파일은 수정하지 않습니다. 열린 파일은 버퍼에서 편집하고, 미열림
파일은 일반 파일 도구로 처리하도록 지시합니다. 외부 skill 실행에 추가 권한이 필요하면 Vim에 요청을 표시하고,
한 번 허용·세션 동안 허용·거절을 선택할 수 있습니다. 사용자의 판단 없이
권한 요청을 일괄 거절하거나 자동 허용하지 않습니다.

Sandbox와 승인 정책은 기본적으로 Codex 설정 또는 재개한 세션에서 상속합니다.
플러그인이 매 질문마다 권한을 초기화하지 않으므로 CLI에서 바꾼 권한이 유지됩니다.
`g:codex_sandbox_mode`/`g:codex_approval_policy`는 시작·재개 시 명시적으로
덮어쓸 필요가 있을 때만 설정하세요. 파일 생성·미열림 파일 수정·skill 실행은
Codex 자체의 권한과 승인 처리를 그대로 따릅니다.

## 검증

```sh
make test          # Python 프로토콜·세션 검사, headless Vim의 버퍼·UI 통합 검사
make test-tmux     # 별도 tmux 서버에서 pane 생성·재사용·닫기 검사
make test-native   # 실제 Codex app-server/MCP/CLI /ide 검사, 모델 질문은 전송하지 않음
```

일반 테스트는 로컬 Codex 대역을 사용합니다. 대역의 응답 생성 과정에서도 실제
MCP subprocess와 Vim channel을 거쳐 미저장 버퍼 수정까지 검증합니다.
실제 모델이 수정 도구를 선택하고 답변하는 과정은 자동 테스트에 포함하지 않습니다.
