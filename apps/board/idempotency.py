import functools

from django.core.cache import cache
from rest_framework.response import Response

from .exceptions import IdempotencyKeyRequired

IDEMPOTENCY_TTL_SECONDS = 300


def idempotent(view_method):
    """Idempotency-Key 헤더를 요구하고, 같은 키로 재요청하면 처음 응답을 그대로 재생한다."""

    @functools.wraps(view_method)
    def wrapper(self, request, *args, **kwargs):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise IdempotencyKeyRequired()

        cache_key = f"idem:{request.user.user_id}:{request.path}:{key}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached["body"], status=cached["status"])

        response = view_method(self, request, *args, **kwargs)
        if response.status_code < 500:
            cache.set(
                cache_key,
                {"body": response.data, "status": response.status_code},
                IDEMPOTENCY_TTL_SECONDS,
            )
        return response

    return wrapper
