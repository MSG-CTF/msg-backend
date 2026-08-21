# 보드 페이지

| Method | 경로 | 설명 |
|---|---|---|
| GET | /api/v1/board | 보드판 - 전체 칸 배치 조회 |
| GET | /api/v1/board/me | 보드판 팀별 - 내 팀 상태 전체 조회 |
| GET | /api/v1/board/cell/current | 도착한 칸 상세 + 문제 후보 3개 |
| GET | /api/v1/board/dice/status | 주사위 굴릴 수 있는지 상태 조회 |
| POST | /api/v1/board/dice/roll | 주사위 2개 굴려서 이동 (팀장만) |
| POST | /api/v1/board/dice/confirm | 주사위 재굴림 결과 확정 (팀장만) |
| POST | /api/v1/board/chance/now | 찬스칸 도착 시 카드 뽑기 |
| GET | /api/v1/board/chance/catalog | 전체 chance 카드 종류 정의 |
| POST | /api/v1/board/airport/move | Airport 칸 자유 이동 (팀장만) |
| POST | /api/v1/board/chance/use | chance 카드 사용 (주사위 굴리기 전, 팀장만) |
| POST | /api/v1/board/chance/confirm | 찬스카드로 주사위를 두 번 굴린 뒤 선택 결과 확정 (팀장만) |
| POST | /api/v1/board/chance/discard | 보유 찬스카드 2장 중 1장 버리기 (팀장만) |
| POST | /api/v1/board/cell/open | 도착한 칸에서 문제 선택해 오픈 (경로에서 index 제거) |
| POST | /api/v1/board/quarantine/escape | 무인도 탈출 |
| GET | /api/v1/board/opened_challenges | 열린 문제 목록 + 풀이 여부(is_solved) 조회 |
| POST | /api/v1/board/roulette/spin | 룰렛칸 도착 시 50/100/150/200 중 하나를 25% 확률로 뽑아 마일리지 획득 |
