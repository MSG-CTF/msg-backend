# 서비스 키 분리와 교체

TC-613의 운영 절차이며 담당자는 백엔드 배포 권한을 가진 운영자임
키 원문은 승인된 비밀 저장소와 보호된 환경 주입 경로에서만 취급함
명령행 인자, 티켓, 감사 이벤트, 응답 본문, 화면 캡처에 키를 넣지 않음

## 키의 용도와 시작 조건

| 설정 | 용도 | 교체 영향 |
| --- | --- | --- |
| DJANGO_SECRET_KEY | Django 서명, 관리자 미리보기 권한 | 기존 Django 서명과 미리보기 권한이 무효화될 수 있음 |
| JWT_SECRET | 로그인 access 및 refresh 서명 | 기존 access와 refresh가 모두 401로 거절됨 |
| KOTH_TEAM_TOKEN_SECRET | 팀·문제별 KOTH 토큰과 운영 테스트 토큰 생성 | 등록한 DB 키 버전이 전환되면 이전 버전 토큰 거절 |

세 값은 모두 명시하고 서로 다른 난수 키를 사용함
KOTH 키는 공백 없는 32바이트 이상이어야 하며 secrets.token_urlsafe(48) 같은 CSPRNG로 생성함
개발과 CI에도 같은 검사를 적용하며 누락 시 JWT 키를 대신 사용하지 않음
길이 검사는 난수 품질을 증명하지 않으므로 사람이 만든 문장이나 다른 서비스 키를 재사용하지 않음

## 최초 배포

1. 변경 티켓에 담당자, 배포 커밋, 작업 시간, 검증 담당자, 키 접근 감사 위치를 기록함
2. KOTH 로그인·토큰 조회·검증과 관리자 게임 테스트를 점검 상태로 두고 이전 코드의 모든 작업자를 트래픽에서 제거함
3. 마이그레이션과 새 코드를 배포하되 KOTH 트래픽은 아직 열지 않음
4. 승인된 현재 키가 주입된 관리용 프로세스에서 아래 최초 등록을 실행함
5. 각 작업자에서 status의 active_version과 configured_key_active를 확인한 뒤 트래픽을 재개함

```sh
python manage.py migrate --noinput

OPERATION_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
python manage.py rotate_koth_team_token_key \
  --actor operator.login \
  --reason "TC-613 최초 키 등록 / 변경 티켓 번호" \
  --expected-version 0 \
  --operation-id "$OPERATION_ID"

python manage.py rotate_koth_team_token_key --status
```

최초 등록은 현재 키로 생성한 해시와 일치하는 기존 팀·운영 테스트 토큰만 버전 1로 인계함
문제 없는 구형 토큰, 다른 키에서 나온 해시, 관리자가 없는 운영 토큰은 인계하지 않음
운영 테스트 팀 ID 행은 계속 보존하므로 이전 테스트 팀이 실제 채점에 섞이지 않음
등록하지 않은 상태의 HTTP 요청은 503 KOTH_TOKEN_KEY_UNAVAILABLE이며 첫 요청이 키를 자동 등록하지 않음

이전 코드에는 DB 키 버전 검사가 없으므로 이전 코드와 새 코드를 섞어 둔 채 최초 등록하지 않음
키가 등록된 뒤에는 이 마이그레이션의 역실행을 차단하며 코드 롤백 때 키 버전과 감사 이력을 삭제하지 않음

## KOTH 키 교체

1. 변경 티켓에 교체 사유, 현재 버전, 담당자와 검증 계획을 기록함
2. 정상 교체는 점검 시간을 잡고 유출 대응은 KOTH 인증 진입점을 먼저 차단함
3. 비밀 저장소에서 독립된 새 키를 준비하고 새 관리용 프로세스와 새 작업자에 같은 값을 주입함
4. 새 키가 주입된 관리용 프로세스에서 현재 버전을 조건으로 전환 명령을 실행함
5. 새 키 작업자에서 configured_key_active가 true인지 확인한 뒤 트래픽을 전환함
6. 문제 서버의 기존 게임 세션을 각 게임의 세션 폐기 절차로 종료하고 아래 검증 결과를 기록함

