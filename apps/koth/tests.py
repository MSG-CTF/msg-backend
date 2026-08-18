import uuid

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.koth.models import (
    KothChallenge,
    KothChallengeStatus,
    KothClub,
    KothScoringPeriod,
    KothSolve,
    KothTeamToken,
)
from apps.koth.services import apply_period_results, compute_period_awards

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=LOCMEM)
class KothApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.team = Team.objects.create(team_name="우리팀")
        self.other_team = Team.objects.create(team_name="다른팀")
        self.banned_team = Team.objects.create(team_name="밴팀", is_banned=True)
        User.objects.create_user(login_id="leader", password="pw1234", nickname="팀장", team=self.team, is_leader=True)
        User.objects.create_user(login_id="noteam", password="pw1234", nickname="무소속", team=None)

        self.club = KothClub.objects.create(name="동아리A")
        self.challenge = KothChallenge.objects.create(
            club=self.club, title="koth_web_1", category="WEB", open_group=1,
            status=KothChallengeStatus.ACTIVE,
        )
        self.second_challenge = KothChallenge.objects.create(
            club=self.club, title="koth_web_2", category="WEB", open_group=2,
            status=KothChallengeStatus.SCHEDULED,
        )

    def auth(self, login_id):
        res = self.client.post(
            "/api/v1/auth/login", {"login_id": login_id, "password": "pw1234"}, format="json"
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.data['data']['access_token']}")

    def internal_headers(self, token=None):
        return {"HTTP_X_INTERNAL_TOKEN": token if token is not None else settings.KOTH_INTERNAL_TOKEN}

    # ---------------------------------------------------------------- clubs

    def test_clubs_list_nests_challenges_under_club(self):
        self.client.credentials()
        response = self.client.get("/api/v1/koth/clubs")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total_count"], 2)  # 문제(challenge) 개수
        self.assertEqual(data["active_count"], 1)
        self.assertEqual(len(data["clubs"]), 1)  # 클럽 개수
        club = data["clubs"][0]
        self.assertEqual(club["club_id"], str(self.club.club_id))
        self.assertEqual(set(club.keys()), {"club_id", "name", "challenges"})
        self.assertEqual(len(club["challenges"]), 2)
        challenge_ids = {c["koth_challenge_id"] for c in club["challenges"]}
        self.assertEqual(challenge_ids, {str(self.challenge.koth_challenge_id), str(self.second_challenge.koth_challenge_id)})
        first = next(c for c in club["challenges"] if c["koth_challenge_id"] == str(self.challenge.koth_challenge_id))
        self.assertEqual(first["open_group"], 1)
        self.assertEqual(first["status"], "ACTIVE")
        self.assertIsNone(first["current_owner_team_id"])

    def test_clubs_list_shows_current_owner_per_challenge(self):
        KothSolve.objects.create(team=self.team, challenge=self.challenge, earned_score=100)

        response = self.client.get("/api/v1/koth/clubs")

        club = response.json()["data"]["clubs"][0]
        first = next(c for c in club["challenges"] if c["koth_challenge_id"] == str(self.challenge.koth_challenge_id))
        self.assertEqual(first["current_owner_team_id"], str(self.team.team_id))
        self.assertEqual(first["current_score"], 100)

    def test_club_detail_returns_nested_challenges(self):
        response = self.client.get(f"/api/v1/koth/clubs/{self.club.club_id}")

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["club_id"], str(self.club.club_id))
        self.assertEqual(len(data["challenges"]), 2)

    def test_club_detail_invalid_id(self):
        response = self.client.get("/api/v1/koth/clubs/not-a-uuid")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_CLUB_ID")

    def test_club_detail_not_found(self):
        response = self.client.get(f"/api/v1/koth/clubs/{uuid.uuid4()}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "CLUB_NOT_FOUND")

    # ---------------------------------------------------------------- me

    def test_me_requires_auth(self):
        response = self.client.get("/api/v1/koth/me")
        self.assertEqual(response.status_code, 401)

    def test_me_returns_zero_score_by_default(self):
        self.auth("leader")
        response = self.client.get("/api/v1/koth/me")

        data = response.json()["data"]
        self.assertEqual(data["team_id"], str(self.team.team_id))
        self.assertEqual(data["total_koth_score"], 0)
        self.assertEqual(data["total_count"], 2)

    def test_me_reflects_solve_and_rank(self):
        KothSolve.objects.create(team=self.other_team, challenge=self.challenge, earned_score=200)
        KothSolve.objects.create(team=self.team, challenge=self.challenge, earned_score=100)
        self.auth("leader")

        response = self.client.get("/api/v1/koth/me")

        item = next(
            c for c in response.json()["data"]["challenges"]
            if c["koth_challenge_id"] == str(self.challenge.koth_challenge_id)
        )
        self.assertEqual(item["earned_score"], 100)
        self.assertEqual(item["rank"], 2)
        self.assertEqual(response.json()["data"]["total_koth_score"], 100)

    def test_me_requires_team(self):
        self.auth("noteam")
        response = self.client.get("/api/v1/koth/me")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "USER_HAS_NO_TEAM")

    # ---------------------------------------------------------------- leaderboard

    def test_leaderboard_requires_koth_challenge_id(self):
        self.auth("leader")
        response = self.client.get("/api/v1/koth/leaderboard")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "KOTH_CHALLENGE_ID_REQUIRED")

    def test_leaderboard_invalid_id_format(self):
        self.auth("leader")
        response = self.client.get("/api/v1/koth/leaderboard?koth_challenge_id=not-a-uuid")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_KOTH_CHALLENGE_ID")

    def test_leaderboard_not_found(self):
        self.auth("leader")
        response = self.client.get(f"/api/v1/koth/leaderboard?koth_challenge_id={uuid.uuid4()}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "KOTH_CHALLENGE_NOT_FOUND")

    def test_leaderboard_sorted_by_score(self):
        KothSolve.objects.create(team=self.team, challenge=self.challenge, earned_score=50)
        KothSolve.objects.create(team=self.other_team, challenge=self.challenge, earned_score=150)
        self.auth("leader")

        response = self.client.get(
            f"/api/v1/koth/leaderboard?koth_challenge_id={self.challenge.koth_challenge_id}"
        )

        board = response.json()["data"]["leaderboard"]
        self.assertEqual(board[0]["team_id"], str(self.other_team.team_id))
        self.assertEqual(board[0]["rank"], 1)
        self.assertEqual(board[1]["team_id"], str(self.team.team_id))
        self.assertEqual(board[1]["rank"], 2)

    # ---------------------------------------------------------------- team_token

    def test_team_token_requires_auth(self):
        response = self.client.get("/api/v1/koth/team_token")
        self.assertEqual(response.status_code, 401)

    def test_team_token_is_stable_across_calls(self):
        self.auth("leader")
        first = self.client.get("/api/v1/koth/team_token").json()["data"]["team_token"]
        second = self.client.get("/api/v1/koth/team_token").json()["data"]["team_token"]

        self.assertEqual(first, second)
        self.assertEqual(KothTeamToken.objects.filter(team=self.team).count(), 1)

    # ---------------------------------------------------------------- internal/teams

    def test_internal_teams_requires_token(self):
        response = self.client.get("/internal/teams")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], "INVALID_INTERNAL_TOKEN")

    def test_internal_teams_rejects_wrong_token(self):
        response = self.client.get("/internal/teams", **self.internal_headers("wrong"))
        self.assertEqual(response.status_code, 401)

    def test_internal_teams_excludes_banned(self):
        response = self.client.get("/internal/teams", **self.internal_headers())

        self.assertEqual(response.status_code, 200)
        names = {row["team_name"] for row in response.json()["data"]["teams"]}
        self.assertIn("우리팀", names)
        self.assertNotIn("밴팀", names)

    # ---------------------------------------------------------------- internal team-token verify

    def test_verify_team_token_success(self):
        token = KothTeamToken.objects.create(team=self.team)

        response = self.client.post(
            "/internal/koth/team_tokens/verify",
            {"koth_challenge_id": str(self.challenge.koth_challenge_id), "team_token": token.token},
            format="json",
            **self.internal_headers(),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["valid"])
        self.assertEqual(data["team_id"], str(self.team.team_id))
        self.assertEqual(data["team_name"], "우리팀")

    def test_verify_team_token_hyphen_alias(self):
        token = KothTeamToken.objects.create(team=self.team)

        response = self.client.post(
            "/internal/koth/team-tokens/verify",
            {"koth_challenge_id": str(self.challenge.koth_challenge_id), "team_token": token.token},
            format="json",
            **self.internal_headers(),
        )

        self.assertTrue(response.json()["data"]["valid"])

    def test_verify_team_token_wrong_token_is_invalid_not_error(self):
        response = self.client.post(
            "/internal/koth/team_tokens/verify",
            {"koth_challenge_id": str(self.challenge.koth_challenge_id), "team_token": "garbage"},
            format="json",
            **self.internal_headers(),
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertFalse(data["valid"])
        self.assertIsNone(data["team_id"])

    def test_verify_team_token_banned_team_is_invalid(self):
        token = KothTeamToken.objects.create(team=self.banned_team)

        response = self.client.post(
            "/internal/koth/team_tokens/verify",
            {"koth_challenge_id": str(self.challenge.koth_challenge_id), "team_token": token.token},
            format="json",
            **self.internal_headers(),
        )

        self.assertFalse(response.json()["data"]["valid"])

    def test_verify_team_token_requires_internal_token(self):
        response = self.client.post(
            "/internal/koth/team_tokens/verify",
            {"koth_challenge_id": str(self.challenge.koth_challenge_id), "team_token": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 401)

    def test_verify_team_token_requires_fields(self):
        response = self.client.post(
            "/internal/koth/team_tokens/verify", {}, format="json", **self.internal_headers()
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "INVALID_REQUEST")

    # ---------------------------------------------------------------- 브루트포스 방어 (429)

    def _verify(self, team_token, koth_challenge_id=None):
        return self.client.post(
            "/internal/koth/team_tokens/verify",
            {
                "koth_challenge_id": str(koth_challenge_id or self.challenge.koth_challenge_id),
                "team_token": team_token,
            },
            format="json",
            **self.internal_headers(),
        )

    def test_verify_locks_after_three_consecutive_failures(self):
        first = self._verify("wrong")
        second = self._verify("wrong")
        third = self._verify("wrong")

        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.json()["data"]["valid"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 429)
        self.assertEqual(third.json()["code"], "TOO_MANY_ATTEMPTS")
        self.assertIn("retry_after_seconds", third.json()["data"])

    def test_verify_lock_blocks_further_retries_of_the_same_wrong_token(self):
        # 제한은 team_token + koth_challenge_id 단위 (admin.md). 같은 틀린 토큰을 계속 재시도하면 잠긴다.
        self._verify("wrong")
        self._verify("wrong")
        self._verify("wrong")

        fourth = self._verify("wrong")

        self.assertEqual(fourth.status_code, 429)

    def test_verify_success_resets_failure_counter(self):
        raw_token = "temporarily-unissued-token"
        self._verify(raw_token)
        self._verify(raw_token)

        KothTeamToken.objects.create(team=self.team, token=raw_token)
        success = self._verify(raw_token)
        self.assertTrue(success.json()["data"]["valid"])

        KothTeamToken.objects.filter(token=raw_token).delete()
        third = self._verify(raw_token)
        self.assertEqual(third.status_code, 200)  # 성공으로 카운터가 리셋됐으니 아직 락 아님

    def test_verify_lock_is_scoped_per_challenge(self):
        self._verify("wrong")
        self._verify("wrong")
        self._verify("wrong")

        other = self._verify("wrong", koth_challenge_id=self.second_challenge.koth_challenge_id)

        self.assertEqual(other.status_code, 200)  # 다른 koth_challenge_id는 별도 카운터


class KothScoringEngineTestCase(TestCase):
    """/internal/koth/scores 배점 엔진 (admin.md 배점표 확정 반영)."""

    def setUp(self):
        self.team1 = Team.objects.create(team_name="팀1")
        self.team2 = Team.objects.create(team_name="팀2")
        self.team3 = Team.objects.create(team_name="팀3")
        club = KothClub.objects.create(name="동아리A")
        self.challenge = KothChallenge.objects.create(
            club=club, title="koth_web_1", category="WEB", open_group=1,
            status=KothChallengeStatus.ACTIVE,
        )

    def test_compute_period_awards_matches_fixed_table(self):
        awards = compute_period_awards([
            {"team_id": self.team1.team_id, "period_rank": 1},
            {"team_id": self.team2.team_id, "period_rank": 2},
            {"team_id": self.team3.team_id, "period_rank": 3},
        ])

        self.assertEqual(awards[self.team1.team_id], 40)
        self.assertEqual(awards[self.team2.team_id], 25)
        self.assertEqual(awards[self.team3.team_id], 15)

    def test_compute_period_awards_beyond_fifth_place_is_zero(self):
        # 6등 팀 하나만 달랑 있는 게 아니라, 1~5등이 다 채워진 뒤 6등으로 온 정상적인 경우를 검증한다.
        filler_teams = [Team.objects.create(team_name=f"필러{i}") for i in range(5)]
        results = [{"team_id": t.team_id, "period_rank": i + 1} for i, t in enumerate(filler_teams)]
        results.append({"team_id": self.team1.team_id, "period_rank": 6})

        awards = compute_period_awards(results)

        self.assertEqual(awards[self.team1.team_id], 0)

    def test_compute_period_awards_two_way_tie_for_first(self):
        awards = compute_period_awards([
            {"team_id": self.team1.team_id, "period_rank": 1},
            {"team_id": self.team2.team_id, "period_rank": 1},
        ])

        # (40+25)/2 = 32.5 -> 32점씩, 1점은 지급하지 않음
        self.assertEqual(awards[self.team1.team_id], 32)
        self.assertEqual(awards[self.team2.team_id], 32)

    def test_compute_period_awards_three_way_tie_for_first(self):
        awards = compute_period_awards([
            {"team_id": self.team1.team_id, "period_rank": 1},
            {"team_id": self.team2.team_id, "period_rank": 1},
            {"team_id": self.team3.team_id, "period_rank": 1},
        ])

        # (40+25+15)/3 = 26.67 -> 26점씩
        self.assertEqual(awards[self.team1.team_id], 26)
        self.assertEqual(awards[self.team2.team_id], 26)
        self.assertEqual(awards[self.team3.team_id], 26)

    def test_apply_period_results_creates_solve_and_accumulates(self):
        period_id = timezone.now().replace(minute=0, second=0, microsecond=0)
        apply_period_results(
            self.challenge, period_id,
            [{"team_id": self.team1.team_id, "period_rank": 1}],
        )

        solve = KothSolve.objects.get(team=self.team1, challenge=self.challenge)
        self.assertEqual(solve.earned_score, 40)
        first_solved_at = solve.solved_at

        next_period = period_id + timezone.timedelta(minutes=15)
        apply_period_results(
            self.challenge, next_period,
            [{"team_id": self.team1.team_id, "period_rank": 2}],
        )

        solve.refresh_from_db()
        self.assertEqual(solve.earned_score, 65)  # 40 + 25
        self.assertEqual(solve.solved_at, first_solved_at)  # 최초 solved_at은 갱신되지 않음

    def test_apply_period_results_is_idempotent_for_same_period(self):
        period_id = timezone.now().replace(minute=0, second=0, microsecond=0)
        results = [{"team_id": self.team1.team_id, "period_rank": 1}]

        apply_period_results(self.challenge, period_id, results)
        apply_period_results(self.challenge, period_id, results)  # 같은 구간 재요청

        solve = KothSolve.objects.get(team=self.team1, challenge=self.challenge)
        self.assertEqual(solve.earned_score, 40)  # 두 번 반영되지 않음
        self.assertEqual(KothScoringPeriod.objects.filter(challenge=self.challenge).count(), 1)

    def test_apply_period_results_excludes_unknown_team(self):
        period_id = timezone.now().replace(minute=0, second=0, microsecond=0)
        apply_period_results(
            self.challenge, period_id,
            [{"team_id": uuid.uuid4(), "period_rank": 1}],
        )
        # 존재하지 않는 team_id는 조용히 무시되고 예외를 던지지 않는다
        self.assertEqual(KothSolve.objects.filter(challenge=self.challenge).count(), 0)
