# msgCTF API 명세 검증 하네스

팀원들이 각자 작성한 API 명세(노션 페이지)를 모아 아래 4가지를 자동으로 점검하는 CLI 도구.

1. **AI 말투 탐지** — 명세 문장에 AI가 쓴 듯한 상투구/패턴이 섞여 있는지 (챗봇식 클로징 인사, "다음은 ~입니다" 도입부, 이모지 불릿, em-dash 남용 등)
2. **팀간 충돌 탐지** — 같은 endpoint(method+path)를 팀원마다 다르게 정의했는지 (요청 필드, 응답 envelope, status code, 인증 요구사항), 그리고 사실상 같은 기능을 서로 다른 경로명으로 각자 만들었는지, 본문에 남긴 "폐기"/"대체" 메모
3. **응답 방식 등 전역 일관성 검증** — 응답 envelope 구조(wrapped/flat), 필드 네이밍 컨벤션(camelCase/snake_case), 타임스탬프 형식, 성공 status code 관례(200 vs 201)가 팀 전체에서 통일되어 있는지 (팀들끼리 상대 비교)
4. **공통 규약 준수 검증** *(선택, `--contract` 지정 시)* — 위 1~3번은 팀들끼리 상대 비교(다수결)지만, 이건 이미 합의된 규약 문서(`contract.yaml`)와 명세를 직접 대조한다: 응답 envelope이 정확히 `{code, message, data}` 3키인지, enum 필드 값이 규약 사전에 있는 값인지(`role`/`category`/`cell.type` 등), 금지 용어를 썼는지

## 설치

```bash
pip install -r requirements.txt
```

## 사용 흐름

이 도구는 Notion에 직접 접근하지 않는다. 3단계로 나뉜다.

1. **노션 페이지 읽기** — Claude(MCP)나 사람이 직접 Notion 페이지를 읽어서 로컬 `.md` 파일로 저장한다.
2. **로컬 파일 검증** — `python run_harness.py verify-file <파일...>` 로 저장된 파일만 검증한다.
   이 단계는 순수 로컬 동작이라 **Notion 토큰이 전혀 필요 없다** — 저장소를 클론한 사람 누구나
   바로 쓸 수 있다.
3. **결과를 댓글로 남기기** — 리포트를 읽고, Claude(MCP)나 사람이 Notion에 댓글을 남긴다.

즉 "Notion 읽기"와 "Notion 쓰기"는 이 저장소 밖에서 일어나고, 이 저장소는 가운데 검증 단계만 책임진다.

## 실행

### 1) 로컬 파일/디렉토리 검증 (기본, 권장)

```bash
# 파일 하나
python run_harness.py verify-file specs/board_dice_roll.md

# 여러 파일
python run_harness.py verify-file specs/team_a.md specs/team_b.md

# 팀원별 하위 폴더 구조
python run_harness.py verify-file specs/
```

경로가 디렉토리면: 하위에 폴더가 있으면 **폴더 하나 = 팀 하나**, 하위에 파일만 있으면
**파일 하나 = 팀 하나**로 취급한다.

```
fixtures/
  alice/api.md
  bob/api.md
  carol/api.md
```

`fixtures/` 에는 데모용 샘플 3인분(alice/bob/carol)이 이미 들어 있고, 의도적으로
AI 말투·응답 envelope 불일치·경로 중복·네이밍 컨벤션 혼용을 포함하고 있어
`python run_harness.py verify-file fixtures` 로 바로 동작을 확인할 수 있다.

### 2) 공통 규약 대비 검증까지 함께 돌리기

`contract.yaml`에 팀이 합의한 규약(enum 사전, ID 타입, base URL 등)을 적어두고:

```bash
python run_harness.py verify-file fixtures --contract contract.yaml
```

`--contract`를 안 주면 1~3번 체크만 수행하고, 4번(공통 규약 준수)은 건너뛴다.

### 3) [레거시] Notion REST API로 직접 조회

CI처럼 사람이 개입하지 않고 자동으로 돌려야 하는 경우에만 사용한다. `NOTION_TOKEN` 발급과
대상 페이지 연결(Connect)이 필요하다.

```bash
cp config.example.yaml config.yaml
# config.yaml의 members[].page_id 를 실제 노션 페이지 ID로 교체
set NOTION_TOKEN=secret_xxx      # PowerShell: $env:NOTION_TOKEN="secret_xxx"
python run_harness.py verify-notion --config config.yaml
```

## 출력

- 콘솔 요약
- `reports/report.md` (상세 Markdown 리포트, `--out` 으로 경로 변경 가능)
- `--json-out reports/report.json` 지정 시 JSON도 함께 생성 (CI 연동 등에 활용)

종료 코드: 발견된 이슈가 하나라도 있으면 `1`, 없으면 `0`, 사용법 오류면 `2`.

## 명세 작성 시 파서가 인식하는 포맷

```markdown
## POST /api/auth/login

### Request
​```json
{ "email": "...", "password": "..." }
​```

### Response
​```json
{ "success": true, "data": { "user_id": 1, "created_at": "2026-07-28T10:00:00Z" } }
​```

status: 201 Created, 401 Unauthorized
```

- 헤딩(`##`~`######`)에 `METHOD /path` 형태가 있으면 그 아래를 하나의 endpoint 블록으로 인식
- `Request`/`Response`/`Error` 소제목 뒤의 ```json``` 코드펜스를 파싱해 필드 목록·응답 envelope·에러 status code를 추출
  (예: `## Error` 아래 `{"status": 404, "code": "...", "message": "..."}` 형태도 인식)
