# `chore/devsecops-v3` 브랜치 검수 안내서

## 1. 1분 요약

이 브랜치는 백엔드 코드를 바로 배포하는 브랜치가 아닙니다.

백엔드가 GitHub의 공용 CI 파이프라인을 사용하도록 연결하고, 그 CI가 요구하는
보안 설정과 Docker 실행 환경을 함께 준비하는 브랜치입니다.

- 기준 브랜치: `main`
- 작업 브랜치: `chore/devsecops-v3`
- 현재 호출 버전: `MSG-CTF/jm_devsecops@v3.3.0`
- CI 실행 시점: `main`으로 보내는 PR, `main`에 반영된 push
- CD(운영 배포): 아직 포함하지 않음
- 원격 브랜치 push와 PR 생성: 별도 승인 후 진행

현재 `v3.3.0`을 연결한 이유는 이미 만들어진 안정 태그이기 때문입니다. 중앙
DevSecOps 저장소의 최신 `main`에는 병합 커밋까지 검사하도록 Gitleaks를 보완한
내용이 있지만 아직 `v3.3.1` 태그가 만들어지지 않았습니다. 과거 `.env.save`에
있던 키를 회전하거나 폐기하고, 과거 기록을 어떻게 처리할지 결정한 다음
`v3.3.1`을 발행하여 호출 파일의 버전을 한 줄 변경해야 합니다.

## 2. 이 브랜치가 필요한 이유

호출 파일만 추가하면 CI는 실행되지만 첫 실행부터 실패합니다. 기존 `main`에는
다음 준비물이 부족했기 때문입니다.

1. Django 운영 보안 검사에서 W004, W008, W012, W016 경고가 발생했습니다.
2. 저장소 루트에 백엔드 이미지를 만들 `Dockerfile`이 없었습니다.
3. 컨테이너에서 Django를 실행할 Gunicorn이 없었습니다.
4. SAST가 URL 입력 처리와 루트 사용자 컨테이너 문제를 발견했습니다.
5. 오래된 Django와 sqlparse 버전을 업데이트해야 했습니다.

그래서 이 브랜치는 CI 호출 파일과 준비물을 한 묶음으로 수정합니다. 이렇게 해야
PR에서 CI를 실행했을 때 단순히 파이프라인만 연결된 것이 아니라 실제 애플리케이션,
데이터베이스, Redis, Docker 이미지와 보안 설정까지 함께 확인할 수 있습니다.

## 3. 파일별 변경 내용과 이유

### `.github/workflows/ci-cd.yml`

백엔드 저장소가 중앙 reusable CI를 호출하는 작은 연결 파일입니다.

- `main` 대상 PR에서 실행합니다.
- `main`에 반영된 push에서 실행합니다.
- 같은 커밋의 오래된 실행은 취소해 CI 자원 낭비를 줄입니다.
- 소스 읽기는 `contents: read`만 허용합니다.
- 보안 결과 업로드를 위해 `security-events: write`를 허용합니다.
- `/healthz` 대신 실제로 존재하는 `/admin/login/`을 Docker smoke test 경로로 사용합니다.
- 중앙 파이프라인은 `v3.3.0` 태그로 고정하여 중앙 `main` 변경이 백엔드 CI에
  갑자기 섞이지 않게 합니다.

`push`는 모든 브랜치가 아니라 `main`으로 한정했습니다. 작업 브랜치를 push한 뒤
`main` 대상 PR을 만들면 PR 검사 한 번이 실행되고, 병합 후 `main` 검사 한 번이
실행됩니다. 작업 브랜치 push와 PR 이벤트가 같은 커밋에서 중복 실행되는 일을
막기 위한 설정입니다.

### `Dockerfile`

CI가 백엔드 운영 이미지를 실제로 만들고 실행해 보기 위한 루트 Dockerfile입니다.

