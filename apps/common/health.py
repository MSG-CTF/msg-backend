import uuid

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse

# SLA 감시용 헬스체크. /api/v1 밖의 예외 경로라 공통 봉투를 쓰지 않고
# 인증 없이 상태만 내려준다 (contract.yaml base_url_exceptions 참고).


def _check_database():
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return "ok"
    except Exception:
        return "down"


def _check_cache():
    try:
        key = "healthz:" + uuid.uuid4().hex
        cache.set(key, "pong", 10)
        value = cache.get(key)
        cache.delete(key)
        return "ok" if value == "pong" else "down"
    except Exception:
        return "down"


def healthz(request):
    database = _check_database()
    cache_status = _check_cache()
    healthy = database == "ok" and cache_status == "ok"

    return JsonResponse(
        {
            "status": "ok" if healthy else "down",
            "database": database,
            "cache": cache_status,
        },
        status=200 if healthy else 503,
    )
