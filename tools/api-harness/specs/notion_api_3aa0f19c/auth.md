# 로그인 페이지 (인증)

| Method | 경로 | 설명 |
|---|---|---|
| POST | /api/v1/auth/login | 로그인, 인증 토큰 발급 |
| POST | /api/v1/auth/logout | 로그아웃 |
| GET | /api/v1/auth/me | 로그인 상태 확인 |
| POST | /api/v1/auth/refresh | 인증 토큰 재발급 |

메모: 팀별 토큰 인증 방식 별도 필요 (로그인용 JWT와 다른 팀별 식별 토큰, 인증 API 필요 - 아직 미정)
