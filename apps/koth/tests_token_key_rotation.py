"""Adversarial regressions for TC-613, including real PostgreSQL contention."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import uuid
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command, CommandError
from django.db import connection, connections, DatabaseError, transaction
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Role, Team, User
from apps.adminpanel.models import AdminEvent
from apps.common.jwt import hash_token, issue_access_token
from .models import (
    KothChallenge,
    KothClub,
    KothPreviewToken,
    KothTeamToken,
    KothTokenKeyRotation,
    KothTokenKeyState,
)
from . import tests as api_fixtures
from .token_keys import activate_key, current_key_version, KeyRotationError
from .tokens import build_preview_token, build_team_token, preview_team_id


class KeyApiMixin:
    def issue(self):
        return self.client.get(
            f"/api/v1/koth/team_token?koth_challenge_id={self.challenge.pk}"
        )

    def verify(self, raw):
        return APIClient().post(
            "/internal/koth/team_tokens/verify",
            {
                "koth_challenge_id": str(self.challenge.pk),
                "team_token": raw,
            },
            format="json",
            HTTP_X_INTERNAL_TOKEN="inbound-secret",
        )

    def rotate(self, **changes):
        options = {
            "actor": "security-operator",
            "reason": "TC-613 cutover",
            "expected_version": 1,
            "operation_id": uuid.uuid4(),
        }
        options.update(changes)
        return activate_key(**options)


class TokenKeyRotationTests(KeyApiMixin, TestCase):
    setUp = api_fixtures.KothApiTests.setUp
    auth = api_fixtures.KothApiTests.auth

    def test_cutover_rejects_old_token_before_any_team_fetch(self):
        self.auth()
        old = self.issue().data["data"]["team_token"]
        with override_settings(KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)):
            receipt, _ = self.rotate()
            self.assertEqual(receipt.version, 2)
            rejected = self.verify(old)
            self.assertEqual(rejected.status_code, 200)
            self.assertIs(rejected.data["data"]["valid"], False)
            new = self.issue()
            self.assertEqual(new.status_code, 200)
            self.assertNotEqual(old, new.data["data"]["team_token"])
            self.assertIs(
                self.verify(new.data["data"]["team_token"]).data["data"]["valid"], True
            )
            # Rotating KOTH does not log users out of the independent JWT session.
            self.assertEqual(self.client.get("/api/v1/auth/me").status_code, 200)

    def test_unregistered_configuration_and_stale_workers_cannot_write_or_verify(self):
        self.auth()
        old_key = settings.KOTH_TEAM_TOKEN_SECRET
        old = self.issue().data["data"]["team_token"]
        with override_settings(KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)):
            for response in [self.issue(), self.verify(old)]:
                self.assertEqual(
                    (response.status_code, response.data["code"]),
                    (503, "KOTH_TOKEN_KEY_UNAVAILABLE"),
                )
            self.rotate()
            fresh = self.issue().data["data"]["team_token"]
            with override_settings(KOTH_TEAM_TOKEN_SECRET=old_key):
                self.assertEqual(self.issue().status_code, 503)
                self.assertEqual(self.verify(old).status_code, 503)
                self.assertEqual(self.verify(fresh).status_code, 503)
            self.assertIs(self.verify(old).data["data"]["valid"], False)
            self.assertIs(self.verify(fresh).data["data"]["valid"], True)
            stored = KothTeamToken.objects.get(team=self.team, challenge=self.challenge)
            self.assertEqual(
                (stored.key_version, stored.token_hash), (2, hash_token(fresh))
            )

    def test_retired_key_and_same_key_cannot_be_registered_as_a_new_rotation(self):
        old_key = settings.KOTH_TEAM_TOKEN_SECRET
        with self.assertRaises(KeyRotationError):
            self.rotate()
        with override_settings(KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)):
            self.rotate()
        with override_settings(KOTH_TEAM_TOKEN_SECRET=old_key):
            with self.assertRaises(KeyRotationError):
                self.rotate(expected_version=2)
        self.assertEqual(KothTokenKeyState.objects.get().version, 2)
        self.assertEqual(KothTokenKeyRotation.objects.count(), 2)

    def test_retry_records_exactly_one_receipt_and_event(self):
        operation_id = uuid.uuid4()
        with override_settings(KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)):
            first, created = self.rotate(operation_id=operation_id)
            again, replayed = self.rotate(operation_id=operation_id)
            self.assertTrue(created)
            self.assertFalse(replayed)
            self.assertEqual(first.pk, again.pk)
            for changes in [
                {"reason": "different reason"},
                {"actor": "another-operator"},
                {"expected_version": 2},
            ]:
                with self.subTest(changes=changes), self.assertRaises(KeyRotationError):
                    self.rotate(operation_id=operation_id, **changes)
        self.assertEqual(KothTokenKeyRotation.objects.count(), 2)
        self.assertEqual(
            AdminEvent.objects.filter(type="SERVICE_KEY_ROTATED").count(), 2
        )

    def test_replaying_old_receipt_does_not_reactivate_its_key(self):
        initial = KothTokenKeyRotation.objects.get(version=1)
        with override_settings(KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)):
            self.rotate()
        receipt, created = activate_key(
            actor=initial.actor,
            reason=initial.reason,
            expected_version=0,
            operation_id=initial.pk,
        )
        self.assertFalse(created)
        self.assertEqual(receipt.version, 1)
        self.assertEqual(KothTokenKeyState.objects.get().version, 2)
        with self.assertRaises(Exception) as caught:
            current_key_version()
        self.assertEqual(caught.exception.code, "KOTH_TOKEN_KEY_UNAVAILABLE")

    def test_audit_or_state_failure_rolls_back_the_entire_cutover(self):
        self.auth()
        old = self.issue().data["data"]["team_token"]
        targets = [
            "apps.koth.token_keys.AdminEvent.objects.create",
            "apps.koth.token_keys.KothTokenKeyRotation.objects.create",
            "apps.koth.token_keys.KothTokenKeyState.save",
        ]
        for target in targets:
            with self.subTest(target=target):
                with override_settings(
                    KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)
                ):
                    with patch(
                        target,
                        side_effect=DatabaseError("private-connection-credential"),
                    ):
                        with self.assertRaises(DatabaseError):
                            self.rotate()
                self.assertEqual(KothTokenKeyState.objects.get().version, 1)
                self.assertEqual(KothTokenKeyRotation.objects.count(), 1)
                self.assertEqual(
                    AdminEvent.objects.filter(type="SERVICE_KEY_ROTATED").count(), 1
                )
                self.assertIs(self.verify(old).data["data"]["valid"], True)

    def test_key_state_database_failure_returns_redacted_503(self):
        self.auth()

        def unavailable(execute, sql, params, many, context):
            if "koth_token_key_state" in sql:
                raise DatabaseError("private-db-credential")
            return execute(sql, params, many, context)

        with connection.execute_wrapper(unavailable):
            response = self.issue()
        self.assertEqual(
            (response.status_code, response.data["code"]),
            (503, "KOTH_TOKEN_KEY_UNAVAILABLE"),
        )
        self.assertNotIn("private-db-credential", str(response.data))
        contract = json.loads(
            (
                Path(settings.BASE_DIR) / "docs/koth-team-token-key.openapi.json"
            ).read_text(encoding="utf-8")
        )
        self.assertIn(
            response.data["code"],
            contract["components"]["schemas"]["key_unavailable"]["properties"]["code"][
                "enum"
            ],
        )

    def test_legacy_preview_identity_remains_excluded_after_rotation(self):
        admin = User.objects.create_user(
            login_id="rotation-preview", nickname="preview", role=Role.ADMIN
        )
        row = KothPreviewToken.objects.create(
            admin=admin,
            challenge=self.challenge,
            preview_team_id=preview_team_id(admin.pk),
            token_hash=hash_token("old-preview"),
            key_version=1,
        )
        with override_settings(KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)):
            self.rotate()
            self.assertFalse(self.verify("old-preview").data["data"]["valid"])
        self.assertTrue(
            KothPreviewToken.objects.filter(
                pk=row.pk, preview_team_id=row.preview_team_id
            ).exists()
        )

    def test_command_output_and_audit_never_include_key_material(self):
        new_key = secrets.token_urlsafe(48)
        stdout = io.StringIO()
        with override_settings(KOTH_TEAM_TOKEN_SECRET=new_key):
            call_command(
                "rotate_koth_team_token_key",
                actor="operator",
                reason="incident response",
                expected_version=1,
                operation_id=str(uuid.uuid4()),
                stdout=stdout,
            )
            call_command("rotate_koth_team_token_key", status=True, stdout=stdout)
        text = stdout.getvalue() + " ".join(
            AdminEvent.objects.values_list("message", flat=True)
        )
        for key in [
            new_key,
            settings.KOTH_TEAM_TOKEN_SECRET,
            settings.JWT_SECRET,
            settings.SECRET_KEY,
        ]:
            self.assertNotIn(key, text)
        self.assertNotIn("fingerprint", text)
        latest = KothTokenKeyRotation.objects.get(version=2)
        self.assertEqual(latest.actor, "operator")
        event = json.loads(latest.audit_event.message)
        self.assertEqual((event["previous_version"], event["version"]), (1, 2))
        self.assertEqual(event["reason"], "incident response")
        self.assertEqual(event["activated_at"], event["previous_revoked_at"])

    def test_command_redacts_database_failure_and_requires_receipt_fields(self):
        with self.assertRaises(CommandError):
            call_command("rotate_koth_team_token_key", actor="operator")
        with patch(
            "apps.koth.management.commands.rotate_koth_team_token_key.activate_key",
            side_effect=DatabaseError("private-db-credential"),
        ):
            with self.assertRaises(CommandError) as caught:
                call_command(
                    "rotate_koth_team_token_key",
                    actor="operator",
                    reason="rotation",
                    expected_version=1,
                    operation_id=str(uuid.uuid4()),
                )
        self.assertNotIn("private-db-credential", str(caught.exception))

    def test_receipt_metadata_rejects_secret_values_and_invalid_versions(self):
        for key in [
            settings.KOTH_TEAM_TOKEN_SECRET,
            settings.JWT_SECRET,
            settings.SECRET_KEY,
        ]:
            with self.assertRaises(KeyRotationError) as caught:
                self.rotate(reason="accidental key " + key)
            self.assertNotIn(key, str(caught.exception))
        for version in [-1, True, 2**63]:
            with self.subTest(version=version), self.assertRaises(KeyRotationError):
                self.rotate(expected_version=version)
        for reason in ["", " ", "line\nbreak", "x" * 501]:
            with (
                self.subTest(reason_length=len(reason)),
                self.assertRaises(KeyRotationError),
            ):
                self.rotate(reason=reason)

    def test_django_admin_can_read_but_cannot_mutate_key_state_or_receipts(self):
        user = User.objects.create_superuser(
            login_id="key-audit-reader", password="test-only-password"
        )
        client = Client()
        client.force_login(user)
        receipt = KothTokenKeyRotation.objects.get(version=1)
        for model, pk in [
            ("kothtokenkeystate", 1),
            ("kothtokenkeyrotation", receipt.pk),
        ]:
            base = f"/admin/koth/{model}/"
            self.assertEqual(client.get(base + str(pk) + "/change/").status_code, 200)
            self.assertEqual(
                client.post(
                    base + str(pk) + "/change/", {"version": 99, "_save": "Save"}
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(base + str(pk) + "/delete/", {"post": "yes"}).status_code,
                403,
            )
            self.assertEqual(
                client.post(base + "add/", {"version": 99}).status_code, 403
            )
        self.assertEqual(KothTokenKeyState.objects.get().version, 1)
        self.assertEqual(KothTokenKeyRotation.objects.count(), 1)


class InitialTokenKeyTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="adopt-token-team")
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="adoption"),
            title="adoption",
            status="ACTIVE",
            open_group=1,
        )

    def test_initial_registration_adopts_only_proven_legacy_hashes(self):
        valid = KothTeamToken.objects.create(
            team=self.team,
            challenge=self.challenge,
            token_hash=hash_token(build_team_token(self.team.pk, self.challenge.pk)),
        )
        other = Team.objects.create(team_name="unproven-team")
        unproven = KothTeamToken.objects.create(
            team=other,
            challenge=self.challenge,
            token_hash=hash_token("foreign-key-token"),
        )
        admin = User.objects.create_user(
            login_id="adoption-admin", nickname="admin", role=Role.ADMIN
        )
        preview = KothPreviewToken.objects.create(
            admin=admin,
            challenge=self.challenge,
            preview_team_id=preview_team_id(admin.pk),
            token_hash=hash_token(build_preview_token(admin.pk, self.challenge.pk)),
        )
        receipt, _ = activate_key(
            actor="operator",
            reason="initial registration",
            expected_version=0,
            operation_id=uuid.uuid4(),
        )
        for row in [valid, preview, unproven]:
            row.refresh_from_db()
        self.assertEqual(
            (valid.key_version, preview.key_version, unproven.key_version), (1, 1, 0)
        )
        self.assertEqual(
            (receipt.adopted_team_token_count, receipt.adopted_preview_token_count),
            (1, 1),
        )

    def test_initial_audit_failure_leaves_legacy_rows_and_state_unchanged(self):
        valid = KothTeamToken.objects.create(
            team=self.team,
            challenge=self.challenge,
            token_hash=hash_token(build_team_token(self.team.pk, self.challenge.pk)),
        )
        with patch(
            "apps.koth.token_keys.AdminEvent.objects.create",
            side_effect=DatabaseError("private"),
        ):
            with self.assertRaises(DatabaseError):
                activate_key(
                    actor="operator",
                    reason="initial registration",
                    expected_version=0,
                    operation_id=uuid.uuid4(),
                )
        valid.refresh_from_db()
        self.assertEqual(valid.key_version, 0)
        self.assertFalse(KothTokenKeyState.objects.exists())
        self.assertFalse(KothTokenKeyRotation.objects.exists())

    def test_http_never_initializes_a_missing_registry(self):
        admin = User.objects.create_user(
            login_id="uninitialized-admin",
            nickname="admin",
            role=Role.ADMIN,
            team=self.team,
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + issue_access_token(admin))
        response = client.get(
            f"/api/v1/koth/team_token?koth_challenge_id={self.challenge.pk}"
        )
        self.assertEqual(
            (response.status_code, response.data["code"]),
            (503, "KOTH_TOKEN_KEY_UNAVAILABLE"),
        )
        self.assertFalse(KothTokenKeyState.objects.exists())
        self.assertFalse(KothTeamToken.objects.exists())


@skipUnless(connection.vendor == "postgresql", "Requires PostgreSQL row locks")
class ConcurrentKeyRotationTests(TransactionTestCase):
    def test_competing_cutovers_commit_one_generation_and_one_receipt(self):
        activate_key(
            actor="operator",
            reason="initial",
            expected_version=0,
            operation_id=uuid.uuid4(),
        )
        barrier = threading.Barrier(2)

        def attempt():
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                activate_key(
                    actor="operator",
                    reason="concurrent rotation",
                    expected_version=1,
                    operation_id=uuid.uuid4(),
                )
                return "committed"
            except KeyRotationError:
                return "conflict"
            finally:
                connections.close_all()

        with override_settings(KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["committed", "conflict"])
        self.assertEqual(KothTokenKeyState.objects.get().version, 2)
        self.assertEqual(KothTokenKeyRotation.objects.count(), 2)
        self.assertEqual(
            AdminEvent.objects.filter(type="SERVICE_KEY_ROTATED").count(), 2
        )


@skipUnless(
    connection.vendor == "postgresql", "Requires a shared PostgreSQL test database"
)
class RestartedKeyWorkerTests(KeyApiMixin, TransactionTestCase):
    setUp = api_fixtures.KothApiTests.setUp
    auth = api_fixtures.KothApiTests.auth

    def request_in_fresh_worker(self, key, action, raw=""):
        configuration = {
            "database": connection.settings_dict,
            "action": action,
            "environment": {
                "DJANGO_SECRET_KEY": settings.SECRET_KEY,
                "JWT_SECRET": settings.JWT_SECRET,
                "KOTH_TEAM_TOKEN_SECRET": key,
                "DJANGO_DEBUG": "True",
            },
            "challenge_id": str(self.challenge.pk),
            "team_token": raw,
            "access_token": issue_access_token(self.user),
        }
        result = subprocess.run(
            [sys.executable, "-m", "apps.koth._token_key_test_worker"],
            cwd=settings.BASE_DIR,
            env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
            input=json.dumps(configuration),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, "Isolated worker did not complete")
        return json.loads(result.stdout)

    def test_restarted_old_worker_cannot_restore_a_token_after_rotation(self):
        self.auth()
        old_key = settings.KOTH_TEAM_TOKEN_SECRET
        old = self.issue().data["data"]["team_token"]
        new_key = secrets.token_urlsafe(48)
        with override_settings(KOTH_TEAM_TOKEN_SECRET=new_key):
            self.rotate()
            self.assertEqual(
                self.request_in_fresh_worker(old_key, "issue")["status"], 503
            )
            self.assertEqual(
                self.request_in_fresh_worker(old_key, "verify", old)["status"], 503
            )
            self.assertIs(
                self.request_in_fresh_worker(new_key, "verify", old)["valid"], False
            )
            new = self.issue().data["data"]["team_token"]
            self.assertEqual(
                self.request_in_fresh_worker(old_key, "issue")["status"], 503
            )
            self.assertIs(
                self.request_in_fresh_worker(new_key, "verify", new)["valid"], True
            )
            self.assertIs(
                self.request_in_fresh_worker(new_key, "verify", old)["valid"], False
            )
            self.assertEqual(
                KothTeamToken.objects.get(
                    team=self.team, challenge=self.challenge
                ).token_hash,
                hash_token(new),
            )

    def test_waiting_old_worker_rechecks_the_generation_after_cutover_commits(self):
        self.auth()
        old_key = settings.KOTH_TEAM_TOKEN_SECRET
        old = self.issue().data["data"]["team_token"]
        new_key = secrets.token_urlsafe(48)
        with ThreadPoolExecutor(max_workers=1) as executor:
            with transaction.atomic():
                KothTokenKeyState.objects.select_for_update().get(pk=1)
                request = executor.submit(
                    self.request_in_fresh_worker, old_key, "issue"
                )
                waiting = False
                for _ in range(120):
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_stat_clear_snapshot()")
                        cursor.execute(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                            "AND wait_event_type = 'Lock' AND query LIKE %s)",
                            ["%koth_token_key_state%"],
                        )
                        waiting = cursor.fetchone()[0]
                    if waiting:
                        break
                    time.sleep(0.1)
                self.assertTrue(
                    waiting, "Fresh request did not reach the key-state lock"
                )
                with override_settings(KOTH_TEAM_TOKEN_SECRET=new_key):
                    self.rotate()
            self.assertEqual(request.result(timeout=30)["status"], 503)
        with override_settings(KOTH_TEAM_TOKEN_SECRET=new_key):
            self.assertIs(self.verify(old).data["data"]["valid"], False)
            self.assertEqual(
                KothTeamToken.objects.get(
                    team=self.team, challenge=self.challenge
                ).token_hash,
                hash_token(old),
            )

    def test_reverse_migration_cannot_erase_the_key_generation_and_audit_history(self):
        # A dependent app can roll back before the protected key migration fails.
        # Restore every app even when the expected IrreversibleError is raised.
        latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
        self.addCleanup(lambda: MigrationExecutor(connection).migrate(latest))
        with self.assertRaises(IrreversibleError):
            call_command(
                "migrate",
                "koth",
                "0013_preview_tokens",
                interactive=False,
                stdout=io.StringIO(),
            )
        self.assertEqual(KothTokenKeyState.objects.get().version, 1)
        self.assertEqual(KothTokenKeyRotation.objects.count(), 1)
        self.assertEqual(
            AdminEvent.objects.filter(type="SERVICE_KEY_ROTATED").count(), 1
        )