```sh
# 새 키가 주입된 프로세스에서 실행하며 키를 인자로 넘기지 않음
OPERATION_ID="$(python -c 'import uuid; print(uuid.uuid4())')"
python manage.py rotate_koth_team_token_key \
  --actor operator.login \
  --reason "키 교체 / 변경 티켓 번호" \
  --expected-version 1 \
  --operation-id "$OPERATION_ID"

python manage.py rotate_koth_team_token_key --status
```

예시의 expected-version은 직전에 조회한 실제 버전으로 바꿈
활성 버전 전환과 SERVICE_KEY_ROTATED 이벤트 및 교체 영수증은 하나의 DB 트랜잭션으로 저장함
감사 저장에 실패하면 전환도 취소되며 이전 버전을 유지함
DB 전환이 커밋되면 팀의 재조회 여부와 관계없이 이전 버전 토큰이 검증에서 거절됨
이전 키를 가진 새 코드 작업자는 조회·발급·검증을 503으로 거절하며 DB 토큰을 덮어쓰지 않음
참가자는 새 팀 토큰을 조회해 게임에 다시 입력하며 API의 성공 응답 필드는 바뀌지 않음

호스트의 환경 파일만 수정해도 실행 중인 컨테이너의 환경변수가 바뀌지는 않음
기존 컨테이너에 exec한 프로세스가 새 키를 읽었다고 가정하지 말고 승인된 새 환경으로 관리 프로세스와 작업자를 다시 생성함
설정을 바꾼 모든 작업자에서 status를 확인하며 임의로 환경 전체를 출력하지 않음

## 필수 검증

| 확인 | 기대 결과 |
| --- | --- |
| 팀 재조회 전 구 토큰 검증 | 내부 인증이 유효한 요청에서 HTTP 200, valid false |
| 새 토큰 조회와 검증 | 새 토큰으로 HTTP 200, valid true |
| 이전 키 작업자의 조회·검증 | 503 KOTH_TOKEN_KEY_UNAVAILABLE, DB 토큰 변경 없음 |
| 이전 키로 재시작한 작업자 | 계속 503, 이전 토큰 부활 없음 |
| 현재 키와 다른 키를 등록 없이 적용 | 503, HTTP 요청으로 자동 활성화하지 않음 |
| KOTH 교체 후 로그인 JWT | 기존 JWT의 원래 만료 시각까지 로그인 유지 |
| 관리자 미리보기 게임 세션 | 교체 후 다음 권한 확인에서 이전 버전 거절 |
| 감사 영수증 | 수행자·사유·이전/신규 버전·적용/폐기 시각과 이벤트 ID 확인 |

외부 문제 서버가 이미 만든 자체 쿠키·JWT·게임 세션은 플랫폼 키 전환만으로 모두 폐기되지 않음
각 문제 서버 담당자가 네이티브 세션 저장소나 세션 세대를 폐기하고 실제 구 세션 요청이 거절되는 근거를 남겨야 작업을 완료함
플랫폼의 관리자 미리보기 프록시는 매 요청에 DB 키 버전을 확인해 이전 바인딩을 거절함

## 실패와 재시도

- 통신 단절로 성공 여부가 불명확하면 status와 operation_id로 영수증을 확인함
- 같은 작업은 actor, reason, expected-version과 operation-id를 그대로 사용해 재시도함
- 같은 operation-id로 다른 요청을 보내거나 이미 바뀐 버전에 교체를 시도하면 거절됨
- 과거 영수증 재조회는 그 키를 다시 활성화하지 않으므로 created false만으로 적용 완료를 판단하지 않음
- active_version과 configured_key_active가 모두 의도한 상태일 때만 작업 완료로 기록함
- 폐기된 키나 현재 키를 새로운 교체에 재사용할 수 없으며 복구 시에는 새 키 C로 전진 교체함
- 코드 복구도 DB 버전 검사를 유지하는 빌드로 진행하며 패치 이전 코드로 인증을 우회하지 않음

