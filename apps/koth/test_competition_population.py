"""Competition counts must ignore explicitly registered preview/test identities."""

import hashlib
import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.common.jwt import hash_token
from apps.koth import services
from apps.koth.models import (
    KothChallenge,
    KothClub,
    KothPreviewToken,
    KothScorePeriod,
    KothSolve,
)
from apps.koth.test_http_clients import score_server


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class KothCompetitionPopulationTests(TestCase):
    def setUp(self):
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Population regression"),
            title="Population regression",
            status="ACTIVE",
            open_group=1,
            inbound_internal_token_hash=hash_token("population-internal-test"),
            score_api_token_env="POPULATION_TEST_SCORE_TOKEN",
        )
        self.admin = User.objects.create_user(
            login_id="population-admin", nickname="Admin", role="ADMIN"
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.real = Team.objects.bulk_create(
            [Team(team_name=f"participant-{n:02}") for n in range(75)]
        )
        self.fixtures = Team.objects.bulk_create(
            [Team(team_name=f"explicit-load-fixture-{n:02}") for n in range(75)]
        )
        User.objects.bulk_create(
            [
                User(
                    login_id=f"population-{i}-{member}",
                    nickname="Member",
                    team=team,
                    password="!",
                    is_leader=member == 0,
                )
                for i, team in enumerate(self.real + self.fixtures)
                for member in range(2)
            ]
        )
        self.period = datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)

    def mark_fixtures(self, challenge=None):
        challenge = challenge or self.challenge
        KothPreviewToken.objects.bulk_create(
            [
                KothPreviewToken(
                    challenge=challenge,
                    admin=None,
                    preview_team_id=team.pk,
                    key_version=0,
                    token_hash=hashlib.sha256(
                        f"unusable-marker:{challenge.pk}:{team.pk}".encode()
                    ).hexdigest(),
                )
                for team in self.fixtures
            ]
        )

    def payload(self):
        combined = [team for pair in zip(self.real, self.fixtures) for team in pair]
        return {
            "code": "SUCCESS",
            "data": {
                "koth_challenge_id": str(self.challenge.pk),
                "period_id": self.period.isoformat(),
                "results": [
                    {
                        "team_id": str(team.pk),
                        "period_rank": rank,
                        "metric_score": 151 - rank,
                    }
                    for rank, team in enumerate(combined, 1)
                ],
            },
        }

    def test_admin_counts_75_registered_teams_not_150_people_or_150_team_rows(self):
        self.mark_fixtures()
        response = self.client.get("/api/v1/admin/koth/challenges")
        self.assertEqual(response.status_code, 200, response.data)
        scoring = response.data["data"]["scoring"]
        points = scoring["points_by_division"]["EXTERNAL"]
        self.assertEqual(Team.objects.count(), 150)
        self.assertEqual(User.objects.filter(team__in=self.real).count(), 150)
        self.assertEqual(services.scoring_team_counts(), {"EXTERNAL": 75})
        self.assertEqual(scoring["first_place_points"], 40)
        self.assertEqual(len(points), 37)
        self.assertEqual((points[0], points[-1]), (40, 1))
        self.assertTrue(all(a > b for a, b in zip(points, points[1:])))

    def test_game_team_list_uses_the_same_competition_population(self):
        self.mark_fixtures()
        response = APIClient().get(
            "/internal/teams",
            {"koth_challenge_id": str(self.challenge.pk)},
            HTTP_X_INTERNAL_TOKEN="population-internal-test",
        )
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data["data"]
        self.assertEqual(data["total_count"], 75)
        self.assertEqual(
            {row["team_id"] for row in data["teams"]},
            {str(team.pk) for team in self.real},
        )

    def test_repeated_markers_banned_and_deleted_test_teams_do_not_distort_counts(self):
        self.mark_fixtures()
        second = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Second marker source"),
            title="Second marker source",
            open_group=2,
        )
        self.mark_fixtures(second)
        Team.objects.filter(pk=self.real[-1].pk).update(is_banned=True)
        self.fixtures[-1].delete()
        self.assertEqual(services.scoring_team_counts(), {"EXTERNAL": 74})
        self.assertEqual(KothPreviewToken.objects.count(), 150)

    @patch.dict(os.environ, {"POPULATION_TEST_SCORE_TOKEN": "fixture-score-token"})
    def test_real_http_poll_ignores_fixture_ranks_and_applied_period_is_immutable(self):
        self.mark_fixtures()
        payload = self.payload()
        with score_server(body=json.dumps(payload).encode()) as (url, requests):
            self.challenge.score_api_url = url
            self.assertTrue(services.poll_challenge_period(self.challenge, self.period))
            scores = dict(KothSolve.objects.values_list("team_id", "earned_score"))
            self.assertEqual(len(scores), 37)
            self.assertEqual(scores[self.real[0].pk], 40)
            self.assertEqual(scores[self.real[36].pk], 1)
            self.assertFalse(set(scores) & {team.pk for team in self.fixtures})
            record = KothScorePeriod.objects.get()
            self.assertEqual(record.status, "APPLIED")
            self.assertEqual(record.response_payload, payload)
            self.assertEqual(
                record.award_snapshot["team_count_by_division"],
                {"INTERNAL": 0, "EXTERNAL": 75},
            )
            snapshot, applied_at = record.award_snapshot, record.applied_at
            # Later eligibility changes must not recalculate a completed period.
            KothPreviewToken.objects.filter(
                preview_team_id=self.fixtures[0].pk
            ).delete()
            self.assertFalse(
                services.poll_challenge_period(self.challenge, self.period)
            )
            record.refresh_from_db()
            self.assertEqual(
                (record.award_snapshot, record.applied_at), (snapshot, applied_at)
            )
            self.assertEqual(
                dict(KothSolve.objects.values_list("team_id", "earned_score")), scores
            )
            self.assertEqual(len(requests), 1)

    def test_marker_added_after_fetch_is_rechecked_under_the_team_snapshot_lock(self):
        results = [
            (team, rank, 1) for rank, team in enumerate(self.real + self.fixtures, 1)
        ]
        self.mark_fixtures()
        awards, snapshot = services._locked_awards_and_snapshot(results)
        self.assertEqual(
            snapshot["team_count_by_division"], {"INTERNAL": 0, "EXTERNAL": 75}
        )
        self.assertEqual(len(awards), 37)
        self.assertTrue(all(team in self.real for team, _ in awards))
        self.assertTrue(
            all(snapshot["awarded_scores"][str(team.pk)] == 0 for team in self.fixtures)
        )
