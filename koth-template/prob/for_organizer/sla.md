# koth-problem-name SLA

이 문서는 사람이 읽는 KOTH 서비스 상태 설명입니다.
TTL/idle/hard timeout 같은 수명 정책은 출제자가 정하지 않으며, 시스템 공통 기본값이 적용됩니다.
아래 내용은 참고용 목표이며 실제 값은 시스템 설정을 따릅니다.

## 서비스 개요

- 참가자 진입점: `service` 컨테이너의 8080 포트
- 참가자 인증: KOTH 팀 토큰
- 팀 토큰 검증 API: `/internal/koth/team-tokens/verify`
- 점수 조회 주기: 15분
- 점수 조회 API: `GET /internal/koth/scores`
- 점수 조회 주체: 플랫폼 백엔드

## 상태 판정

- healthy: 참가자 진입점이 응답하고, 팀 토큰 인증 및 점수 API가 정상 동작
- unhealthy: 서비스 timeout, 인증 API 연동 실패, 점수 API timeout, JSON 형식 오류, 필수 필드 누락

## 점수 반영

- 플랫폼 백엔드는 15분마다 문제 서버의 `GET /internal/koth/scores`를 호출합니다.
- 문제 서버는 `period_id` 기준으로 팀별 `metric_score`, `rank`, `awarded_score`를 계산합니다.
- `scored_at`은 플랫폼 백엔드가 함께 보내는 호출 시각이며, 값이 다르면 `period_id`를 우선합니다.
- 문제 서버는 응답 전에 해당 period 결과를 저장합니다.
- 같은 `period_id` 재요청에는 저장된 같은 결과를 반환합니다.
- 문제 서버 응답은 `code`, `message`, `data` 공통 응답 봉투를 사용합니다.
- 백엔드는 응답의 `awarded_score`를 팀별 KOTH 누적 점수에 더합니다.
- `metric_score`는 순위 산정 원점수(Double)이고, `awarded_score`는 해당 구간 지급 점수(Long)입니다.
- 전체 KOTH 점수는 지금까지 지급된 `awarded_score` 합산으로 계산합니다.

## 데이터

- 팀 상태는 반드시 팀 토큰 검증 API에서 받은 `team_id` 기준으로 저장합니다.
- 브라우저가 보낸 `team_id`는 사용하지 않습니다.
- 원본 팀 토큰은 저장하거나 로그에 남기지 않습니다.
- period별 점수 결과는 대회 종료 전까지 재조회 가능해야 합니다.
- 한 번 저장한 period 결과는 이후 팀 상태가 바뀌어도 수정하지 않습니다.

## 재시도

- 백엔드가 응답을 받지 못하면 같은 `period_id`로 다시 조회할 수 있습니다.
- 문제 서버는 같은 `period_id`에 대해 멱등하게 동작해야 합니다.
- 아직 결과가 준비되지 않았으면 409 `PERIOD_NOT_READY`로 응답할 수 있습니다.