- Python 3.12 Alpine 이미지를 digest로 고정했습니다.
- 패키지를 설치한 뒤 `pip check`로 의존성 충돌을 검사합니다.
- 애플리케이션은 관리자(root)가 아닌 `app` 사용자로 실행됩니다.
- Gunicorn이 PID 1에서 실행되도록 `exec`를 사용합니다.
- 컨테이너가 종료 신호를 받으면 중간 shell이 아니라 Gunicorn이 직접 신호를
  받을 수 있습니다.
- 기본 포트는 8080이고 `PORT` 환경변수로 바꿀 수 있습니다.

Docker Compose는 여러 컨테이너를 함께 실행하는 설명서이고 Dockerfile은 백엔드
컨테이너 한 개를 만드는 조리법입니다. 역할이 달라서 Compose 파일이 있어도 루트
Dockerfile이 필요합니다.

### `.dockerignore`

Docker 빌드에 필요 없는 Git 기록, 환경변수 파일, 캐시, 문서, KOTH 템플릿 등을
이미지 빌드 문맥에서 제외합니다. 빌드 속도를 높이고 `.env` 같은 민감한 파일이
실수로 이미지 안에 복사될 위험을 낮춥니다.

### `requirements.txt`

- Django를 `5.2.17`로 업데이트했습니다.
- sqlparse를 `0.6.0`으로 업데이트했습니다.
- 운영 WSGI 서버인 Gunicorn `26.1.0`을 추가했습니다.

로컬 개발 서버인 `manage.py runserver`는 개발용입니다. 운영 컨테이너에서는 여러
요청과 종료 신호를 안정적으로 처리하기 위해 Gunicorn을 사용합니다.

### `config/settings.py`

Django 운영 보안 검사에 필요한 설정을 추가했습니다.

- `DEBUG` 기본값을 `False`로 변경했습니다.
- 운영 모드에서 HTTP 요청을 HTTPS로 전환합니다.
- 세션 쿠키와 CSRF 쿠키를 HTTPS에서만 전송합니다.
- 운영 모드에서 HSTS를 사용합니다.
- 프록시가 전달하는 `X-Forwarded-Proto`를 통해 원래 요청이 HTTPS인지 판단합니다.

이 변경으로 `check --deploy --fail-level WARNING`에서 발생하던 W004, W008, W012,
W016을 해결했습니다.

또한 `SCHEDULER_BASE_URL`이 `http` 또는 `https` 주소인지 검사하고, 사용자 정보,
경로, query, fragment가 들어간 주소는 거절합니다. 설정값을 이용한 의도하지 않은
파일 접근이나 이상한 URL 호출을 줄이기 위한 방어입니다.

### `apps/instances/services.py`

Scheduler 호출은 `config/settings.py`에서 검증을 통과한 URL만 사용한다는 근거를
코드에 명확히 남겼습니다. Bandit의 B310 경고를 무조건 숨긴 것이 아니라, URL을
먼저 제한한 뒤 해당 호출 한 줄에만 `# nosec B310`을 적용했습니다.

### `koth-template/prob/for_organizer/checker/checker.py`

`TARGET_HOST`와 `TARGET_PORT`를 그대로 URL 문자열에 넣지 않도록 수정했습니다.

- IPv4와 IPv6 주소를 검사합니다.
- DNS 이름 형식을 검사합니다.
- 포트는 숫자이며 1~65535 범위인지 검사합니다.
- URL scheme은 `http`로 고정합니다.
- 올바르지 않은 입력이면 외부 요청을 보내지 않고 점수 0을 반환합니다.

입력 검증 후 실제 `urlopen` 한 줄에만 Bandit 예외를 표시했습니다.

### KOTH의 두 `Dockerfile`

Checker는 `checker`, service는 `app`이라는 비루트 사용자로 실행되게 했습니다.
컨테이너가 공격받더라도 관리자 권한으로 실행되는 경우보다 피해 범위를 줄이기
위한 기본 방어입니다.

### `apps/accounts/tests.py`

