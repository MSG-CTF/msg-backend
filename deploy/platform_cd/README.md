# 사이트별 공식 팀을 보존하는 관리 배포

이 디렉터리는 운영 서버의 고정 CD helper를 검토하고 재현하기 위한 소스다
GitHub의 제한된 배포 키로 이 파일들을 업로드하거나 변경할 수 없다
이미지 전환은 앱 서비스에만 적용하며 DB 마이그레이션·계정 변경·게임 작업은 포함하지 않는다

## 정책과 명단

- /etc/ctf-platform-cd/config.json은 root 소유의 비공개 정책 파일이다
- base_files와 base_file_sha256은 기존 Compose 파일과 검토한 해시를 지정한다
- base_env_file 또는 base_env_json에는 Compose 보간에 필요한 기존 이미지·프로젝트 값만 지정한다
- service_containers는 실제 backend와 frontend 컨테이너 이름을 지정한다
- trusted_helper_sha256은 설치한 helper와 기존 프론트 빌드 recipe의 검토한 해시다
- ranking_scope_file과 ranking_scope_sha256은 root 소유 공식 명단과 해시를 고정한다

명단의 필드는 schema_version, track, division, team_ids, source_workbook_sha256으로 제한한다
schema_version은 1이며 external/EXTERNAL에는 중복 없는 정규 UUID 50개, internal/INTERNAL에는 25개가 필요하다
공식 명단은 운영 엑셀과 대조한 관리 자료로만 만들고 Git에 올리지 않는다
파일과 상위 디렉터리는 root 소유이며 다른 사용자의 쓰기를 허용하지 않는다

## 검토한 코드 설치

관리자는 같은 커밋의 install.py, ranking_scope.py, platform_release.py를 root 전용 임시 디렉터리에 준비한다
외부에는 기존 SSH receiver를 갱신하기 위한 legacy_entry.py도 준비한다
내부에는 기존 앱의 Compose 파일·이미지·서비스 이름을 검토한 로컬 정책이 필요하다
내부용 설치는 SSH 사용자나 키 또는 sudo 권한을 생성하지 않는다

    python3 install.py --scope /root/reviewed-site-scope.json --source-sha <검토한40자리커밋> --expected-policy-sha256 <검토한현재정책해시>

설치기는 build.lock과 deployment.lock을 순서대로 잡고 기존 정책·helper·Compose pin을 재검증한다
공식 명단과 helper를 설치하고 정책을 마지막에 갱신한다
외부는 /etc/ctf-external-secrets/app/public-runtime.env, 내부는 /etc/ctf-internal-secrets/app/runtime.env의 PUBLIC_RANKING_DIVISION과 PUBLIC_RANKING_TEAM_IDS만 관리한다
나머지 환경 파일 내용은 바이트 그대로 보존한다
설치 자체는 컨테이너를 재시작하거나 DB에 접속하지 않는다

## 읽기 전용 조회

외부의 실제 제한된 SSH 경로는 GitHub Actions의 verify_deploy=true로 확인한다
관리자는 두 사이트 모두 아래 헤더 한 줄을 platform_release.py backend의 stdin에 전달할 수 있다
probe는 archive를 읽거나 main을 배포하지 않으므로 archive 값은 스키마 검사용 placeholder다

    {"action":"probe","component":"backend","source_sha":"<40자리커밋>","archive_sha256":"<0이64개인문자열>","archive_bytes":1}

성공 결과는 PREFLIGHT_VERIFIED와 ranking_scope를 반환한다
검증기는 PGOPTIONS와 PostgreSQL READ ONLY 트랜잭션으로 명단을 조회하고 메모리에만 존재하는 access token으로 공개 GET API를 확인한다
로그인·로그아웃·게임 조작·refresh session 생성은 호출하지 않는다
API 결과와 설정의 UUID 집합이 공식 명단과 정확히 같아야 한다
KOTH는 공식 명단의 부분집합만 허용하며 게임 서버용 팀 공급 경로는 별도로 유지한다

## 실패와 복구

템플릿 해시·공식 명단이 달라지면 후보 시작과 이미지 전환 전에 중단한다
후보의 명단 검증이 실패하면 운영 앱을 교체하지 않는다
전환 후 HTTPS 또는 공개 명단 검증이 실패하면 같은 공식 명단을 유지한 이전 앱 이미지로 되돌리고 다시 확인한다
DB restore나 migrate는 수행하지 않는다
정책·명단·helper를 되돌려야 할 때는 guard-install-커밋접두사/paths.json과 서버에만 남긴 원본 백업을 관리자가 검토한다
복구도 두 잠금 아래 수행하고 읽기 전용 조회를 다시 통과시킨다
기존 비밀 환경 파일의 다른 값을 덮어쓰지 않도록 현재 파일과 백업을 먼저 비교한다

## 검증

    python3 -m unittest discover -s deploy -p 'test_*.py' -v

테스트는 잘못된 UUID·사이트·파일 해시·쓰기 권한, 후보 검증 실패, 전환 후 복구, 뒤처진 main 건너뛰기, 민감한 출력 차단을 포함한다
운영 API의 읽기 전용 검증은 단위 테스트와 별도로 두 사이트에서 수행한다
