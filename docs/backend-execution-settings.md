# 문제 실행 설정과 비밀값

프론트는 문제 ID만 보냅니다
백엔드는 활성 릴리스의 환경변수와 비밀값 참조를 골라 스케줄러에 전달합니다
비밀값 원문은 런타임 worker만 조회할 수 있습니다

| 단계 | 입력과 처리 |
| --- | --- |
| 출제자 → CI | env는 일반 값, secret_env는 백엔드에 등록된 비밀값 이름 |
| CI → 백엔드 | 새 필드가 있으면 artifact schema 2.1로 발행 |
| 관리자 → 백엔드 | 기존 2.0 릴리스의 이미지·포트는 유지하고 env·secret_env만 새 설정 버전으로 복제 |
| 릴리스 등록 | 이름을 해당 문제의 최신 비밀값 버전으로 고정, 누락되면 등록 실패 |
| 릴리스 활성화 | approved_at 기록, 이전 승인 릴리스도 reset을 위해 조회 허용 |
| 백엔드 → 스케줄러 | env·컨테이너별 secret_ref·release_id 전달, 새 설정은 POST /api/v2/instances 사용 |
| 런타임 → 백엔드 | 전용 서비스 토큰으로 참조·컨테이너 이름·이미지 digest를 확인하고 값 조회 |

## 운영자가 준비할 것

1) 마이그레이션을 적용하고 RUNTIME_SECRET_ENCRYPTION_KEYS를 설정합니다
   쉼표로 구분한 Fernet 키 목록이며 첫 키로 암호화하고 나머지 키로 이전 값을 복호화합니다
2) RUNTIME_SECRET_API_TOKEN을 런타임 전용 토큰으로 설정합니다
   참가자 JWT, 관리자 JWT, 스케줄러 토큰과 각각 분리합니다
3) 관리자 API에 문제별 비밀값을 등록한 뒤 기존 릴리스를 복제하거나 새 릴리스를 등록해 활성화합니다
   flag 값은 해당 문제의 채점 hash와 일치해야 합니다

| API | 권한 | 요청 | 응답 |
| --- | --- | --- | --- |
| POST /api/v1/admin/challenges/{challenge_id}/runtime-secrets | 관리자 JWT | name, value | secret_id UUID, name, version |
| GET /api/v1/admin/challenges/{challenge_id}/runtime-secrets | 관리자 JWT | 없음 | 비밀값 이름, 버전, 저장 시각과 최신 여부 |
| POST /api/v1/admin/challenges/{challenge_id}/releases/{release_id}/derive | 관리자 JWT | containers[].name/env/secret_env | 원본 이미지·포트·격리를 유지한 새 릴리스 |
| POST /internal/v1/runtime-secrets/resolve | 런타임 전용 Bearer | secret_ref UUID, container, image | data.env에 값 반환, Cache-Control: no-store |

관리자 릴리스 조회의 컨테이너별 secret_bindings는 주입 이름, 저장 이름, 연결 버전과 최신 여부를 반환합니다
다른 문제의 비밀값은 연결된 참조가 있어도 조회하지 않으며 저장값 없음으로 표시합니다
관리자 메타데이터 조회는 값을 복호화하지 않고 원문·hash·암호문을 응답에 포함하지 않습니다
연결 상태는 릴리스 설정을 뜻하며 실제 컨테이너 주입 검증 결과와는 구분합니다

비밀값은 인증된 암호화로 DB에 저장합니다
새 등록은 새 버전을 만들며 기존 릴리스의 참조는 바꾸지 않습니다
릴리스 조회·스케줄러 요청에는 원문을 담지 않습니다
키를 잃으면 값을 복구할 수 없으므로 DB 백업과 별도로 키 보관·교체 절차가 필요합니다
운영에서는 DEBUG를 끄고 요청·응답 body 수집을 비활성화해야 합니다

## 검수 기준

| 확인할 것 | 기대 결과 |
| --- | --- |
| 참가자가 실행 설정을 추가 | 문제 ID 외 입력 거절 |
| FLAG·비밀번호·토큰을 일반 env에 입력 | 거절, secret_env로 등록 |
| 다른 문제의 비밀값 이름 사용 | 참조를 찾지 못해 릴리스 등록 실패 |
| 미승인 릴리스, 다른 컨테이너·digest로 조회 | 값 반환 없이 404 |
| 릴리스 전환 후 기존 인스턴스 reset | 원래 revision·env·비밀값 버전 유지 |
| 같은 발행 번호로 설정 복제 후 재연결 | 스케줄러 release_id로 정확한 버전 선택, ID가 없어 모호하면 실패 |
| 암호화 키 누락·잘못된 키·암호문 교체 | 값 반환 없이 503 |
| 구형 schema 2.0에 새 필드 추가 | 빈 값도 거절, 조용한 누락 방지 |

이 변경은 고정된 비밀값 버전을 지원합니다
이미지에 플래그가 들어 있거나 프로그램이 환경변수 대신 파일을 읽는 경우에는 관리자 주입 설정만으로 해당 값이 사라지거나 동작이 바뀌지 않습니다
인스턴스별 값 생성, 비밀값 폐기 API, 채점 hash의 릴리스별 버전 관리는 추가 구현이 필요합니다
FLAG를 교체하면 기존 hash 검사도 바뀌므로 기존 인스턴스와 reset의 처리 기준을 함께 정해야 합니다

일반 env와 비밀값은 컨테이너당 합계 32개, 값당 UTF-8 4096바이트, 이름과 값을 합쳐 16384바이트까지 허용합니다
이름은 64자 이하의 대문자 식별자이며 값은 문자열만 허용합니다
비밀값 이름은 64자 이하의 소문자 식별자입니다

관련 검사: [실행 설정 테스트](../apps/instances/tests_execution_settings.py)
