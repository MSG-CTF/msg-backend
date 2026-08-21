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
    def test_mileage_grant(self):
        """양수 지급 → ADMIN_GRANT, 잔액 증가."""
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        before = self.team.mileage

        res = self.client.post(url, {"amount": 50, "reason": "보상"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["previous_mileage"], before)
        self.assertEqual(res.data["data"]["current_mileage"], before + 50)

        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, before + 50)

        from apps.teams.models import MileageHistory, MileageType
        row = MileageHistory.objects.filter(team=self.team).latest("created_at")
        self.assertEqual(row.type, MileageType.ADMIN_GRANT)
        self.assertEqual(row.amount, 50)

    def test_mileage_deduct(self):
        """음수 회수 → ADMIN_DEDUCT."""
        from apps.accounts.models import Team
        Team.objects.filter(pk=self.team.pk).update(mileage=100)
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"

        res = self.client.post(url, {"amount": -30, "reason": "회수"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["current_mileage"], 70)

        from apps.teams.models import MileageHistory, MileageType
        row = MileageHistory.objects.filter(team=self.team).latest("created_at")
        self.assertEqual(row.type, MileageType.ADMIN_DEDUCT)

    def test_mileage_invariant(self):
        """불변식: mileage_history 총합 == team.mileage."""
        from django.db.models import Sum
        from apps.accounts.models import Team
        from apps.teams.models import MileageHistory
        Team.objects.filter(pk=self.team.pk).update(mileage=0)
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"

        self.client.post(url, {"amount": 100, "reason": "a"}, format="json")
        self.client.post(url, {"amount": -30, "reason": "b"}, format="json")
        self.client.post(url, {"amount": 50, "reason": "c"}, format="json")

        self.team.refresh_from_db()
        total = MileageHistory.objects.filter(team=self.team).aggregate(s=Sum("amount"))["s"]
        self.assertEqual(total, self.team.mileage)
        self.assertEqual(self.team.mileage, 120)

    def test_mileage_zero_rejected(self):
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        res = self.client.post(url, {"amount": 0, "reason": "x"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_AMOUNT")

    def test_mileage_insufficient(self):
        """회수액이 잔액보다 크면 거부, 잔액 불변."""
        from apps.accounts.models import Team
        Team.objects.filter(pk=self.team.pk).update(mileage=20)
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"

        res = self.client.post(url, {"amount": -50, "reason": "x"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INSUFFICIENT_MILEAGE")
        self.assertEqual(res.data["data"]["current_mileage"], 20)
        self.assertEqual(res.data["data"]["requested_amount"], 50)

        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 20)   # 안 바뀌어야 함

    def test_mileage_missing_fields(self):
        self.auth("root")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        for body in [{}, {"amount": 50}, {"reason": "x"}, {"amount": 50, "reason": "  "}]:
            self.assertEqual(self.client.post(url, body, format="json").status_code, 400)

    def test_mileage_participant_blocked(self):
        self.auth("player")
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        res = self.client.post(url, {"amount": 50, "reason": "x"}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_mileage_team_not_found(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/teams/00000000-0000-0000-0000-000000000000/mileage",
            {"amount": 50, "reason": "x"}, format="json",
        )
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")