# 관리자 페이지

| Method | 경로 | 설명 |
|---|---|---|
| POST | /api/v1/admin/teams/{team_id}/ban | 팀 벤 처리 |
| DELETE | /api/v1/admin/teams/{team_id}/ban | 팀 벤 해제 |
| POST | /api/v1/admin/teams/{team_id}/mileage | 마일리지 관리 |
| GET | /api/v1/admin/teams | 팀별 목록 조회 |
| GET | /api/v1/admin/instances | 인스턴스 목록 |
| POST | /api/v1/admin/payment/checkout | QR 스캔 결제 처리 |
| GET | /api/v1/admin/payment/history | 전체 결제 히스토리 조회 |
| DELETE | /api/v1/admin/payment/{history_id}/refund | 결제 환불 |
| POST | /api/v1/admin/instances/{instance_id}/reset | 인스턴스 강제 재시작 |
| DELETE | /api/v1/admin/instances/{instance_id} | 인스턴스 강제 종료 |
| GET | /api/v1/admin/resources | 계정/노드별 리소스 상태 조회 |
| GET | /api/v1/admin/events | 최근 이벤트 로그 조회 |

메모 (미정 사항):
- 팀 벤 처리 시 롤백 과정 포함 여부 (별도 기능으로 분리할지 논의 중)
- clear 칸 관리, 문제 공개상태 관리 필요
- 주사위 오류시 고정 주사위 지급 처리 필요
- 에러 코드 통일 필요
- admin 계정 관리 방식 미정 (어드민 테이블 별도 vs 어드민 팀 별도)
