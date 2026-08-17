from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class AdminTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="감자는외로워", team_score=350)
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자",
            team=None, role=Role.ADMIN,
        )

    def auth(self, login_id):
        res = self.client.post("/api/v1/auth/login",
                               {"login_id": login_id, "password": "pw1234"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}")

    def test_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/teams")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "FORBIDDEN")

    def test_admin_allowed_with_members(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/teams")
        self.assertEqual(res.status_code, 200)
        team = res.data["data"]["teams"][0]
        self.assertEqual(team["member_count"], 1)
        self.assertEqual(team["members"][0]["login_id"], "player")

    def test_invalid_sort(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/teams?sort=hello")
        self.assertEqual(res.status_code, 400)

    def test_huge_page_rejected(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/teams?page=99999999999")
        self.assertEqual(res.status_code, 400)

    def test_ban_and_unban(self):
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/ban"

        res = self.client.post(url, {"ban_reason": "어뷰징"}, format="json")
        self.assertEqual(res.data["code"], "SUCCESS")

        res = self.client.post(url, {"ban_reason": "또"}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "ALREADY_BANNED")

        self.assertEqual(self.client.delete(url).data["code"], "SUCCESS")
        self.assertEqual(self.client.delete(url).data["code"], "NOT_BANNED")

    def test_ban_reason_validation(self):
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/ban"
        for body in [{}, {"ban_reason": "   "}, {"ban_reason": {"a": 1}}]:
            self.assertEqual(self.client.post(url, body, format="json").status_code, 400)

    def test_team_not_found(self):
        self.auth("root")
        for tid in ["00000000-0000-0000-0000-000000000000", "hello"]:
            res = self.client.post(f"/api/v1/admin/teams/{tid}/ban",
                                   {"ban_reason": "x"}, format="json")
            self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")