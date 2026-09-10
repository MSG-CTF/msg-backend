# KOTH 참가자 접속 URL

각 KOTH 문제 응답에 `challenge_url` 필드를 추가한다. 기존 응답 필드와 인증 조건은 유지한다.

| API | 필드 위치 | 인증 |
| --- | --- | --- |
| `GET /api/v1/koth/clubs` | `data.clubs[].challenges[].challenge_url` | 불필요 |
| `GET /api/v1/koth/clubs/{club_id}` | `data.challenges[].challenge_url` | 불필요 |
| `GET /api/v1/koth/me` | `data.challenges[].challenge_url` | Bearer 토큰 |

## 응답 규칙

- 타입: `string | null`. 문제 응답마다 필드가 항상 포함된다.
- 문제 서버 주소를 등록하지 않았거나 지운 경우 `null`을 반환한다.
- 등록한 경우 참가자가 접속할 수 있는 전체 HTTP(S) URL을 반환한다. 포트, 경로, 쿼리를 그대로 유지한다.
- 문제 상태와 관계없이 등록된 주소를 반환한다. 프론트는 `status === "ACTIVE"`이고 URL이 있을 때 접속 버튼을 활성화한다.
- 내부 점수 수집 주소인 `score_api_url`에서 접속 주소를 추정하지 않는다. 내부 URL과 인증 정보는 응답에 포함하지 않는다.

주소 미등록 시 문제 객체 예시(일부 필드):

```json
{
  "title": "KOTH A",
  "status": "ACTIVE",
  "challenge_url": null
}
```

주소 등록 후 같은 문제 객체 예시(아래 주소는 예시용):

```json
{
  "title": "KOTH A",
  "status": "ACTIVE",
  "challenge_url": "http://192.0.2.10:8080/"
}
```

## 적용 및 주소 등록

1. 코드 배포 시 `python manage.py migrate`를 실행한다. 기존 문제의 주소는 빈 값으로 초기화되며 API에서는 `null`로 표시된다.
2. 주소가 확정되면 Django 관리자(`/admin/`)의 KOTH 문제 수정 화면에서 `Challenge url`을 입력하고 저장한다. 미정일 때는 비워 둬도 된다.
3. 참가자가 실제로 접근할 수 있는 도메인 또는 IP와 필요한 포트·경로를 입력한다. 프록시를 통해 HTTPS로 제공한다면 공개 HTTPS 주소를 입력한다. API가 `:8080`을 자동으로 붙이지 않는다.
4. 프론트는 URL이 없으면 “접속 주소 준비 중” 등으로 표시하고, 주소가 등록되면 해당 URL로 이동하도록 연결한다.

## 내부 채점 주소

참가자용 `challenge_url`과 별도로 `score_api_url`에는 최종 HTTP(S) 채점 주소를
등록한다. 내부 인증 토큰이 다른 서버로 전달되지 않도록 3xx 응답은 따라가지 않고
채점 실패로 처리한다. 주소에 사용자 정보나 fragment를 넣지 않는다.
기존 쿼리는 보존하며 `period_id`와 `scored_at`은 요청하는 채점 구간으로 설정한다.
출제자용 checker도 지정된 HTTP 서버의 직접 응답만 검사하며 리다이렉트는 실패로 처리한다.