과거에 저장소에 들어갔던 JWT 키 문자열을 현재 소스 코드에서 제거했습니다.
테스트할 때마다 임시 키를 만들고, 현재 키가 아닌 다른 키로 서명한 토큰이
거절되는지 같은 보안 동작을 계속 검증합니다.

## 4. 중앙 CI가 검사하는 순서

백엔드의 호출 파일은 검사 코드를 복사하지 않고 중앙 저장소의 reusable CI를
부릅니다. 큰 흐름은 다음과 같습니다.

1. Gitleaks가 Git 기록에서 시크릿 흔적을 검사합니다.
2. Semgrep과 Bandit이 Python 코드의 보안 문제를 검사합니다.
3. Flake8이 실행을 망가뜨릴 수 있는 Python 오류를 검사합니다.
4. PostgreSQL과 Redis를 시작합니다.
5. 누락된 migration이 있는지 확인하고 실제 migration을 적용합니다.
6. PostgreSQL 쿼리와 Redis 저장·조회를 확인합니다.
7. Django 전체 테스트를 실행하고 테스트가 0개면 실패시킵니다.
8. Django `check --deploy`로 운영 보안 설정을 검사합니다.
9. 루트 Dockerfile로 이미지를 빌드합니다.
10. 컨테이너가 비루트 사용자로 실행되는지 확인합니다.
11. 컨테이너를 실행하고 `/admin/login/` 요청이 성공하는지 확인합니다.
12. Trivy가 소스 의존성과 완성된 Docker 이미지의 취약점을 검사합니다.

이 파이프라인은 CI입니다. 현재 운영 서버로 이미지를 보내거나 서비스를 교체하는
CD 단계는 없습니다.

## 5. 로컬에서 완료한 검증

현재 로컬 브랜치에서는 다음 결과를 확인했습니다.

- GitHub Actions 문법 검사 통과
- Python 문법 검사 통과
- Flake8의 치명적 오류 0건
- Semgrep 차단 등급 오류 0건
- Bandit 차단 등급 오류 0건
- PostgreSQL 연결, `SELECT 1`, migration 적용 통과
- Redis 직접 저장·조회와 Django cache 저장·조회 통과
- Django 테스트 66개 통과
- Django 배포 검사 경고 0건
- 루트 백엔드 Docker 이미지 빌드 통과
- Alpine 환경에서 psycopg-binary 설치와 `pip check` 통과
- Gunicorn이 PID 1로 실행되는 것 확인
- 비루트 사용자 `app` 실행 확인
- `/admin/login/` smoke test HTTP 200 확인
- KOTH checker와 service 이미지 빌드 및 비루트 사용자 확인
- Trivy 파일시스템과 최종 백엔드 이미지 취약점 0건
- 현재 작업 트리를 대상으로 한 Gitleaks 검사 통과

Black은 기존 코드를 포함한 Python 파일 4개의 서식 차이를 보고하지만 현재 중앙
정책에서 병합 차단 항목은 아닙니다. 기능·보안 변경과 대규모 자동 서식 변경을 한
PR에 섞지 않기 위해 이번 브랜치에서는 자동 정렬하지 않았습니다.

## 6. 아직 끝나지 않은 시크릿과 `v3.3.1` 작업

현재 연결된 `v3.3.0`의 기본 Gitleaks Git 검사는 일반 커밋 차이는 검사하지만,
과거 `.env.save`가 병합 커밋으로만 들어온 경우의 diff를 놓칩니다. 그래서 초록불이
나와도 과거 기록에 시크릿이 없다는 뜻은 아닙니다.

병합 커밋까지 포함하는 중앙 `main` 방식으로 로컬 검사했을 때 `.env.save`의
`JWT_SECRET`과 `DJANGO_SECRET_KEY` 위치에서 총 12개의 기록이 발견됐습니다.
문서에는 실제 값은 적지 않습니다.

