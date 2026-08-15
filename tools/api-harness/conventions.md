# API 공통 규약

> 출처: 노션 "API공통/참고" 문서 (2026-07-28 확인). `contract.yaml`은 이 문서 중
> 하네스가 기계적으로 검증 가능한 부분(응답 envelope, enum 사전, 금지 용어)만 뽑아 담은
> 것이고, 이 파일은 사람이 읽는 전체 규약 원문이다.

**Base URL**: `/api/v1`

## URL 규칙

- 경로 끝에 슬래시를 붙이지 않는다 (`/api/v1/board` ⭕ / `/api/v1/board/` ❌)
- Path variable은 snake_case (`{team_id}`, `{challenge_id}`)
- 리소스명은 복수형 (`/teams`, `/challenges`, `/instances`)

## 응답 봉투

성공·실패 무관하게 항상 3개 키를 모두 포함한다.

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": null
}
```

## 성공 판정 규칙

> HTTP status가 200이어도 성공이 아닐 수 있다.
> 프론트는 반드시 `code === "SUCCESS"` 로 성공을 판정한다.
> (예: 플래그 오답은 `200 OK` + `code: "INCORRECT_FLAG"`)

## data 형태

- `data`는 항상 객체 또는 null. 배열을 최상위에 두지 않는다.
- 목록은 키로 감싼다: `"data": { "challenges": [...], "total_count": 12 }`

## ID 타입

| 필드 | 타입 | 예시 |
| --- | --- | --- |
| `user_id`, `history_id` | `Long` | `1` |
| `team_id`, `club_id`, `koth_challenge_id`, `challenge_id` | `String` (UUID) | `"018f3f1e-0100-7a91-a30b-630000000003"` |
| `card_id` | `String` | `"card_reroll"` (enum 성격) |
| `token` (QR 결제) | `String` | `"pt_9f8a3c2e"` |

`team_id`, `club_id`, `koth_challenge_id`는 2026-08-16 KOTH 템플릿 확정에 맞춰 UUID 문자열로 바뀌었다. 출제자 배포용 템플릿과 KOTH 명세가 모두 UUID를 쓴다.
`challenge_id`는 2026-08-08 규약 개정으로 이미 UUID 문자열이었으나 이 표에는 반영되지 않고 있었다. `/api/v1/board/opened_challenges` 명세가 근거다.

## 시간 포맷

전부 ISO-8601 UTC, 끝에 `Z` (`"2026-11-08T04:00:00Z"`). 표시용 KST 변환은 프론트 책임.
남은 시간이 필요한 곳은 `expires_at`을 준다.

## HTTP 상태코드 규칙

| 상황 | 코드 |
| --- | --- |
| 조회·수정 성공 | `200` |
| 비동기 큐 적재 (인스턴스 생성/재시작/연장/종료) | `202` |
| 요청 값 오류 | `400` |
| 인증 실패 | `401` |
| 권한 없음 | `403` |
| 리소스 없음 | `404` |
| 상태 충돌 (이미 ~함, ~상태가 아님) | `409` |
| 서버 오류 | `500` |
| 조회 결과가 없음 | `200` · `data: null` (404 아님) |

## 공통 에러 코드

| HTTP | code | message |
| --- | --- | --- |
| 401 | `TOKEN_MISSING` | 로그인이 필요합니다 |
| 401 | `TOKEN_EXPIRED` | 세션이 만료되었습니다 |
| 401 | `TOKEN_INVALID` | 유효하지 않은 인증 정보입니다 |
| 403 | `FORBIDDEN` | 권한이 필요합니다 |
| 403 | `TEAM_BANNED` | 활동이 정지된 팀입니다 |
| 400 | `INVALID_REQUEST` | 요청 값이 올바르지 않습니다 |
| 404 | `USER_HAS_NO_TEAM` | 소속된 팀이 없습니다 |
| 500 | `INTERNAL_ERROR` | 서버 오류가 발생했습니다 |

> `TOKEN_EXPIRED`를 받으면 프론트는 `/auth/refresh`로 자동 재발급 후 1회 재시도한다.
> `TOKEN_MISSING` / `TOKEN_INVALID` 는 재시도 없이 로그인 화면으로 보낸다.

## Enum 사전

| 항목 | 값 |
| --- | --- |
| `role` | `PARTICIPANT`, `ADMIN` |
| `difficulty` | `EASY`, `MEDIUM`, `HARD` |
| `category` | `WEB`, `SYSTEM`, `REV`, `CRYPTO`, `FORENSIC`, `MISC` |
| `instance.status` | `REQUESTED`, `SCHEDULING`, `PROVISIONING`, `RUNNING`, `RESTARTING`, `RESETTING`, `STOPPING`, `STOPPED`, `FAILED`, `EXPIRED`, `CLEANUP_PENDING`, `CLEANED` (scheduler 정의) |
| `mileage.type` | `CHALLENGE_SOLVE`, `START_BONUS`, `ROULETTE`, `KOTH_REWARD`, `ADMIN_GRANT`, `REFUND`, `PURCHASE`, `ADMIN_DEDUCT` |
| `cell.type` | `START`, `CHALLENGE`, `CHANCE`, `AIRPORT`, `QUARANTINE`, `ROULETTE` |

## 팀장 권한 판정

- 팀장은 팀 생성 시 확정되며 대회 중 변경하지 않는다.
- 팀장 여부는 `access_token`의 `is_leader` claim으로만 판정한다. **매 요청 DB 조회를 하지 않는다.**
- 서버는 토큰 서명 검증에 성공한 뒤에만 claim을 읽는다. 요청 body·header·query로 들어온 `is_leader` 값은 절대 신뢰하지 않는다.
- 프론트가 버튼을 숨기는 것은 편의 기능일 뿐이므로, 서버는 항상 독립적으로 재검증한다.
- 팀장만 호출 가능한 API: `POST /board/dice`, `POST /board/airport/move`, `POST /board/chance/use`, `POST /board/roulette/spin`
- 팀장이 아니면 `403 NOT_TEAM_LEADER`
- 관리자(`role: ADMIN`)는 `is_leader`가 항상 `false` → 위 API 호출 불가

> 운영 중 부득이하게 팀장을 바꿔야 하는 경우(계정 분실 등): DB를 수정한 뒤 해당 팀원의
> `refresh_token`을 삭제해 재로그인시켜야 한다. 이미 발급된 `access_token`은 최대 1시간
> 동안 옛 `is_leader` 값을 그대로 들고 있다.

## 밴(BAN) 처리

- 밴된 팀(`is_banned: true`)은 모든 쓰기 작업이 차단된다. 조회(`GET`)는 허용한다.
- 쓰기 = `POST` / `PUT` / `PATCH` / `DELETE`
- 차단 시 `403 TEAM_BANNED`
- **예외 (밴 상태여도 허용)**: `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`
  - 로그인을 막으면 참가자가 "활동이 정지되었습니다" 안내조차 볼 수 없다.
  - 토큰 갱신을 막으면 1시간 뒤 `TOKEN_EXPIRED`가 떠서 밴된 사실이 아니라 장애로 오해한다.
- 관리자 API(`/admin/**`)에는 이 검사를 적용하지 않는다.
- 차단 지점은 Interceptor 한 곳으로 일원화한다. API마다 개별 구현하지 않는다.

> **`is_banned`는 토큰 claim에 넣지 않는다.** 밴은 대회 중에 발생하고 즉시 적용돼야 하므로,
> 쓰기 요청마다 DB에서 조회한다. claim에 넣으면 밴된 팀이 토큰 만료까지 최대 1시간 동안
> 계속 플레이할 수 있다. (`is_leader`와 반대 — 그쪽은 변경되지 않으므로 claim을 쓴다.)

**확인 필요**
- 팀을 밴할 때 실행 중인 인스턴스를 자동 종료할지, 관리자가 수동으로 강제 종료할지
- 밴된 팀을 리더보드/랭킹에 계속 노출할지

## 인스턴스 상태 (scheduler 정의)

플랫폼은 상태를 새로 만들거나 압축하지 않는다. scheduler가 정의한 값을 그대로 전달한다.

| 상태 | 의미 | 접속 가능 | 참가자 화면 문구 |
| --- | --- | --- | --- |
| `REQUESTED` | 생성 요청이 저장된 상태 | ✕ | 요청 접수됨 |
| `SCHEDULING` | Broker 후보 조회·선택 진행 중 | ✕ | 준비 중 |
| `PROVISIONING` | Runtime에 workload 생성 요청 중 | ✕ | 준비 중 |
| `RUNNING` | 생성되어 사용 가능 | ○ | 접속 정보 표시 |
| `RESTARTING` | 재시작 요청 처리 중 | ✕ | 재시작 중 |
| `RESETTING` | 초기화 요청 처리 중 | ✕ | 초기화 중 |
| `STOPPING` | 삭제 요청 처리 중 | ✕ | 종료 중 |
| `STOPPED` | Runtime workload 삭제 완료 | ✕ | 종료됨 |
| `FAILED` | 생성·재시작·초기화·정리 중 실패 | ✕ | 오류 — 다시 시도 |
| `EXPIRED` | TTL 또는 hard timeout 만료 | ✕ | 시간 만료 |
| `CLEANUP_PENDING` | 정리 필요하지만 끝나지 않음 | ✕ | (참가자 미표시) |
| `CLEANED` | Runtime 리소스 정리까지 완료 | ✕ | (참가자 미표시) |

**상태 분류**
- 활성(active): `REQUESTED`, `SCHEDULING`, `PROVISIONING`, `RUNNING`, `RESTARTING`, `RESETTING`, `STOPPING`
- 종료(terminal): `STOPPED`, `FAILED`, `EXPIRED`, `CLEANED`
- `CLEANUP_PENDING`은 리소스가 아직 회수되지 않은 상태다. ⚠️ 팀 동시 실행 제한에 포함할지 확인 필요.

**필드 유효 규칙**
- `host`, `port`, `expires_at`, `remaining_seconds`는 `RUNNING`일 때만 유효하다. 그 외 상태에서는 `null`을 내린다.
- `GET /teams/me/instance`는 활성 상태 인스턴스가 있을 때만 객체를 반환하고, 종료 상태만 남았으면 `data: null`을 반환한다.

**상태 전이**

```
REQUESTED → SCHEDULING → PROVISIONING → RUNNING
RUNNING → RESTARTING → RUNNING
RUNNING → RESETTING  → RUNNING
RUNNING → STOPPING   → STOPPED
RUNNING → EXPIRED (TTL / hard timeout)
어느 단계든 → FAILED
STOPPED / FAILED / EXPIRED → CLEANUP_PENDING → CLEANED
```

> 프론트는 모르는 상태값을 받으면 "준비 중"으로 처리하고 폴링을 계속한다. scheduler에
> 상태가 추가돼도 화면이 깨지지 않도록 방어적으로 짜둘 것.

## 마일리지 타입

마일리지가 오가는 모든 지점을 타입으로 구분한다.

| type | 발생 지점 | 부호 |
| --- | --- | --- |
| `CHALLENGE_SOLVE` | 문제 해결 | + |
| `START_BONUS` | START 칸 통과 | + |
| `ROULETTE` | 룰렛칸 당첨 | + |
| `KOTH_REWARD` | KOTH 보상 | + |
| `ADMIN_GRANT` | 관리자 수동 지급 | + |
| `REFUND` | 결제 환불 | + |
| `PURCHASE` | QR 결제 (부스 구매) | − |
| `ADMIN_DEDUCT` | 관리자 수동 차감 | − |

**규칙**
- 부호는 `amount` 필드가 가진다. `type`은 그 이유를 나타낼 뿐이다.
- **`direction`·`EARN`·`SPEND` 같은 별도 부호 필드를 두지 않는다.** 같은 정보를 두 곳에
  저장하면 언젠가 서로 어긋난다. 필요하면 `type`에서 유도한다.
- 불변식: `mileage_history`의 `amount`를 전부 더하면 `Team.mileage`와 일치해야 한다.
- 이미 쌓인 행은 수정하거나 삭제하지 않는다. 되돌려야 하면 반대 방향 행을 새로 쌓는다
  (예: `PURCHASE -30` → `REFUND +30`). 장부와 같은 원리로, "무슨 일이 있었는지" 기록이 남는다.

> ⚠️ 힌트 구매를 도입하기로 하면 `HINT_PURCHASE`(−)를 추가한다. 현재 기획 미정.

## 용어

- 보드판 칸은 **cell**로 통일 (`total_cells`, `cells`, `CELL_NOT_FOUND`). **`tile` 사용 금지.**
- 문제 리소스 자체의 제목은 `title`, 다른 리소스에 얹힌 참조는 `challenge_title`.

---

## 이 문서와 `contract.yaml`의 관계

하네스(`checks/contract.py`)는 이 문서 전체를 이해하지 못한다. 아래 4가지, 기계적으로
비교 가능한 부분만 `contract.yaml`에 옮겨서 검증한다.

| 이 문서의 내용 | `contract.yaml` 키 | 검증 방식 |
| --- | --- | --- |
| 응답 봉투 (`code`/`message`/`data`) | `envelope_keys` | 성공·에러 응답 최상위 키 집합이 정확히 일치하는지 |
| Enum 사전 | `enum_rules` | 명세에 등장하는 값이 허용 목록 안에 있는지 (nested 필드까지) |
| 용어 (`tile` 금지) | `forbidden_terms` | 명세 본문에 금지어가 있는지 |
| data 형태 (배열 금지) | (코드에 하드코딩, `data_top_level_array` 체크) | 최상위 `data`가 배열 타입인지 |

팀장 권한 판정, 밴 처리, 인스턴스 상태 전이, 마일리지 불변식 같은 **비즈니스 로직 규칙**은
자연어 설명을 정확성 있게 자동 검증하기 어려워 하네스 범위 밖으로 뒀다 — 이건 코드 리뷰나
사람 검토로 확인해야 한다.
