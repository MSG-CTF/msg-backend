import datetime
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.board.models import Cell, TeamChallengeAccess
from apps.challenge.models import Challenge, FlagSubmissionLock, Solve
from apps.challenge.services import hash_flag
from apps.teams.models import MileageHistory, MileageType

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class ChallengeSubmitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="테스트팀")
        self.user = User.objects.create_user(
            login_id="challenger",
            password="pw1234",
            nickname="챌린저",
            team=self.team,
        )
        self.challenge = Challenge.objects.create(
            title="SQL Injection 기초",
            category=Challenge.CategoryType.WEB,
            difficulty=Challenge.DifficultyType.EASY,
            score=1000,
            initial_score=1000,
            minimum_score=100,
            decay=20,
            current_score=1000,
            description="테스트 문제",
            flag_hash=hash_flag("MSG{correct_flag}"),
            is_published=True,
        )
        self.cell = Cell.objects.create(
            cell_index=1,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="challenge-cell-1",
        )
        TeamChallengeAccess.objects.create(
            team=self.team,
            challenge=self.challenge,
            source_cell=self.cell,
        )
        self.auth()

    def auth(self):
        res = self.client.post(
            "/api/v1/auth/login",
            {"login_id": "challenger", "password": "pw1234"},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}")

    def submit(self, flag="MSG{wrong_flag}"):
        return self.client.post(
            f"/api/v1/challenges/{self.challenge.challenge_id}/submit",
            {"flag": flag},
            format="json",
        )

    def test_detail_and_submit_require_team_challenge_access(self):
        TeamChallengeAccess.objects.filter(
            team=self.team,
            challenge=self.challenge,
        ).delete()

        detail = self.client.get(f"/api/v1/challenges/{self.challenge.challenge_id}")
        submit = self.submit("MSG{correct_flag}")

        self.assertEqual(detail.status_code, 403)
        self.assertEqual(detail.data["code"], "CHALLENGE_LOCKED")
        self.assertEqual(submit.status_code, 403)
        self.assertEqual(submit.data["code"], "CHALLENGE_LOCKED")

    def test_submission_deadline_is_derived_from_access_opened_at(self):
        TeamChallengeAccess.objects.filter(
            team=self.team,
            challenge=self.challenge,
        ).update(opened_at=timezone.now() - datetime.timedelta(minutes=16))

        response = self.submit("MSG{correct_flag}")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["data"]["is_extra_dice_granted"])

    def test_three_wrong_flags_lock_submission(self):
        # 같은 팀이 같은 문제에 3회 연속 오답을 내면 제출 제한이 걸린다
        self.assertEqual(self.submit().data["code"], "INCORRECT_FLAG")
        self.assertEqual(self.submit().data["code"], "INCORRECT_FLAG")

        res = self.submit()

        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.data["code"], "TOO_MANY_ATTEMPTS")
        self.assertIn("retry_after_seconds", res.data["data"])

        flag_lock = FlagSubmissionLock.objects.get(team=self.team, challenge=self.challenge)
        self.assertEqual(flag_lock.failed_count, 3)
        self.assertIsNotNone(flag_lock.locked_until)

    def test_locked_submission_is_rejected_without_scoring(self):
        # 락 상태에서는 정답을 제출해도 채점하지 않고 429를 반환한다
        FlagSubmissionLock.objects.create(
            team=self.team,
            challenge=self.challenge,
            failed_count=3,
            locked_until=timezone.now() + datetime.timedelta(seconds=30),
        )

        res = self.submit("MSG{correct_flag}")

        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.data["code"], "TOO_MANY_ATTEMPTS")

    def test_repeated_correct_flag_does_not_create_duplicate_solve(self):
        # 같은 팀이 정답을 다시 제출해도 풀이 기록은 한 번만 생성된다
        first = self.submit("MSG{correct_flag}")
        second = self.submit("MSG{correct_flag}")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["code"], "SUCCESS")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.data["code"], "ALREADY_SOLVED")
        self.assertEqual(
            Solve.objects.filter(team=self.team, challenge=self.challenge).count(),
            1,
        )

    def test_submit_updates_dynamic_score_and_matches_ranking_contract(self):
        response = self.submit("MSG{correct_flag}")

        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(
            set(data),
            {
                "challenge_id",
                "earned_score",
                "earned_mileage",
                "is_extra_dice_granted",
                "team_score",
                "mileage",
                "solved_at",
            },
        )
        self.assertEqual(data["earned_score"], 1000)
        self.assertEqual(data["earned_mileage"], 30)
        self.assertEqual(data["team_score"], 998)
        self.assertEqual(data["mileage"], 30)

        self.challenge.refresh_from_db()
        self.team.refresh_from_db()
        solve = Solve.objects.get(team=self.team, challenge=self.challenge)
        self.assertEqual(solve.earned_score, Decimal("1000"))
        self.assertEqual(self.challenge.current_score, Decimal("998"))
        self.assertEqual(self.team.team_score, Decimal("998"))

        ranking = self.client.get("/api/v1/ranking").data["data"]["rankings"][0]
        leaderboard = self.client.get("/api/v1/leaderboard").data["data"]["teams"][0]
        self.assertEqual(ranking["team_score"], 998)
        self.assertEqual(leaderboard["team_score"], 998)
        self.assertEqual(leaderboard["solves"][0]["points"], 998)
        detail = self.client.get(f"/api/v1/challenges/{self.challenge.challenge_id}")
        self.assertEqual(detail.data["data"]["score"], 998)

    def test_later_solve_updates_scores_of_all_teams_that_solved_challenge(self):
        self.submit("MSG{correct_flag}")

        other_team = Team.objects.create(team_name="second-team")
        User.objects.create_user(
            login_id="second-user",
            password="pw1234",
            nickname="second-user",
            team=other_team,
        )
        second_cell = Cell.objects.create(
            cell_index=2,
            type=Cell.CellType.CHALLENGE,
            difficulty=Cell.Difficulty.EASY,
            name="challenge-cell-2",
        )
        TeamChallengeAccess.objects.create(
            team=other_team,
            challenge=self.challenge,
            source_cell=second_cell,
        )
        second_client = APIClient()
        login = second_client.post(
            "/api/v1/auth/login",
            {"login_id": "second-user", "password": "pw1234"},
            format="json",
        )
        second_client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {login.data['data']['access_token']}"
        )

        response = second_client.post(
            f"/api/v1/challenges/{self.challenge.challenge_id}/submit",
            {"flag": "MSG{correct_flag}"},
            format="json",
        )

        self.challenge.refresh_from_db()
        self.team.refresh_from_db()
        other_team.refresh_from_db()
        self.assertEqual(response.data["data"]["earned_score"], 998)
        self.assertEqual(self.challenge.current_score, Decimal("991"))
        self.assertEqual(self.team.team_score, Decimal("991"))
        self.assertEqual(other_team.team_score, Decimal("991"))

    def test_submission_failure_rolls_back_solve_score_and_mileage(self):
        with patch(
            "apps.challenge.views.update_dynamic_score_and_team_scores",
            side_effect=RuntimeError("simulated scoring failure"),
        ):
            with self.assertLogs("apps.common.exceptions", level="ERROR"):
                response = self.submit("MSG{correct_flag}")

        self.assertEqual(response.status_code, 500)
        self.challenge.refresh_from_db()
        self.team.refresh_from_db()
        self.assertFalse(Solve.objects.filter(team=self.team, challenge=self.challenge).exists())
        self.assertFalse(MileageHistory.objects.filter(team=self.team).exists())
        self.assertEqual(self.challenge.current_score, Decimal("1000"))
        self.assertEqual(self.team.team_score, Decimal("0"))
        self.assertEqual(self.team.mileage, 0)

    def test_successful_solve_awards_mileage_by_difficulty(self):
        extra_challenges = []
        for difficulty, reward in (
            (Challenge.DifficultyType.MEDIUM, 60),
            (Challenge.DifficultyType.HARD, 120),
        ):
            challenge = Challenge.objects.create(
                title=f"{difficulty} 문제",
                category=Challenge.CategoryType.WEB,
                difficulty=difficulty,
                score=500,
                description="난이도별 보상 테스트 문제",
                flag_hash=hash_flag(f"MSG{{{difficulty.lower()}_flag}}"),
                is_published=True,
            )
            cell = Cell.objects.create(
                cell_index=len(extra_challenges) + 2,
                type=Cell.CellType.CHALLENGE,
                difficulty=difficulty,
                name=f"challenge-cell-{len(extra_challenges) + 2}",
            )
            TeamChallengeAccess.objects.create(
                team=self.team,
                challenge=challenge,
                source_cell=cell,
            )
            extra_challenges.append((challenge, reward, f"MSG{{{difficulty.lower()}_flag}}"))

        cases = [(self.challenge, 30, "MSG{correct_flag}"), *extra_challenges]
        for challenge, reward, flag in cases:
            with self.subTest(difficulty=challenge.difficulty):
                response = self.client.post(
                    f"/api/v1/challenges/{challenge.challenge_id}/submit",
                    {"flag": flag},
                    format="json",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data["data"]["earned_mileage"], reward)

        self.team.refresh_from_db()
        self.assertEqual(self.team.mileage, 210)
        self.assertEqual(
            list(
                MileageHistory.objects.filter(team=self.team, type=MileageType.CHALLENGE_SOLVE)
                .order_by("created_at")
                .values_list("amount", flat=True)
            ),
            [30, 60, 120],
        )