다음 순서로 처리해야 합니다.

1. 발견된 값이 실제로 사용된 적이 있는 키인지 담당자가 확인합니다.
2. 실제 키이거나 확실하지 않으면 먼저 새 키로 교체하고 기존 키를 폐기합니다.
3. 키를 바꾸어도 Git 과거 기록은 사라지지 않으므로 처리 방식을 정합니다.
4. 기록을 유지한다면 폐기 확인 후 정확한 fingerprint만 예외 처리합니다. 규칙 전체나
   `.env.save` 경로 전체를 무시하면 안 됩니다.
5. 기록을 삭제한다면 팀과 합의한 뒤 Git history rewrite를 별도 작업으로 진행합니다.
6. 중앙 저장소 현재 `main`을 `v3.3.1` 태그로 발행합니다.
7. `.github/workflows/ci-cd.yml`의 `@v3.3.0` 한 줄을 `@v3.3.1`로 변경합니다.
8. `main` 대상 PR에서 병합 커밋을 포함한 Gitleaks와 전체 CI가 통과하는지 확인합니다.

키 회전은 Gitleaks를 초록색으로 만드는 작업이 아니라, 노출된 키로 접근하지 못하게
막는 실제 보안 조치입니다. 따라서 예외 처리보다 키 회전이 먼저입니다.

## 7. PM 검수 항목

PM은 다음 사항을 확인하면 됩니다.

- CI 호출 버전을 당장은 `v3.3.0`으로 검토하고, 최종 병합 전에 `v3.3.1` 전환
  조건을 충족할 것인지
- `main`만 운영 브랜치인지, 별도의 `dev` 또는 `develop` 브랜치도 CI 대상인지
- `/admin/login/`을 내부 smoke test 경로로 사용하는 것이 맞는지
- 운영 환경이 TLS를 종료하는 프록시에서 `X-Forwarded-Proto: https`를 전달하는지
- `SCHEDULER_BASE_URL`이 주소의 루트만 사용하는 현재 제약과 맞는지
- 과거 `.env.save` 키의 회전 또는 폐기 담당자와 완료 기준
- Git 기록을 유지하고 정확한 fingerprint를 예외 처리할지, 별도 history rewrite를
  할지
- 이 브랜치는 CI 준비만 포함하고 CD는 후속 작업으로 분리하는 것이 맞는지

## 8. 권장 진행 순서

1. 이 로컬 브랜치와 문서를 다시 검수합니다.
2. 사용자의 명시적 허락을 받은 뒤 `chore/devsecops-v3` 브랜치만 원격에 push합니다.
3. 아직 PR을 만들지 않고 PM에게 브랜치와 `main` 비교 링크를 전달합니다.
4. PM 의견을 로컬 브랜치에 반영합니다.
5. 과거 시크릿을 확인하고 필요한 키를 회전·폐기합니다.
6. 과거 기록 처리 방식을 적용하고 중앙 `v3.3.1`을 발행합니다.
7. 백엔드 호출 버전을 `v3.3.1`로 변경합니다.
8. 사용자의 별도 허락을 받은 뒤 `main` 대상 Draft PR 또는 일반 PR을 만듭니다.
9. PR에서 표시되는 실제 필수 검사 이름을 branch protection에 등록합니다.
   reusable 호출이므로 `ci / security-scan`, `ci / sast-scan`,
   `ci / lint-and-test`, `ci / docker-build-check` 형태로 표시됩니다.
10. 모든 필수 검사가 통과하고 리뷰 승인을 받은 뒤에만 `main`으로 병합합니다.

작업 브랜치만 push하면 현재 이벤트 설정상 GitHub CI는 실행되지 않습니다. 실제
GitHub CI 확인은 `main` 대상 PR을 만들 때 시작됩니다. PR 전에 PM이 브랜치를
검토하는 단계에서는 이 문서, 코드 diff, 로컬 검증 결과를 기준으로 확인합니다.
