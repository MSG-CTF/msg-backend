import importlib
import re
import uuid
from unittest.mock import patch

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, TestTeamIdentity, User
from apps.common.jwt import hash_token
from apps.koth import services
from apps.koth.models import KothChallenge, KothClub, KothPreviewToken


class QaTeamClassificationTests(TestCase):
    def assert_test_team(self, team):
        self.assertTrue(TestTeamIdentity.objects.filter(pk=team.pk).exists())
        self.assertFalse(services.eligible_teams().filter(pk=team.pk).exists())

    def test_existing_direct_fixture_creation_is_automatically_classified(self):
        team = Team.objects.create(team_name="QA12-ROLLOUT-f60b7620-0")
        self.assert_test_team(team)
        self.assertEqual(KothChallenge.objects.count(), 0)
        self.assertEqual(KothPreviewToken.objects.count(), 0)

    def test_bulk_creation_classifies_only_the_reserved_namespace(self):
        names = [
            "QA12-LIFECYCLE-16bd5bf8-11",
            "qa24-recovery-DEADBEEF-9",
            "QA 연구회",
            "테스트팀",
        ]
        teams = Team.objects.bulk_create([Team(team_name=name) for name in names])
        self.assertEqual(
            set(TestTeamIdentity.objects.values_list("pk", flat=True)),
            {team.pk for team in teams[:2]},
        )
        self.assertEqual(
            set(services.eligible_teams().values_list("pk", flat=True)),
            {team.pk for team in teams[2:]},
        )

    def test_regular_names_and_near_matches_remain_official_without_members(self):
        names = [
            "QA",
            "QA12-ROLLOUT-deadbee-0",
            "QA12-ROLLOUT-deadbeeff-0",
            "XQA12-ROLLOUT-deadbeef-0",
            "QA12-ROLLOUT-deadbeef-0\n",
            "QA12-ROLLOUT-deadbeef-0 ",
        ]
        for name in names:
            with self.subTest(name=name):
                team = Team.objects.create(team_name=name)
                self.assertFalse(TestTeamIdentity.objects.filter(pk=team.pk).exists())
                self.assertTrue(services.eligible_teams().filter(pk=team.pk).exists())

    def test_explicit_factory_classifies_arbitrary_qa_names(self):
        team = Team.objects.create_qa_team(team_name="운영 회귀 전용")
        self.assert_test_team(team)
        self.assertEqual(
            TestTeamIdentity.objects.get(pk=team.pk).registration_source, "EXPLICIT_QA"
        )

    def test_factory_rolls_back_team_when_classification_fails(self):
        with patch("apps.accounts.models.TestTeamIdentity.objects.using") as using:
            using.return_value.get_or_create.side_effect = IntegrityError(
                "classification unavailable"
            )
            with self.assertRaises(IntegrityError):
                Team.objects.create_qa_team(team_name="rollback-qa")
        self.assertFalse(Team.objects.filter(team_name="rollback-qa").exists())

    def test_outer_transaction_rollback_removes_team_and_identity(self):
        team_id = uuid.uuid4()
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                Team.objects.create(
                    team_id=team_id, team_name="QA12-ROLLOUT-deadbeef-1"
                )
                raise RuntimeError("member creation failed")
        self.assertFalse(Team.objects.filter(pk=team_id).exists())
        self.assertFalse(TestTeamIdentity.objects.filter(pk=team_id).exists())

    def test_recreating_same_name_with_a_new_uuid_is_classified_again(self):
        first = Team.objects.create(team_name="QA12-ROLLOUT-deadbeef-2")
        old_id = first.pk
        first.delete()
        second = Team.objects.create(team_name="QA12-ROLLOUT-deadbeef-2")
        self.assertNotEqual(old_id, second.pk)
        self.assert_test_team(second)
        self.assertTrue(TestTeamIdentity.objects.filter(pk=old_id).exists())

    def test_rename_and_bulk_update_cannot_silently_promote_qa(self):
        team = Team.objects.create(team_name="ordinary-rename")
        Team.objects.filter(pk=team.pk).update(team_name="QA12-RENAME-deadbeef-1")
        self.assert_test_team(team)
        team.refresh_from_db()
        team.team_name = "ordinary-again"
        Team.objects.bulk_update([team], ["team_name"])
        self.assert_test_team(team)
        self.assertEqual(TestTeamIdentity.objects.filter(pk=team.pk).count(), 1)

    def test_conflict_ignored_insert_does_not_classify_the_existing_official_team(self):
        official = Team.objects.create(team_name="collision-official")
        Team.objects.bulk_create(
            [Team(team_id=official.pk, team_name="QA12-COLLISION-deadbeef-1")],
            ignore_conflicts=True,
        )
        self.assertTrue(services.eligible_teams().filter(pk=official.pk).exists())
        self.assertFalse(TestTeamIdentity.objects.filter(pk=official.pk).exists())
        self.assertEqual(
            Team.objects.get(pk=official.pk).team_name, "collision-official"
        )

    def test_raw_sql_insert_is_classified_and_matches_python_namespace(self):
        migration = importlib.import_module(
            "apps.accounts.migrations.0004_test_team_identity"
        )
        samples = [
            "QA12-ROLLOUT-deadbeef-0",
            "qa12-restore-DEADBEEF-9",
            "QA12-ROLLOUT-deadbeef-0\n",
            "QA 연구회",
        ]
        for name in samples:
            team_id = uuid.uuid4()
            with connection.cursor() as cursor:
                cursor.execute("SELECT %s ~ %s", [name, migration.QA_PATTERN])
                matches = cursor.fetchone()[0]
                self.assertEqual(
                    matches, re.fullmatch(migration.QA_PATTERN, name) is not None
                )
                cursor.execute(
                    "INSERT INTO teams (team_id, team_name, team_score, mileage, is_banned, created_at, updated_at) VALUES (%s, %s, 0, 0, FALSE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                    [team_id, name],
                )
            self.assertEqual(
                TestTeamIdentity.objects.filter(pk=team_id).exists(), matches
            )

    def test_legacy_preview_bulk_registration_persists_after_token_and_challenge_cleanup(
        self,
    ):
        team = Team.objects.create(team_name="legacy-test-identity")
        challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="legacy-classification"),
            title="Legacy",
            open_group=1,
        )
        KothPreviewToken.objects.bulk_create(
            [
                KothPreviewToken(
                    challenge=challenge,
                    preview_team_id=team.pk,
                    key_version=0,
                    token_hash=hash_token("inert-classification-fixture"),
                )
            ]
        )
        self.assert_test_team(team)
        challenge.delete()
        self.assertEqual(KothPreviewToken.objects.count(), 0)
        self.assert_test_team(team)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
)
class QaAdminAccountCreationTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            login_id="qa-create-admin", nickname="Admin", role=Role.ADMIN
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def payload(self, team_name, login_id="qa-account-member"):
        return {
            "login_id": login_id,
            "password": "Fixture!9mR_47s",
            "nickname": "QA member",
            "role": "PARTICIPANT",
            "is_leader": True,
            "team_name": team_name,
        }

    def test_admin_creation_requires_no_additional_client_flag(self):
        response = self.client.post(
            "/api/v1/admin/accounts",
            self.payload("QA12-ACCOUNT-deadbeef-0"),
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        team_id = response.data["data"]["team_id"]
        user = User.objects.get(pk=response.data["data"]["user_id"])
        self.assertEqual(user.team_id, uuid.UUID(team_id))
        self.assertTrue(user.check_password(self.payload("")["password"]))
        self.assertEqual((user.role, user.is_leader), (Role.PARTICIPANT, True))
        self.assertEqual(
            self.client.get("/api/v1/admin/teams").data["data"]["total_count"], 0
        )
        test = self.client.get("/api/v1/admin/teams", {"team_scope": "test"}).data[
            "data"
        ]
        self.assertEqual(test["total_count"], 1)
        self.assertEqual(test["teams"][0]["team_scope"], "test")

    def test_ordinary_account_creation_stays_official(self):
        response = self.client.post(
            "/api/v1/admin/accounts", self.payload("QA 연구회"), format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            self.client.get("/api/v1/admin/teams").data["data"]["total_count"], 1
        )
        self.assertEqual(TestTeamIdentity.objects.count(), 0)

    def test_late_member_failure_does_not_leave_an_unclassified_or_orphan_team(self):
        with patch(
            "apps.adminpanel.views.User.objects.create_user",
            side_effect=IntegrityError("duplicate account"),
        ):
            response = self.client.post(
                "/api/v1/admin/accounts",
                self.payload("QA12-ROLLBACK-deadbeef-0"),
                format="json",
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(Team.objects.count(), 0)
        self.assertEqual(TestTeamIdentity.objects.count(), 0)


class QaClassificationMigrationTests(TransactionTestCase):
    def test_backfill_preserves_business_rows_and_orphan_preview_identities(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        executor.migrate([("accounts", "0003_team_koth_division")])
        apps = executor.loader.project_state(
            [
                ("accounts", "0003_team_koth_division"),
                ("koth", "0014_kothpreviewtoken_key_version_and_more"),
            ]
        ).apps
        OldTeam = apps.get_model("accounts", "Team")
        OldUser = apps.get_model("accounts", "User")
        Preview = apps.get_model("koth", "KothPreviewToken")
        Club = apps.get_model("koth", "KothClub")
        Challenge = apps.get_model("koth", "KothChallenge")
        ordinary = OldTeam.objects.create(team_name="migration-official", mileage=71)
        generated = OldTeam.objects.create(
            team_name="QA12-MIGRATION-deadbeef-0", mileage=17
        )
        legacy = OldTeam.objects.create(team_name="migration-legacy", is_banned=True)
        OldUser.objects.create(
            login_id="migration-member",
            nickname="Member",
            password="!",
            team=ordinary,
            is_leader=True,
        )
        challenge = Challenge.objects.create(
            club=Club.objects.create(name="Migration"), title="Migration", open_group=1
        )
        orphan_id = uuid.uuid4()
        for team_id in [legacy.pk, orphan_id]:
            Preview.objects.create(
                challenge=challenge,
                preview_team_id=team_id,
                token_hash=hash_token(str(team_id)),
            )
        before = {
            model._meta.label: list(model.objects.order_by("pk").values())
            for model in [OldTeam, OldUser, Preview]
        }
        MigrationExecutor(connection).migrate(latest)
        self.assertEqual(
            set(TestTeamIdentity.objects.values_list("pk", flat=True)),
            {generated.pk, legacy.pk, orphan_id},
        )
        after = {
            model._meta.label: list(model.objects.order_by("pk").values())
            for model in [OldTeam, OldUser, Preview]
        }
        self.assertEqual(before, after)
        self.assertTrue(services.eligible_teams().filter(pk=ordinary.pk).exists())