- "header"/"헤더" 라는 단어 근처의 JSON(Request Header 등)은 전송 계층 필드로 보고 request 필드에서 제외
- `status:` 또는 `상태코드` 라는 단어가 포함된 프로즈 줄에서도 status code를 읽음 (JSON 본문 안의 무관한 숫자는 무시)
- `Authorization`/`Bearer`/`인증`/`토큰` 등의 키워드가 있는 줄을 인증 관련 메모로 수집
- 헤딩 블록이 전혀 없는 페이지는 `| Method | Path | ... |` 형태의 표에서 method+path만 추출 (충돌 탐지용)

### [레거시] 노션 DB가 '엔드포인트 1개 = 페이지 1개' 구조인 경우

`verify-notion` 모드에서, 메소드/URL이 헤딩이 아니라 페이지 속성(select/title/url 타입)으로 되어 있는 노션 데이터베이스라면,
`config.yaml`의 members 항목에서 `page_id` 대신 `page_ids` 리스트를 사용한다:

```yaml
members:
  - name: board
    page_ids:
      - "노션-페이지-ID-1"
      - "노션-페이지-ID-2"
```

이 경우 각 페이지의 title(또는 url 타입 속성)이 `/`로 시작하면 경로로, select/rich_text 속성 값이
`GET/POST/PUT/PATCH/DELETE` 중 하나면 메소드로 자동 인식하고, 본문은 `## Request`/`## Response`/`## Error`
섹션 구조로 파싱한다. 속성에서 method/path를 못 찾으면 자동으로 헤딩 기반 파싱으로 폴백한다.

`page_ids`를 일일이 나열하기 번거로우면 `database_id`로 데이터베이스 안의 모든 페이지를 자동 조회할 수 있다:

```yaml
members:
  - name: board
    database_id: "노션-데이터베이스-ID"
```

## `contract.yaml` 작성법 (4번 체크용)

사람이 읽는 전체 규약 원문은 [conventions.md](conventions.md)에 있고, `contract.yaml`은
그중 하네스가 기계적으로 검증 가능한 부분만 옮겨 담은 것이다. 규약이 바뀌면 두 파일을
같이 갱신해야 한다.

팀이 이미 합의해서 문서로 남긴 규약을 그대로 옮겨 적는다:

```yaml
envelope_keys: [code, message, data]   # 성공/에러 응답이 항상 가져야 하는 최상위 키

forbidden_terms:
  - tile                                # 금지 용어 (예: cell로 통일, tile 금지)

enum_rules:
  - leaf: role                          # JSON에서 실제 등장하는 필드명
    values: [PARTICIPANT, ADMIN]        # leaf 이름만으로 전역 매칭 (문맥 없이도 안전한 필드)
  - leaf: type
    parents: [cell, cells]              # leaf가 이 상위 키(배열이면 배열 키) 아래 있을 때만 매칭
    values: [START, CHALLENGE, CHANCE, AIRPORT, QUARANTINE, ROULETTE]
```

`leaf`/`parents`로 나눈 이유: `status`나 `type`처럼 여러 도메인에서 재사용되는 흔한 필드명은
문맥(상위 키) 없이 매칭하면 완전히 다른 의미의 필드까지 잘못 검사하게 된다
(`instance.status`와 `mileage.type`/`cell.type`이 실제로 이런 경우).

## 한계 (휴리스틱 기반)

- 정규식/JSON 코드펜스 기반 파서이므로 명세 포맷이 위 구조에서 크게 벗어나면 인식률이 떨어진다.
- AI 말투 탐지는 "의심 신호" 수집이지 확정 판정이 아니다 — 사람이 최종 검토해야 한다.
- 중복 엔드포인트 탐지는 경로 뒤쪽 세그먼트가 완전히 같을 때만 잡는다(예: `/login` vs `/api/auth/login`).
  `ranking`과 `leaderboard`처럼 표현만 다르고 의미가 같은 경우처럼 어휘가 아예 다른 중복은 못 잡는다 —
  실제 46개 엔드포인트로 검증하면서 문자열 유사도 기반 비교는 오탐이 너무 많아(88건) 이 방식으로 교체했다.
- 응답 envelope 비교는 `code/status/success/message/data/result/payload/error` 같은 흔한 래퍼 키 이름
  기준으로 '감싸는지(wrapped)/그대로 반환하는지(flat)'를 구분한다 — 팀이 전혀 다른 이름의 래퍼 키를 쓰면
  래핑 자체는 감지하지 못하고 flat으로 오분류할 수 있다.

## 프로젝트 구조

```
harness/
  models.py                  # Endpoint / SpecDocument 데이터 모델
  spec_parser.py              # 마크다운 -> Endpoint 파서
  notion_source.py             # Notion REST API 연동 + 오프라인 로더
  checks/
    ai_tone.py                 # 검증 1: AI 말투 탐지
    conflicts.py                # 검증 2: 팀간 충돌 탐지 (팀간 상대 비교)
    response_consistency.py      # 검증 3: 응답 방식 등 전역 일관성 (팀간 상대 비교)
    contract.py                  # 검증 4: 공통 규약 준수 (contract.yaml과 절대 비교)
  report.py                   # 콘솔/Markdown/JSON 리포트 렌더링
  cli.py                       # verify-file(기본) / verify-notion(레거시) 서브커맨드
run_harness.py                 # 실행 진입점
config.example.yaml            # 설정 예시
contract.yaml                  # 공통 규약 정의 (msgCTF "API공통/참고" 문서 기반)
fixtures/                      # 오프라인 데모용 샘플 명세 3인분
```
