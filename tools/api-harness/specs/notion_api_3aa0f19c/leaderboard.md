# 리더보드 페이지

| Method | 경로 | 설명 |
|---|---|---|
| GET | /api/v1/leaderboard | 상위 8팀 점수 그래프와 팀이름 목록 |

메모: 플래그 제출 → challenges.current_score, teams.team_score 갱신. 리더보드 API 호출 시 team_score 읽어서 표시.
