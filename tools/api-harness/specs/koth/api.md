# KOTH API 계약 스냅샷

노션 KOTH 데이터베이스를 2026-08-24에 내보낸 로컬 계약 파일이다. 동적 값(UUID, UTC 시각, 팀 토큰)은 예시 형식만 검증한다.

## GET /api/v1/koth/clubs

인증 없음.

### Response

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "clubs": [{
      "club_id": "018f3f1e-0600-7a91-a30b-630000000001",
      "name": "MJSEC",
      "challenges": [{
        "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
        "title": "KOTH A",
        "status": "ACTIVE",
        "open_group": 1,
        "current_owner_team_id": "018f3f1e-0100-7a91-a30b-630000000003",
        "current_owner_team_name": "MJSEC",
        "current_score": 200,
        "opened_at": "2026-07-31T10:00:00Z",
        "closed_at": null
      }]
    }],
    "total_count": 3,
    "challenge_count": 6,
    "active_count": 2
  }
}
```

Status Code: 200

### Error

Status Code: 500

```json
{"code":"KOTH_CHALLENGES_LOAD_FAILED","message":"KOTH 문제 목록을 불러오지 못했습니다.","data":null}
```

## GET /api/v1/koth/clubs/{club_id}

인증 없음. `club_id`는 UUID 문자열이다.

### Response

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "club_id": "018f3f1e-0600-7a91-a30b-630000000001",
    "name": "MJSEC",
    "challenges": [{
      "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
      "title": "KOTH A",
      "status": "ACTIVE",
      "open_group": 1,
      "opened_at": "2026-07-31T10:00:00Z",
      "closed_at": null,
      "current_owner_team_id": "018f3f1e-0100-7a91-a30b-630000000003",
      "current_owner_team_name": "MJSEC",
      "current_score": 200
    }],
    "challenge_count": 2
  }
}
```

Status Code: 200

### Error

Status Code: 400

```json
{"code":"INVALID_CLUB_ID","message":"club_id 형식이 올바르지 않습니다.","data":null}
```

Status Code: 404

```json
{"code":"CLUB_NOT_FOUND","message":"존재하지 않는 동아리입니다.","data":null}
```

## GET /api/v1/koth/me

Authorization: Bearer JWT 필요.

### Response

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "team_id": "018f3f1e-0100-7a91-a30b-630000000003",
    "team_name": "MJSEC",
    "total_koth_score": 250,
    "challenges": [{
      "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
      "club_id": "018f3f1e-0600-7a91-a30b-630000000001",
      "title": "KOTH A",
      "status": "ACTIVE",
      "earned_score": 200,
      "rank": 1,
      "solved_at": "2026-07-31T10:15:00Z",
      "opened_at": "2026-07-31T10:00:00Z",
      "closed_at": null
    }],
    "total_count": 6,
    "active_count": 2
  }
}
```

Status Code: 200, 401

## GET /api/v1/koth/team_token

Authorization: Bearer JWT 필요. 같은 팀의 모든 구성원은 같은 `team_token`을 받는다.

### Response

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "team_id": "018f3f1e-0100-7a91-a30b-630000000003",
    "team_name": "MJSEC",
    "team_token": "koth_example_token",
    "issued_at": "2026-07-31T10:00:00Z"
  }
}
```

Status Code: 200, 401, 404

## POST /internal/koth/team_tokens/verify

문제 서버 전용 API. Header `X-Internal-Token` 필요.

### Request

```json
{
  "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
  "team_token": "koth_example_token"
}
```

### Response

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "valid": true,
    "team_id": "018f3f1e-0100-7a91-a30b-630000000003",
    "team_name": "MJSEC",
    "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010"
  }
}
```

검증 실패 시 `valid`는 `false`이고 `team_id`, `team_name`은 `null`이다.

Status Code: 200

### Error

Status Code: 400

```json
{"code":"INVALID_REQUEST","message":"요청 값이 올바르지 않습니다","data":null}
```

Status Code: 401

```json
{"code":"INVALID_INTERNAL_TOKEN","message":"문제 서버 인증에 실패했습니다.","data":null}
```

## GET /internal/teams

문제 서버 전용 API. Header `X-Internal-Token`과 query `koth_challenge_id`가 필요하다. 차단된 팀은 제외한다.

### Request

```json
{"koth_challenge_id":"018f3f1e-0700-7a91-a30b-630000000010"}
```

### Response

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "teams": [{"team_id":"018f3f1e-0100-7a91-a30b-630000000003","team_name":"MJSEC"}],
    "total_count": 1
  }
}
```

Status Code: 200, 401

## GET /internal/koth/scores

이 endpoint는 Django 수신 route가 아니다. 플랫폼의 `poll_koth_scores` 관리 커맨드가 KOTH 문제 서버에 15분마다 호출하는 outbound contract다. Header `X-KOTH-Internal-Token`, query `period_id`, `scored_at`을 사용한다.

### Request

```json
{"period_id":"2026-07-31T10:15:00Z","scored_at":"2026-07-31T10:15:00Z"}
```

### Response

```json
{
  "code": "SUCCESS",
  "message": "성공",
  "data": {
    "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
    "period_id": "2026-07-31T10:15:00Z",
    "results": [{
      "team_id": "018f3f1e-0100-7a91-a30b-630000000003",
      "period_rank": 1,
      "metric_score": 98.73
    }]
  }
}
```

빈 구간은 `data`가 `null`이다. Status Code: 200, 400, 401, 409, 500
