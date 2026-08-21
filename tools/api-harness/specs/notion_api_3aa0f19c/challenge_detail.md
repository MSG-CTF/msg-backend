# 문제 상세

| Method | 경로 | 설명 |
|---|---|---|
| GET | /api/v1/challenges/{challenge_id} | 문제 상세 조회 |
| POST | /api/v1/instances | 인스턴스 생성 |
| GET | /api/v1/teams/me/instance | 인스턴스 상태 조회 |
| POST | /api/v1/instances/{instance_id}/reset | 인스턴스 재시작 |
| DELETE | /api/v1/instances/{instance_id} | 인스턴스 종료 |
| POST | /api/v1/instances/{instance_id}/extend | TTL 연장 |
| POST | /api/v1/challenges/{challenge_id}/submit | 플래그 제출 |
| POST | /api/v1/admin/challenges/{challenge_id}/docker_image | 문제 도커 이미지 등록 |
