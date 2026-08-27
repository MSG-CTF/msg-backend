import functools
import hashlib
import time
import uuid

from django.core.cache import cache
from rest_framework.response import Response

from .exceptions import IdempotencyInProgress, IdempotencyKeyConflict, IdempotencyKeyRequired

IDEMPOTENCY_TTL_SECONDS = 300
IDEMPOTENCY_WAIT_SECONDS = 10
IDEMPOTENCY_POLL_SECONDS = 0.05


def _request_fingerprint(request):
    return hashlib.sha256(request.body).hexdigest()


def _response_from_cache(cached, fingerprint):
    if cached.get("fingerprint") not in (None, fingerprint):
        raise IdempotencyKeyConflict()
    return Response(cached["body"], status=cached["status"])


def idempotent(view_method):
    """Idempotency-Key 헤더를 요구하고, 같은 키로 재요청하면 처음 응답을 그대로 재생한다."""

    @functools.wraps(view_method)
    def wrapper(self, request, *args, **kwargs):
        key = request.headers.get("Idempotency-Key")
        if not key:
            raise IdempotencyKeyRequired()

        fingerprint = _request_fingerprint(request)
        cache_key = f"idem:{request.user.user_id}:{request.method}:{request.path}:{key}"
        lock_key = f"{cache_key}:lock"

        while True:
            cached = cache.get(cache_key)
            if cached is not None:
                return _response_from_cache(cached, fingerprint)

            lock_value = {"token": uuid.uuid4().hex, "fingerprint": fingerprint}
            # django-redis implements add() as an atomic SET NX operation.
            if cache.add(lock_key, lock_value, IDEMPOTENCY_TTL_SECONDS):
                try:
                    response = view_method(self, request, *args, **kwargs)
                    if response.status_code < 500:
                        cache.set(
                            cache_key,
                            {
                                "body": response.data,
                                "status": response.status_code,
                                "fingerprint": fingerprint,
                            },
                            IDEMPOTENCY_TTL_SECONDS,
                        )
                    return response
                finally:
                    # The response is written before releasing the claim. The TTL
                    # remains a crash-safety fallback if the process dies earlier.
                    cache.delete(lock_key)

            existing_lock = cache.get(lock_key)
            if (
                isinstance(existing_lock, dict)
                and existing_lock.get("fingerprint") not in (None, fingerprint)
            ):
                raise IdempotencyKeyConflict()

            deadline = time.monotonic() + IDEMPOTENCY_WAIT_SECONDS
            while time.monotonic() < deadline:
                cached = cache.get(cache_key)
                if cached is not None:
                    return _response_from_cache(cached, fingerprint)
                if cache.get(lock_key) is None:
                    break
                time.sleep(IDEMPOTENCY_POLL_SECONDS)
            else:
                raise IdempotencyInProgress()

    return wrapper
