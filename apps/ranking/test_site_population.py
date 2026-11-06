"""Each site's ranking is confined to its configured participant roster."""
import json
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.challenge.models import Challenge, Solve
from apps.koth.models import KothChallenge, KothClub, KothSolve
from apps.koth.services import eligible_teams


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
    KOTH_LEADERBOARD_CACHE_SECONDS=60,
    ALLOWED_HOSTS=["testserver", "msgctf.kr", "internal.msgctf.kr"],
)
class SiteRankingPopulationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.external = Team.objects.bulk_create([
            Team(team_name=f"external-roster-{i:02}", koth_division="EXTERNAL") for i in range(50)
        ])
        cls.internal = Team.objects.bulk_create([
            Team(team_name=f"internal-roster-{i:02}", koth_division="INTERNAL") for i in range(25)
        ])
        cls.extra = Team.objects.create(team_name="sla-koth-unregistered", koth_division="INTERNAL")
        cls.extra_external = Team.objects.create(team_name="unregistered-external", koth_division="EXTERNAL")
        cls.external_user = User.objects.create_user(login_id="external-reader", nickname="External", team=cls.external[0])
        cls.internal_user = User.objects.create_user(login_id="internal-reader", nickname="Internal", team=cls.internal[0])
        for team, user, points in [(cls.external[0], cls.external_user, 100), (cls.internal[0], cls.internal_user, 9000)]:
            challenge = Challenge.objects.create(
                title=team.team_name, category="WEB", difficulty="EASY", score=points,
                current_score=points, flag_hash="fixture-only", is_published=True,
            )
            Solve.objects.create(team=team, solved_by_user=user, challenge=challenge, earned_score=points, earned_mileage=1)
        cls.koth = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Site ranking"), title="Site ranking", open_group=1,
            inbound_internal_token_hash="fixture-only",
        )
        for team, score in [(cls.external[0], 10), (cls.internal[0], 90), (cls.extra, 999)]:
            KothSolve.objects.create(team=team, challenge=cls.koth, earned_score=score, solved_at=timezone.now())

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.client.force_authenticate(self.external_user)

    def scope(self, division, teams=None):
        if teams is None:teams = self.external if division == "EXTERNAL" else self.internal
        return override_settings(
            PUBLIC_RANKING_DIVISION=division,
            PUBLIC_RANKING_TEAM_IDS=tuple(str(team.pk) for team in teams),
            PUBLIC_RANKING_EXCLUDED_TEAM_IDS=(),
        )

    def read(self, path, **headers):
        response = self.client.get(path, HTTP_ACCEPT="application/json", **headers)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["code"], "SUCCESS")
        return response.json()["data"]

    def test_external_contains_exactly_50_roster_teams(self):
        with self.scope("EXTERNAL"):
            data = self.read("/api/v1/ranking?size=100")
        self.assertEqual(data["total_count"], 50)
        self.assertEqual({row["team_id"] for row in data["rankings"]}, {str(team.pk) for team in self.external})
        self.assertEqual(data["rankings"][0]["team_score"], 110)

    def test_internal_contains_exactly_25_roster_teams(self):
        with self.scope("INTERNAL"):
            data = self.read("/api/v1/ranking?size=100")
        self.assertEqual(data["total_count"], 25)
        self.assertEqual({row["team_id"] for row in data["rankings"]}, {str(team.pk) for team in self.internal})
        self.assertEqual(data["rankings"][0]["team_score"], 9090)

    def test_scope_is_applied_before_pagination_and_rank_numbers(self):
        with self.scope("EXTERNAL"):
            data = self.read("/api/v1/ranking?size=1")
        self.assertEqual((data["total_count"], data["rankings"][0]["rank"]), (50, 1))
        self.assertEqual(data["rankings"][0]["team_id"], str(self.external[0].pk))

    def test_request_query_host_and_user_do_not_change_site_scope(self):
        self.client.force_authenticate(self.internal_user)
        with self.scope("EXTERNAL"):
            data = self.read("/api/v1/ranking?size=100&division=INTERNAL&koth_division=INTERNAL&team_scope=all", HTTP_HOST="internal.msgctf.kr")
            self.assertIsNone(self.read("/api/v1/ranking/me"))
            self.assertIsNone(self.read("/api/v1/ranking/member"))
        self.assertEqual(data["total_count"], 50)

    def test_my_team_and_member_ranks_use_the_site_population(self):
        for division, user in [("EXTERNAL", self.external_user), ("INTERNAL", self.internal_user)]:
            with self.subTest(division=division), self.scope(division):
                self.client.force_authenticate(user)
                self.assertEqual(self.read("/api/v1/ranking/me")["rank"], 1)
                self.assertEqual(self.read("/api/v1/ranking/member")["rank"], 1)

    def test_graph_uses_the_same_site_roster(self):
        for division, team in [("EXTERNAL", self.external[0]), ("INTERNAL", self.internal[0])]:
            with self.subTest(division=division), self.scope(division):
                data = self.read("/api/v1/leaderboard")
                self.assertEqual([row["team_id"] for row in data["teams"]], [str(team.pk)])

    def test_late_unregistered_team_in_same_division_does_not_appear(self):
        with self.scope("INTERNAL"):
            data = self.read("/api/v1/ranking?size=100")
        self.assertNotIn(str(self.extra.pk), [row["team_id"] for row in data["rankings"]])

    def test_koth_display_cache_is_scoped_by_division_and_roster(self):
        path=f"/api/v1/koth/leaderboard?koth_challenge_id={self.koth.pk}"
        with self.scope("EXTERNAL"):
            self.assertEqual([row["team_id"] for row in self.read(path)["leaderboard"]], [str(self.external[0].pk)])
        with self.scope("INTERNAL"):
            self.assertEqual([row["team_id"] for row in self.read(path)["leaderboard"]], [str(self.internal[0].pk)])
        with self.scope("EXTERNAL", self.external[1:]):
            self.assertEqual(self.read(path)["leaderboard"], [])

    def test_public_settings_do_not_change_global_game_eligibility(self):
        before=set(eligible_teams().values_list("pk",flat=True))
        with self.scope("INTERNAL"):
            self.assertEqual(set(eligible_teams().values_list("pk",flat=True)), before)

    def test_all_ranking_reads_preserve_accounts_affiliations_and_game_rows(self):
        def snapshot():
            return json.dumps({model._meta.label:list(model.objects.order_by("pk").values()) for model in [Team,User,Solve,KothSolve]},sort_keys=True,default=str)
        before=snapshot()
        with self.scope("EXTERNAL"):
            for path in ["/api/v1/ranking","/api/v1/leaderboard","/api/v1/ranking/me","/api/v1/ranking/member",f"/api/v1/koth/leaderboard?koth_challenge_id={self.koth.pk}"]:
                self.read(path)
        self.assertEqual(snapshot(),before)
