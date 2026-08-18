# checker

checker는 문제 서버 내부에서 팀별 성능값을 계산할 때 참고할 수 있는 보조 프로그램입니다.
최종 점수 결과는 문제 서버의 `GET /internal/koth/scores` API가 반환해야 합니다.

## 입력

환경 변수로 검사 대상을 받습니다.

- `TARGET_HOST`
- `TARGET_PORT`
- `TEAM_ID`
- `KOTH_CHALLENGE_ID`

동아리 하나가 문제를 여러 개 내므로 `CLUB_ID`가 아니라 `KOTH_CHALLENGE_ID`로 대상을 지정합니다. 두 값 모두 UUID 문자열입니다.

## 출력

stdout에 팀별 성능 측정 결과 JSON 하나만 출력합니다.

```json
{
  "team_id": "018f3f1e-0100-7a91-a30b-630000000003",
  "koth_challenge_id": "018f3f1e-0700-7a91-a30b-630000000010",
  "metric_score": 1200,
  "captured_at": "2026-07-28T10:00:00Z"
}
```

문제 서버는 모든 팀의 `metric_score`를 모은 뒤 `period_rank`를 계산해 `GET /internal/koth/scores` 응답으로 반환합니다. 지급 점수는 플랫폼이 배점표로 계산하므로 문제 서버가 내지 않습니다.

## 주의사항

- checker는 참가자에게 공개하지 않습니다.
- checker 내부 secret은 레포에 넣지 말고 운영 환경 변수로 주입합니다.
- 실패 상황에서도 JSON을 출력하도록 작성합니다.
- 실패 시에는 일반적으로 `metric_score`를 0으로 출력합니다.
- timeout 안에 끝나도록 작성합니다.
- checker 출력은 내부 참고용이며, 플랫폼 백엔드가 직접 받는 최종 형식은 `/internal/koth/scores` 응답입니다.
