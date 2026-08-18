# KOTH 페이지

| Method | 경로 | 설명 |
|---|---|---|
| GET | /internal/teams | KOTH 문제 서버 → 플랫폼 참가 팀 목록 조회 (75팀 등수 계산용) |
| POST | /internal/koth/team_tokens/verify | KOTH 문제 서버 → 플랫폼 팀 토큰 검증 |
| GET | /api/v1/koth/team_token | 참가자가 자기 팀의 KOTH 팀 토큰 조회 |
| GET | /api/v1/koth/leaderboard | KOTH 문제별 순위 조회 |
| GET | /api/v1/koth/clubs | KOTH 문제 6개 목록과 활성 상태 조회 |
| GET | /api/v1/koth/me | 내 팀 KOTH 문제별 점수와 순위 조회 |
| GET | /internal/koth/scores | 플랫폼 백엔드 → KOTH 문제 서버 15분 점수 조회 (논의 중) |
| GET | /api/v1/koth/clubs/{club_id} | 클럽별 KOTH 문제 상세와 현재 점유 상태 조회 |
