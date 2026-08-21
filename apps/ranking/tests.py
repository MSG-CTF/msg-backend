from datetime import datetime, timedelta, timezone
from django.test import SimpleTestCase
from apps.ranking.ranking import build_team_ranking, resolve_last_solved_at


BASE_TIME = datetime(2026, 8, 16, 0, 0, 0, tzinfo=timezone.utc) 


def make_team(name, jeopardy=0, koth=0, jeopardy_at=None, koth_at=None):
    return {
        "team_id": name,          
        "team_name": name,
        "jeopardy_score": jeopardy,
        "koth_score": koth,
        "jeopardy_solved_at": jeopardy_at,
        "koth_solved_at": koth_at,
    }


class ResolveLastSolvedAtTest(SimpleTestCase):

    def test_both_none_returns_none(self):
        result = resolve_last_solved_at(None, None)
        self.assertIsNone(result)

    def test_only_jeopardy_returns_jeopardy(self):
        result = resolve_last_solved_at(BASE_TIME, None)
        self.assertEqual(result, BASE_TIME)

    def test_only_koth_returns_koth(self):
        result = resolve_last_solved_at(None, BASE_TIME)
        self.assertEqual(result, BASE_TIME)

    def test_returns_later_one(self):
        later = BASE_TIME + timedelta(hours=1)
        result = resolve_last_solved_at(BASE_TIME, later)
        self.assertEqual(result, later)


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

    def test_returns_only_top_eight(self):
        team_data = []
        for i in range(12):
            team_data.append(make_team(f"team{i}", jeopardy=i, jeopardy_at=BASE_TIME))
        result = build_team_ranking(team_data)
        self.assertEqual(len(result), 8)

    def test_rank_starts_from_one(self):
        team_data = [
            make_team("A", jeopardy=100, jeopardy_at=BASE_TIME),
            make_team("B", jeopardy=50, jeopardy_at=BASE_TIME),
        ]
        result = build_team_ranking(team_data)
        self.assertEqual(result[0]["rank"], 1)
        self.assertEqual(result[1]["rank"], 2)
