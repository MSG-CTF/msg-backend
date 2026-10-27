# Windows EXE 접수와 실행 연결

## Windows 직접 Runtime 모드 (#205)

`EXE_WINDOWS_DIRECT_RUNTIME_ENABLED=True`인 새 `pwn-msgdriver` 제출은
Backend -> Runtime -> Broker -> Windows HTTPS 실행기 경로를 사용한다.
Backend가 Scheduler에 VM을 배정받거나 Broker와 Windows 실행기를 직접 호출하지 않는다.
접수·전송·직접 Runtime 모드의 세 플래그 기본값은 True다. 환경변수로 False를 명시하면
해당 기능을 끌 수 있으며, 직접 Runtime 모드를 끄면 접수를 차단한다. Scheduler로 fallback하지 않는다.
플래그가 True여도 Runtime 인증·비공개 저장소·FLAG 비밀값 등 필수 설정 검사는 유지한다.
이 설정이 부족하면 관리자 활성화와 EXE 접수는 409 `EXECUTION_NOT_READY`로 거절한다.

## release 없는 접수의 배포 순서

1. 먼저 `release_id`가 없는 `broker_pool` 요청을 받는 Runtime 버전을 배포한다.
   이 버전은 기존 제출의 유효한 release UUID도 계속 받는다.
2. Backend의 `ExeSubmission.release` NULL 허용 DB 마이그레이션을 적용한다.
3. Backend 웹과 전송 worker를 이 변경으로 배포한 뒤 무해한 EXE로 확인한다.

Backend를 먼저 배포하면 이전 Runtime이 새 요청을 400으로 거절한다. Backend는
전송 결과가 불명확한 제출을 다른 VM에서 자동 재실행하지 않으므로, 해당 기록은
Runtime DB와 Broker 이벤트를 대조해 운영자가 처리해야 한다. 배포 중에는 새 EXE
접수를 중지하고 이미 접수된 파일과 실행 기록을 보존한다.

- `EXE_WINDOWS_RUNTIME_POOL_ID`는 `EXE_CHECKERS_JSON`에 등록된 논리적 Runtime
  UUID다. 실제 VM UUID나 노드 ID를 넣지 않는다. 선택한 풀의 HTTPS 주소, 서비스
  토큰과 승인 버전 설정이 유효해야 접수를 활성화할 수 있다. Windows는 Scheduler
  토큰이나 배정 API를 사용하지 않는다. Broker가 준비된 Windows VM을 고르므로
  Backend의 컨테이너용 active release를 요구하지 않는다.
- EXE는 Backend의 비공개 영속 저장소에만 저장한다. 웹과 worker 컨테이너가 같은
  파일을 읽을 수 있도록 동일 볼륨을 마운트하고 재배포 후에도 보존해야 한다.
- 접수 때 내부 추적용 assignment UUID와 풀 ID를 저장한다. 이는 실제 VM 점유나
  준비 완료 증거가 아니다. `assignment_mode=broker_pool`은 제출 snapshot에
  고정되므로 이후 전역 설정을 바꿔도 과거 Scheduler 제출을 직접 모드로 바꾸지 않는다.
- Runtime POST에는 `selection_mode: "broker_pool"`과 제출/배정/팀/문제 ID,
  파일 SHA-256, 시간/출력 제한, `checker_commit_sha`, `config_version`만 보낸다.
  선택된 VM에서 측정하지 않은 `vm_template_sha256`와 `driver_sha256`는
  설정에 남아 있더라도 전송하거나 RESULT에 기록하지 않는다. `vm_id`와
  `readiness_check_id`, EXE 바이트, FLAG와 파일 URL은 보내지 않는다.
  `release_id`는 새 제출에서 생략하고 DB에도 NULL로 저장한다. 이전 제출에 고정된
  release ID는 이력 보존과 재전송 호환을 위해 유지한다. `Idempotency-Key`는
  `submission_id`다. Runtime이 VM 선택과 Broker 점유를 수행한다.
- Runtime은 작업 토큰으로 같은 Backend의 `/payload`에서 EXE를 받고 `/events`에
  RESULT를 보낸다. 내부/외부 Backend는 각각 별도 DB, 비공개 저장소, 서비스 토큰과
  서명 키를 사용한다. 직접 모드 작업 토큰의 `/secret` 접근은 403으로 차단한다.
- 작업 토큰은 기존과 같이 45분 유효하다. 장시간 대기나 RESULT ACK 유실로 만료되면
  Runtime은 해당 Backend의 서비스 토큰으로 아래 갱신 API를 호출한다.

```text
POST /internal/v1/checker-submissions/{submission_id}/token
Authorization: Bearer <this-backend-checker-service-token>
Content-Type: application/json

{"assignment_id":"<saved-assignment-uuid>","payload_sha256":"<saved-file-sha256>"}
```

응답은 200 envelope의 `data.submission_id`, `data.assignment_id`, `data.job_token`이다.
다른 서비스 토큰은 401, 다른 배정/해시는 400이며, 기존 Scheduler 제출이나 정리가
확인됐거나 내부 추적이 종료된 건에는 404를 반환한다. RESULT가 저장됐어도 정리 확인 전이면
토큰을 갱신해 같은 event ID의 ACK 유실을 복구할 수 있다. 갱신은 파일/배정을 새로
만들지 않으며 `Cache-Control: no-store`를 사용한다. 토큰 원문은 로그에 남기지 않는다.

최초 Runtime POST를 보낸 이후 응답이 불명확하면 같은 제출 ID로 GET만 수행한다.
GET은 접수 확인용이며 RESULT를 대체하지 않는다. GET 404도 미실행의 증거가 아니므로
새 POST나 다른 VM에서 자동 재실행하지 않는다. Runtime DB와 Broker 이력의 운영자
확인이 필요하고 그동안 파일과 추적 정보를 보존한다.

현재 단계의 완료 범위는 무해한 EXE의 payload 전달과 RESULT 저장까지다.
Broker의 RESET_REQUIRED나 RESULT의 cleanup_complete 힌트로 정리를 완료하지 않는다.
실제 복구와 별도 CLEANUP(true) 확인 전에는 파일 삭제/추적 종료/같은 팀의 새 실행을
허용하지 않는다. 따라서 RESULT 뒤 CLEANUP_PENDING은 이 단계의 예상 상태다.
Runtime #97은 FLAG 주입, VM 이미지 복구와 CLEANUP(true)를 제공하지 않으므로 실제
문제 운영이 미검증인 환경은 접수·전송 플래그를 False로 명시해 비활성으로 유지한다.
복구와 정리 계약이 검증된 뒤 별도 활성화한다.

## 이전 실행 기록

기존 Scheduler 배정 기록은 삭제하거나 broker_pool 작업으로 자동 전환하지 않는다.
worker는 snapshot의 assignment_mode가 broker_pool인 제출만 전송/조회/정리하며,
이전 기록은 requires_review=true로 표시한다. 이전 기록의 파일 제공과 자동 종료/삭제는
차단한다. 실제 실행/점유/정리 상태를 운영자가 확인하기 전에는 다른 VM에서 재실행하거나
배정 종료 시각을 채우지 않는다. 일반 문제의 Scheduler 인스턴스 경로는 변경하지 않는다.
