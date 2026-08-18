# AGENTS.md

MSG CTF 백엔드(`MSG-CTF/msg-backend`) 저장소에서 AI 에이전트(Claude Code, Codex 등)가 작업할 때 지켜야 할 규칙.
사람이 읽는 개발 문서가 아니라 에이전트용 작업 규칙 문서다. 여기 적힌 내용과 사용자 지시가 충돌하면 사용자 지시가 우선하고,
여기 적힌 내용과 다른 문서(PRD 등)가 충돌하면 이 문서를 따른다.

담당 범위: 보드(Board) / KOTH 기능. 스택은 Django 5.2 + DRF, 앱은 `apps/` 아래에 있다.

## 1. 절대 하면 안 되는 것

1. 커밋과 PR에 AI 흔적을 남기지 않는다. `Co-Authored-By: Claude`, `Generated with Claude Code`, 🤖 이모지, "AI가 작성함" 류의 문구를 커밋 메시지·PR 본문·코드 주석 어디에도 넣지 않는다. 공동 작성자 표기는 어떤 형태로도 금지.
2. 원격에 올리는 일은 전부 사람(강지원)이 한다. 에이전트는 로컬 커밋까지만 하고 `git push`를 실행하지 않는다. PR 생성, 리뷰 요청, main 병합도 마찬가지다. `gh pr create`, `gh pr merge`, 웹 UI 조작으로 PR을 만들지 않는다.
3. `main` 브랜치에 직접 커밋하지 않는다. 작업은 항상 `feature/*` 브랜치에서 한다.
4. force push(`git push -f`, `--force-with-lease`), 남의 브랜치 rebase, 이미 원격에 올라간 커밋 amend를 하지 않는다.
5. `git reset --hard`, `git clean -fd`, 브랜치 삭제처럼 되돌리기 어려운 명령은 사람 확인 없이 실행하지 않는다.
6. 시크릿을 커밋하지 않는다. `.env`, `DJANGO_SECRET_KEY` 실제 값, `NOTION_TOKEN`, DB 접속 정보는 코드나 문서에 하드코딩하지 않는다.

## 2. 커밋

커밋 전에 아래 순서를 전부 통과해야 한다.

1. `git status`로 의도한 파일만 스테이지에 있는지 확인한다. `git add -A`, `git add .`는 쓰지 않고 경로를 명시해서 add한다.
2. 마이그레이션 누락 확인: `python manage.py makemigrations --check --dry-run --settings=config.dev_settings`
3. 테스트: `python manage.py test --settings=config.test_settings`
4. API 명세를 건드렸으면 하네스 실행(5번 항목 참고). 종료 코드가 0이 아니면 커밋하지 않고 먼저 고친다.
5. 커밋 메시지는 한국어, `feat: 보드 주사위 굴리기 API 추가` 형식. 타입은 feat / fix / refactor / docs / test / chore.
6. 커밋은 기능 단위로 쪼갠다. 마이그레이션 파일은 그 마이그레이션을 만든 모델 변경과 같은 커밋에 넣는다.

로컬 커밋까지가 에이전트가 할 수 있는 최대치다. push, PR 생성, 리뷰 반영 요청, 병합은 사람이 한다.
작업을 마치면 커밋해 둔 상태로 두고, push할 준비가 됐다는 것과 검사 결과를 사람에게 보고한다.

## 3. 커밋에 들어가면 안 되는 경로

현재 `.gitignore`에는 아래 중 일부만 들어 있다. add하기 전에 직접 확인한다.

1. `venv/`, `__pycache__/`, `*.pyc`, `db.sqlite3`, `.env`
2. `_external_2026_MSG_CTF/`, `_external_MSG_CTF_WEB/` — 참고용 외부 저장소 사본
3. `.codex_tmp_*/`, `.worktrees/`, `.claude/` — 에이전트 작업용 임시 디렉토리
4. `runserver.out.log`, `runserver.err.log`
5. `tools/api-harness/reports/` — 하네스 실행 산출물
6. `tools/api-harness/config.yaml` — 노션 page_id와 토큰 설정이 들어가는 파일 (`config.example.yaml`만 커밋)

