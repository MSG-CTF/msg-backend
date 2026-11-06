# 생성·초기화 응답 유실 복구

스케줄러가 요청을 처리한 뒤 응답만 끊기면 백엔드는 같은 작업 ID로 결과를 조회한다
참가자 재시도, 내 인스턴스 조회, 정기 이력 동기화에서 이 결과를 복구한다
관리자 강제 초기화도 같은 작업 기록을 사용하며 요청 당시의 감사 기록을 남긴다

## 적용 순서

1. 스케줄러의 요청 ID 저장과 결과 조회 API를 먼저 배포한다
2. 이 변경의 마이그레이션을 적용하고 백엔드를 배포한다
3. SCHEDULER_REQUEST_RECOVERY_ENABLED를 True로 설정한다
4. 기존 reconcile_instances 정기 작업을 유지한다

스케줄러에 전달한 작업 ID는 InstanceWriteOperation의 기본 키다
결과의 작업 ID, 사용자, 팀, 문제, 릴리스, 교체 원본을 모두 대조한 뒤 저장한다
동일한 원본에 대한 초기화 재시도는 이미 생성된 인스턴스를 반환한다

## 대기시간

| 설정 | 기본값 | 용도 |
| --- | --- | --- |
| SCHEDULER_TIMEOUT_SECONDS | 5초 | 기존 조회 요청 |
| SCHEDULER_WRITE_TIMEOUT_SECONDS | 10초 | 생성·초기화 등 변경 요청 |
| SCHEDULER_REQUEST_LOOKUP_TIMEOUT_SECONDS | 5초 | 작업 ID 결과 조회 |
| SCHEDULER_REQUEST_RECOVERY_WAIT_SECONDS | 600초 | 아직 보이지 않는 작업 결과의 확인 기간 |

결과 조회가 404이면 새 생성 요청을 보내지 않고 확인을 계속한다
확인 기간이 지나도 결과가 없으면 운영진 확인 상태로 남긴다
조회 서버의 일시 장애는 복구 대기를 유지한다

배포 전에 생긴 기록은 request_id_used가 False다
이 기록은 임의의 활성 인스턴스와 연결하지 않는다
기존 recover_instance_writes 명령으로 원격 ID를 확인하고 처리한다

기능 설정을 다시 False로 바꿔도 이미 작업 ID를 전달한 기록의 결과 조회는 유지된다
스케줄러와 백엔드의 추가 컬럼은 기존 버전이 사용하지 않으므로 롤백 시 삭제할 필요가 없다
