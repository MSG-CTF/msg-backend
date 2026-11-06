"""Admin team scope, pagination, aggregates, and business-row preservation."""

import json
from pathlib import Path

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, TestTeamIdentity, User
from apps.common.jwt import hash_token
from apps.koth.models import KothChallenge, KothClub, KothPreviewToken, KothSolve


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class AdminTeamScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            login_id="scope-admin", role=Role.ADMIN, nickname="Admin"
        )
        cls.official = Team.objects.bulk_create(
            [
                Team(
                    team_name=f"공식-{n:02}",
                    mileage=10,
                    koth_division="INTERNAL" if n < 25 else "EXTERNAL",
                )
                for n in range(75)
            ]
        )
        cls.test_teams = Team.objects.bulk_create(
            [Team(team_name=f"테스트-{n}", mileage=100) for n in range(2)]
        )
        cls.archived = Team.objects.bulk_create(
            [
                Team(
                    team_name=f"보관-{n}",
                    mileage=1000,
                    is_banned=True,
                    ban_reason="보관",
                )
                for n in range(2)
            ]
        )
        cls.player = User.objects.create_user(
            login_id="registered-player",
            nickname="Member",
            team=cls.official[0],
            password="already-registered",
        )
        User.objects.create_user(
            login_id="registered-leader",
            nickname="Leader",
            team=cls.official[0],
            is_leader=True,
        )
        cls.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Scope regression"),
            title="Scope regression",
            open_group=1,
            inbound_internal_token_hash=hash_token("scope-primary-internal"),
        )
        second = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Second scope marker"),
            title="Second marker",
            open_group=1,
            inbound_internal_token_hash=hash_token("scope-second-internal"),
        )
        for challenge in [cls.challenge, second]:
            for team in cls.test_teams + [cls.archived[0]]:
                KothPreviewToken.objects.create(
                    challenge=challenge,
                    preview_team_id=team.pk,
                    key_version=0,
                    token_hash=hash_token(f"scope-marker:{challenge.pk}:{team.pk}"),
                )
        for team, score in [
            (cls.official[2], 25),
            (cls.test_teams[0], 5000),
            (cls.archived[0], 9000),
        ]:
            KothSolve.objects.create(
                team=team,
                challenge=cls.challenge,
                earned_score=score,
                solved_at=timezone.now(),
            )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def get(self, **params):
        response = self.client.get("/api/v1/admin/teams", params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["data"]

    def test_default_is_exactly_75_official_teams_even_without_member_accounts(self):
        data = self.get(size=100)
        self.assertEqual(data["team_scope"], "official")
        self.assertEqual(data["total_count"], 75)
        self.assertEqual(
            {row["team_id"] for row in data["teams"]},
            {str(t.pk) for t in self.official},
        )
        self.assertEqual(data["summary"]["total_count"], 75)
        self.assertEqual(data["summary"]["banned_count"], 0)
        self.assertEqual(data["summary"]["total_mileage"], 750)
        self.assertEqual(data["summary"]["leader"]["team_id"], str(self.official[2].pk))
        self.assertEqual(data["summary"]["leader"]["team_score"], 25)
        self.assertTrue(all(row["team_scope"] == "official" for row in data["teams"]))

    def test_test_archived_and_all_scopes_partition_existing_rows_without_duplicate_markers(
        self,
    ):
        expected = {
            "test": self.test_teams,
            "archived": self.archived,
            "all": self.official + self.test_teams + self.archived,
        }
        for scope, teams in expected.items():
            with self.subTest(scope=scope):
                data = self.get(team_scope=scope, size=100)
                self.assertEqual(data["total_count"], len(teams))
                self.assertEqual(
                    {row["team_id"] for row in data["teams"]},
                    {str(t.pk) for t in teams},
                )
                self.assertEqual(
                    data["summary"]["total_mileage"], sum(t.mileage for t in teams)
                )
                self.assertEqual(
                    data["summary"]["banned_count"], sum(t.is_banned for t in teams)
                )
        archived = self.get(team_scope="archived")
        self.assertTrue(
            all(row["team_scope"] == "archived" for row in archived["teams"])
        )

    def test_pagination_totals_and_summary_use_the_entire_filtered_population(self):
        seen = []
        for page in range(1, 5):
            data = self.get(page=page, sort="name")
            self.assertEqual(data["total_count"], 75)
            self.assertEqual(data["summary"]["total_mileage"], 750)
            self.assertEqual(
                data["summary"]["leader"]["team_id"], str(self.official[2].pk)
            )
            seen.extend(row["team_id"] for row in data["teams"])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(len(seen), 75)
        self.assertEqual(self.get(page=5)["teams"], [])

    def test_search_filters_rows_count_mileage_and_leader_together(self):
        data = self.get(search="공식-00", size=1, sort="name")
        self.assertEqual(data["total_count"], 1)
        self.assertEqual(data["summary"]["total_mileage"], 10)
        self.assertEqual(data["summary"]["leader"]["team_id"], str(self.official[0].pk))
        self.assertEqual(self.get(search="테스트")["total_count"], 0)
        self.assertEqual(
            self.get(team_scope="test", search="테스트-0")["total_count"], 1
        )

    def test_empty_search_has_zero_summary_and_no_leader(self):
        data = self.get(search="missing-team")
        self.assertEqual(data["teams"], [])
        self.assertEqual(
            data["summary"],
            {"total_count": 0, "banned_count": 0, "total_mileage": 0, "leader": None},
        )

    def test_invalid_scope_and_camel_case_query_are_rejected(self):
        for params in [
            {"team_scope": "OFFICIAL"},
            {"team_scope": ""},
            {"team_scope": "unknown"},
            {"teamScope": "all"},
        ]:
            with self.subTest(params=params):
                response = self.client.get("/api/v1/admin/teams", params)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data["code"], "INVALID_REQUEST")

    def test_authentication_and_admin_permission_apply_to_every_scope(self):
        for scope in ["official", "test", "archived", "all"]:
            with self.subTest(scope=scope):
                self.client.force_authenticate(None)
                self.assertEqual(
                    self.client.get(
                        "/api/v1/admin/teams", {"team_scope": scope}
                    ).status_code,
                    401,
                )
                self.client.force_authenticate(self.player)
                self.assertEqual(
                    self.client.get(
                        "/api/v1/admin/teams", {"team_scope": scope}
                    ).status_code,
                    403,
                )

    def test_reads_preserve_accounts_password_hashes_membership_scores_mileage_and_markers(
        self,
    ):
        models = [Team, User, KothSolve, KothPreviewToken, TestTeamIdentity]
        before = {
            model: list(model.objects.order_by("pk").values()) for model in models
        }
        for scope in ["official", "test", "archived", "all"]:
            self.get(team_scope=scope, size=100)
        self.assertEqual(
            before,
            {model: list(model.objects.order_by("pk").values()) for model in models},
        )
        member_team = next(
            row
            for row in self.get(size=100)["teams"]
            if row["team_id"] == str(self.official[0].pk)
        )
        self.assertEqual(member_team["member_count"], 2)
        self.assertTrue(member_team["members"][0]["is_leader"])
        self.assertNotIn("password", json.dumps(member_team))

    def test_oversized_pages_are_clamped_without_skipping_matching_teams(self):
        Team.objects.bulk_create(
            [Team(team_name=f"추가-{n:02}", mileage=10) for n in range(35)]
        )
        first = self.get(size=101, sort="name")
        second = self.get(size=101, sort="name", page=2)
        self.assertEqual((first["size"], second["size"]), (100, 100))
        self.assertEqual((len(first["teams"]), len(second["teams"])), (100, 10))
        identifiers = [row["team_id"] for row in first["teams"] + second["teams"]]
        self.assertEqual(len(set(identifiers)), 110)
        for page in [first, second]:
            self.assertEqual(page["total_count"], 110)
            self.assertEqual(page["summary"]["total_count"], 110)
            self.assertEqual(page["summary"]["total_mileage"], 1100)
            self.assertEqual(
                page["summary"]["leader"]["team_id"], str(self.official[2].pk)
            )

    def test_versioned_contract_matches_scope_enum_and_emitted_snake_case_fields(self):
        contract = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "docs/admin-team-list.openapi.json"
            ).read_text()
        )
        operation = contract["paths"]["/api/v1/admin/teams"]["get"]
        parameter = next(
            p for p in operation["parameters"] if p["name"] == "team_scope"
        )
        self.assertEqual(
            set(parameter["schema"]["enum"]), {"official", "test", "archived", "all"}
        )
        self.assertEqual(parameter["schema"]["default"], "official")
        size = next(p for p in operation["parameters"] if p["name"] == "size")
        self.assertNotIn("maximum", size["schema"])
        self.assertEqual(
            contract["components"]["schemas"]["TeamListData"]["properties"]["size"][
                "maximum"
            ],
            100,
        )

        def check_keys(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertRegex(key, r"^[a-z][a-z0-9_]*$")
                    check_keys(item)
            elif isinstance(value, list):
                for item in value:
                    check_keys(item)

        check_keys(self.get(size=100))
