from django.test import TestCase
from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from apps.accounts.models import Team, User
from apps.challenge.models import Challenge, Solve
from apps.koth.models import KothChallenge, KothClub, KothSolve


class LeaderboardAPITest(TestCase):

    def setUp(self):
        #문제 하나를 만들어 여러 팀이 풀게 한다
        self.challenge = Challenge.objects.create(
            title="테스트문제",
            category="WEB",
            difficulty="EASY",
            score=Decimal("1000"),
            flag_hash="dummy",
            is_published=True,
        )

    def make_koth_solve(self, team, name, score, at):
        club = KothClub.objects.create(name=f"동아리_{name}")
        kc = KothChallenge.objects.create(
            club=club,
            title=name,
            open_group=1,
            inbound_internal_token_hash=f"hash_{name}",
        )
        return KothSolve.objects.create(
            team=team, challenge=kc,
            earned_score=Decimal(score), solved_at=at,
        )

    def make_team_with_solve(self, name, score, solved_at=None):
        team = Team.objects.create(team_name=name, team_score=Decimal(score))
        challenge = Challenge.objects.create(
            title=f"문제_{name}",
            category="WEB",
            difficulty="EASY",
            score=Decimal("1000"),
            flag_hash=f"hash_{name}",
            is_published=True,
        )
        solve = Solve.objects.create(
            team=team,
            challenge=challenge,
            earned_score=Decimal("1000"),
            earned_mileage=100,
        )
        if solved_at is not None:
            # auto_now_add라 생성 후 직접 갱신한다
            Solve.objects.filter(pk=solve.pk).update(solved_at=solved_at)
        return team

    def test_team_without_solve_is_excluded(self):
        self.make_team_with_solve("푼팀", 100)
        Team.objects.create(team_name="안푼팀", team_score=Decimal("9999"))

        data = self.client.get("/api/v1/leaderboard").data["data"]

        names = [t["team_name"] for t in data["teams"]]
        self.assertIn("푼팀", names)
        self.assertNotIn("안푼팀", names)

    def test_banned_team_is_excluded(self):
        self.make_team_with_solve("정상팀", 100)
        banned = self.make_team_with_solve("밴팀", 9999)
        Team.objects.filter(pk=banned.pk).update(is_banned=True)

        data = self.client.get("/api/v1/leaderboard").data["data"]

        names = [t["team_name"] for t in data["teams"]]
        self.assertNotIn("밴팀", names)

    def test_is_top3_marks_only_first_three(self):
        for i in range(5):
            self.make_team_with_solve(f"팀{i}", 500 - i * 10)

        data = self.client.get("/api/v1/leaderboard").data["data"]

        flags = [t["is_top3"] for t in data["teams"]]
        self.assertEqual(flags, [True, True, True, False, False])

    def test_returns_at_most_eight_teams(self):
        for i in range(12):
            self.make_team_with_solve(f"팀{i}", 500 - i)

        data = self.client.get("/api/v1/leaderboard").data["data"]

        self.assertEqual(len(data["teams"]), 8)
        self.assertEqual(data["total_count"], 8)

    def test_solves_are_sorted_by_time(self):
        now = timezone.now()
        team = self.make_team_with_solve("팀", 100, solved_at=now)
        later = Solve.objects.create(
            team=team,
            challenge=self.challenge,
            earned_score=Decimal("1000"),
            earned_mileage=100,
        )
        Solve.objects.filter(pk=later.pk).update(solved_at=now + timedelta(hours=1))

        data = self.client.get("/api/v1/leaderboard").data["data"]

        times = [s["solved_at"] for s in data["teams"][0]["solves"]]
        self.assertEqual(times, sorted(times))

    def test_empty_when_no_team(self):
        data = self.client.get("/api/v1/leaderboard").data["data"]

        self.assertEqual(data["teams"], [])
        self.assertEqual(data["total_count"], 0)


    def test_koth_only_team_is_included(self):
        # KOTH만 푼 팀도 그래프에 포함한다
        team = Team.objects.create(team_name="koth팀")
        self.make_koth_solve(team, "koth1", "40", timezone.now())

        data = self.client.get("/api/v1/leaderboard").data["data"]

        names = [t["team_name"] for t in data["teams"]]
        self.assertIn("koth팀", names)

    def test_header_score_matches_graph_sum(self):
        # 헤더 team_score와 그래프 points 합이 같아야 한다
        self.make_team_with_solve("팀", 0)

        data = self.client.get("/api/v1/leaderboard").data["data"]
        team = data["teams"][0]

        graph_sum = sum(s["points"] for s in team["solves"])
        self.assertEqual(team["team_score"], graph_sum)

    def test_total_score_sums_jeopardy_and_koth(self):
        # 총점은 제오파디 + KOTH다
        team = self.make_team_with_solve("팀", 0)
        self.make_koth_solve(team, "koth1", "40", timezone.now())

        data = self.client.get("/api/v1/leaderboard").data["data"]

        # 제오파디 1000 + KOTH 40
        self.assertEqual(data["teams"][0]["team_score"], 1040)

    def test_graph_includes_koth_solve(self):
        # 그래프에 KOTH 항목이 들어간다
        team = self.make_team_with_solve("팀", 0)
        self.make_koth_solve(team, "koth1", "40", timezone.now())

        data = self.client.get("/api/v1/leaderboard").data["data"]

        types = {s["source_type"] for s in data["teams"][0]["solves"]}
        self.assertEqual(types, {"JEOPARDY", "KOTH"})