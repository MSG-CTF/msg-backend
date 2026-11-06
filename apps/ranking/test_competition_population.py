"""Public ranks use the registered competition population without editing it."""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge, Solve
from apps.koth.models import KothChallenge, KothClub, KothPreviewToken
from apps.ranking.views import collect_member_data


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class PublicCompetitionPopulationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.official = Team.objects.create(team_name="정식 참가팀")
        # Test-like words are valid participant names; registration decides scope.
        self.similar_name = Team.objects.create(team_name="devcheck-team-official")
        self.no_members = Team.objects.create(team_name="계정 준비 중 참가팀")
        self.fixture = Team.objects.create(team_name="internal-check-team-01")
        self.banned = Team.objects.create(team_name="정지된 팀", is_banned=True)
        self.admin = User.objects.create_user(
            login_id="public-rank-operator", nickname="운영자", role="ADMIN"
        )
        self.member = self.make_user(self.official, "official-member")
        self.fixture_user = self.make_user(self.fixture, "fixture-member")
        self.client.force_authenticate(user=self.member)
        self.challenge = Challenge.objects.create(
            title="인구 회귀 테스트", category="WEB", difficulty="EASY",
            score=Decimal("100"), current_score=Decimal("100"),
            flag_hash="fixture-only", is_published=True,
        )
        self.solve = Solve.objects.create(
            team=self.official, solved_by_user=self.member, challenge=self.challenge,
            earned_score=Decimal("100"), earned_mileage=10,
        )
        for team, user in [(self.fixture, self.fixture_user), (self.banned, None)]:
            challenge = Challenge.objects.create(
                title="높은 점수 " + team.team_name, category="PWN", difficulty="HARD",
                score=Decimal("9999"), current_score=Decimal("9999"),
                flag_hash="fixture-only-" + str(team.pk), is_published=True,
            )
            Solve.objects.create(
                team=team, solved_by_user=user, challenge=challenge,
                earned_score=Decimal("9999"), earned_mileage=99,
            )
        for index in range(2):
            club = KothClub.objects.create(name=f"순위 회귀 테스트 동아리 {index}")
            challenge = KothChallenge.objects.create(
                club=club, title=f"분류 표식 {index}", open_group=1,
                inbound_internal_token_hash=f"fixture-only-{index}",
            )
            marker = KothPreviewToken.objects.create(
                admin=None, challenge=challenge, preview_team_id=self.fixture.pk,
                token_hash=hashlib.sha256(f"public-rank-{index}".encode()).hexdigest(),
                key_version=0,
            )
            # Classification persists even when a token is no longer usable.
            KothPreviewToken.objects.filter(pk=marker.pk).update(
                issued_at=timezone.now() - timedelta(days=30)
            )

    @staticmethod
    def make_user(team, login_id):
        return User.objects.create_user(
            login_id=login_id, nickname=login_id, team=team, role="PARTICIPANT"
        )

    def get_data(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["code"], "SUCCESS")
        return response.data["data"]

    def test_public_ranking_excludes_marked_team_before_pagination(self):
        data = self.get_data("/api/v1/ranking?size=1")
        self.assertEqual(data["total_count"], 3)
        self.assertEqual(len(data["rankings"]), 1)
        self.assertEqual(data["rankings"][0]["team_id"], str(self.official.pk))
        self.assertEqual(data["rankings"][0]["rank"], 1)

    def test_zero_solve_teams_without_members_and_test_like_names_remain(self):
        data = self.get_data("/api/v1/ranking?size=100")
        self.assertEqual(
            {row["team_id"] for row in data["rankings"]},
            {str(team.pk) for team in [self.official, self.similar_name, self.no_members]},
        )

    def test_public_query_cannot_opt_back_into_fixture_scope(self):
        data = self.get_data("/api/v1/ranking?team_scope=all&size=100")
        self.assertNotIn(str(self.fixture.pk), [row["team_id"] for row in data["rankings"]])

    def test_leaderboard_graph_excludes_high_scoring_fixture(self):
        data = self.get_data("/api/v1/leaderboard")
        self.assertEqual(data["total_count"], 1)
        self.assertEqual([row["team_id"] for row in data["teams"]], [str(self.official.pk)])
        self.assertEqual(data["teams"][0]["team_score"], 100)
        self.assertEqual(sum(row["points"] for row in data["teams"][0]["solves"]), 100)

    def test_official_my_team_rank_is_not_shifted_by_fixture(self):
        self.client.force_authenticate(user=self.member)
        data = self.get_data("/api/v1/ranking/me")
        self.assertEqual(data["rank"], 1)
        self.assertEqual(data["team_id"], str(self.official.pk))

    def test_fixture_my_team_and_member_ranks_are_absent(self):
        self.client.force_authenticate(user=self.fixture_user)
        self.assertIsNone(self.get_data("/api/v1/ranking/me"))
        self.assertIsNone(self.get_data("/api/v1/ranking/member"))

    def test_member_ranking_uses_same_competition_population(self):
        self.assertEqual([row["user_id"] for row in collect_member_data()], [str(self.member.pk)])
        self.client.force_authenticate(user=self.member)
        data = self.get_data("/api/v1/ranking/member")
        self.assertEqual(data["rank"], 1)
        self.assertEqual(data["user_score"], 100)

    def test_ranking_reads_preserve_accounts_teams_and_solves(self):
        def snapshot():
            return json.dumps({
                model._meta.label: list(model.objects.order_by(model._meta.pk.name).values())
                for model in [User, Team, Solve, KothPreviewToken]
            }, sort_keys=True, default=str)
        before = snapshot()
        self.client.force_authenticate(user=self.member)
        for url in ["/api/v1/ranking", "/api/v1/leaderboard", "/api/v1/ranking/me", "/api/v1/ranking/member"]:
            self.get_data(url)
        self.assertEqual(snapshot(), before)
