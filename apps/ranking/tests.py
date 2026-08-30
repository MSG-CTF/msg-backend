from datetime import datetime, timedelta, timezone
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from apps.accounts.models import Team
from apps.challenge.models import Challenge, Solve
from apps.koth.models import KothChallenge, KothClub, KothSolve
from apps.ranking.ranking import (
    build_team_ranking,
    build_member_ranking,
    resolve_last_solved_at,
)
from apps.ranking.scoring import calculate_dynamic_score
from apps.ranking.views import collect_team_data

BASE_TIME = datetime(2026, 8, 16, 0, 0, 0, tzinfo=timezone.utc)


def make_team(name, jeopardy=0, koth=0, mileage=0, jeopardy_at=None, koth_at=None):
    return {
        "team_id": name,
        "team_name": name,
        "jeopardy_score": jeopardy,
        "koth_score": koth,
        "mileage": mileage,
        "jeopardy_solved_at": jeopardy_at,
        "koth_solved_at": koth_at,
    }


def make_member(name, score=0, solved=0, at=None):
    return {
        "user_id": name,
        "nickname": name,
        "team_id": "team_" + name,
        "team_name": "team_" + name,
        "user_score": score,
        "solved_count": solved,
        "last_solved_at": at,
    }


class ResolveLastSolvedAtTest(SimpleTestCase):

    def test_both_none_returns_none(self):
        result = resolve_last_solved_at(None, None)
        self.assertIsNone(result)

    def test_only_jeopardy_returns_jeopardy(self):
        result = resolve_last_solved_at(BASE_TIME, None)
        self.assertEqual(result, BASE_TIME)

    def test_koth_only_uses_koth_time(self):
        result = resolve_last_solved_at(None, BASE_TIME)
        self.assertEqual(result, BASE_TIME)

    def test_prefers_jeopardy_over_koth(self):
        later = BASE_TIME + timedelta(hours=1)
        result = resolve_last_solved_at(BASE_TIME, later)
        self.assertEqual(result, BASE_TIME)


class BuildTeamRankingTest(SimpleTestCase):

    def test_score_is_sum_of_jeopardy_and_koth(self):
        team_data = [make_team("A", jeopardy=100, koth=50, jeopardy_at=BASE_TIME)]
        result = build_team_ranking(team_data)
        self.assertEqual(result[0]["team_score"], 150)

    def test_higher_score_first(self):
        team_data = [
            make_team("low", jeopardy=50, jeopardy_at=BASE_TIME),
            make_team("high", jeopardy=100, jeopardy_at=BASE_TIME),
        ]
        result = build_team_ranking(team_data)
        self.assertEqual(result[0]["team_name"], "high")
        self.assertEqual(result[1]["team_name"], "low")

    def test_tie_breaks_by_earlier_solve(self):
        early = BASE_TIME
        late = BASE_TIME + timedelta(hours=1)
        team_data = [
            make_team("late", jeopardy=100, jeopardy_at=late),
            make_team("early", jeopardy=100, jeopardy_at=early),
        ]
        result = build_team_ranking(team_data)
        self.assertEqual(result[0]["team_name"], "early")

    def test_no_solve_team_goes_last(self):
        team_data = [
            make_team("nothing"),
            make_team("solved", jeopardy=10, jeopardy_at=BASE_TIME),
        ]
        result = build_team_ranking(team_data)
        self.assertEqual(result[0]["team_name"], "solved")
        self.assertEqual(result[1]["team_name"], "nothing")

    def test_rank_starts_from_one(self):
        team_data = [
            make_team("A", jeopardy=100, jeopardy_at=BASE_TIME),
            make_team("B", jeopardy=50, jeopardy_at=BASE_TIME),
        ]
        result = build_team_ranking(team_data)
        self.assertEqual(result[0]["rank"], 1)
        self.assertEqual(result[1]["rank"], 2)

    def test_limit_cuts_result(self):
        team_data = []
        for i in range(12):
            team_data.append(make_team(f"team{i}", jeopardy=i, jeopardy_at=BASE_TIME))
        result = build_team_ranking(team_data, limit=8)
        self.assertEqual(len(result), 8)

    def test_no_limit_returns_all(self):
        team_data = []
        for i in range(12):
            team_data.append(make_team(f"team{i}", jeopardy=i, jeopardy_at=BASE_TIME))
        result = build_team_ranking(team_data)
        self.assertEqual(len(result), 12)

    def test_tie_breaks_by_team_id(self):
        team_data = [
            make_team("bbb", jeopardy=100, jeopardy_at=BASE_TIME),
            make_team("aaa", jeopardy=100, jeopardy_at=BASE_TIME),
        ]
        result = build_team_ranking(team_data)
        self.assertEqual(result[0]["team_name"], "aaa")


