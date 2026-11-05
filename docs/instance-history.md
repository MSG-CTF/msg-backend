# 종료된 인스턴스 이력 갱신

생성·실행 중인 이력도 조회해 TTL·하드타임아웃 종료를 반영합니다
새 인스턴스가 실행 중이어도 이전 STOPPING·CLEANUP_PENDING 기록을 따로 조회합니다
사용자가 화면을 닫거나 현재 실행이 없어져도 주기 작업이 갱신을 이어갑니다
일반 삭제도 같은 방식으로 처리합니다

스케줄러가 반환한 인스턴스·팀·문제·릴리스가 저장된 값과 일치할 때만 상태를 반영합니다
현재 단건 응답에 없는 user_id는 응답에 포함된 경우에만 대조합니다
404나 통신 오류만으로 CLEANED를 기록하지 않습니다
조회 중 변경된 행은 건너뛰고 다음 주기에 다시 확인합니다
실행 설정과 이미 저장된 삭제 사유는 보존합니다
비어 있는 삭제 사유는 스케줄러의 delete_reason을 사용합니다
TTL_EXPIRED·IDLE_EXPIRED는 같은 값으로, HARD_TIMEOUT_EXPIRED는 백엔드 HARD_TIMEOUT으로 저장합니다
CREATE_FAILED_CLEANUP은 생성 실패의 정리 사유이므로 참가자 삭제 사유를 만들지 않습니다
기존 스케줄러가 사유를 생략하면 시간을 비교해 추측하지 않습니다
종료 사유까지 반영하려면 스케줄러의 delete_reason 응답을 먼저 배포해야 합니다
참가자 조회로 먼저 종료 상태를 확인한 경우에도 같은 사유 매핑을 사용합니다

한 주기는 기본 100건·20초이며, 이미 진행 중인 HTTP 요청 한 건은 설정된 timeout까지 기다릴 수 있습니다
조회 중에는 DB 행 잠금을 유지하지 않습니다
PostgreSQL advisory lock으로 중복 실행을 막고 Redis에 확인 순서를 저장해 오래된 오류 기록이 뒤의 기록을 막지 않게 합니다
조회 한 건이 실패해도 다음 주기에 다시 시도하며 다른 기록의 갱신은 유지합니다
관리자 원격 확인 결과 필드 추가를 위해 Backend 배포 전에 instances.0024_instance_scheduler_observation을 적용합니다
기존 인스턴스의 관측 값은 비어 있으며 확인한 것으로 소급 처리하지 않습니다

## 관리자 목록의 원격 관측

GET /api/v1/admin/instances의 각 행에 scheduler_observation을 추가합니다
기존 status와 updated_at은 DB 상태와 수명 변경 시각이며 의미를 바꾸지 않습니다
목록 조회는 Scheduler를 호출하지 않고 마지막 주기 작업의 저장 결과만 읽습니다

- status: 소유 정보와 릴리스 검증을 통과한 마지막 Scheduler 상태. 조회 실패 시 null
- checked_at: 검증·반영 결과를 저장한 시각. 아직 확인하지 않았다면 null
- check_status: NOT_CHECKED / OK / ERROR / STALE
- error_code: NOT_FOUND / UNAUTHORIZED / FORBIDDEN / UNAVAILABLE / INVALID_RESPONSE 또는 null
- status_mismatch: OK일 때만 DB status와 원격 status의 비교 결과, 나머지는 null

60초를 초과했거나 확인 후 DB 행이 변경됐거나 미래 시각이면 STALE로 표시합니다
과거 성공을 현재 원격 상태로 간주하지 않으며 NOT_CHECKED와 ERROR도 정상 일치로 표시하지 않습니다
관측 필드만 갱신할 때는 updated_at을 바꾸지 않아 종료 대기 시간과 오래된 응답 검사를 유지합니다
실패 응답 원문·인증값·URL·환경변수는 관측 필드와 관리자 응답에 저장하지 않습니다

프론트는 DB 상태와 관측 상태·시각을 구분하고 status_mismatch가 null이면 일치로 처리하지 않아야 합니다
서버의 기존 reconcile timer가 실행되지 않으면 NOT_CHECKED 또는 STALE로 남습니다

## 운영 VM 적용

수정된 백엔드 이미지를 배포한 뒤 이 저장소의 deploy 디렉터리에서 한 번 설치합니다
기존 자동 배포는 API 컨테이너만 교체하므로 최초 timer 등록이 필요합니다
등록 이후에는 배포된 backend 컨테이너의 명령을 실행하므로 이미지가 바뀌어도 같은 timer를 사용합니다

    sudo python3 install_instance_reconciler.py --check
    sudo python3 install_instance_reconciler.py
    systemctl status msg-instance-reconcile.timer
    journalctl -u msg-instance-reconcile.service

timer는 부팅 30초 뒤 시작하고, 이전 실행이 끝난 뒤 15초 간격으로 실행합니다
동기화 오류가 있으면 종료 코드 1과 건수를 남기며 다음 주기에 다시 실행합니다
명령이나 DB 준비가 안 된 구형 배포에는 설치하지 않습니다

한 번만 실행하거나 특정 시험 인스턴스를 확인할 수도 있습니다

    python manage.py reconcile_instances
    python manage.py reconcile_instances --interval 15
    python manage.py reconcile_instances --instance-id INSTANCE_UUID

이미 CLEANED인 과거 이력도 해당 instance-id를 지정하면 관측 결과와 비어 있는 종료 사유를 확인할 수 있습니다
기본 주기 작업은 이미 종료된 이력을 반복 조회하지 않습니다

중지할 때는 sudo systemctl disable --now msg-instance-reconcile.timer를 실행합니다
