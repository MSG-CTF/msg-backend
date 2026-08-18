# 운영자 문서

이 문서는 참가자에게 공개하지 않습니다.

## 배포 전 확인

- `docker compose up --build`로 서비스가 정상 실행되는지 확인
- 팀 토큰 입력 화면이 있는지 확인
- 문제 서버가 `/internal/koth/team-tokens/verify`를 호출해 `team_id`를 받아오는지 확인
- 브라우저가 보낸 `team_id`를 사용하지 않는지 확인
- `GET /internal/koth/scores`가 정상 JSON을 반환하는지 확인
- 같은 `period_id`로 재요청했을 때 같은 결과가 반환되는지 확인
- 실제 운영 secret, 팀 토큰 원본, 내부 토큰이 레포나 로그에 들어가지 않았는지 확인

## 플랫폼팀 제공 항목

플랫폼팀은 출제자에게 다음 값을 제공해야 합니다.

- `koth_challenge_id`: 문제 식별값
- 팀 토큰 검증 API 주소: `/internal/koth/team-tokens/verify`
- 전체 팀 조회 API 주소: `/internal/teams`
- 문제 서버 내부 API 인증값: `X-KOTH-Internal-Token`
- 플랫폼 API 호출용 인증값: `X-Internal-Token`
- 15분 채점 구간 규칙: `period_id`, `scored_at` 형식

## 팀 토큰 인증 흐름

KOTH 문제는 팀별 토큰으로 참가 팀을 식별합니다.
로그인 JWT와 KOTH 팀 토큰은 서로 다른 값입니다.

1. 참가자가 플랫폼에서 자기 팀 토큰을 확인합니다.
2. 참가자가 KOTH 문제 서버에 팀 토큰을 입력합니다.
3. 문제 서버가 플랫폼 백엔드의 `/internal/koth/team-tokens/verify`를 호출합니다.
4. 플랫폼 백엔드가 `valid`, `team_id`, `team_name`, `koth_challenge_id`를 반환합니다.
5. 문제 서버는 반환받은 `team_id`를 기준으로 로그인 세션과 팀별 상태를 관리합니다.
6. 원본 팀 토큰은 저장하거나 로그에 남기지 않습니다.

검증 API 요청:

```json
{
  "koth_challenge_id": 10,
  "team_token": "<TEAM_TOKEN>"
}
```

검증 API 응답:

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "valid": true,
    "team_id": 3,
    "team_name": "MJSEC",
    "koth_challenge_id": 10
  }
}
```

검증 실패 응답:

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "valid": false,
    "team_id": null,
    "team_name": null,
    "koth_challenge_id": 10
  }
}
```

필수 규칙:

- 출제자에게 전체 팀 토큰 목록을 전달하지 않습니다.
- 브라우저가 보낸 `team_id`는 사용하지 않습니다.
- 차단된 팀 토큰은 검증에 실패해야 합니다.
- 문제 서버용 내부 인증값은 KOTH 문제마다 따로 발급합니다.

## 전체 팀 조회

채점/문제 서버가 점수 계산에 사용할 참가 팀 목록을 가져옵니다.

Method: `GET`

URL: `/internal/teams`

Header:

```json
{
  "X-Internal-Token": "<INTERNAL_TOKEN>"
}
```

Query:

```json
{
  "koth_challenge_id": 10
}
```

Response:

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "teams": [
      {
        "team_id": 3,
        "team_name": "MJSEC"
      },
      {
        "team_id": 4,
        "team_name": "TEAM4"
      }
    ],
    "total_count": 2
  }
}
```

차단된 팀은 제외하며, 팀 토큰 원본은 반환하지 않습니다.
전체 팀 수가 75팀이어도 같은 응답 구조를 사용하고, 목록은 항상 `data.teams`로 감쌉니다.

## 문제 서버 공통 점수 API

플랫폼 백엔드는 15분마다 각 KOTH 문제 서버의 아래 API를 호출합니다.
문제 서버가 플랫폼으로 점수를 POST하는 방식은 사용하지 않습니다.

Method: `GET`

URL: `/internal/koth/scores`

Header:

```json
{
  "X-KOTH-Internal-Token": "<문제별 내부 인증값>"
}
```

결과가 없는 구간:

```json
{
  "code": "SUCCESS",
  "message": "결과가 없습니다.",
  "data": null
}
```

Query:

```json
{
  "period_id": "2026-07-28T10:15:00Z",
  "scored_at": "2026-07-28T10:15:00Z"
}
```

`period_id`는 15분 구간의 시작 시각을 ISO-8601 UTC(`Z`)로 보냅니다. 분 값은 `00`, `15`, `30`, `45` 중 하나여야 하며, `scored_at`과 값이 다르면 `period_id`를 기준으로 계산합니다.

Response:

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "koth_challenge_id": 10,
    "period_id": "2026-07-28T10:15:00Z",
    "results": [
      {
        "team_id": 3,
        "rank": 1,
        "metric_score": 98.73,
        "awarded_score": 100
      },
      {
        "team_id": 4,
        "rank": 2,
        "metric_score": 92.15,
        "awarded_score": 70
      }
    ],
    "total_count": 2
  }
}
```

