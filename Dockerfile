FROM python:3.12-alpine@sha256:d09d15e60962ca365d1cd544a48773bac9d33f2fb1b00f2aa0deec78ade7dc31

RUN addgroup --system --gid 101 app \
    && adduser --system --uid 100 --ingroup app --home /app app

RUN apk upgrade --no-cache \
    && apk add --no-cache nginx

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check \
    && python -m pip uninstall --yes pip

COPY --chown=app:app . .

ENV HOME=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_CONCURRENCY=2 \
    LOGIN_WEB_CONCURRENCY=2 \
    GUNICORN_WORKER_CLASS=gthread \
    GUNICORN_THREADS=4 \
    GUNICORN_KEEPALIVE=10 \
    GUNICORN_TIMEOUT=30

USER app

RUN test "$(id -u)" = 100 && test "$(id -g)" = 101
RUN python -c 'import os; from deploy.web_server import commands; assert commands(os.environ) == commands({}), "Image web defaults differ from launcher defaults"'

EXPOSE 8080

CMD ["python", "deploy/web_server.py"]
