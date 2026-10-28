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

Broker의 RESET_REQUIRED나 RESULT의 cleanup_complete 힌트로 정리를 완료하지 않는다.
완료된 broker_pool RESULT 뒤에는 제출 간격 제한을 지키면 같은 팀의 다음 EXE를
접수할 수 있다. 이전 제출의 파일 삭제와 추적 종료에는 실제 복구 뒤 별도
CLEANUP(true) 확인이 계속 필요하다. 따라서 RESULT 뒤 CLEANUP_PENDING은 예상 상태다.
Runtime은 복구 담당자의 READY 통지를 받으면 Backend에 CLEANUP을 재시도한다.
FLAG 주입과 VM 이미지 복구, 실제 문제 E2E 검증은 별도 운영 조건이다.

Runtime은 CLEANUP 응답을 잃었을 때 아래 receipt API를 먼저 조회한다.
요청 본문은 원래 CLEANUP 이벤트와 정확히 같아야 한다. 이 API는 상태를 변경하거나
작업 토큰을 발급하지 않으며, 정리 완료 후에도 서비스 토큰으로 조회할 수 있다.

```text
POST /internal/v1/checker-submissions/{submission_id}/events/{event_id}/receipt
Authorization: Bearer <this-backend-checker-service-token>
Content-Type: application/json

<original CLEANUP event JSON>
```

`data.accepted=true`는 같은 제출과 같은 본문이 저장됐다는 뜻이다. `false`면
아직 기록이 없어 Runtime이 작업 토큰을 갱신해 같은 이벤트를 재전송한다.
Backend의 receipt API와 DB 변경을 먼저 배포한 뒤 Runtime의 복구 이벤트 전달을
활성화한다. 복구 담당자는 Broker가 해당 VM을 READY로 확정한 뒤 같은 execution ID와
고정된 event ID로 Runtime 복구 API를 호출한다.

## 무해한 EXE 한 건 연동 시험

참가자용 액세스 토큰은 로컬 파일에 저장하고, 승인된 무해한 EXE와 실제 Windows 문제
UUID를 지정해 다음 도구를 한 번 실행한다. 제출 전에 Backend와 Runtime 변경이 배포되고
시험 VM이 READY인지 확인한다. 토큰 값은 명령줄과 로그에 넣지 않는다.

```text
py -3.10 tools/windows_one_submission_smoke.py --backend-origin https://msgctf.kr --challenge-id <Windows 문제 UUID> --exe-path <승인된 무해한 EXE 경로> --access-token-file <참가자 토큰 파일 경로>
```

도구는 idempotency key와 submission ID를 출력하고, 같은 제출의 RESULT와 별도
CLEANUP 완료까지 조회한다. POST 응답이 불명확하면 새 요청을 만들지 말고 출력된
idempotency key로 재시도한다. RESULT는 확인됐지만 CLEANUP을 기다리다 시간이
초과되면 submission ID로 상태를 계속 조회한다. 상시 worker와 수동 전송 worker를
동시에 실행하지 않는다.

## 비공개 단일 QA 시험

서버 내부 명령으로만 미게시 `pwn-msgdriver`를 접수한다. 일반 HTTP API에는 QA 예외나
새 권한을 추가하지 않는다. 운영 책임자의 승인 기록을 확보한 서버 운영자만 실행한다.
`--approved-by`는 승인한 기존 관리자 login ID이며 실제 승인 절차를 대신하지 않는다.

- 정상 PARTICIPANT 사용자와 승인된 팀 ID를 지정한다. 기존 ban과 문제 접근 권한은 유지한다.
- 세 EXE 플래그와 문제별 실행 설정은 여전히 활성화가 필요하다. 명령이 설정을 켜거나
  문제를 게시하거나 접근 권한을 추가하지 않는다. 문제는 계속 미게시로 유지한다.
- 파일 SHA-256과 Runtime의 `checker_commit_sha`/`config_version`을 승인 범위와 대조한다.
  측정하지 않은 VM/driver 해시나 Backend active release를 요구하지 않는다.
- 승인자·승인 참조·사용자·팀·파일 해시·실행 버전을 제출 snapshot에 보존한다.
  같은 멱등키는 같은 제출을 반환하고, 한 승인 참조로 다른 제출을 만들지 않는다.

```bash
python manage.py submit_private_qa_exe \
  --user-id <approved-user-uuid> --team-id <approved-team-uuid> \
  --challenge-id <pwn-msgdriver-uuid> --file /private/test.exe \
  --sha256 <approved-file-sha256> --idempotency-key <trial-uuid> \
  --approved-by <admin-login-id> --approval-reference <approval-record> \
  --checker-commit-sha <runtime-sha> --config-version <runtime-config-version>
```

이 명령은 접수만 하고 Runtime을 호출하지 않는다. 일반 배치 worker도 private QA 제출은
건너뛴다. 반환된 `submission_id`로 다음 명령을 명시적으로 실행한다.

```bash
python manage.py dispatch_exe_submissions --submission-id <submission-uuid>
```

지정 실행은 해당 제출의 전송/접수 조회만 수행하며 다른 제출·파일 정리를 처리하지 않는다.
`--limit`과 함께 쓸 수 없다. 최초 전송 전 Runtime 실행 버전이 승인값과 달라지면 전송을
차단한다. 응답 유실 후 같은 ID를 다시 지정하면 GET으로 조회하며 새 실행을 만들지 않는다.
출력에는 작업 상태·오류 코드만 포함하며 토큰·FLAG·실행 stdout/stderr는 출력하지 않는다.

미게시 상태에서는 참가자 상세/이력 API도 차단된다. 실행 로그는 기존 관리자
`GET /api/v1/admin/challenges/{challenge_id}/remote_runs`에서 확인한다.
이 시험은 Backend-Runtime 연동 검증이며 일반 참가자 HTTP 접수/조회 UI 검증은 아니다.
callback 접근 허용·VM READY·시험 범위를 먼저 확인하고, 상시 timer는 끈 상태로 실행한다.
RESULT 수신만으로 복구/정리를 완료하거나 두 번째 제출을 실행하지 않는다.

## 이전 실행 기록

기존 Scheduler 배정 기록은 삭제하거나 broker_pool 작업으로 자동 전환하지 않는다.
worker는 snapshot의 assignment_mode가 broker_pool인 제출만 전송/조회/정리하며,
이전 기록은 requires_review=true로 표시한다. 이전 기록의 파일 제공과 자동 종료/삭제는
차단한다. 실제 실행/점유/정리 상태를 운영자가 확인하기 전에는 다른 VM에서 재실행하거나
배정 종료 시각을 채우지 않는다. 일반 문제의 Scheduler 인스턴스 경로는 변경하지 않는다.
