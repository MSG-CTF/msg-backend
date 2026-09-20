import threading
import uuid

from datetime import timedelta
from decimal import Decimal
from django.db import connections
from django.utils import timezone
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings

from rest_framework.test import APIClient

from apps.common.jwt import ACCESS, decode_token, hash_token, issue_access_token
from apps.accounts.models import (
    Role,
    Team,
    User,
)
from apps.board.models import Cell, TeamChallengeAccess
from apps.teams.models import (
    MileageHistory,
    MileageType,
    PaymentToken,
    PaymentTokenStatus,
)
from apps.challenge.models import Challenge
from apps.instances.models import DeleteReason, Instance, InstanceStatus
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

    def mileage(self, body, key=None):
        return self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/mileage",
            body, format="json",
            HTTP_IDEMPOTENCY_KEY=key or uuid.uuid4().hex,
        )

    def test_mileage_idempotent_retry_applies_once(self):
        self.auth("root")
        k = "grant-1"
        first = self.mileage({"amount": 100, "reason": "보상"}, key=k)
        second = self.mileage({"amount": 100, "reason": "보상"}, key=k)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["data"]["current_mileage"],
                         first.data["data"]["current_mileage"])
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)
        self.assertEqual(MileageHistory.objects.filter(team=self.team).count(), 1)

    def test_mileage_same_key_different_body_conflict(self):
        self.auth("root")
        k = "grant-2"
        self.mileage({"amount": 100, "reason": "보상"}, key=k)
        res = self.mileage({"amount": 50, "reason": "보상"}, key=k)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "IDEMPOTENCY_KEY_CONFLICT")
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)

    def test_mileage_requires_idempotency_key(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/mileage",
            {"amount": 100, "reason": "보상"}, format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "IDEMPOTENCY_KEY_REQUIRED")

    def test_mileage_key_too_long_rejected(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/mileage",
            {"amount": 100, "reason": "보상"}, format="json",
            HTTP_IDEMPOTENCY_KEY="x" * 201,
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_mileage_failed_request_still_binds_key(self):
        from apps.accounts.models import Team
        Team.objects.filter(pk=self.team.pk).update(mileage=20)
        self.auth("root")
        k = "deduct-fail-1"

        first = self.mileage({"amount": -50, "reason": "회수"}, key=k)
        self.assertEqual(first.status_code, 400)
        self.assertEqual(first.data["code"], "INSUFFICIENT_MILEAGE")

        replay = self.mileage({"amount": -50, "reason": "회수"}, key=k)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.data["code"], "INSUFFICIENT_MILEAGE")

        conflict = self.mileage({"amount": 10, "reason": "회수"}, key=k)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.data["code"], "IDEMPOTENCY_KEY_CONFLICT")

        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 20)

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

        res = self.mileage({"amount": 50, "reason": "보상"})
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

        res = self.mileage({"amount": -30, "reason": "회수"})
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

        self.mileage({"amount": 100, "reason": "a"})
        self.mileage({"amount": -30, "reason": "b"})
        self.mileage({"amount": 50, "reason": "c"})

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

        res = self.mileage({"amount": -50, "reason": "x"})
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
            HTTP_IDEMPOTENCY_KEY=uuid.uuid4().hex,
        )
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_team_detail_success(self):
        from apps.teams.models import MileageHistory, MileageType
        self.auth("root")
        MileageHistory.objects.create(team=self.team, type=MileageType.ADMIN_GRANT,
                                      amount=100, reason="지급", processed_by="root")
        MileageHistory.objects.create(team=self.team, type=MileageType.PURCHASE,
                                      amount=-30, reason="음료", processed_by="root")
        MileageHistory.objects.create(team=self.team, type=MileageType.REFUND,
                                      amount=30, reason="환불", processed_by="root")
        res = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}")
        self.assertEqual(res.data["code"], "SUCCESS")
        d = res.data["data"]
        self.assertEqual(d["team_id"], str(self.team.team_id))
        self.assertEqual(d["member_count"], 1)
        self.assertEqual(d["members"][0]["login_id"], "player")
        self.assertEqual(d["mileage_summary"],
                         {"total_earned": 130, "total_spent": 30,
                          "purchase_count": 1, "refund_count": 1})
        self.assertEqual(len(d["recent_mileage_history"]), 3)

    def test_team_detail_not_found(self):
        import uuid
        self.auth("root")
        res = self.client.get(f"/api/v1/admin/teams/{uuid.uuid4()}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_team_detail_participant_blocked(self):
        self.auth("player")
        res = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}")
        self.assertEqual(res.status_code, 403)

    def test_team_detail_history_limit(self):
        from apps.teams.models import MileageHistory, MileageType
        self.auth("root")
        for i in range(15):
            MileageHistory.objects.create(team=self.team, type=MileageType.ADMIN_GRANT,
                                          amount=1, reason=f"r{i}", processed_by="root")
        res = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}?history_limit=5")
        self.assertEqual(len(res.data["data"]["recent_mileage_history"]), 5)

    def test_team_detail_board_position(self):
        from apps.board.models import Cell, TeamBoardState
        self.auth("root")
        cell = Cell.objects.create(cell_index=12, type="CHALLENGE", name="12번칸")
        TeamBoardState.objects.create(team=self.team, position=cell)
        res = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}")
        self.assertEqual(res.data["data"]["board_position_states"], 12)

    def test_team_detail_board_position_null_when_no_state(self):
        self.auth("root")
        res = self.client.get(f"/api/v1/admin/teams/{self.team.team_id}")
        self.assertIsNone(res.data["data"]["board_position_states"])
    def test_mileage_history_lists_all(self):
        self.auth("root")
        MileageHistory.objects.create(team=self.team, type=MileageType.ADMIN_GRANT,
                                      amount=100, reason="a", processed_by="root")
        MileageHistory.objects.create(team=self.team, type=MileageType.PURCHASE,
                                      amount=-30, reason="b", processed_by="root")
        res = self.client.get("/api/v1/admin/mileage_history")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["total_count"], 2)
        row = res.data["data"]["history"][0]
        self.assertEqual(
            set(row),
            {"history_id", "team_id", "team_name", "type", "amount",
             "reason", "processed_by", "created_at"},
        )

    def test_mileage_history_filter_by_type(self):
        self.auth("root")
        MileageHistory.objects.create(team=self.team, type=MileageType.ADMIN_GRANT,
                                      amount=100, reason="a", processed_by="root")
        MileageHistory.objects.create(team=self.team, type=MileageType.PURCHASE,
                                      amount=-30, reason="b", processed_by="root")
        res = self.client.get("/api/v1/admin/mileage_history?type=PURCHASE")
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(res.data["data"]["history"][0]["type"], "PURCHASE")

    def test_mileage_history_filter_by_team(self):
        other = Team.objects.create(team_name="다른팀")
        MileageHistory.objects.create(team=self.team, type=MileageType.ADMIN_GRANT,
                                      amount=100, reason="a", processed_by="root")
        MileageHistory.objects.create(team=other, type=MileageType.ADMIN_GRANT,
                                      amount=50, reason="c", processed_by="root")
        self.auth("root")
        res = self.client.get(f"/api/v1/admin/mileage_history?team_id={self.team.team_id}")
        self.assertEqual(res.data["data"]["total_count"], 1)

    def test_mileage_history_invalid_type(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/mileage_history?type=NOPE")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_mileage_history_invalid_team_id(self):
        self.auth("root")
        res = self.client.get("/api/v1/admin/mileage_history?team_id=not-a-uuid")
        self.assertEqual(res.status_code, 400)

    def test_mileage_history_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/mileage_history")
        self.assertEqual(res.status_code, 403)

    def test_account_create_success(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "newbie", "password": "pw12345678", "nickname": "새사람",
             "team_id": str(self.team.team_id), "is_leader": True},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(
            set(d),
            {"user_id", "login_id", "nickname", "role", "is_leader",
             "team_id", "team_name", "created_at"},
        )
        self.assertNotIn("password", d)
        self.assertEqual(d["team_id"], str(self.team.team_id))
        u = User.objects.get(login_id="newbie")
        self.assertTrue(u.check_password("pw12345678"))

    def test_account_create_without_team(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "solo", "password": "pw12345678", "nickname": "혼자"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.data["data"]["team_id"])
        self.assertEqual(res.data["data"]["role"], "PARTICIPANT")

    def test_account_create_admin_role(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "op2", "password": "pw12345678", "nickname": "운영2", "role": "ADMIN"},
            format="json",
        )
        self.assertEqual(res.data["data"]["role"], "ADMIN")

    def test_account_create_duplicate_login_id(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "player", "password": "pw12345678", "nickname": "중복"},
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "LOGIN_ID_TAKEN")

    def test_account_create_missing_or_bad_fields(self):
        self.auth("root")
        for body in [
            {},
            {"login_id": "a"},
            {"login_id": "a", "password": "pw12345678"},
            {"login_id": "a", "password": "short", "nickname": "x"},
        ]:
            self.assertEqual(
                self.client.post("/api/v1/admin/accounts", body, format="json").status_code, 400)

    def test_account_create_invalid_role(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "b", "password": "pw12345678", "nickname": "x", "role": "SUPER"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_account_create_invalid_team(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "c", "password": "pw12345678", "nickname": "x",
             "team_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_board_dice_grant(self):
        from apps.board.models import Cell, TeamBoardState
        self.auth("root")
        cell, _ = Cell.objects.get_or_create(cell_index=1, defaults={"type": "START", "name": "출발"})
        state = TeamBoardState.objects.create(team=self.team, position=cell, dice_rolls_left=0)
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": 2, "reason": "주사위 소실 보정"}, format="json",
        )
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["previous_dice_rolls_left"], 0)
        self.assertEqual(d["dice_rolls_left"], 2)
        self.assertEqual(d["adjusted_by"], "root")
        state.refresh_from_db()
        self.assertEqual(state.dice_rolls_left, 2)

    def test_board_dice_deduct(self):
        from apps.board.models import Cell, TeamBoardState
        self.auth("root")
        cell, _ = Cell.objects.get_or_create(cell_index=1, defaults={"type": "START", "name": "출발"})
        TeamBoardState.objects.create(team=self.team, position=cell, dice_rolls_left=3)
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": -2, "reason": "회수"}, format="json",
        )
        self.assertEqual(res.data["data"]["dice_rolls_left"], 1)

    def test_board_dice_insufficient(self):
        from apps.board.models import Cell, TeamBoardState
        self.auth("root")
        cell, _ = Cell.objects.get_or_create(cell_index=1, defaults={"type": "START", "name": "출발"})
        TeamBoardState.objects.create(team=self.team, position=cell, dice_rolls_left=1)
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": -5, "reason": "x"}, format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INSUFFICIENT_DICE")
        self.assertEqual(res.data["data"]["current_dice_rolls_left"], 1)
        self.assertEqual(res.data["data"]["requested_amount"], 5)

    def _dice_state(self, rolls, next_reset_at=None):
        from apps.board.models import Cell, TeamBoardState
        cell, _ = Cell.objects.get_or_create(
            cell_index=1, defaults={"type": "START", "name": "출발"}
        )
        state, _ = TeamBoardState.objects.get_or_create(
            team=self.team, defaults={"position": cell}
        )
        TeamBoardState.objects.filter(pk=state.pk).update(
            dice_rolls_left=rolls, next_dice_reset_at=next_reset_at
        )
        state.refresh_from_db()
        return state

    def _adjust(self, amount):
        self.auth("root")
        return self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": amount, "reason": "운영 보정"}, format="json",
        )

    def test_board_dice_deduct_restarts_recharge(self):
        """상한이던 팀에서 회수하면 충전이 다시 시작된다."""
        from apps.board.models import TeamBoardState
        self._dice_state(3, None)
        res = self._adjust(-1)
        self.assertEqual(res.status_code, 200)
        state = TeamBoardState.objects.get(team=self.team)
        self.assertEqual(state.dice_rolls_left, 2)
        self.assertIsNotNone(state.next_dice_reset_at)

    def test_board_dice_grant_to_cap_stops_recharge(self):
        """지급으로 상한에 닿으면 충전이 멈춘다."""
        from apps.board.models import TeamBoardState
        self._dice_state(2, timezone.now() + timedelta(minutes=10))
        res = self._adjust(1)
        self.assertEqual(res.status_code, 200)
        state = TeamBoardState.objects.get(team=self.team)
        self.assertEqual(state.dice_rolls_left, 3)
        self.assertIsNone(state.next_dice_reset_at)

    def test_board_dice_grant_does_not_exceed_board_cap(self):
        """보드 보상과 같이 보유 상한을 넘겨 지급하지 않는다."""
        from apps.board.models import TeamBoardState
        from apps.board.services import MAX_DICE_ROLLS
        self._dice_state(2, timezone.now() + timedelta(minutes=10))
        res = self._adjust(5)
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["dice_rolls_left"], MAX_DICE_ROLLS)
        self.assertEqual(d["amount"], MAX_DICE_ROLLS - 2)
        state = TeamBoardState.objects.get(team=self.team)
        self.assertEqual(state.dice_rolls_left, MAX_DICE_ROLLS)
        self.assertIsNone(state.next_dice_reset_at)

    def test_board_dice_grant_keeps_existing_recharge_deadline(self):
        """충전 대기 중 지급을 받아도 남은 시간이 늘어나지 않는다."""
        from apps.board.models import TeamBoardState
        deadline = (timezone.now() + timedelta(minutes=10)).replace(microsecond=0)
        self._dice_state(0, deadline)
        res = self._adjust(1)
        self.assertEqual(res.status_code, 200)
        state = TeamBoardState.objects.get(team=self.team)
        self.assertEqual(state.dice_rolls_left, 1)
        self.assertEqual(state.next_dice_reset_at.replace(microsecond=0), deadline)

    def test_board_dice_applies_pending_recharge_before_adjusting(self):
        """밀린 자동 충전을 먼저 반영한 뒤 조정한다."""
        from apps.board.models import TeamBoardState
        self._dice_state(0, timezone.now() - timedelta(minutes=1))
        res = self._adjust(1)
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["previous_dice_rolls_left"], 1)
        self.assertEqual(d["dice_rolls_left"], 2)
        self.assertEqual(TeamBoardState.objects.get(team=self.team).dice_rolls_left, 2)

    def test_board_dice_zero_rejected(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": 0, "reason": "x"}, format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_AMOUNT")

    def test_board_dice_out_of_range(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": 21, "reason": "x"}, format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_board_dice_team_not_found(self):
        self.auth("root")
        res = self.client.post(
            f"/api/v1/admin/teams/{uuid.uuid4()}/board/dice",
            {"amount": 1, "reason": "x"}, format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "TEAM_NOT_FOUND")

    def test_account_create_participant_blocked(self):
        self.auth("player")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "d", "password": "pw12345678", "nickname": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_registered_password_passes_login(self):
        self.auth("root")
        pw = "  spaced pw 12345  "
        create = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "spacey", "password": pw, "nickname": "공백"},
            format="json",
        )
        self.assertEqual(create.status_code, 200)

        login = APIClient().post(
            "/api/v1/auth/login",
            {"login_id": "spacey", "password": pw},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.data["code"], "SUCCESS")

    def test_password_max_length_unified(self):
        self.auth("root")
        pw128 = "a" * 128
        self.assertEqual(
            self.client.post(
                "/api/v1/admin/accounts",
                {"login_id": "len128", "password": pw128, "nickname": "긴비번"},
                format="json",
            ).status_code,
            200,
        )
        login = APIClient().post(
            "/api/v1/auth/login",
            {"login_id": "len128", "password": pw128},
            format="json",
        )
        self.assertEqual(login.status_code, 200)

        too_long = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "len129", "password": "a" * 129, "nickname": "너무긴비번"},
            format="json",
        )
        self.assertEqual(too_long.status_code, 400)

    def test_account_create_leader_conflict_not_login_id(self):
        self.auth("root")
        User.objects.create_user(
            login_id="leader1", password="pw12345678", nickname="팀장1",
            team=self.team, is_leader=True,
        )
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "leader2", "password": "pw12345678", "nickname": "팀장2",
             "team_id": str(self.team.team_id), "is_leader": True},
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "TEAM_ALREADY_HAS_LEADER")
        self.assertFalse(User.objects.filter(login_id="leader2").exists())

    def test_admin_account_is_never_leader(self):
        self.auth("root")
        rejected = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "admin_leader", "password": "pw12345678", "nickname": "관리자팀장",
             "team_id": str(self.team.team_id), "role": "ADMIN", "is_leader": True},
            format="json",
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(User.objects.filter(login_id="admin_leader").exists())

        created = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "admin1", "password": "pw12345678", "nickname": "관리자", "role": "ADMIN"},
            format="json",
        )
        self.assertEqual(created.status_code, 200)
        self.assertFalse(created.data["data"]["is_leader"])

        login = APIClient().post(
            "/api/v1/auth/login",
            {"login_id": "admin1", "password": "pw12345678"},
            format="json",
        )
        self.assertEqual(login.status_code, 200)
        self.assertFalse(login.data["data"]["is_leader"])
        payload = decode_token(login.data["data"]["access_token"], ACCESS)
        self.assertFalse(payload["is_leader"])

    def test_challenge_visibility_toggle(self):
        self.auth("root")
        ch = Challenge.objects.create(title="웹1", category="WEB", difficulty="EASY",
                                      score=100, flag_hash="x", is_published=True)
        res = self.client.patch(
            f"/api/v1/admin/challenges/{ch.challenge_id}/visibility",
            {"is_published": False, "reason": "서버 오류로 비공개"}, format="json",
        )
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertTrue(d["previous_is_published"])
        self.assertFalse(d["is_published"])
        self.assertEqual(d["changed_by"], "root")
        self.assertEqual(
            set(d),
            {"challenge_id", "title", "previous_is_published", "is_published",
             "affected_team_count", "changed_at", "changed_by"},
        )
        ch.refresh_from_db()
        self.assertFalse(ch.is_published)

    def test_challenge_visibility_affected_team_count(self):
        from apps.board.services import (
            get_current_cell_candidates,
            open_current_cell_challenge,
        )
        ch, _ = self._board_challenge_setup()
        get_current_cell_candidates(self.team)
        open_current_cell_challenge(self.team, ch.challenge_id)

        res = self._unpublish(ch)
        self.assertEqual(res.data["data"]["affected_team_count"], 1)

    def _board_challenge_setup(self, published=True, cell_index=2, number=1):
        from apps.board.models import BoardChallenge, Cell, TeamBoardState
        Cell.objects.get_or_create(
            cell_index=1, defaults={"type": "START", "name": "출발"}
        )
        ch = Challenge.objects.create(title=f"보드{number}", category="WEB", difficulty="EASY",
                                      score=100, flag_hash="x", is_published=published)
        BoardChallenge.objects.create(challenge=ch, challenge_number=number)
        cell = Cell.objects.create(cell_index=cell_index, type="CHALLENGE",
                                   difficulty="EASY", name=f"{cell_index}번칸")
        TeamBoardState.objects.create(team=self.team, position=cell)
        return ch, cell

    def _unpublish(self, ch):
        self.auth("root")
        return self.client.patch(
            f"/api/v1/admin/challenges/{ch.challenge_id}/visibility",
            {"is_published": False, "reason": "출제 오류"}, format="json",
        )

    def test_opened_team_keeps_access_after_unpublish(self):
        from apps.board.models import TeamChallengeAccess
        from apps.board.services import (
            get_current_cell_candidates,
            open_current_cell_challenge,
        )
        ch, _ = self._board_challenge_setup()
        get_current_cell_candidates(self.team)
        open_current_cell_challenge(self.team, ch.challenge_id)

        res = self._unpublish(ch)
        self.assertEqual(res.data["data"]["affected_team_count"], 1)
        self.assertTrue(
            TeamChallengeAccess.objects.filter(team=self.team, challenge=ch).exists()
        )

    def test_challenge_visibility_invalid_body(self):
        self.auth("root")
        ch = Challenge.objects.create(title="웹3", category="WEB", difficulty="EASY",
                                      score=100, flag_hash="x", is_published=True)
        url = f"/api/v1/admin/challenges/{ch.challenge_id}/visibility"
        for body in [{}, {"is_published": True}, {"reason": "x"},
                     {"is_published": "yes", "reason": "x"}]:
            self.assertEqual(self.client.patch(url, body, format="json").status_code, 400)

    def test_challenge_visibility_not_found(self):
        self.auth("root")
        res = self.client.patch(
            f"/api/v1/admin/challenges/{uuid.uuid4()}/visibility",
            {"is_published": False, "reason": "x"}, format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data["code"], "CHALLENGE_NOT_FOUND")

    def test_challenge_visibility_participant_blocked(self):
        self.auth("player")
        ch = Challenge.objects.create(title="웹4", category="WEB", difficulty="EASY",
                                      score=100, flag_hash="x", is_published=True)
        res = self.client.patch(
            f"/api/v1/admin/challenges/{ch.challenge_id}/visibility",
            {"is_published": False, "reason": "x"}, format="json",
        )
        self.assertEqual(res.status_code, 403)


    def test_board_dice_participant_blocked(self):
        self.auth("player")
        res = self.client.post(
            f"/api/v1/admin/teams/{self.team.team_id}/board/dice",
            {"amount": 1, "reason": "x"}, format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_account_create_with_new_team(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "newteam", "password": "pw12345678", "nickname": "새팀장",
             "team_name": "새로운팀", "is_leader": True},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["team_name"], "새로운팀")
        self.assertIsNotNone(d["team_id"])
        self.assertTrue(d["is_leader"])
        team = Team.objects.get(team_name="새로운팀")
        self.assertEqual(str(team.team_id), d["team_id"])
        self.assertEqual(team.mileage, 0)

    def test_account_create_duplicate_team_name(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "dup", "password": "pw12345678", "nickname": "x",
             "team_name": self.team.team_name},
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "TEAM_NAME_TAKEN")
        self.assertFalse(User.objects.filter(login_id="dup").exists())

    def test_account_create_team_id_and_name_together(self):
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "both", "password": "pw12345678", "nickname": "x",
             "team_id": str(self.team.team_id), "team_name": "또다른팀"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_account_create_team_id_with_blank_team_name(self):
        """team_name 이 비어 있어도 두 필드를 함께 보내면 거절한다."""
        self.auth("root")
        for name in ["", "   ", None]:
            with self.subTest(name=name):
                res = self.client.post(
                    "/api/v1/admin/accounts",
                    {"login_id": "blankpair", "password": "pw12345678", "nickname": "x",
                     "team_id": str(self.team.team_id), "team_name": name},
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
                self.assertFalse(User.objects.filter(login_id="blankpair").exists())

    def test_account_create_blank_team_name(self):
        self.auth("root")
        for name in ["", "   "]:
            with self.subTest(name=name):
                res = self.client.post(
                    "/api/v1/admin/accounts",
                    {"login_id": "blank", "password": "pw12345678", "nickname": "x",
                     "team_name": name},
                    format="json",
                )
                self.assertEqual(res.status_code, 400)

    def test_new_team_not_created_when_login_id_taken(self):
        """계정 생성이 실패하면 팀도 남지 않는다."""
        self.auth("root")
        res = self.client.post(
            "/api/v1/admin/accounts",
            {"login_id": "player", "password": "pw12345678", "nickname": "x",
             "team_name": "롤백될팀"},
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertFalse(Team.objects.filter(team_name="롤백될팀").exists())

@override_settings(CACHES=LOCMEM)
class AdminDashboardTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="팀A", team_score=100, mileage=200)
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

    def test_dashboard_aggregates(self):
        from apps.teams.models import MileageHistory, MileageType
        from apps.challenge.models import Challenge, Solve
        from apps.instances.models import Instance, InstanceStatus
        MileageHistory.objects.create(team=self.team, type=MileageType.PURCHASE,
                                      amount=-100, reason="x", processed_by="root")
        MileageHistory.objects.create(team=self.team, type=MileageType.REFUND,
                                      amount=30, reason="x", processed_by="root")
        ch = Challenge.objects.create(title="c1", category="WEB", difficulty="EASY",
                                      score=500, flag_hash="x", is_published=True)
        Challenge.objects.create(title="c2", category="WEB", difficulty="EASY",
                                 score=500, flag_hash="x", is_published=False)
        Instance.objects.create(user=self.player, team=self.team, challenge=ch,
                                status=InstanceStatus.RUNNING)
        Instance.objects.create(user=self.player, team=self.team, challenge=ch,
                                status=InstanceStatus.FAILED)
        Solve.objects.create(team=self.team, challenge=ch, solved_by_user=self.player,
                             earned_score=500, earned_mileage=100)

        res = self.client.get("/api/v1/admin/dashboard")
        self.assertEqual(res.data["code"], "SUCCESS")
        d = res.data["data"]
        self.assertEqual(d["teams"]["total_count"], 1)
        self.assertEqual(d["teams"]["total_mileage"], 200)
        self.assertEqual(d["payment"],
                         {"purchase_count": 1, "refund_count": 1, "net_spent": 70})
        self.assertEqual(d["instances"], {"running": 1, "failed": 1, "total": 2})
        self.assertEqual(d["challenges"], {"total": 2, "published": 1, "solved_total": 1})
        self.assertIsNone(d["contest"])

    def test_dashboard_with_active_contest(self):
        import datetime
        from django.utils import timezone
        from apps.timer.models import Contest
        now = timezone.now()
        Contest.objects.create(name="대회", is_active=True,
                               start_time=now - datetime.timedelta(hours=1),
                               end_time=now + datetime.timedelta(hours=1))
        res = self.client.get("/api/v1/admin/dashboard")
        self.assertEqual(res.data["data"]["contest"]["status"], "RUNNING")
        self.assertGreater(res.data["data"]["contest"]["remaining_seconds"], 0)

    def test_dashboard_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/dashboard")
        self.assertEqual(res.status_code, 403)

@override_settings(CACHES=LOCMEM)
class AdminChallengeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="팀A", team_score=0)
        self.team2 = Team.objects.create(team_name="팀B", team_score=0)
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

    def challenge_body(self, **overrides):
        body = {
            "challenge_slug": "sql-injection-basic",
            "title": "SQL Injection 기초",
            "category": "WEB",
            "difficulty": "EASY",
            "description": "취약점을 찾아 플래그를 획득하세요.",
            "flag": "MSG{admin_create_test}",
        }
        body.update(overrides)
        return body

    def test_challenge_create_uses_defaults_and_hashes_flag(self):
        from apps.challenge.services import is_correct_flag

        res = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(),
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["code"], "SUCCESS")
        challenge = Challenge.objects.get(challenge_id=res.data["data"]["challenge_id"])
        self.assertEqual(challenge.challenge_slug, "sql-injection-basic")
        self.assertEqual(res.data["data"]["challenge_slug"], "sql-injection-basic")
        self.assertEqual(challenge.initial_score, 1000)
        self.assertEqual(challenge.minimum_score, 600)
        self.assertEqual(challenge.decay, 70)
        self.assertEqual(challenge.score, challenge.initial_score)
        self.assertEqual(challenge.current_score, challenge.initial_score)
        self.assertFalse(challenge.is_published)
        self.assertNotEqual(challenge.flag_hash, "MSG{admin_create_test}")
        self.assertTrue(is_correct_flag("MSG{admin_create_test}", challenge.flag_hash))
        self.assertNotIn("flag", res.data["data"])
        self.assertNotIn("flag_hash", res.data["data"])

    def test_challenge_create_accepts_custom_scoring(self):
        res = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(initial_score=1500, minimum_score=750, decay=80),
            format="json",
        )

        self.assertEqual(res.status_code, 200)
        challenge = Challenge.objects.get(challenge_id=res.data["data"]["challenge_id"])
        self.assertEqual(challenge.initial_score, 1500)
        self.assertEqual(challenge.minimum_score, 750)
        self.assertEqual(challenge.decay, 80)
        self.assertEqual(challenge.score, 1500)
        self.assertEqual(challenge.current_score, 1500)

    def test_challenge_create_rejects_invalid_scoring(self):
        invalid_values = [
            {"initial_score": 599, "minimum_score": 600},
            {"minimum_score": -1},
            {"decay": 0},
            {"initial_score": 1000.50},
            {"minimum_score": 600.50},
        ]

        for values in invalid_values:
            with self.subTest(values=values):
                res = self.client.post(
                    "/api/v1/admin/challenges",
                    self.challenge_body(**values),
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")
        self.assertEqual(Challenge.objects.count(), 0)

    def test_challenge_create_and_first_solve_never_exceeds_initial_score(self):
        created = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(
                initial_score=1000,
                minimum_score=600,
                decay=70,
            ),
            format="json",
        )
        self.assertEqual(created.status_code, 200)

        challenge = Challenge.objects.get(
            challenge_id=created.data["data"]["challenge_id"]
        )
        challenge.is_published = True
        challenge.save(update_fields=["is_published"])
        cell = Cell.objects.create(
            cell_index=1,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="registered-challenge",
        )
        TeamChallengeAccess.objects.create(
            team=self.team,
            challenge=challenge,
            source_cell=cell,
        )

        self.auth("player")
        submitted = self.client.post(
            f"/api/v1/challenges/{challenge.challenge_id}/submit",
            {"flag": "MSG{admin_create_test}"},
            format="json",
        )

        self.assertEqual(submitted.status_code, 200)
        challenge.refresh_from_db()
        self.assertLessEqual(challenge.current_score, challenge.initial_score)
        self.assertEqual(challenge.current_score, challenge.initial_score)

    def test_max_score_challenges_can_be_solved_by_same_team(self):
        maximum_score = 9_999_999_999

        for index in range(2):
            flag = f"MSG{{max_score_{index}}}"
            self.auth("root")
            created = self.client.post(
                "/api/v1/admin/challenges",
                self.challenge_body(
                    challenge_slug=f"max-score-{index}",
                    title=f"Max score challenge {index}",
                    flag=flag,
                    initial_score=maximum_score,
                    minimum_score=maximum_score,
                    decay=70,
                ),
                format="json",
            )
            self.assertEqual(created.status_code, 200)

            challenge = Challenge.objects.get(
                challenge_id=created.data["data"]["challenge_id"]
            )
            challenge.is_published = True
            challenge.save(update_fields=["is_published"])
            cell = Cell.objects.create(
                cell_index=index + 1,
                type=Cell.CellType.CHALLENGE,
                difficulty=Cell.Difficulty.EASY,
                name=f"max-score-challenge-{index}",
            )
            TeamChallengeAccess.objects.create(
                team=self.team,
                challenge=challenge,
                source_cell=cell,
            )

            self.auth("player")
            submitted = self.client.post(
                f"/api/v1/challenges/{challenge.challenge_id}/submit",
                {"flag": flag},
                format="json",
            )
            self.assertEqual(submitted.status_code, 200)

        self.team.refresh_from_db()
        self.assertEqual(self.team.team_score, Decimal("19999999998.00"))

    def test_challenge_create_rejects_invalid_and_duplicate_slug(self):
        invalid_slugs = ["Web-Notebook", "web_notebook", "web notebook", "-web", "web-"]
        for slug in invalid_slugs:
            with self.subTest(slug=slug):
                res = self.client.post(
                    "/api/v1/admin/challenges",
                    self.challenge_body(challenge_slug=slug),
                    format="json",
                )
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

        first = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(challenge_slug="web-notebook"),
            format="json",
        )
        duplicate = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(challenge_slug="web-notebook", title="다른 문제"),
            format="json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(duplicate.data["code"], "INVALID_REQUEST")
        self.assertEqual(Challenge.objects.filter(challenge_slug="web-notebook").count(), 1)

    def test_challenge_create_rejects_missing_and_unknown_fields(self):
        missing = self.client.post(
            "/api/v1/admin/challenges",
            {"title": "필드 부족"},
            format="json",
        )
        unknown = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(is_published=True),
            format="json",
        )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(Challenge.objects.count(), 0)

    def test_challenge_create_participant_blocked(self):
        self.auth("player")
        res = self.client.post(
            "/api/v1/admin/challenges",
            self.challenge_body(),
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "FORBIDDEN")
        self.assertFalse(Challenge.objects.exists())

    def test_challenge_list_counts(self):
        from apps.challenge.models import Challenge, Solve
        from apps.instances.models import Instance, InstanceStatus
        ch = Challenge.objects.create(title="웹1", category="WEB", difficulty="EASY",
                                      score=500, flag_hash="x", is_published=True)
        Instance.objects.create(user=self.player, team=self.team, challenge=ch,
                                status=InstanceStatus.RUNNING)
        Instance.objects.create(user=self.player, team=self.team, challenge=ch,
                                status=InstanceStatus.RUNNING)
        Instance.objects.create(user=self.player, team=self.team, challenge=ch,
                                status=InstanceStatus.FAILED)
        Solve.objects.create(team=self.team, challenge=ch, solved_by_user=self.player,
                             earned_score=500, earned_mileage=100)
        Solve.objects.create(team=self.team2, challenge=ch, solved_by_user=None,
                             earned_score=500, earned_mileage=100)
        res = self.client.get("/api/v1/admin/challenges")
        self.assertEqual(res.data["code"], "SUCCESS")
        self.assertEqual(res.data["data"]["total_count"], 1)
        row = res.data["data"]["challenges"][0]
        self.assertEqual(row["running_instance_count"], 2)
        self.assertEqual(row["failed_instance_count"], 1)
        self.assertEqual(row["solved_team_count"], 2)
        self.assertTrue(row["is_published"])

    def test_challenge_list_filters(self):
        from apps.challenge.models import Challenge
        Challenge.objects.create(title="웹", category="WEB", difficulty="EASY",
                                 score=100, flag_hash="x", is_published=True)
        Challenge.objects.create(title="크립토", category="CRYPTO", difficulty="HARD",
                                 score=100, flag_hash="x", is_published=False)
        res = self.client.get("/api/v1/admin/challenges?category=WEB")
        self.assertEqual(res.data["data"]["total_count"], 1)
        res = self.client.get("/api/v1/admin/challenges?is_published=false")
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(res.data["data"]["challenges"][0]["category"], "CRYPTO")

    def test_challenge_list_invalid_sort(self):
        res = self.client.get("/api/v1/admin/challenges?sort=nope")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_challenge_list_participant_blocked(self):
        self.auth("player")
        res = self.client.get("/api/v1/admin/challenges")
        self.assertEqual(res.status_code, 403)

    def test_challenge_list_new_categories(self):
        from apps.challenge.models import Challenge
        Challenge.objects.create(title="web3챌", category="WEB3", difficulty="EASY",
                                 score=100, flag_hash="x", is_published=True)
        Challenge.objects.create(title="osint챌", category="OSINT", difficulty="EASY",
                                 score=100, flag_hash="x", is_published=True)
        res = self.client.get("/api/v1/admin/challenges?category=WEB3")
        self.assertEqual(res.data["data"]["total_count"], 1)
        self.assertEqual(res.data["data"]["challenges"][0]["category"], "WEB3")
        self.assertEqual(
            self.client.get("/api/v1/admin/challenges?category=OSINT").data["data"]["total_count"], 1)

    def test_challenge_list_invalid_is_published(self):
        res = self.client.get("/api/v1/admin/challenges?is_published=maybe")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_challenge_list_invalid_category(self):
        res = self.client.get("/api/v1/admin/challenges?category=NOPE")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")


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


