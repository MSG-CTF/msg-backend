import uuid

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from unittest.mock import patch

from apps.accounts.models import Role, Team, User
from apps.challenge.models import Challenge
from apps.instances.models import DeleteReason, Instance, InstanceStatus

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

@override_settings(CACHES=LOCMEM)
class AdminInstanceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="인스턴스팀", team_score=0)
        self.admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자",
            team=None, role=Role.ADMIN,
        )
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.challenge = Challenge.objects.create(
            title="웹 문제", category="WEB", difficulty="EASY",
            score=500, flag_hash="x", is_published=True,
        )
        self.auth("root")

    def auth(self, login_id):
        res = self.client.post("/api/v1/auth/login",
                               {"login_id": login_id, "password": "pw1234"}, format="json")
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def _instance(self, status=InstanceStatus.RUNNING, user=None):
        return Instance.objects.create(
            user=user or self.player, team=self.team, challenge=self.challenge,
            status=status,
        )

    def test_list_returns_instances_and_summary(self):
        self._instance(status=InstanceStatus.RUNNING)
        self._instance(status=InstanceStatus.STOPPED)
        res = self.client.get("/api/v1/admin/instances")
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["total_count"], 2)
        summary = res.data["data"]["summary"]
        self.assertEqual(summary["by_status"]["RUNNING"], 1)
        self.assertEqual(summary["by_status"]["STOPPED"], 1)
        self.assertEqual(summary["by_team"][0]["running_count"], 1)
        self.assertEqual(summary["by_challenge"][0]["running_count"], 1)

    def test_list_status_filter(self):
        self._instance(status=InstanceStatus.RUNNING)
        self._instance(status=InstanceStatus.STOPPED)
        res = self.client.get("/api/v1/admin/instances?status=RUNNING")
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(res.data["data"]["instances"][0]["status"], "RUNNING")

    def test_list_invalid_status(self):
        res = self.client.get("/api/v1/admin/instances?status=NOPE")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_list_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/instances")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "FORBIDDEN")

    @patch("apps.adminpanel.views.call_scheduler_delete")
    def test_force_delete_success(self, mock_delete):
        mock_delete.return_value = None
        inst = self._instance(status=InstanceStatus.RUNNING)
        res = self.client.delete(f"/api/v1/admin/instances/{inst.instance_id}")
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["data"]["status"], "STOPPING")
        self.assertEqual(res.data["data"]["forced_by"], "root")
        inst.refresh_from_db()
        self.assertEqual(inst.status, InstanceStatus.STOPPING)
        self.assertEqual(inst.delete_reason, DeleteReason.ADMIN_FORCED)

    def test_force_delete_not_found(self):
        res = self.client.delete(f"/api/v1/admin/instances/{uuid.uuid4()}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "INSTANCE_NOT_FOUND")

    def test_force_delete_already_terminated(self):
        inst = self._instance(status=InstanceStatus.STOPPED)
        res = self.client.delete(f"/api/v1/admin/instances/{inst.instance_id}")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "INSTANCE_ALREADY_TERMINATED")

    def test_force_delete_participant_blocked(self):
        inst = self._instance()
        self.auth("player")
        res = self.client.delete(f"/api/v1/admin/instances/{inst.instance_id}")
        self.assertEqual(res.status_code, 403)