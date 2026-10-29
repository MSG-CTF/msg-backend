import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest import mock, skipUnless

from django.core.cache import cache, caches
from django.db import close_old_connections, connections
from django.test import SimpleTestCase, TransactionTestCase
from django_redis.cache import RedisCache
from redis.exceptions import ConnectionError, ResponseError, TimeoutError
from rest_framework.test import APIClient

from apps.accounts.models import RefreshToken, User
from apps.common.throttling import LoginRateThrottle


@skipUnless(isinstance(caches["default"], RedisCache), "Requires the configured Redis cache")
class AtomicLoginThrottleTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user(
            login_id="atomic-login", password="local-test-password", nickname="owned"
        )

    def tearDown(self):
        cache.clear()

    def login(self, ip):
        return APIClient().post(
            "/api/v1/auth/login",
            {"login_id": "atomic-login", "password": "wrong"},
            format="json",
            REMOTE_ADDR=ip,
        )

    def test_parallel_requests_cannot_exceed_the_remaining_account_allowance(self):
        for _ in range(5):
            cache.clear()
            for _ in range(9):
                self.assertEqual(self.login("192.0.2.1").status_code, 401)
            barrier = Barrier(8)

            def attempt(index):
                close_old_connections()
                try:
                    barrier.wait(10)
                    return self.login(f"198.51.100.{index + 1}").status_code
                finally:
                    connections.close_all()

            with ThreadPoolExecutor(max_workers=8) as pool:
                codes = list(pool.map(attempt, range(8)))
            self.assertEqual(codes.count(401), 1, codes)
            self.assertEqual(codes.count(429), 7, codes)

    def test_redis_execution_failure_rejects_authentication_without_leaking_details(self):
        client = caches["default"].client.get_client(write=True)
        for kind in (ConnectionError, TimeoutError, ResponseError):
            with self.subTest(kind=kind), mock.patch.object(
                client, "eval", side_effect=kind("private-redis-credential")
            ), mock.patch.object(User, "check_password") as check:
                with self.assertLogs("apps.common.throttling", level="WARNING") as logs:
                    response = self.login("192.0.2.1")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.data["code"], "RATE_LIMIT_UNAVAILABLE")
                self.assertNotIn("private-redis-credential", str(response.data))
                self.assertNotIn("private-redis-credential", "\n".join(logs.output))
                check.assert_not_called()
        self.assertEqual(RefreshToken.objects.count(), 0)
        self.assertEqual(self.login("192.0.2.1").status_code, 401)

    def test_malformed_atomic_result_never_authenticates(self):
        client = caches["default"].client.get_client(write=True)
        for result in (
            None, False, [], [1], [1, 0, 0], [True, 0], [1, "0"], [2, 0],
            [0, -1], [0, 0], [0, 60001], [1, 10],
        ):
            with self.subTest(result=result), mock.patch.object(
                client, "eval", return_value=result
            ), mock.patch.object(User, "check_password") as check:
                response = self.login("192.0.2.1")
                self.assertEqual(response.status_code, 503)
                check.assert_not_called()
        self.assertEqual(RefreshToken.objects.count(), 0)

    def test_legacy_pickled_history_does_not_conflict_with_the_new_redis_key(self):
        cache.set("throttle_login_atomic-login", [time.time()] * 10, 60)
        self.assertEqual(self.login("192.0.2.1").status_code, 401)


@skipUnless(isinstance(caches["default"], RedisCache), "Requires the configured Redis cache")
class RedisLoginWindowTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.request = SimpleNamespace(data={"login_id": "window-owner"})

    def tearDown(self):
        cache.clear()

    def test_window_expires_and_denied_requests_do_not_extend_it(self):
        with mock.patch.object(LoginRateThrottle, "THROTTLE_RATES", {"login": "2/s"}):
            self.assertTrue(LoginRateThrottle().allow_request(self.request, None))
            self.assertTrue(LoginRateThrottle().allow_request(self.request, None))
            limiter = LoginRateThrottle()
            backend = caches["default"]
            raw = backend.client.get_client(write=True)
            key = backend.make_key(limiter.get_cache_key(self.request, None))
            before = raw.pttl(key)
            self.assertFalse(limiter.allow_request(self.request, None))
            self.assertGreater(limiter.wait(), 0)
            self.assertLessEqual(limiter.wait(), 1)
            self.assertLessEqual(raw.pttl(key), before)
            time.sleep(1.05)
            self.assertTrue(LoginRateThrottle().allow_request(self.request, None))

    def test_zero_rate_keeps_requests_blocked(self):
        with mock.patch.object(LoginRateThrottle, "THROTTLE_RATES", {"login": "0/min"}):
            limiter = LoginRateThrottle()
            self.assertFalse(limiter.allow_request(self.request, None))
            self.assertEqual(limiter.wait(), 60)