## 4. 다른 사람과 충돌 나지 않게 하기

팀 담당 분리: 로그인·마이페이지·어드민은 준하, 리더보드·랭킹·타이머는 가연, 보드·KOTH는 지원, 문제상세·열린문제목록은 규민.
원격에 `feature/auth-mypage-api`, `feature/challenge-api`, `feature/timer-api`, `feature/board-api`, `jm/devsecops-pipeline` 등이 동시에 살아 있다.

1. 다른 사람 담당 앱(`apps/accounts`, `apps/adminpanel` 등)은 읽기만 한다. 수정이 필요하면 직접 고치지 말고 사람에게 보고한다.
2. 충돌 다발 파일은 `config/settings.py`(INSTALLED_APPS, MIDDLEWARE), `config/urls.py`, `requirements.txt`, `.gitignore` 네 개다. 여기서는 내 줄만 최소한으로 추가하고, 남이 쓴 줄을 재정렬하거나 포맷팅하거나 알파벳 순으로 정리하지 않는다.
3. `makemigrations`는 앱을 명시해서 돌린다(`python manage.py makemigrations board`). 전체로 돌리면 남의 앱 마이그레이션까지 만들어져서 번호가 충돌한다.
4. 남의 앱 모델에 ForeignKey를 걸어야 하면 문자열 참조(`"accounts.User"`)를 쓰고, 그 앱 모델 파일은 건드리지 않는다.
5. 작업 시작 전에 `git fetch origin` 후 `git log --oneline HEAD..origin/main`으로 main이 앞서갔는지 본다. 앞서 있으면 merge로 따라가고, rebase는 이미 push한 브랜치에서는 쓰지 않는다.
6. 공통 규약(응답 봉투, ID 타입, 에러 코드)을 바꾸는 변경은 다른 담당자 코드에도 영향을 주므로 혼자 결정하지 않는다. 사람에게 먼저 알린다.
7. 커밋 직전 `git diff --stat`으로 내 담당 밖 파일이 섞여 들어갔는지 확인한다.

## 5. 노션 API 명세와 일치하는지 검증

기준 문서 우선순위는 회의에서 확정된 기획안 > 노션 ONBOARDING·API공통 문서 > 저장소 코드 순이다.
노션 문서 허브의 PRD는 AI로 만든 초안이라 기준이 아니다. PRD와 확정 기획안이 다르면 확정 기획안을 따르고, 차이를 사람에게 보고한다.

저장소 안의 규약 사본은 두 개다. `tools/api-harness/conventions.md`가 사람이 읽는 원문이고,
`tools/api-harness/contract.yaml`이 하네스가 기계적으로 검사하는 부분이다. 규약이 바뀌면 두 파일을 같이 고친다.

지켜야 할 핵심 규약:

1. Base URL은 `/api/v1`, 경로 끝에 슬래시를 붙이지 않는다.
2. 응답은 성공·실패 무관하게 항상 `{code, message, data}` 세 키를 모두 포함한다.
3. 성공 판정은 HTTP status가 아니라 `code == "SUCCESS"`다. 플래그 오답 같은 경우는 200 + 다른 code로 내려간다.
4. `data`는 항상 객체 또는 null이고, 배열을 최상위에 두지 않는다. 목록은 `{"challenges": [...], "total_count": 12}`처럼 키로 감싼다.
5. ID는 전부 UUID 문자열이다(2026-08-08 개정). `card_id`, QR 결제 `token`은 예외로 일반 문자열.
6. 시간은 ISO-8601 UTC에 `Z`를 붙인다. KST 변환은 프론트 책임.
7. 리소스명은 복수형, path variable은 snake_case.

검증 절차:

