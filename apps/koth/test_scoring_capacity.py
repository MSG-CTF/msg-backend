"""TC-599: verify scoring within the confirmed 74-team event capacity."""

from datetime import datetime, timezone
from unittest.mock import patch
from django.test import TestCase, SimpleTestCase
from rest_framework.test import APIClient
from apps.accounts.models import Team, User
from apps.koth.models import KothClub, KothChallenge, KothSolve, KothScorePeriod
from apps.koth.services import (
    _locked_awards_and_snapshot,
    poll_challenge_period,
    rank_points,
)


class Koth74TeamCurveTests(SimpleTestCase):
    def test_82_team_limit_is_still_explicit(self):
        # Document the existing capacity limit without requiring an HTTP 500.
        self.assertEqual(rank_points(81), tuple(range(40, 0, -1)))
        with self.assertRaisesRegex(ValueError, "unique positive integer"):
            rank_points(82)

    def test_every_group_size_zero_through_74(self):
        for count in range(75):
            with self.subTest(count=count):
                points = rank_points(count)
                self.assertEqual(len(points), max(1, count // 2) if count else 0)
                self.assertTrue(all(type(p) is int and 1 <= p <= 40 for p in points))
                self.assertTrue(all(a > b for a, b in zip(points, points[1:])))
                if points:
                    self.assertEqual(points[0], 40)
                if len(points) > 1:
                    self.assertEqual(points[-1], 1)


class Koth74TeamScoringTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            login_id="tc599-admin", nickname="QA", role="ADMIN"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="TC599"),
            title="QA",
            open_group=1,
            status="ACTIVE",
            inbound_internal_token_hash="a" * 64,
        )
        self.period = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)

    def check_distribution(self, internal, external, banned=0):
        teams = []
        for division, count in [("INTERNAL", internal), ("EXTERNAL", external)]:
            teams.extend(
                Team.objects.bulk_create(
                    [
                        Team(team_name=f"{division}-{n}", koth_division=division)
                        for n in range(count)
                    ]
                )
            )
        Team.objects.bulk_create(
            [
                Team(team_name=f"banned-{n}", koth_division="INTERNAL", is_banned=True)
                for n in range(banned)
            ]
        )
        response = self.client.get("/api/v1/admin/koth/challenges")
        self.assertEqual(response.status_code, 200, response.data)
        expected_points = {
            division: list(rank_points(count))
            for division, count in [("INTERNAL", internal), ("EXTERNAL", external)]
        }
        self.assertEqual(
            response.data["data"]["scoring"]["points_by_division"], expected_points
        )
        # Interleave divisions in the score-provider's combined ranking.
        teams.sort(key=lambda t: (int(t.team_name.rsplit("-", 1)[1]), t.koth_division))
        payload = {
            "code": "SUCCESS",
            "data": {
                "koth_challenge_id": str(self.challenge.pk),
                "period_id": self.period.isoformat(),
                "results": [
                    {
                        "team_id": str(t.pk),
                        "period_rank": i,
                        "metric_score": len(teams) + 1 - i,
                    }
                    for i, t in enumerate(teams, 1)
                ],
            },
        }
        expected_awards = {}
        counts = {"INTERNAL": 0, "EXTERNAL": 0}
        for team in teams:
            division = team.koth_division
            index = counts[division]
            points = expected_points[division]
            if index < len(points):
                expected_awards[str(team.pk)] = points[index]
            counts[division] += 1
        with patch("apps.koth.services._request_scores", return_value=payload):
            self.assertTrue(poll_challenge_period(self.challenge, self.period))
            actual = {
                str(tid): score
                for tid, score in KothSolve.objects.values_list(
                    "team_id", "earned_score"
                )
            }
            self.assertEqual(actual, expected_awards)
            record = KothScorePeriod.objects.get()
            self.assertEqual(record.status, "APPLIED")
            self.assertEqual(
                record.award_snapshot["team_count_by_division"],
                {"INTERNAL": internal, "EXTERNAL": external},
            )
            self.assertFalse(poll_challenge_period(self.challenge, self.period))
            self.assertEqual(
                {
                    str(tid): score
                    for tid, score in KothSolve.objects.values_list(
                        "team_id", "earned_score"
                    )
                },
                actual,
            )
        self.assertEqual(
            self.client.get("/api/v1/admin/koth/challenges").status_code, 200
        )

    def test_all_74_internal(self):
        self.check_distribution(74, 0)

    def test_all_74_external(self):
        self.check_distribution(0, 74)

    def test_25_internal_49_external(self):
        self.check_distribution(25, 49)

    def test_37_internal_37_external(self):
        self.check_distribution(37, 37)

    def test_banned_teams_do_not_increase_curve_count(self):
        self.check_distribution(74, 0, banned=8)


class KothOfficialTeamCountTests(TestCase):
    def setUp(self):
        admin = User.objects.create_user(
            login_id="official-count-admin", nickname="QA", role="ADMIN"
        )
        self.client = APIClient()
        self.client.force_authenticate(admin)

    def test_configured_official_counts_drive_admin_curve_and_period_snapshot(self):
        settings_response = self.client.patch(
            "/api/v1/admin/settings",
            {
                "koth": {
                    "external_team_count": 50,
                    "internal_team_count": 25,
                }
            },
            format="json",
        )
        self.assertEqual(settings_response.status_code, 200, settings_response.data)
        self.assertEqual(
            settings_response.data["data"]["koth"],
            {"external_team_count": 50, "internal_team_count": 25},
        )
        external = Team.objects.create(
            team_name="official-external", koth_division="EXTERNAL"
        )
        internal = Team.objects.create(
            team_name="official-internal", koth_division="INTERNAL"
        )

        response = self.client.get("/api/v1/admin/koth/challenges")
        self.assertEqual(response.status_code, 200, response.data)
        scoring = response.data["data"]["scoring"]
        self.assertEqual(len(scoring["points_by_division"]["EXTERNAL"]), 25)
        self.assertEqual(len(scoring["points_by_division"]["INTERNAL"]), 12)
        self.assertEqual(
            scoring["points_by_division"]["EXTERNAL"],
            [
                40,
                33,
                29,
                26,
                24,
                22,
                20,
                18,
                17,
                16,
                15,
                14,
                13,
                12,
                11,
                10,
                9,
                8,
                7,
                6,
                5,
                4,
                3,
                2,
                1,
            ],
        )
        self.assertEqual(
            scoring["points_by_division"]["INTERNAL"],
            [40, 28, 22, 18, 15, 12, 10, 8, 6, 4, 2, 1],
        )

        _, snapshot = _locked_awards_and_snapshot(
            [(external, 1, 1), (internal, 1, 1)]
        )
        self.assertEqual(
            snapshot["team_count_by_division"], {"INTERNAL": 25, "EXTERNAL": 50}
        )
