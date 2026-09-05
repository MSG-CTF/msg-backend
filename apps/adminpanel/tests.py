import uuid

from datetime import timedelta
from django.utils import timezone
from django.core.cache import cache
from django.test import TestCase, override_settings

from rest_framework.test import APIClient

from apps.common.jwt import hash_token
from apps.accounts.models import (
    Role,
    Team,
    User,
)
from apps.teams.models import (
    MileageHistory,
    MileageType,
    PaymentToken,
    PaymentTokenStatus,
)
from apps.challenge.models import Challenge
from apps.instances.models import (
    ChallengeRelease,
    ChallengeRuntimeConfig,
    DeleteReason,
    Instance,
    InstanceStatus,
    ReleaseContainer,
)
from unittest.mock import patch

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
class PaymentTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(
            team_name="감자는외로워", team_score=0, mileage=200
        )
        self.admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자",
            team=None, role=Role.ADMIN,
        )
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.auth("root")

    def auth(self, login_id):
        res = self.client.post("/api/v1/auth/login",
                               {"login_id": login_id, "password": "pw1234"}, format="json")
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}"
        )

    def mint_token(self, raw="tok-abc", team=None, hours=1,
                   status=PaymentTokenStatus.ACTIVE):
        return PaymentToken.objects.create(
            team=team or self.team,
            token_hash=hash_token(raw),
            status=status,
            expires_at=timezone.now() + timedelta(hours=hours),
        )

    def _purchase(self, raw="tok-p", amount=30, item="굿즈"):
        self.mint_token(raw)
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": raw, "amount": amount, "item_name": item},
            format="json",
        )
        return res.data["data"]["history_id"]

    # ---------- checkout ----------
    def test_checkout_success(self):
        self.mint_token("tok-abc")
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-abc", "amount": 30, "item_name": "부스A 음료"},
            format="json",
        )
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["amount"], -30)
        self.assertEqual(res.data["data"]["current_mileage"], 170)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 170)
        token = PaymentToken.objects.get(token_hash=hash_token("tok-abc"))
        self.assertEqual(token.status, PaymentTokenStatus.USED)
        self.assertIsNotNone(token.history_id)

    def test_checkout_token_invalid(self):
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "nope", "amount": 30, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "PAYMENT_TOKEN_INVALID")

    def test_checkout_token_expired(self):
        self.mint_token("tok-exp", hours=-1)
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-exp", "amount": 30, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "PAYMENT_TOKEN_EXPIRED")

    def test_checkout_insufficient_keeps_token(self):
        self.mint_token("tok-poor")
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-poor", "amount": 9999, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INSUFFICIENT_MILEAGE")
        # 잔액 부족은 토큰을 소비하지 않는다
        token = PaymentToken.objects.get(token_hash=hash_token("tok-poor"))
        self.assertEqual(token.status, PaymentTokenStatus.ACTIVE)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 200)

    def test_checkout_bad_amount(self):
        self.mint_token("tok-a")
        for amt in [0, -5]:
            res = self.client.post(
                "/api/v1/admin/payment/checkout",
                {"payment_token": "tok-a", "amount": amt, "item_name": "x"},
                format="json",
            )
            self.assertEqual(res.data["code"], "INVALID_AMOUNT")

    def test_checkout_participant_blocked(self):
        self.auth("player")
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok", "amount": 10, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "FORBIDDEN")

    # ---------- history ----------
    def test_history_lists_purchase(self):
        self.mint_token("tok-h")
        self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-h", "amount": 30, "item_name": "부스A 음료"},
            format="json",
        )
        res = self.client.get("/api/v1/admin/payment/history")
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["total_count"], 1)
        row = res.data["data"]["history"][0]
        self.assertEqual(row["type"], "PURCHASE")
        self.assertEqual(row["amount"], -30)
        self.assertFalse(row["is_refunded"])

    def test_history_team_filter(self):
        other = Team.objects.create(team_name="다른팀", team_score=0, mileage=100)
        self.mint_token("tok-a", team=self.team)
        self.mint_token("tok-b", team=other)
        self.client.post("/api/v1/admin/payment/checkout",
                         {"payment_token": "tok-a", "amount": 10, "item_name": "a"},
                         format="json")
        self.client.post("/api/v1/admin/payment/checkout",
                         {"payment_token": "tok-b", "amount": 20, "item_name": "b"},
                         format="json")
        res = self.client.get(
            f"/api/v1/admin/payment/history?team_id={self.team.team_id}"
        )
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(res.data["data"]["history"][0]["team_id"], str(self.team.team_id))

    # ---------- refund ----------
    def test_refund_success(self):
        hid = self._purchase(amount=30)  # 200 -> 170
        res = self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["refunded_amount"], 30)
        self.assertEqual(res.data["data"]["current_mileage"], 200)
        self.assertNotEqual(res.data["data"]["history_id"], hid)  # 새 REFUND 행
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 200)
        # 원본 PURCHASE 행은 삭제/수정되지 않는다
        self.assertTrue(
            MileageHistory.objects.filter(pk=hid, type=MileageType.PURCHASE).exists()
        )
        self.assertTrue(MileageHistory.objects.get(pk=hid).is_refunded)
        # history 에서 is_refunded 표시
        res = self.client.get("/api/v1/admin/payment/history")
        purchase_row = next(
            r for r in res.data["data"]["history"] if r["history_id"] == hid
        )
        self.assertTrue(purchase_row["is_refunded"])

    def test_refund_already_refunded(self):
        hid = self._purchase()
        self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        res = self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "ALREADY_REFUNDED")

    def test_refund_not_found(self):
        res = self.client.delete(f"/api/v1/admin/payment/{uuid.uuid4()}/refund")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "PAYMENT_NOT_FOUND")

    def test_refund_not_refundable(self):
        # ADMIN_GRANT 행은 환불 대상이 아니다
        h = MileageHistory.objects.create(
            team=self.team, type=MileageType.ADMIN_GRANT, amount=50,
            reason="지급", processed_by="root",
        )
        res = self.client.delete(f"/api/v1/admin/payment/{h.history_id}/refund")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "NOT_REFUNDABLE")

    def test_refund_participant_blocked(self):
        hid = self._purchase()
        self.auth("player")
        res = self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        self.assertEqual(res.status_code, 403)
        
    def test_refund_reflected_in_participant_history(self):
        hid = self._purchase(amount=30)
        self.client.delete(f"/api/v1/admin/payment/{hid}/refund")
        self.auth("player")
        res = self.client.get("/api/v1/teams/me/mileage_history")
        purchase = next(
            r for r in res.data["data"]["history"] if r["history_id"] == hid
        )
        self.assertTrue(purchase["is_refunded"])

    def test_checkout_banned_team_blocked(self):
        Team.objects.filter(pk=self.team.pk).update(is_banned=True, ban_reason="어뷰징")
        self.mint_token("tok-ban")
        res = self.client.post(
            "/api/v1/admin/payment/checkout",
            {"payment_token": "tok-ban", "amount": 30, "item_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "TEAM_BANNED")
        token = PaymentToken.objects.get(token_hash=hash_token("tok-ban"))
        self.assertEqual(token.status, PaymentTokenStatus.ACTIVE)

    def test_history_invalid_team_id_400(self):
        res = self.client.get("/api/v1/admin/payment/history?team_id=not-a-uuid")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

@override_settings(CACHES=LOCMEM, SCHEDULER_API_TOKEN="test-scheduler-token")
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
        self.release = ChallengeRelease.objects.create(
            challenge=self.challenge,
            version=1,
            registry_revision=1,
            challenge_slug="web-basic",
            cpu_millicores=500,
            memory_mib=512,
            ephemeral_storage_mib=1024,
            isolation_profile="WEB",
            source_ref="refs/heads/main",
        )
        ReleaseContainer.objects.create(
            release=self.release,
            name="web",
            image_ref=(
                "ghcr.io/msg-ctf/challenges/web-basic/web@sha256:"
                + "a" * 64
            ),
            ports=[{"port": 8080, "public": True}],
        )
        ChallengeRuntimeConfig.objects.create(
            challenge=self.challenge,
            current_release=self.release,
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
            status=status, release=self.release,
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

    @patch("apps.adminpanel.views.call_scheduler_reset")
    def test_force_reset_replaces_instance(self, mock_reset):
        old = self._instance(status=InstanceStatus.RUNNING)
        new_id = uuid.uuid4()
        mock_reset.return_value = {"instance_id": str(new_id), "status": "RESETTING"}
        res = self.client.post(f"/api/v1/admin/instances/{old.instance_id}/reset")
        self.assertEqual(res.status_code, 202)
        self.assertEqual(res.data["data"]["instance_id"], str(new_id))
        self.assertNotEqual(res.data["data"]["instance_id"], str(old.instance_id))
        self.assertEqual(res.data["data"]["status"], "RESETTING")
        self.assertEqual(res.data["data"]["forced_by"], "root")
        new_inst = Instance.objects.get(pk=new_id)
        self.assertEqual(new_inst.replaced_instance_id, old.instance_id)
        self.assertEqual(new_inst.release_id, self.release.release_id)
        old.refresh_from_db()
        self.assertEqual(old.status, InstanceStatus.STOPPING)
        self.assertEqual(old.delete_reason, DeleteReason.REPLACED_BY_NEW_INSTANCE)

    def test_force_reset_not_restartable(self):
        inst = self._instance(status=InstanceStatus.STOPPED)
        res = self.client.post(f"/api/v1/admin/instances/{inst.instance_id}/reset")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "INSTANCE_NOT_RESTARTABLE")

    def test_force_reset_not_found(self):
        res = self.client.post(f"/api/v1/admin/instances/{uuid.uuid4()}/reset")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "INSTANCE_NOT_FOUND")

    def test_force_reset_participant_blocked(self):
        inst = self._instance()
        self.auth("player")
        res = self.client.post(f"/api/v1/admin/instances/{inst.instance_id}/reset")
        self.assertEqual(res.status_code, 403)

    def test_list_summary_optout(self):
        res = self.client.get("/api/v1/admin/instances?summary=false")
        self.assertIsNone(res.data["data"]["summary"])
