from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from apps.teams.models import MileageHistory


class BoardSpecMigrationTestCase(TransactionTestCase):
    migrate_from = [("board", "0003_teamboardstate_initial_dice")]
    migrate_to = [("board", "0004_align_board_api_spec")]

    def setUp(self):
        executor = MigrationExecutor(connection)
        self.addCleanup(self.restore_latest_schema)
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def restore_latest_schema(self):
        MigrationExecutor(connection).migrate(self.migrate_to)

    def test_existing_game_is_preserved_and_retired_state_is_removed(self):
        Team = self.old_apps.get_model("accounts", "Team")
        Cell = self.old_apps.get_model("board", "Cell")
        State = self.old_apps.get_model("board", "TeamBoardState")
        Draw = self.old_apps.get_model("board", "TeamChanceCard")
        Card = self.old_apps.get_model("board", "ChanceCard")
        Pending = self.old_apps.get_model("board", "PendingDiceRoll")
        Consumption = self.old_apps.get_model("board", "TeamCellConsumption")
        for index, cell_type, name in (
            (1, "START", "출발"), (7, "CHANCE", "황금열쇠"),
            (16, "QUARANTINE", "무인도"), (21, "AIRPORT", "공항"), (30, "CHANCE", "황금열쇠"),
        ):
            Cell.objects.create(cell_index=index, type=cell_type, name=name)
        team = Team.objects.create(team_name="migration-team", mileage=450)
        next_reset = timezone.now() + timedelta(minutes=10)
        State.objects.create(
            team=team, position_id=16, dice_rolls_left=2, next_dice_reset_at=next_reset,
            is_quarantined=True, quarantine_attempts_left=1,
            quarantine_released_at=timezone.now() + timedelta(minutes=15),
            has_passed_start=True, airport_move_used=True,
        )
        consumed = Consumption.objects.create(team=team, cell_id=16)
        pending = Pending.objects.create(
            team=team, dice_a=1, dice_b=1, rolled_number=2, previous_position=14,
            candidate_position=16, movement_path=[15, 16], board_event_code="QUARANTINE",
        )
        legacy = Card.objects.create(
            card_id="card_quarantine_defense", name="무인도 방어",
            effect="QUARANTINE_ESCAPE_FREE", usage_timing="QUARANTINE_STATE",
        )
        retired_draw = Draw.objects.create(team=team, source_cell_id=7, card=legacy)
        kept = Card.objects.create(
            card_id="card_extra_roll", name="주사위 보너스",
            effect="GRANT_EXTRA_ROLL", usage_timing="PRE_ROLL",
        )
        kept_draw = Draw.objects.create(team=team, source_cell_id=30, card=kept)
        history = MileageHistory.objects.create(team_id=team.pk, type="ROULETTE", amount=150, reason="ROULETTE_CELL:25")

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        updated = apps.get_model("board", "TeamBoardState").objects.get(pk=team.pk)
        self.assertEqual(updated.position_id, 16)
        self.assertEqual(updated.dice_rolls_left, 2)
        self.assertEqual(updated.next_dice_reset_at, next_reset)
        self.assertTrue(updated.has_passed_start)
        self.assertTrue(updated.airport_move_used)
        self.assertFalse(any("quarantine" in field.name for field in updated._meta.fields))
        cell = apps.get_model("board", "Cell").objects.get(pk=16)
        self.assertEqual((cell.type, cell.name, cell.difficulty), ("ROULETTE", "룰렛", None))
        updated_pending = apps.get_model("board", "PendingDiceRoll").objects.get(pk=pending.pk)
        self.assertEqual(updated_pending.board_event_code, "ROULETTE")
        self.assertEqual(updated_pending.movement_path, [15, 16])
        self.assertTrue(apps.get_model("board", "TeamCellConsumption").objects.filter(pk=consumed.pk).exists())
        draws = apps.get_model("board", "TeamChanceCard").objects
        self.assertIsNotNone(draws.get(pk=retired_draw.pk).discarded_at)
        self.assertIsNone(draws.get(pk=kept_draw.pk).discarded_at)
        self.assertEqual(apps.get_model("accounts", "Team").objects.get(pk=team.pk).mileage, 450)
        history.refresh_from_db()
        self.assertEqual((history.amount, history.reason), (150, "ROULETTE_CELL:25"))
        self.assertNotIn("quarantine_escape_codes", connection.introspection.table_names())
