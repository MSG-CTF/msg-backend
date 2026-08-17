from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class MyPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="우리팀", team_score=350, mileage=120)
        self.other = Team.objects.create(team_name="남의팀", team_score=9999, mileage=9999)
        self.user = User.objects.create_user(
            login_id="me", password="pw1234", nickname="나", team=self.team, is_leader=True
        )
        User.objects.create_user(
            login_id="stranger", password="pw1234", nickname="남", team=self.other
        )
        self.auth("me")

    def auth(self, login_id):
        res = self.client.post("/api/v1/auth/login",
                               {"login_id": login_id, "password": "pw1234"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}")

    def test_team_me_returns_own_team_only(self):
        res = self.client.get("/api/v1/teams/me")
        self.assertEqual(res.data["data"]["team_name"], "우리팀")
        nicknames = [m["nickname"] for m in res.data["data"]["members"]]
        self.assertNotIn("남", nicknames)

    def test_other_team_data_isolated(self):
        self.auth("stranger")
        res = self.client.get("/api/v1/teams/me")
        self.assertEqual(res.data["data"]["team_name"], "남의팀")

    def test_admin_without_team_gets_404(self):
        User.objects.create_user(login_id="noteam", password="pw1234",
                                 nickname="무소속", team=None)
        self.auth("noteam")
        res = self.client.get("/api/v1/teams/me")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "USER_HAS_NO_TEAM")

    def test_qr_token_key_name(self):
        res = self.client.post("/api/v1/teams/me/qr_token")
        self.assertIn("payment_token", res.data["data"])
        self.assertNotIn("token", res.data["data"])   # 금지 필드

    def test_qr_token_replaces_previous(self):
        from apps.teams.models import PaymentToken, PaymentTokenStatus
        self.client.post("/api/v1/teams/me/qr_token")
        self.client.post("/api/v1/teams/me/qr_token")
        active = PaymentToken.objects.filter(team=self.team,
                                             status=PaymentTokenStatus.ACTIVE).count()
        self.assertEqual(active, 1)

    # ---- 밴 ----
    def test_banned_team_get_allowed_post_blocked(self):
        Team.objects.filter(pk=self.team.pk).update(is_banned=True, ban_reason="테스트")

        res = self.client.get("/api/v1/teams/me")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["data"]["is_banned"])

        res = self.client.post("/api/v1/teams/me/qr_token")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "TEAM_BANNED")

    def test_banned_team_can_still_login(self):
        """규약 「밴 처리」 예외 — 로그인은 허용."""
        Team.objects.filter(pk=self.team.pk).update(is_banned=True, ban_reason="테스트")
        self.client.credentials()
        res = self.client.post("/api/v1/auth/login",
                               {"login_id": "me", "password": "pw1234"}, format="json")
        self.assertEqual(res.status_code, 200)