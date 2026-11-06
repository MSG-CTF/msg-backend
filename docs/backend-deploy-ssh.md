# 제한된 SSH 백엔드 배포

GitHub Actions는 등록된 SSH host key와 전용 msg-backend-deploy 계정으로 배포 식별자만 보낸다
외부 운영 VM의 forced command는 /usr/local/lib/ctf-platform-cd/ssh-backend이며 sudo는 고정 legacy_entry.py의 backend 진입점만 허용한다
전용 계정에는 대화형 셸, Docker 그룹, 일반 sudo 권한이 없다
deploy/backend_release.py의 manifest와 프로토콜 상수는 송신 호환성을 위해 유지하며 현재 운영 receiver는 deploy/platform_cd의 코드다

## 배포와 사전 검증

- GitHub development 환경 Secret: BACKEND_DEPLOY_HOST, BACKEND_DEPLOY_SSH_KEY, BACKEND_DEPLOY_KNOWN_HOSTS
- 요청은 action, source_sha, deployment_id, pipeline_revision 네 항목의 작은 JSON으로 제한
- deploy는 현재 MSG-CTF/msg-backend main SHA만 배포하며 뒤처진 실행은 superseded로 건너뜀
- 호스트의 build.lock과 deployment.lock으로 이미지 빌드와 앱 전환을 직렬화
- root 소유 정책의 helper·Compose 템플릿·공식 팀 명단 해시가 다르면 중단
- 실제 공개 랭킹·그래프·KOTH API의 명단을 빌드 전에 검증
- 이미지 취약점 검사, 읽기 전용 migrate --check, Django 보안 검사와 별도 후보의 health·명단 검증 후 backend 서비스만 교체
- 운영 HTTPS와 공식 명단을 다시 확인하며 실패하면 이전 앱 이미지로 복구

기존 SSH 프로토콜 pin은 유지하며 CI는 임의 저장소·명령·파일 경로·helper를 지정할 수 없다
CI에서 배포 코드를 업로드하지 않으며 현재 main의 앱 이미지만 고정된 root 진입점이 빌드한다
DB 마이그레이션·계정 변경·게임 작업·DNS 변경은 수행하지 않는다

workflow_dispatch의 verify_deploy=true는 같은 SSH와 root 진입점을 통해 현재 운영 설정과 실제 API를 조회한다
브랜치 SHA도 조회에 사용할 수 있으며 이미지 빌드·운영 컨테이너 교체·DB 마이그레이션은 실행하지 않는다
성공 결과에는 공식 팀 수, 명단 digest, API 검증 여부, DB 조회의 읽기 전용 여부가 포함된다

## 외부 50팀과 내부 25팀 유지

/etc/ctf-platform-cd/config.json은 사이트별 Compose 경로·해시와 root 소유 ranking-scope.json의 경로·해시를 고정한다
공식 명단은 운영 엑셀과 대조한 팀 UUID로 구성하며 외부 EXTERNAL 50팀, 내부 INTERNAL 25팀을 각각 강제한다
실제 UUID 명단과 참가자 엑셀은 저장소에 포함하지 않는다
팀 수가 같아도 UUID가 다르거나 다른 사이트 명단이면 검증에 실패한다

공식 명단은 기존 앱 환경 파일의 PUBLIC_RANKING_DIVISION과 PUBLIC_RANKING_TEAM_IDS에 보존한다
관리 배포는 해당 두 값을 이미지 override에도 주입하고 렌더링된 Compose 설정과 실제 API를 검사한다
계정·비밀번호·팀 소속·게임 데이터와 다른 환경변수는 변경하지 않는다
내부 사이트에는 같은 검증기를 관리자가 사용할 수 있는 로컬 정책으로 설치하며 외부 CI의 SSH 권한을 확대하지 않는다

## 운영 진단과 설치

BASE_TEMPLATE_CHANGED는 검토한 Compose 파일과 실제 파일의 해시 불일치다
RANKING_SCOPE_FAILED는 공식 명단·실제 공개 API 검증 실패, HELPER_CHANGED는 설치 helper의 해시 불일치다
MIGRATION_OR_SECURITY_CHECK_FAILED와 UNVERIFIED_RELEASE도 실패로 처리하며 민감한 명령 출력은 CI에 전달하지 않는다
호스트의 root 전용 cd 디렉터리에 단계별 진단을 보관한다

관리자는 검토한 커밋의 deploy/platform_cd/install.py로 설치한다
설치기는 현재 정책의 예상 해시를 비교하고 기존 helper·템플릿 pin을 검증한 뒤 두 잠금 아래 코드·공식 명단·공개 환경변수 두 개·정책을 갱신한다
원본은 서버의 root 전용 백업에 남기고 설치 도중 오류가 나면 원상 복구한다
검토되지 않은 템플릿 변경을 자동 승인하거나 정책 pin을 우회하지 않는다
구체적인 설치·조회·복구 절차는 deploy/platform_cd/README.md를 따른다