## 점수 계산 규칙

출제자는 15분 채점 구간마다 팀별 결과를 계산할 수 있어야 합니다.

필수 결과 필드:

- `team_id`: 팀 토큰 검증으로 확인한 팀 ID
- `rank`: 해당 구간 등수
- `metric_score`: 해당 구간 순위 산정에 사용한 원점수(Double)
- `awarded_score`: 해당 구간 지급 점수(Long)

필수 규칙:

1. 같은 `period_id` 요청에는 같은 결과를 반환합니다.
2. 이미 계산한 `period_id`는 다시 계산하지 않고 저장된 결과를 그대로 반환합니다.
3. 응답 전에 해당 구간 결과를 문제 서버에 저장합니다.
4. 지난 구간 결과를 대회 종료 전까지 다시 조회할 수 있게 합니다.
5. 결과가 없는 구간은 200 OK, `code: "SUCCESS"`, `data: null`로 응답합니다.
6. 백엔드가 응답을 받지 못하면 같은 `period_id`로 재요청할 수 있습니다.
7. 한 번 저장한 구간 결과는 이후 등수가 바뀌어도 수정하지 않습니다.
8. `team_id`는 팀 토큰 검증 API에서 받은 값만 사용합니다.
9. 브라우저에서 받은 `team_id`는 사용하지 않습니다.
10. 모든 응답 body는 `code`, `message`, `data` 3개 키를 포함합니다.
11. `data`는 객체 또는 null이며, 배열을 최상위에 두지 않습니다.

## 백엔드 처리 기준

플랫폼 백엔드는 다음 순서로 동작합니다.

1. 15분마다 `ACTIVE` 상태인 KOTH 문제 서버를 조회합니다.
2. 응답을 못 받으면 같은 `period_id`로 재시도합니다.
3. 한 구간의 재시도 횟수와 간격은 플랫폼팀 정책을 따릅니다.
4. 이미 처리한 `period_id`는 다시 점수에 반영하지 않습니다.
5. 해당 팀이 이 문제에서 처음 양수 점수를 받으면 KOTH SOLVE를 만듭니다.
6. 이후 점수는 기존 SOLVE의 `earned_score`에 더합니다.
7. 기존 SOLVE의 `solved_at`은 수정하지 않습니다.
8. 채점 응답 원본은 운영 확인용 로그로 저장합니다.

## API 테스트 항목

- 정상 `period_id` 첫 요청: 해당 구간 결과 반환
- 같은 `period_id` 재요청: 첫 요청과 같은 결과 반환
- 지난 `period_id` 요청: 저장된 과거 결과 반환
- 잘못된 내부 인증값: 오류 응답
- 잘못된 팀 토큰: `valid: false` 처리
- 동점 팀 존재: 작성한 동점 기준대로 반환
- 문제 상태 오류 또는 채점 실패: 오류 응답

## 오류 처리

- 400 `INVALID_PERIOD`: 채점 구간 값 오류
- 401 `INVALID_INTERNAL_TOKEN`: 문제 서버 인증 실패
- 409 `PERIOD_NOT_READY`: 아직 해당 구간 결과가 준비되지 않음
- 500 `SCORING_FAILED`: 문제 서버 채점 실패

오류 응답 예시:

```json
{
  "code": "INVALID_PERIOD",
  "message": "채점 구간 값이 올바르지 않습니다.",
  "data": null
}
```

## 공개 API 연결

`info.yaml`의 값은 공개 API에 다음처럼 연결됩니다.

- `koth.club.club_id` -> `/api/v1/koth/clubs[].club_id`
- `koth.club.name` -> `/api/v1/koth/clubs/{club_id}.name`
- `koth.club.open_group` -> `/api/v1/koth/clubs[].open_group`
- `koth.club.status` -> `/api/v1/koth/clubs[].status`
- `koth.challenge.koth_challenge_id` -> 백엔드 DB의 KOTH 문제 ID
- `koth.challenge.title` -> `/api/v1/koth/clubs[].title`
- `koth.challenge.category` -> `/api/v1/koth/clubs[].category`
- `koth.scoring.awards[].awarded_score` -> `GET /internal/koth/scores`의 `results[].awarded_score`

전체 KOTH 등수는 백엔드가 KOTH SOLVE의 `earned_score`를 합산해 계산합니다.
