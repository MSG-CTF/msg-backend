# msg-backend

브루마블형 CTF 플랫폼 백엔드 API (Django)

담당 범위: `web`, `api` (auth / team / adminpanel 등). cloud/k8s 배치는 scheduler가 담당하며, 이 레포는 다루지 않습니다.

---

## 기술 스택

| 항목 | 버전 |
|---|---|
| Python | 3.12.13 (Windows는 공식 installer 미제공으로 3.12.10 사용 가능, 하단 참고) |
| Django | 5.2.16 |
| DB | PostgreSQL 16 |
| 캐시 | Redis (설정만 등록, 추후 사용) |
| 인증 | djangorestframework-simplejwt (JWT) |
| 패키지 관리 | requirements.txt |

---

## 1. 사전 설치

- [Python 3.12.13](https://www.python.org/downloads/release/python-31213/) (macOS/Linux)
  - Windows는 공식 installer가 없으므로 [3.12.10](https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe) 사용 권장 (패치 버전 차이만 있어 실질적 문제 없음)
- [PostgreSQL 16](https://www.postgresql.org/download/)
- Git

Windows 사용자는 터미널로 **Git Bash**를 사용하세요 (설치 시 자동 포함). 이 문서의 명령어는 Git Bash / macOS 터미널 기준입니다.

---

## 2. 레포 클론

```bash
git clone https://github.com/MSG-CTF/msg-backend.git
cd msg-backend
```

---

## 3. 가상환경 생성 및 활성화

**생성**
```bash
# macOS
python3.12 -m venv venv

# Windows (Git Bash)
py -3.12 -m venv venv
```

**활성화**
```bash
# macOS / Linux
source venv/bin/activate

# Windows (Git Bash)
source venv/Scripts/activate
```

프롬프트 앞에 `(venv)`가 붙으면 정상 활성화된 것입니다. **이후 모든 명령어는 활성화된 상태에서 실행합니다.**

버전 확인:
```bash
python --version
```

---

## 4. 패키지 설치

```bash
pip install -r requirements.txt
```

---

## 5. 환경변수(.env) 설정

`.env`는 민감정보 포함으로 Git에서 제외되어 있습니다. `.env.example`을 복사해서 직접 만드세요.

```bash
cp .env.example .env
```

`.env` 내용:
```
DEBUG=True
SECRET_KEY=아래_명령으로_생성해서_붙여넣기

DB_NAME=msg_backend
DB_USER=postgres
DB_PASSWORD=1234
DB_HOST=localhost
DB_PORT=5432

REDIS_HOST=localhost
REDIS_PORT=6379
```

`SECRET_KEY` 생성:
```bash
python -c "import secrets; print(secrets.token_urlsafe(50))"
```
나온 값을 `SECRET_KEY=` 뒤에 붙여넣습니다. (이 값은 팀원마다 달라도 무방합니다.)

---

## 6. PostgreSQL 계정 및 DB 생성

`.env`에 적은 `DB_USER`, `DB_PASSWORD`와 실제 PostgreSQL 계정이 **정확히 일치**해야 합니다.

```bash
psql -U postgres
```
(macOS Homebrew 설치 시 `postgres` 계정이 기본으로 없을 수 있습니다 — 그 경우 `psql postgres`로 접속.)

psql 프롬프트 안에서:
```sql
CREATE DATABASE msg_backend;

-- postgres 계정이 없다는 에러가 나면 아래 실행
CREATE USER postgres WITH PASSWORD '1234' SUPERUSER CREATEDB;

-- 이미 있다면 비밀번호만 .env와 맞추기
ALTER USER postgres WITH PASSWORD '1234';

\q
```

---

## 7. 마이그레이션 적용

```bash
python manage.py migrate
```

`OK`가 순서대로 뜨면서 끝나면 정상입니다.

---

## 8. 관리자 계정 생성 (선택)

Django Admin(`/admin/`)에 접속하려면 생성:
```bash
python manage.py createsuperuser
```

> 생성 시 `role`은 기본값(`PARTICIPANT`)으로 들어갑니다. 관리자 권한이 필요하면 `/admin/`에서 직접 `ADMIN`으로 변경하세요.

---

## 9. 서버 실행

```bash
python manage.py runserver
```

정상 출력 예:
```
Django version 5.2.16, using settings 'config.settings'
Starting development server at http://127.0.0.1:8000/
```

확인:
- Django Admin: http://127.0.0.1:8000/admin/
- API 테스트 도구 (devtools, `DEBUG=True`일 때만 노출): http://127.0.0.1:8000/devtools/

---

## 10. API 테스트

**devtools 페이지 사용** (권장, 브라우저에서 버튼으로 테스트)
```
http://127.0.0.1:8000/devtools/
```

**curl로 직접 테스트**
```bash
# 로그인
curl -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"loginId": "kim01", "password": "비밀번호"}'

# 내 정보 확인 (위에서 받은 accessToken 사용)
curl -X GET http://127.0.0.1:8000/api/v1/auth/me \
  -H "Authorization: Bearer {accessToken}"
```

---

## 프로젝트 구조

```
msg-backend/
├── apps/
│   ├── accounts/     로그인 (login / logout / me)
│   ├── teams/        마이페이지 (내 팀 정보 / 풀이기록 / 마일리지 히스토리)
│   ├── adminpanel/   어드민 (팀 관리 / 인스턴스 관리 등)
│   ├── board/        보드 / 문제 (타 담당자)
│   └── common/       공통 유틸 + devtools
├── config/           Django 설정 (settings.py, urls.py)
├── docs/
│   └── ERD.md         DB 구조 문서 (Mermaid)
├── manage.py
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## 협업 규칙

- `main` 브랜치에 직접 커밋 금지. 항상 `feature/기능이름` 브랜치에서 작업 후 PR
- 새 작업 시작 전 항상 최신 main에서 분기:
  ```bash
  git checkout main
  git pull
  git checkout -b feature/새기능이름
  ```
- 각자 `apps/자기담당폴더/` 안에서만 작업 (충돌 최소화)
- `config/settings.py`, `config/urls.py`, `requirements.txt`는 여러 명이 같이 건드리는 파일이므로, 앱/패키지 추가 시 **작게 자주 PR** 올리기. `INSTALLED_APPS` 등 리스트는 알파벳 순으로 추가
- 커밋 전 `.env`가 `git status`에 안 보이는지 항상 확인

## API 공통 규칙

- Base URL: `/api/v1`
- 응답 형식: `{ code, message, data }`
- 인증: `Authorization: Bearer {accessToken}` (JWT)
- Access Token 만료: 1시간 / Refresh Token 만료: 12시간

## 완료된 API

| 페이지 | API | 상태 |
|---|---|---|
| 로그인 | POST `/api/v1/auth/login` | ✅ |
| 로그인 | POST `/api/v1/auth/logout` | ✅ |
| 로그인 | GET `/api/v1/auth/me` | ✅ |
| 마이페이지 | GET `/api/v1/teams/me` | ✅ |
| 마이페이지 | GET `/api/v1/teams/me/solves` | 🔲 진행 예정 |
| 마이페이지 | GET `/api/v1/teams/me/mileage-history` | 🔲 진행 예정 |
| 어드민 | 팀 벤 처리/해제, 마일리지 관리, 팀/인스턴스 목록 | 🔲 진행 예정 |

자세한 API 명세는 팀 문서(API 명세서) 참고.
