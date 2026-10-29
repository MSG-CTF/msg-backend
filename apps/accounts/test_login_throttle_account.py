from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User


class LoginThrottlePerAccountTests(TestCase):
    password = "Throttle-Strong#Pw-2026"

    def setUp(self):
        cache.clear()
        User.objects.create_user(login_id="throttle-target", password=self.password, nickname="target")

    def tearDown(self):
        cache.clear()

    def login(self, login_id, password, ip):
        return APIClient().post(
            "/api/v1/auth/login",
            {"login_id": login_id, "password": password},
            format="json",
            REMOTE_ADDR=ip,
        )

    def test_same_account_is_limited_even_when_ip_changes(self):
        codes = [self.login("throttle-target", "wrong-password", f"10.0.0.{i}").status_code for i in range(1, 12)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10], 429)

    def test_login_id_case_does_not_bypass_limit(self):
        for i in range(10):
            self.login("throttle-target", "wrong-password", f"10.0.2.{i}")
        response = self.login("THROTTLE-TARGET", "wrong-password", "10.0.2.99")
        self.assertEqual(response.status_code, 429)

    def test_other_account_on_same_ip_is_not_limited(self):
        for _ in range(10):
            self.login("throttle-target", "wrong-password", "10.0.1.1")
        response = self.login("other-user", "wrong-password", "10.0.1.1")
        self.assertEqual(response.status_code, 401)
