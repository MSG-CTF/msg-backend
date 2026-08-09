# Backend Code Harness Conventions

1. 외부 API에 노출되는 모든 필드명은 `snake_case`만 허용한다.

검사 대상:
- request body JSON key
- response JSON key
- query parameter 이름
- path variable 이름
- serializer에서 외부로 노출되는 field 이름
- API 문서에 적힌 request/response 필드명

HTTP header 이름은 `snake_case` 검사 대상에서 제외한다.

2. 외부 contract 후보 필드 리스트를 출력한다.

대상:
- `request.data`에서 읽는 key
- response dict key
- serializer field
- query parameter
- path variable
- API 문서의 request/response JSON key

serializer를 사용하는 endpoint는 serializer field를 검사한다.  
serializer를 사용하지 않는 endpoint는 response dict key를 외부 노출 field로 간주해 검사한다.

3. 외부 API에 노출되는 동일 의미 필드는 request/serializer/response/API 문서에서 같은 이름을 사용한다.

DB 내부 전용 필드, `write_only`/`read_only` 필드는 response 노출 금지 필드 또는 예외 목록으로 관리한다.

표준 필드명과 deprecated 필드명은 `canonical_fields.yaml`을 따른다.

4. 응답 봉투 형식을 검사한다.

성공·실패와 무관하게 모든 API 응답의 최상위 key는 정확히 `code`, `message`, `data`만 허용한다.

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": null
}
.env, secret, token, DB dump 커밋을 금지한다.
.gitignore만 확인하지 않고 staged diff와 tracked file 기준으로 파일명과 내용을 검사한다.
검사 후보:
git diff --cached --name-only
git diff --cached
git ls-files
endpoint path 리스트를 출력한다.
endpoint path 리스트는 Django route와 API contract 파일을 각각 출력하고 차이를 비교한다.
Public API endpoint는 trailing slash 없음으로 통일한다.
trailing slash 정책은 최종 public URL 기준으로 검사한다. Django include path와 leaf route 조합 결과도 검사 대상이다.
예:
/api/v1/board
/api/v1/board/me
count 필드는 _count suffix를 사용한다.
예:
total_cell_count
solved_team_count
retry_count
forbidden response field가 response에 출력되지 않게 한다.
forbidden_response_fields는 response JSON key의 exact match로 검사한다.
단어 포함 검색으로 검사하지 않는다.
response에서 exact key token은 금지한다.
access_token, refresh_token, team_token은 endpoint contract에 명시된 경우에만 허용한다.
forbidden_response_fields는 response JSON key에만 적용한다.
request header, request body, DB column 검사에는 적용하지 않는다.
예: token 금지는 token key만 막고, access_token/refresh_token/team_token은 별도 contract 기준으로 판단한다.
금지 필드 목록은 forbidden_response_fields.json을 따른다.
API 문서 파일과 코드를 비교한다.
API 문서는 Notion 링크 복붙이 아니라 파일 형태로 둔다.
비교 대상:
Django route
serializer
request 처리 key
response key
문서에만 있는 항목과 코드에만 있는 항목을 출력한다.
null과 빈 배열 [] 사용 기준을 통일한다.
각 필드가 null인지 빈 배열인지의 최종 기준은 endpoint별 contract 파일에 명시한다.
null:
- 단건 객체가 없을 때
- 아직 값이 없는 scalar일 때

[]:
- 목록이지만 비어 있을 때
외부 서비스 의존 테스트는 기본 하네스에서 제외한다.
제외된 endpoint/test 목록은 하네스 결과에 출력한다.
rg로 camelCase response key를 탐지한다.
rg 탐지는 보조수단이며, 최종 실패 판정은 JSON key/parser 기반 검사로 한다.
git diff --check로 whitespace를 확인한다.

git status --short로 예상 밖 변경 파일을 확인한다.

migration 누락 여부를 확인한다.

python manage.py makemigrations --check --dry-run
Notion에서 export한 contract file과 실제 API response snapshot을 비교한다.
기본 하네스는 Notion live API를 호출하지 않는다.
snapshot 비교 시 아래 값은 정규화한다.
- UUID
- timestamp
- access_token
- refresh_token
- dice 결과
- generated id

정규화된 동적 값은 값 자체가 아니라 타입/형식으로 검사한다.
예: UUID string, ISO-8601 UTC string, integer, boolean
response message와 API 문서 sample의 한국어 문자열이 UTF-8로 깨지지 않았는지 검사한다.