from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class MergeOpenedChallengesMigrationTests(TransactionTestCase):
    migrate_from = [
        ("board", "0002_idempotencyrequest"),
        ("challenge", "0003_alter_challenge_category"),
    ]
    migrate_to = [("challenge", "0004_merge_opened_challenges_into_team_access")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        self.addCleanup(self.restore_migrations, executor.loader.graph.leaf_nodes())
        executor.migrate(self.migrate_from)
        self.old_apps = executor.loader.project_state(self.migrate_from).apps

    def restore_migrations(self, targets):
        MigrationExecutor(connection).migrate(targets)

    def test_merges_solved_and_unsolved_rows_with_and_without_board_access(self):
        Team = self.old_apps.get_model("accounts", "Team")
        Challenge = self.old_apps.get_model("challenge", "Challenge")
        OpenedChallenge = self.old_apps.get_model("challenge", "OpenedChallenge")
        Solve = self.old_apps.get_model("challenge", "Solve")
        Cell = self.old_apps.get_model("board", "Cell")
        Access = self.old_apps.get_model("board", "TeamChallengeAccess")
        team = Team.objects.create(team_name="migration-team")
        opened_at = timezone.now() - timedelta(hours=1)
        board_opened_at = opened_at - timedelta(minutes=5)
        solved_at = opened_at + timedelta(minutes=2)
        cases = []

        for has_access, solved in ((False, False), (False, True), (True, False), (True, True)):
            cell = Cell.objects.create(
                cell_index=len(cases) + 1, type="CHALLENGE", name="migration-cell",
            )
            challenge = Challenge.objects.create(
                title=f"migration-{has_access}-{solved}", category="WEB",
                difficulty="EASY", score=1000, flag_hash="a" * 64,
            )
            opened = OpenedChallenge.objects.create(
                team=team, challenge=challenge, cell_index=cell.pk,
                solve_deadline_at=opened_at + timedelta(minutes=15),
            )
            OpenedChallenge.objects.filter(pk=opened.pk).update(opened_at=opened_at)
            access = None
            if has_access:
                access = Access.objects.create(team=team, challenge=challenge, source_cell=cell)
                Access.objects.filter(pk=access.pk).update(opened_at=board_opened_at)
            if solved:
                solve = Solve.objects.create(
                    team=team, challenge=challenge, earned_score=1000, earned_mileage=30,
                )
                Solve.objects.filter(pk=solve.pk).update(solved_at=solved_at)
            cases.append((challenge.pk, cell.pk, access.pk if access else None, solved))

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        apps = executor.loader.project_state(self.migrate_to).apps
        Access = apps.get_model("board", "TeamChallengeAccess")
        self.assertEqual(Access.objects.count(), len(cases))
        for challenge_id, cell_id, access_id, solved in cases:
            with self.subTest(has_access=access_id is not None, solved=solved):
                access = Access.objects.get(team_id=team.pk, challenge_id=challenge_id)
                self.assertEqual(access.source_cell_id, cell_id)
                self.assertEqual(access.status, "CLEARED" if solved else "OPENED")
                self.assertEqual(access.cleared_at, solved_at if solved else None)
                self.assertEqual(access.opened_at, board_opened_at if access_id else opened_at)
                if access_id:
                    self.assertEqual(access.pk, access_id)
