import datetime

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge, FlagSubmissionLock, OpenedChallenge, Solve
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
            score=500,
            description="테스트 문제",
            flag_hash=hash_flag("MSG{correct_flag}"),
            is_published=True,
        )
        OpenedChallenge.objects.create(
            team=self.team,
            challenge=self.challenge,
            cell_index=1,
            solve_deadline_at=timezone.now() + datetime.timedelta(minutes=15),
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
            OpenedChallenge.objects.create(
                team=self.team,
                challenge=challenge,
                cell_index=len(extra_challenges) + 2,
                solve_deadline_at=timezone.now() + datetime.timedelta(minutes=15),
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
