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

    def test_login_id_whitespace_does_not_bypass_limit(self):
        for i in range(10):
            self.login("throttle-target", "wrong-password", f"10.0.2.{i}")
        response = self.login("\tthrottle-target\n", "wrong-password", "10.0.2.99")
        self.assertEqual(response.status_code, 429)

    def test_other_account_on_same_ip_is_not_limited(self):
        User.objects.create_user(login_id="other-user", password=self.password, nickname="other")
        for _ in range(10):
            self.login("throttle-target", "wrong-password", "10.0.1.1")
        response = self.login("other-user", self.password, "10.0.1.1")
        self.assertEqual(response.status_code, 200)

    def test_case_sensitive_accounts_have_independent_limits(self):
        User.objects.create_user(login_id="THROTTLE-TARGET", password=self.password, nickname="upper")
        for _ in range(10):
            self.login("throttle-target", "wrong-password", "10.0.1.1")
        for ip in ("10.0.1.1", "10.0.2.1"):
            with self.subTest(ip=ip):
                self.assertEqual(self.login("THROTTLE-TARGET", self.password, ip).status_code, 200)

    def test_twelve_accounts_on_the_same_ip_can_log_in_after_one_account_is_limited(self):
        for _ in range(10):
            self.login("throttle-target", "wrong-password", "10.0.1.1")
        for i in range(12):
            login_id = f"same-school-{i}"
            User.objects.create_user(login_id=login_id, password=self.password, nickname=login_id)
            self.assertEqual(self.login(login_id, self.password, "10.0.1.1").status_code, 200)

    def test_numeric_zero_and_string_identifiers_share_the_same_limit(self):
        for value in (0, 0.0, -0.0):
            with self.subTest(value=repr(value)):
                cache.clear()
                identifier = str(value)
                User.objects.create_user(login_id=identifier, password=self.password, nickname=identifier)
                self.assertEqual(self.login(value, self.password, "10.0.1.1").status_code, 200)
                for _ in range(9):
                    self.assertEqual(self.login(f" {identifier} ", "wrong", "10.0.2.1").status_code, 401)
                self.assertEqual(self.login(value, self.password, "10.0.3.1").status_code, 429)