class CalculateDynamicScoreTest(SimpleTestCase):

    def test_no_solve_returns_initial(self):
        result = calculate_dynamic_score(1000, 600, 70, 0)
        self.assertEqual(result, 1000)

    def test_decreases_as_solve_count_grows(self):
        ten = calculate_dynamic_score(1000, 600, 70, 10)
        thirty = calculate_dynamic_score(1000, 600, 70, 30)
        self.assertLess(thirty, ten)

    def test_reaches_minimum_at_decay(self):
        result = calculate_dynamic_score(1000, 600, 70, 70)
        self.assertEqual(result, 600)

    def test_never_below_minimum(self):
        result = calculate_dynamic_score(1000, 600, 70, 200)
        self.assertEqual(result, 600)

    def test_known_value(self):
        result = calculate_dynamic_score(1000, 600, 70, 10)
        self.assertEqual(result, 992)

    def test_accepts_decimal_input(self):
        result = calculate_dynamic_score(Decimal("1000"), Decimal("600"), 70, 10)
        self.assertEqual(result, 992)


class BuildMemberRankingTest(SimpleTestCase):

    def test_higher_score_first(self):
        data = [
            make_member("low", 500, 1, BASE_TIME),
            make_member("high", 900, 2, BASE_TIME),
        ]
        result = build_member_ranking(data)
        self.assertEqual(result[0]["nickname"], "high")

    def test_tie_breaks_by_earlier_solve(self):
        early = BASE_TIME
        late = BASE_TIME + timedelta(hours=1)
        data = [
            make_member("late", 800, 1, late),
            make_member("early", 800, 1, early),
        ]
        result = build_member_ranking(data)
        self.assertEqual(result[0]["nickname"], "early")

    def test_no_solve_goes_last(self):
        data = [
            make_member("nothing"),
            make_member("solved", 100, 1, BASE_TIME),
        ]
        result = build_member_ranking(data)
        self.assertEqual(result[1]["nickname"], "nothing")

    def test_rank_starts_from_one(self):
        data = [
            make_member("a", 900, 2, BASE_TIME),
            make_member("b", 500, 1, BASE_TIME),
        ]
        result = build_member_ranking(data)
        self.assertEqual(result[0]["rank"], 1)
        self.assertEqual(result[1]["rank"], 2)


class CollectTeamDataTest(TestCase):

    def make_challenge(self, name, score):
        return Challenge.objects.create(
            title=name,
            category="WEB",
            difficulty="EASY",
            score=Decimal(score),
            current_score=Decimal(score),
            flag_hash=f"hash_{name}",
            is_published=True,
        )

    def make_koth_challenge(self, club, name):
        return KothChallenge.objects.create(
            club=club,
            title=name,
            open_group=1,
            inbound_internal_token_hash=f"hash_{name}",
        )

    def test_jeopardy_score_sums_current_score(self):
        team = Team.objects.create(team_name="팀", team_score=Decimal("999"))
        for i in range(3):
            c = self.make_challenge(f"문제{i}", "1000")
            Solve.objects.create(
                team=team, challenge=c,
                earned_score=Decimal("1000"), earned_mileage=100,
            )

        data = collect_team_data()
        self.assertEqual(data[0]["jeopardy_score"], Decimal("3000"))

    def test_koth_sum_is_not_inflated_by_join(self):
        team = Team.objects.create(team_name="팀")

        for i in range(3):
            c = self.make_challenge(f"문제{i}", "1000")
            Solve.objects.create(
                team=team, challenge=c,
                earned_score=Decimal("1000"), earned_mileage=100,
            )

        for i in range(2):
            club = KothClub.objects.create(name=f"동아리{i}")
            kc = self.make_koth_challenge(club, f"koth{i}")
            KothSolve.objects.create(
                team=team, challenge=kc,
                earned_score=Decimal("100"),
                solved_at=BASE_TIME,
            )

        data = collect_team_data()

        self.assertEqual(data[0]["jeopardy_score"], Decimal("3000"))
        self.assertEqual(data[0]["koth_score"], Decimal("200"))

    def test_koth_first_solved_at_uses_min(self):
        team = Team.objects.create(team_name="팀")
        early = BASE_TIME
        late = BASE_TIME + timedelta(hours=2)

        for i, at in enumerate([late, early]):
            club = KothClub.objects.create(name=f"동아리{i}")
            kc = self.make_koth_challenge(club, f"koth{i}")
            KothSolve.objects.create(
                team=team, challenge=kc,
                earned_score=Decimal("100"), solved_at=at,
            )

        data = collect_team_data()

        self.assertEqual(data[0]["koth_solved_at"], early)

    def test_team_without_solve_has_zero(self):
        Team.objects.create(team_name="팀", team_score=Decimal("500"))

        data = collect_team_data()

        self.assertEqual(data[0]["jeopardy_score"], Decimal("0"))
        self.assertEqual(data[0]["koth_score"], Decimal("0"))
        self.assertIsNone(data[0]["jeopardy_solved_at"])
        self.assertIsNone(data[0]["koth_solved_at"])