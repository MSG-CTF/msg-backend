import hashlib
import logging
import threading
import uuid

from django.core.cache import cache as default_cache, caches
from django.core.cache.backends.locmem import LocMemCache
from django_redis.cache import RedisCache

from rest_framework.exceptions import APIException, ValidationError
from rest_framework.throttling import SimpleRateThrottle, UserRateThrottle

from apps.accounts.serializers import LoginSerializer
from apps.common.exceptions import RateLimitUnavailable

logger = logging.getLogger(__name__)

_LOCAL_LOGIN_LOCK = threading.RLock()

# 한 계정의 최근 시도 확인과 추가를 한 번에 처리한다
_LOGIN_WINDOW_SCRIPT = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
if limit < 1 then
    return {0, window}
end
if redis.call('ZCARD', KEYS[1]) < limit then
    redis.call('ZADD', KEYS[1], now, ARGV[3])
    redis.call('PEXPIRE', KEYS[1], window)
    return {1, 0}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {0, math.max(1, tonumber(oldest[2]) + window - now)}
"""


class LoginRateThrottle(SimpleRateThrottle):

    scope = "login"

    def get_cache_key(self, request, view):
        # 인증과 같은 변환을 쓰되 대소문자가 다른 별도 계정은 구분한다
        if not isinstance(request.data, dict):
            return None
        try:
            login_id = LoginSerializer().fields["login_id"].run_validation(
                request.data.get("login_id")
            )
        except ValidationError:
            # 잘못된 입력의 400 응답은 기존 로그인 serializer가 처리한다
            return None
        digest = hashlib.sha256(login_id.encode("utf-8")).hexdigest()
        # 이전 pickle 목록과 Redis sorted set이 같은 키를 사용하지 않게 한다
        return f"throttle_login_v2_{digest}"

    def allow_request(self, request, view):
        try:
            if self.rate is None:
                return True
            key = self.get_cache_key(request, view)
            if key is None:
                return True
            backend = caches["default"] if self.cache is default_cache else self.cache
            if isinstance(backend, LocMemCache):
                # 개발·단위 시험의 프로세스 내 캐시도 횟수 갱신을 직렬 처리한다
                with _LOCAL_LOGIN_LOCK:
                    return super().allow_request(request, view)
            if not isinstance(backend, RedisCache):
                raise RuntimeError("Unsupported login rate-limit cache")
            result = backend.client.get_client(write=True).eval(
                _LOGIN_WINDOW_SCRIPT,
                1,
                backend.make_key(key),
                int(self.duration * 1000),
                self.num_requests,
                uuid.uuid4().hex,
            )
            if (
                not isinstance(result, (list, tuple))
                or len(result) != 2
                or any(type(value) is not int for value in result)
                or result[0] not in (0, 1)
                or not 0 <= result[1] <= self.duration * 1000
                or (result[0] == 1 and result[1] != 0)
                or (result[0] == 0 and result[1] == 0)
            ):
                raise RuntimeError("Invalid login rate-limit result")
            self._retry_after = result[1] / 1000
            return result[0] == 1
        except APIException:
            raise
        except Exception:
            logger.warning("Login rate limit unavailable; request rejected")
            raise RateLimitUnavailable() from None

    def wait(self):
        if hasattr(self, "_retry_after"):
            return self._retry_after
        return super().wait()

    def throttle_success(self):
        self.history.insert(0, self.now)
        # Some backends report a rejected write without raising an exception.
        if self.cache.set(self.key, self.history, self.duration) is False:
            raise RuntimeError("Rate limit cache write rejected")
        return True


class ReadApiThrottle(UserRateThrottle):
    """순위·타이머·리더보드처럼 화면이 주기적으로 다시 불러오는 조회 API의 사용자별 한도.

    프론트는 30초마다 다시 불러오므로 기본 한도(분당 60회)는 정상 사용을 막지 않는다.
    캐시 장애로 횟수를 셀 수 없으면 조회 자체가 실패하지 않도록 이번 요청은 통과시킨다.
    """

    scope = "read_api"

    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.exception("조회 스로틀 캐시 접근 실패 — 이번 요청은 통과시킨다")
            return True
