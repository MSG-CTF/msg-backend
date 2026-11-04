import io
import secrets
import uuid

from cryptography.fernet import Fernet
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.common.key_lifecycle import key_id, ServiceKeyUnavailable
from apps.instances.models import RuntimeSecret, ServiceKeyRotation, ServiceKeyState
from apps.instances.runtime_secrets import create_runtime_secret, decrypt_runtime_secret
from apps.koth import tests as koth_fixtures
from apps.koth.models import KothTeamToken, KothTokenKeyState

OLD_KOTH = secrets.token_urlsafe(48)
NEW_KOTH = secrets.token_urlsafe(48)
OLD_RUNTIME = Fernet.generate_key().decode()
NEW_RUNTIME = Fernet.generate_key().decode()


@override_settings(
    KOTH_TEAM_TOKEN_SECRET=OLD_KOTH, RUNTIME_SECRET_ENCRYPTION_KEYS=[OLD_RUNTIME]
)
class KeyLifecycleTests(TestCase):
    setUp = koth_fixtures.KothApiTests.setUp
    auth = koth_fixtures.KothApiTests.auth

    def activate(self, purpose, reencrypt=False, **kwargs):
        output = io.StringIO()
        if purpose == "koth":
            kwargs.setdefault(
                "expected_version", KothTokenKeyState.objects.get(pk=1).version
            )
            kwargs.setdefault("operation_id", str(uuid.uuid4()))
        call_command(
            "activate_service_key",
            purpose=purpose,
            actor="tc622-review",
            reason="isolated rotation test",
            reencrypt=reencrypt,
            stdout=output,
            **kwargs,
        )

    def issue(self):
        self.auth()
        return self.client.get(
            f"/api/v1/koth/team_token?koth_challenge_id={self.challenge.pk}"
        )

    def verify(self, token):
        return APIClient().post(
            "/internal/koth/team_tokens/verify",
            {"koth_challenge_id": str(self.challenge.pk), "team_token": token},
            format="json",
            HTTP_X_INTERNAL_TOKEN="inbound-secret",
        )

    def test_rotation_revokes_old_token_and_rejects_stale_issuer_and_verifier(self):
        old = self.issue().data["data"]["team_token"]
        with override_settings(KOTH_TEAM_TOKEN_SECRET=NEW_KOTH):
            self.activate("koth")
            self.assertFalse(self.verify(old).data["data"]["valid"])
            new = self.issue().data["data"]["team_token"]
            self.assertTrue(self.verify(new).data["data"]["valid"])
        self.assertEqual(self.issue().status_code, 503)
        self.assertEqual(self.verify(old).status_code, 503)
        self.assertEqual(
            ServiceKeyState.objects.get(pk="koth").active_key_id,
            key_id("koth", NEW_KOTH),
        )
        self.assertEqual(ServiceKeyRotation.objects.filter(purpose="koth").count(), 2)
        with self.assertRaises(CommandError):
            self.activate("koth")

    def test_runtime_rotation_reencrypts_existing_data_and_rejects_old_worker(self):
        self.activate("runtime", True)
        # Use a separate ordinary challenge; KOTH's model is not a RuntimeSecret FK.
        from apps.challenge.models import Challenge

        challenge = Challenge.objects.create(
            title="TC622",
            challenge_slug="tc622-key-fixture",
            category="WEB",
            difficulty="EASY",
            score=1,
            description="fixture",
            flag_hash="0" * 64,
        )
        row = create_runtime_secret(
            challenge, "internal_token", "synthetic-review-value", "tc622-review"
        )
        old_ciphertext = row.encrypted_value
        with override_settings(
            RUNTIME_SECRET_ENCRYPTION_KEYS=[NEW_RUNTIME, OLD_RUNTIME]
        ):
            self.activate("runtime", True)
        row.refresh_from_db()
        self.assertNotEqual(row.encrypted_value, old_ciphertext)
        with override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[NEW_RUNTIME]):
            self.assertEqual(decrypt_runtime_secret(row), "synthetic-review-value")
        with self.assertRaises(ServiceKeyUnavailable):
            decrypt_runtime_secret(row)
        self.assertEqual(
            ServiceKeyRotation.objects.filter(purpose="runtime")
            .latest("created_at")
            .reencrypted_rows,
            1,
        )

    def test_failed_reencryption_rolls_back_rows_state_and_audit(self):
        self.activate("runtime", True)
        from apps.challenge.models import Challenge

        challenge = Challenge.objects.create(
            title="TC622",
            challenge_slug="tc622-rollback-fixture",
            category="WEB",
            difficulty="EASY",
            score=1,
            description="fixture",
            flag_hash="0" * 64,
        )
        row = create_runtime_secret(
            challenge, "internal_token", "synthetic-rollback-value", "tc622-review"
        )
        original = row.encrypted_value
        count = ServiceKeyRotation.objects.count()
        with override_settings(RUNTIME_SECRET_ENCRYPTION_KEYS=[NEW_RUNTIME]):
            with self.assertRaises(CommandError):
                self.activate("runtime", True)
        row.refresh_from_db()
        self.assertEqual(row.encrypted_value, original)
        self.assertEqual(
            ServiceKeyState.objects.get(pk="runtime").active_key_id,
            key_id("runtime", OLD_RUNTIME),
        )
        self.assertEqual(ServiceKeyRotation.objects.count(), count)

    @override_settings(SERVICE_KEY_STATE_REQUIRED=True)
    def test_missing_authority_fails_closed(self):
        KothTokenKeyState.objects.all().delete()
        self.assertEqual(self.issue().status_code, 503)
        self.assertFalse(KothTeamToken.objects.exists())

    def test_same_operation_activation_is_idempotent(self):
        operation_id = str(uuid.uuid4())
        with override_settings(KOTH_TEAM_TOKEN_SECRET=NEW_KOTH):
            self.activate("koth", expected_version=1, operation_id=operation_id)
            self.activate("koth", expected_version=1, operation_id=operation_id)
        self.assertEqual(ServiceKeyRotation.objects.filter(purpose="koth").count(), 2)
        self.assertEqual(ServiceKeyState.objects.get(pk="koth").generation, 2)
