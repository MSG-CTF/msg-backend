FROM python:3.12.10-slim

# 베이스 이미지의 알려진 취약점 패치.
# psycopg-binary는 휠(.whl)로 배포되므로 build-essential / libpq-dev가 필요 없다.
# 컴파일러를 런타임 이미지에 남기면 침해 시 공격자가 그 자리에서 코드를 빌드할 수 있다.
RUN apt-get update && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성만 먼저 복사해서 캐시 활용 (코드만 바뀌면 pip install 다시 안 돌게)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 나머지 소스 코드 복사
COPY . .

# root로 컨테이너를 실행하지 않는다 - 컨테이너 탈출 시 피해 반경 최소화
RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && chown -R app:app /app
ENV HOME=/app
USER app

EXPOSE 8080

# SECURE_SSL_REDIRECT가 켜져 있어 http 요청은 301로 튕긴다.
# 프록시 뒤에 있는 것처럼 X-Forwarded-Proto를 붙여 실제 앱 응답을 확인한다.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(urllib.request.Request(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz', headers={'X-Forwarded-Proto':'https'}))" || exit 1

# Cloud Run은 $PORT로 리슨 포트를 지정한다 (기본 8080).
# 로컬 docker-compose에서는 command를 runserver로 오버라이드해서 사용.
CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8080}"]
