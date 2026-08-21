# MSG CTF Backend

## 로컬 실행

처음 한 번만 PostgreSQL을 준비한다. Docker를 쓴다면 Docker Desktop을 켠 뒤 아래 명령을 실행한다.

```powershell
docker compose up -d
.\scripts\setup_local.ps1
```

이미 PostgreSQL 16이 `localhost:5432`에서 실행 중이면 `docker compose up -d`는 건너뛴다.

그다음부터는 아래 명령 하나로 실행한다. 최신 코드를 받은 뒤에도 마이그레이션을 자동 반영하고, 비어 있는 로컬 DB에만 보드 초기 데이터를 넣는다.

```powershell
.\scripts\run_local.ps1
```

서버 주소는 http://127.0.0.1:8000 이다. 로컬 개발에서는 `.env`의 `USE_LOCMEM_CACHE=1`로 Redis 없이 실행한다.

보드 데이터를 초기화하려면 `.\venv\Scripts\python.exe manage.py seed_board`를 직접 실행한다. 이 명령은 기존 보드 진행 데이터를 지운다.
