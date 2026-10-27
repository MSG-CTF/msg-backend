from datetime import timedelta

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Team
from apps.board.models import Cell, TeamBoardState, TeamCellConsumption
from apps.board.services import (
    LAST_CELL_INDEX,
    START_CELL_INDEX,
    apply_pending_dice_recharge,
    get_or_create_board_state,
)


class ResumeRechargeTests(TestCase):
    """완료 보드를 관리자가 재개하면 멈출 때 남은 충전 시간을 이어준다 (QA TC-219)."""

    def setUp(self):
        call_command("seed_board", verbosity=0)
        self.team = Team.objects.create(team_name="완료팀")
        get_or_create_board_state(self.team)
        TeamBoardState.objects.filter(team=self.team).update(
            dice_rolls_left=1, next_dice_reset_at=timezone.now() + timedelta(minutes=9)
        )
        for index in range(START_CELL_INDEX + 1, LAST_CELL_INDEX + 1):
            TeamCellConsumption.objects.create(
                team=self.team, cell=Cell.objects.get(cell_index=index)
            )

    def state(self):
        return TeamBoardState.objects.get(team=self.team)

    def test_resumed_board_continues_remaining_recharge(self):
        apply_pending_dice_recharge(self.state())
        paused = self.state()
        self.assertIsNone(paused.next_dice_reset_at)
        self.assertAlmostEqual(paused.recharge_paused_seconds, 9 * 60, delta=5)

        # 관리자가 칸 하나를 되돌려 완료를 해제한 뒤의 첫 갱신
        TeamCellConsumption.objects.filter(
            team=self.team, cell_id=LAST_CELL_INDEX
        ).delete()
        apply_pending_dice_recharge(self.state())

        resumed = self.state()
        self.assertIsNone(resumed.recharge_paused_seconds)
        remaining = resumed.next_dice_reset_at - timezone.now()
        self.assertAlmostEqual(remaining.total_seconds(), 9 * 60, delta=5)

    def test_full_dice_discards_paused_time(self):
        apply_pending_dice_recharge(self.state())
        TeamBoardState.objects.filter(team=self.team).update(dice_rolls_left=3)

        apply_pending_dice_recharge(self.state())

        state = self.state()
        self.assertIsNone(state.recharge_paused_seconds)
        self.assertIsNone(state.next_dice_reset_at)
