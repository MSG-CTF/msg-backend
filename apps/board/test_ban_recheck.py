import uuid

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.board.models import TeamBoardState
from apps.board.services import (
    get_or_create_board_state,
    move_team_via_airport,
    open_current_cell_challenge,
    roll_dice,
    spin_roulette,
)
from apps.common.exceptions import TeamBanned
from apps.common.jwt import issue_access_token

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class BoardBanRecheckTests(TestCase):
    """인증 뒤 정지가 커밋돼도 보드 쓰기는 상태 잠금 뒤 다시 확인해 거절한다 (QA TC-60)."""

    def setUp(self):
        cache.clear()
        call_command("seed_board", verbosity=0)
        self.team = Team.objects.create(team_name="정지될팀")
        get_or_create_board_state(self.team)
        # 인증 단계는 이미 통과했고 그 뒤 정지가 커밋된 상태
        Team.objects.filter(pk=self.team.pk).update(is_banned=True)

    def test_participant_board_writes_are_rejected(self):
        state_before = TeamBoardState.objects.values().get(team=self.team)
        writes = {
            "roll_dice": lambda: roll_dice(self.team),
            "spin_roulette": lambda: spin_roulette(self.team),
            "open_challenge": lambda: open_current_cell_challenge(self.team, uuid.uuid4()),
            "airport": lambda: move_team_via_airport(self.team, 2),
        }
        for name, write in writes.items():
            with self.subTest(name):
                with self.assertRaises(TeamBanned):
                    write()
        self.assertEqual(TeamBoardState.objects.values().get(team=self.team), state_before)

    def test_admin_dice_adjust_still_works_for_banned_team(self):
        admin = User.objects.create_user(
            login_id="root", password="pw1234", nickname="운영자", role=Role.ADMIN
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {issue_access_token(admin)}")

        res = client.post(
            f"/api/v1/admin/teams/{self.team.pk}/board/dice",
            {"amount": 1, "reason": "정지 중 보정"},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