class MileageIdempotencyRaceTest(TransactionTestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="레이스팀", mileage=0)
        self.admin = User.objects.create_user(
            login_id="root2", password="pw1234", nickname="운영자2",
            team=None, role=Role.ADMIN,
        )

    def test_concurrent_same_key_applies_once(self):
        token = issue_access_token(self.admin)
        url = f"/api/v1/admin/teams/{self.team.team_id}/mileage"
        results = {}
        errors = []
        start = threading.Barrier(2)

        def send(tag):
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            start.wait()
            try:
                res = client.post(
                    url, {"amount": 100, "reason": "보상"},
                    format="json", HTTP_IDEMPOTENCY_KEY="race-1",
                )
                results[tag] = res.status_code
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=send, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 100)
        self.assertEqual(MileageHistory.objects.filter(team=self.team).count(), 1)
        # 한 요청이 반영하고 나머지는 저장된 응답을 재생하므로 둘 다 200.
        self.assertEqual(sorted(results.values()), [200, 200])

@override_settings(CACHES=LOCMEM)
class AdminSettingsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="감자는외로워")
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자",
            team=None, role=Role.ADMIN,
        )
        self.url = "/api/v1/admin/settings"

    def auth(self, login_id):
        res = self.client.post("/api/v1/auth/login",
                               {"login_id": login_id, "password": "pw1234"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}")

    def _contest(self, start_offset_hours=-1, end_offset_hours=5):
        from apps.timer.models import Contest
        now = timezone.now()
        return Contest.objects.create(
            name="본선", is_active=True,
            start_time=now + timedelta(hours=start_offset_hours),
            end_time=now + timedelta(hours=end_offset_hours),
        )

    # ---- 조회 ----
    def test_get_returns_defaults_without_stored_rows(self):
        self.auth("root")
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(set(d), {"contest", "board", "flag", "updated_at", "updated_by"})
        self.assertEqual(d["board"]["dice_rolls_per_reset"], 3)
        self.assertEqual(d["board"]["dice_reset_interval_minutes"], 15)
        self.assertEqual(d["board"]["solve_deadline_minutes"], 15)
        self.assertEqual(d["flag"]["max_attempts"], 3)
        self.assertEqual(d["flag"]["lock_seconds"], 30)
        self.assertIsNone(d["updated_at"])

    def test_get_without_active_contest(self):
        self.auth("root")
        d = self.client.get(self.url).data["data"]
        self.assertEqual(d["contest"]["status"], "BEFORE")
        self.assertIsNone(d["contest"]["started_at"])
        self.assertIsNone(d["contest"]["ends_at"])

    def test_get_reflects_active_contest(self):
        self._contest()
        self.auth("root")
        d = self.client.get(self.url).data["data"]
        self.assertEqual(d["contest"]["status"], "RUNNING")
        self.assertIsNotNone(d["contest"]["started_at"])

    def test_participant_blocked(self):
        self.auth("player")
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(
            self.client.patch(self.url, {"flag": {"max_attempts": 5}}, format="json").status_code,
            403,
        )

    def test_token_missing(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)

    # ---- 변경 ----
    def test_patch_partial_update_keeps_other_keys(self):
        self.auth("root")
        res = self.client.patch(self.url, {"flag": {"lock_seconds": 60}}, format="json")
        self.assertEqual(res.status_code, 200)
        d = res.data["data"]
        self.assertEqual(d["flag"]["lock_seconds"], 60)
        self.assertEqual(d["flag"]["max_attempts"], 3)
        self.assertEqual(d["board"]["dice_rolls_per_reset"], 3)
        self.assertEqual(d["updated_by"], "root")
        self.assertIsNotNone(d["updated_at"])

    def test_patch_persists(self):
        self.auth("root")
        self.client.patch(self.url, {"board": {"dice_rolls_per_reset": 4}}, format="json")
        d = self.client.get(self.url).data["data"]
        self.assertEqual(d["board"]["dice_rolls_per_reset"], 4)

    def test_patch_multiple_groups(self):
        self.auth("root")
        res = self.client.patch(
            self.url,
            {"board": {"solve_deadline_minutes": 20}, "flag": {"max_attempts": 5}},
            format="json",
        )
        d = res.data["data"]
        self.assertEqual(d["board"]["solve_deadline_minutes"], 20)
        self.assertEqual(d["flag"]["max_attempts"], 5)

    def test_patch_rejects_out_of_range(self):
        self.auth("root")
        for body in [
            {"board": {"dice_rolls_per_reset": 0}},
            {"board": {"dice_rolls_per_reset": 21}},
            {"board": {"dice_reset_interval_minutes": 1441}},
            {"board": {"solve_deadline_minutes": 181}},
            {"flag": {"max_attempts": 11}},
            {"flag": {"lock_seconds": 3601}},
        ]:
            with self.subTest(body=body):
                res = self.client.patch(self.url, body, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_patch_rejects_bad_types(self):
        self.auth("root")
        for body in [
            {"flag": {"max_attempts": "3"}},
            {"flag": {"max_attempts": True}},
            {"flag": {"max_attempts": 1.5}},
            {"board": "not-an-object"},
        ]:
            with self.subTest(body=body):
                self.assertEqual(
                    self.client.patch(self.url, body, format="json").status_code, 400
                )

    def test_patch_rejects_unknown_keys(self):
        self.auth("root")
        for body in [
            {"board": {"nope": 1}},
            {"unknown_group": {"a": 1}},
            {},
        ]:
            with self.subTest(body=body):
                self.assertEqual(
                    self.client.patch(self.url, body, format="json").status_code, 400
                )

    def test_patch_nothing_persisted_when_one_value_invalid(self):
        """한 값이라도 범위를 벗어나면 아무것도 저장하지 않는다."""
        self.auth("root")
        res = self.client.patch(
            self.url,
            {"flag": {"lock_seconds": 60, "max_attempts": 99}},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.client.get(self.url).data["data"]["flag"]["lock_seconds"], 30)

    # ---- 대회 시각 ----
    def test_patch_contest_end_time(self):
        contest = self._contest()
        self.auth("root")
        new_end = (timezone.now() + timedelta(hours=9)).replace(microsecond=0)
        res = self.client.patch(
            self.url,
            {"contest": {"ends_at": new_end.isoformat().replace("+00:00", "Z")}},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        contest.refresh_from_db()
        self.assertEqual(contest.end_time.replace(microsecond=0), new_end)

    def test_patch_started_contest_start_time_rejected(self):
        self._contest(start_offset_hours=-1)
        self.auth("root")
        new_start = (timezone.now() + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        res = self.client.patch(
            self.url, {"contest": {"started_at": new_start}}, format="json"
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "CONTEST_ALREADY_STARTED")

    def test_patch_not_started_contest_start_time_allowed(self):
        contest = self._contest(start_offset_hours=2, end_offset_hours=8)
        self.auth("root")
        new_start = (timezone.now() + timedelta(hours=3)).replace(microsecond=0)
        res = self.client.patch(
            self.url,
            {"contest": {"started_at": new_start.isoformat().replace("+00:00", "Z")}},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        contest.refresh_from_db()
        self.assertEqual(contest.start_time.replace(microsecond=0), new_start)

    def test_patch_end_before_start_rejected(self):
        self._contest()
        self.auth("root")
        past = (timezone.now() - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        res = self.client.patch(self.url, {"contest": {"ends_at": past}}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_patch_contest_bad_datetime(self):
        self._contest()
        self.auth("root")
        for value in ["2026-08-24 18:00", "not-a-date", "2026-08-24T18:00:00", 123]:
            with self.subTest(value=value):
                self.assertEqual(
                    self.client.patch(
                        self.url, {"contest": {"ends_at": value}}, format="json"
                    ).status_code,
                    400,
                )

    def test_patch_contest_without_active_contest(self):
        self.auth("root")
        res = self.client.patch(
            self.url, {"contest": {"ends_at": "2026-08-24T18:00:00Z"}}, format="json"
        )
        self.assertEqual(res.status_code, 400)

    def test_patch_contest_unknown_key(self):
        self._contest()
        self.auth("root")
        self.assertEqual(
            self.client.patch(
                self.url, {"contest": {"status": "ENDED"}}, format="json"
            ).status_code,
            400,
        )

    def test_contest_only_change_records_updater(self):
        """대회 시각만 바꿔도 수정자와 수정 시각이 남는다."""
        self._contest()
        self.auth("root")
        new_end = (timezone.now() + timedelta(hours=9)).isoformat().replace("+00:00", "Z")
        res = self.client.patch(self.url, {"contest": {"ends_at": new_end}}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["updated_by"], "root")
        self.assertIsNotNone(res.data["data"]["updated_at"])

        d = self.client.get(self.url).data["data"]
        self.assertEqual(d["updated_by"], "root")
        self.assertEqual(d["updated_at"], res.data["data"]["updated_at"])

    def test_contest_change_overwrites_previous_updater(self):
        """이전 관리자 정보가 남지 않는다."""
        User.objects.create_user(
            login_id="root2", password="pw1234", nickname="운영자2",
            team=None, role=Role.ADMIN,
        )
        self._contest()
        self.auth("root")
        self.client.patch(self.url, {"flag": {"lock_seconds": 60}}, format="json")

        self.auth("root2")
        new_end = (timezone.now() + timedelta(hours=9)).isoformat().replace("+00:00", "Z")
        self.client.patch(self.url, {"contest": {"ends_at": new_end}}, format="json")
        self.assertEqual(self.client.get(self.url).data["data"]["updated_by"], "root2")

    def test_explicit_null_rejected(self):
        """항목을 null 로 명시하면 타입 오류다."""
        self._contest()
        self.auth("root")
        for body in [{"board": None}, {"flag": None}, {"contest": None}]:
            with self.subTest(body=body):
                res = self.client.patch(self.url, body, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertEqual(res.data["code"], "INVALID_REQUEST")

    def test_omitted_group_still_kept(self):
        """보내지 않은 항목은 그대로 유지된다."""
        self.auth("root")
        self.client.patch(self.url, {"flag": {"lock_seconds": 60}}, format="json")
        d = self.client.patch(
            self.url, {"board": {"dice_rolls_per_reset": 4}}, format="json"
        ).data["data"]
        self.assertEqual(d["flag"]["lock_seconds"], 60)
        self.assertEqual(d["board"]["dice_rolls_per_reset"], 4)

    def test_meta_key_not_exposed(self):
        """수정 정보용 내부 키가 응답에 새어나오지 않는다."""
        self.auth("root")
        self.client.patch(self.url, {"flag": {"lock_seconds": 60}}, format="json")
        d = self.client.get(self.url).data["data"]
        self.assertEqual(set(d["board"]), {
            "dice_rolls_per_reset", "dice_reset_interval_minutes", "solve_deadline_minutes",
        })
        self.assertEqual(set(d["flag"]), {"max_attempts", "lock_seconds"})


@override_settings(CACHES=LOCMEM)
class AdminEventTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="감자는외로워")
        self.other = Team.objects.create(team_name="세그폴트")
        self.player = User.objects.create_user(
            login_id="player", password="pw1234", nickname="참가자", team=self.team
        )
        self.admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자",
            team=None, role=Role.ADMIN,
        )
        self.url = "/api/v1/admin/events"

    def auth(self, login_id):
        res = self.client.post("/api/v1/auth/login",
                               {"login_id": login_id, "password": "pw1234"}, format="json")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}")

    def _event(self, **kwargs):
        from apps.adminpanel.models import AdminEvent
        defaults = {
            "type": AdminEvent.EventType.TEAM_BANNED,
            "severity": AdminEvent.Severity.WARNING,
            "message": "팀 활동이 정지되었습니다.",
            "actor": "root",
        }
        defaults.update(kwargs)
        return AdminEvent.objects.create(**defaults)

    def test_empty_list(self):
        self.auth("root")
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.data["data"], {"events": [], "total_count": 0, "page": 1, "size": 50}
        )

    def test_list_fields(self):
        from apps.adminpanel.models import AdminEvent
        ch = Challenge.objects.create(title="웹1", category="WEB", difficulty="EASY",
                                      score=100, flag_hash="x")
        instance_id = uuid.uuid4()
        self._event(
            type=AdminEvent.EventType.INSTANCE_FAILED,
            severity=AdminEvent.Severity.CRITICAL,
            message="인스턴스 생성에 실패했습니다.",
            team=self.team, challenge=ch, instance_id=instance_id, actor="system",
        )
        self.auth("root")
        row = self.client.get(self.url).data["data"]["events"][0]
        self.assertEqual(
            set(row),
            {"event_id", "type", "severity", "message", "team_id", "team_name",
             "challenge_id", "challenge_title", "instance_id", "actor", "created_at"},
        )
        self.assertEqual(row["type"], "INSTANCE_FAILED")
        self.assertEqual(row["severity"], "CRITICAL")
        self.assertEqual(row["team_name"], "감자는외로워")
        self.assertEqual(row["challenge_title"], "웹1")
        self.assertEqual(row["instance_id"], str(instance_id))

    def test_null_relations(self):
        self._event(team=None)
        self.auth("root")
        row = self.client.get(self.url).data["data"]["events"][0]
        for key in ("team_id", "team_name", "challenge_id", "challenge_title", "instance_id"):
            self.assertIsNone(row[key])

    def test_newest_first(self):
        first = self._event(message="먼저")
        second = self._event(message="나중")
        self.auth("root")
        messages = [e["message"] for e in self.client.get(self.url).data["data"]["events"]]
        self.assertEqual(messages, ["나중", "먼저"])

    def test_filter_by_type(self):
        from apps.adminpanel.models import AdminEvent
        self._event(type=AdminEvent.EventType.TEAM_BANNED)
        self._event(type=AdminEvent.EventType.SETTINGS_CHANGED)
        self.auth("root")
        d = self.client.get(f"{self.url}?type=SETTINGS_CHANGED").data["data"]
        self.assertEqual(d["total_count"], 1)
        self.assertEqual(d["events"][0]["type"], "SETTINGS_CHANGED")

    def test_filter_by_team(self):
        self._event(team=self.team)
        self._event(team=self.other)
        self.auth("root")
        d = self.client.get(f"{self.url}?team_id={self.team.team_id}").data["data"]
        self.assertEqual(d["total_count"], 1)
        self.assertEqual(d["events"][0]["team_name"], "감자는외로워")

    def test_invalid_type(self):
        self.auth("root")
        res = self.client.get(f"{self.url}?type=NOPE")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INVALID_REQUEST")
        self.assertEqual(res.data["message"], "이벤트 타입이 올바르지 않습니다.")

    def test_invalid_team_id(self):
        self.auth("root")
        self.assertEqual(self.client.get(f"{self.url}?team_id=not-a-uuid").status_code, 400)

    def test_pagination(self):
        for i in range(7):
            self._event(message=f"e{i}")
        self.auth("root")
        d = self.client.get(f"{self.url}?page=2&size=3").data["data"]
        self.assertEqual(d["total_count"], 7)
        self.assertEqual(d["page"], 2)
        self.assertEqual(d["size"], 3)
        self.assertEqual(len(d["events"]), 3)

    def test_invalid_pagination(self):
        self.auth("root")
        for query in ["page=0", "size=0", "page=abc"]:
            with self.subTest(query=query):
                self.assertEqual(self.client.get(f"{self.url}?{query}").status_code, 400)

    def test_participant_blocked(self):
        self.auth("player")
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_token_missing(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)


class SettingsLockOrderTest(TransactionTestCase):
    """설정 저장이 항상 같은 키 순서로 잠기는지 확인한다."""

    URL = "/api/v1/admin/settings"

    def setUp(self):
        self.admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자",
            team=None, role=Role.ADMIN,
        )

    def _client(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(self.admin)}")
        return client

    def _seed(self):
        """교착은 기존 행을 UPDATE 할 때 생긴다. 미리 만들어 INSERT 경로를 피한다."""
        self._client().patch(
            self.URL,
            {"board": {"dice_rolls_per_reset": 3, "solve_deadline_minutes": 15}},
            format="json",
        )

    def test_writes_in_sorted_key_order(self):
        """요청에 들어온 순서와 무관하게 키를 정렬한 순서로 저장한다."""
        from apps.adminpanel.models import AdminSetting

        written = []
        real = AdminSetting.objects.update_or_create

        def record(**kwargs):
            written.append(kwargs["key"])
            return real(**kwargs)

        with patch.object(AdminSetting.objects, "update_or_create", record):
            res = self._client().patch(
                self.URL,
                {"board": {"solve_deadline_minutes": 20, "dice_rolls_per_reset": 4}},
                format="json",
            )

        self.assertEqual(res.status_code, 200)
        setting_keys = [k for k in written if not k.startswith("_meta.")]
        self.assertEqual(setting_keys, sorted(setting_keys))

    def test_reversed_key_order_does_not_deadlock(self):
        """항목 순서가 다른 두 요청이 겹쳐도 500 없이 처리된다."""
        from apps.adminpanel.models import AdminSetting

        self._seed()

        bodies = [
            {"board": {"dice_rolls_per_reset": 4, "solve_deadline_minutes": 20}},
            {"board": {"solve_deadline_minutes": 30, "dice_rolls_per_reset": 2}},
        ]
        results = {}
        errors = []
        # 양쪽이 첫 키를 잠근 뒤에야 다음 키로 넘어가게 해 겹치는 시점을 맞춘다.
        after_first_write = threading.Barrier(len(bodies))
        seen = set()
        guard = threading.Lock()
        real = AdminSetting.objects.update_or_create

        def hooked(**kwargs):
            result = real(**kwargs)
            ident = threading.get_ident()
            with guard:
                is_first = ident not in seen
                seen.add(ident)
            if is_first:
                try:
                    after_first_write.wait(timeout=2)
                except threading.BrokenBarrierError:
                    pass
            return result

        def send(index):
            try:
                res = self._client().patch(self.URL, bodies[index], format="json")
                results[index] = res.status_code
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(AdminSetting.objects, "update_or_create", hooked):
            threads = [threading.Thread(target=send, args=(i,)) for i in range(len(bodies))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), len(bodies))
        self.assertEqual(sorted(results.values()), [200, 200])