1. 노션 명세 페이지를 로컬 `.md`로 저장한다(`tools/api-harness/specs/` 아래).
2. `python run_harness.py verify-file specs/ --contract contract.yaml`를 `tools/api-harness/`에서 실행한다.
3. 리포트(`reports/report.md`)에서 내 담당(board/KOTH) 항목을 확인하고 고친다. 남의 팀 항목은 고치지 말고 보고만 한다.
4. 노션 페이지 수정이나 댓글 작성은 사람 승인 후에만 한다. 하네스는 노션에 직접 쓰지 않는다.

구현이 명세와 다를 때는 코드를 명세에 맞추는 게 기본이다. 명세 쪽이 틀렸다고 판단되면 코드를 먼저 바꾸지 말고 사람에게 알린다.

## 6. 아키텍처 경계 (플랫폼팀 web/api)

MSG CTF는 플랫폼(web/api), 스케줄러, 리소스 브로커, 런타임/쿠버네티스, DevSecOps로 역할이 나뉘어 있다. 이 저장소는 플랫폼 담당이다.

1. k8s API를 이 저장소에서 직접 호출하지 않는다. 인스턴스 생성·배치·종료는 scheduler/broker/runtime에 요청을 넘긴다.
2. cloud credential을 API 서버나 설정 파일에 넣지 않는다.
3. scheduler/broker/runtime 내부 모델을 그대로 import하지 않는다.
4. 관리자 화면에 secret, token, raw credential을 노출하지 않는다.
5. 비용 계산을 플랫폼이 직접 하지 않는다. broker/monitor에서 받은 숫자를 표시만 한다.

## 7. 코드와 문서 스타일

이 저장소의 커밋은 팀원들이 리뷰한다. AI가 쓴 티가 나면 안 된다.

1. 함수마다 설명 docstring을 붙이지 않는다. 주변 코드의 주석 밀도(거의 없음)에 맞춘다.
2. 프레임워크가 생성한 스캐폴딩 주석(`# Create your models here.` 등)은 지운다.
3. 뻔한 줄에 주석을 달지 않고, 학생이 직접 안 쓸 법한 과한 타입 힌트나 방어 코드를 넣지 않는다.
4. 기준 스타일은 준하의 `apps/accounts`다. 새 코드는 그 스타일에 맞춘다.
5. 문서·명세도 같다. 이모지 불릿, "다음은 ~입니다" 도입부, em-dash 남용, 챗봇식 마무리 인사를 쓰지 않는다. 하네스의 AI 말투 탐지가 이걸 잡는다.
6. 목록은 대시 불릿 대신 번호나 문장으로 쓴다.

## 8. 실행 환경

로컬 개발은 sqlite(`config.dev_settings`), 테스트는 인메모리 sqlite(`config.test_settings`)를 쓴다.

```bash
python manage.py runserver 8000 --settings=config.dev_settings
```

```bash
python manage.py test --settings=config.test_settings
```

```bash
python manage.py seed_board --settings=config.dev_settings
```

보드판(36칸: 문제 30 + 찬스 2 + START/공항/무인도/룰렛 각 1)은 코드에 하드코딩하지 않고 `seed_board` 커맨드로 DB에 넣는다.

## 9. 막히면 멈춘다

아래 상황에서는 추측으로 진행하지 말고 사람에게 묻는다.

1. 확정 기획안과 노션 문서, 실제 코드가 서로 다를 때
2. 내 담당 밖 앱이나 공통 규약을 고쳐야 풀리는 문제일 때
3. 이미 push된 마이그레이션을 되돌려야 할 때
4. 아직 확정되지 않은 규칙(기본 roll_count, 찬스카드 구성, 룰렛 마일리지 확률 등)에 의존하는 구현일 때
5. 되돌리기 어렵거나 외부로 나가는 동작(push, PR, 노션 쓰기, 외부 API 호출)이 필요할 때

## 10. 작업 끝났을 때 보고 형식

1. 바꾼 파일 목록
2. 테스트와 하네스 실행 결과(통과/실패, 실패면 출력 그대로)
3. 노션 명세와 다르게 구현한 부분이 있으면 그 이유
4. 사람이 해야 할 남은 일(push, PR 생성, 노션 반영, 팀원 확인 필요 사항)