## 감사 기록과 접근 기록

DB의 koth_token_key_rotations에 operation_id, actor, reason, 이전/신규 버전, activated_at과 영향받은 토큰 수를 보존함
같은 트랜잭션의 admin_events에는 SERVICE_KEY_ROTATED와 연결된 메타데이터를 저장함
previous_revoked_at은 새 버전의 activated_at과 같은 전환 시각이며 최초 등록에는 null임
출력용 영수증 JSON과 일반 관리자 이벤트 및 명령 출력에는 키 원문이나 키 지문을 넣지 않음
내부 키 비교용 지문만 키 상태·교체 테이블에 보관하며 관리자 화면의 변경·추가·삭제는 막음
DB 관리자 권한의 직접 변경까지 막는 외부 보존 장치는 별도이며 영수증을 접근 제한된 중앙 감사 저장소에도 보관함

status는 최근 100건을 보여주며 전체 이력은 읽기 전용 Django 관리자 화면의 KothTokenKeyRotation에서 확인함
행의 issued_at은 토큰 재발급 시각이며 서비스 키 교체 이력으로 대신 사용하지 않음
수행자는 티켓에 영수증 JSON, 배포 커밋, 작업자 적용 확인, 구/신규 토큰과 게임 세션의 검증 결과, OS 접근 감사 조회 위치를 첨부함
비밀값과 원본 토큰이 들어간 요청·응답 본문은 첨부하지 않음

현재 환경 파일은 root 소유 0600으로 유지함
호스트의 키 파일 접근 기록은 앱의 교체 영수증과 별개로 OS auditd나 비밀 저장소의 접근 감사에서 확인함
Linux 호스트 예시는 deploy/service-key-audit.rules이며 실제 키 경로와 아키텍처에 맞춰 운영자가 적용함
파일 교체 후 규칙을 다시 확인하고 파일 내용을 출력하지 않는 읽기 작업으로 감사 이벤트가 발생하는지 확인함
접근 감사 수집과 중앙 보존이 확인되지 않았으면 TC-613 운영 검수를 완료 처리하지 않음

auditd 경로 감시는 inode를 기준으로 하므로 파일 교체 뒤 재검증이 필요함
규칙 문법과 아키텍처별 적용은 [Linux Audit의 규칙 문서](https://github.com/linux-audit/audit-userspace/blob/master/docs/audit.rules.7)와 [auditctl 문서](https://github.com/linux-audit/audit-userspace/blob/master/docs/auditctl.8)를 따름

## JWT와 Django 키의 교체

JWT와 Django 키는 KOTH 키 등록 명령의 대상이 아니며 KOTH 영수증을 이 두 키의 적용 증거로 사용하지 않음
각 키도 담당자·사유·비밀 저장소 버전 ID·이전/신규 버전·적용/폐기 시각·배포 커밋·검증 결과·접근 감사 위치를 중앙 변경 기록에 남김

JWT 교체는 전체 인증 작업자를 점검 상태로 제거한 뒤 새 독립 키로 다시 생성함
이 구현은 이전 JWT 키를 함께 허용하지 않으므로 기존 access와 refresh는 모두 401이며 사용자는 다시 로그인함
refresh DB 행이 남아 있어도 서명 검증이 먼저 실패하므로 새 키에서 재발급되지 않음
이전 JWT 키를 되돌리면 아직 만료되지 않은 토큰이 다시 살아날 수 있으므로 유출 대응 복구에는 새 키를 사용함
검증은 구 access와 refresh의 401, 새 로그인 성공, 새 토큰의 만료 정보, 모든 작업자의 새 설정 적용으로 진행함

Django 키 교체도 모든 서명 검증 작업자를 함께 전환하고 관리자 로그인과 미리보기 권한을 다시 발급함
현재 설정은 SECRET_KEY_FALLBACKS를 사용하지 않으므로 이전 서명을 계속 허용한다고 가정하지 않음
키별 영수증이 없는 수동 작업은 중앙 변경 기록과 실제 검증 근거가 준비되기 전까지 완료 처리하지 않음
