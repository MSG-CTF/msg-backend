"""The former purge name must use the same fenced, auditable key cutover."""

import io
import json
import secrets
import uuid

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.adminpanel.models import AdminEvent
from .models import KothTeamToken, KothTokenKeyRotation
from . import tests as api_fixtures


class RotateKothTeamTokenSecretTests(TestCase):
    setUp = api_fixtures.KothApiTests.setUp
    auth = api_fixtures.KothApiTests.auth

    def issue(self):
        self.auth()
        return self.client.get(
            f"/api/v1/koth/team_token?koth_challenge_id={self.challenge.pk}"
        ).data["data"]["team_token"]

    def test_refuses_without_receipt_metadata(self):
        self.issue()
        with self.assertRaises(CommandError):
            call_command("rotate_koth_team_token_secret")
        self.assertEqual(KothTeamToken.objects.count(), 1)

    def test_obsolete_purge_flag_cannot_delete_current_tokens(self):
        self.issue()
        with self.assertRaises(CommandError):
            call_command("rotate_koth_team_token_secret", "--yes-i-rotated-the-secret")
        self.assertEqual(KothTeamToken.objects.count(), 1)

    def test_same_key_cannot_be_pretended_to_have_rotated(self):
        self.issue()
        with self.assertRaises(CommandError):
            call_command(
                "rotate_koth_team_token_secret",
                actor="operator",
                reason="must use a new key",
                expected_version=1,
                operation_id=str(uuid.uuid4()),
                stdout=io.StringIO(),
            )
        self.assertEqual(KothTeamToken.objects.count(), 1)
        self.assertEqual(KothTokenKeyRotation.objects.count(), 1)

    def test_alias_retry_preserves_tokens_reissued_after_the_cutover(self):
        old = self.issue()
        operation_id = str(uuid.uuid4())
        with override_settings(KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)):
            output = io.StringIO()
            options = {
                "actor": "operator",
                "reason": "alias cutover",
                "expected_version": 1,
                "operation_id": operation_id,
            }
            call_command("rotate_koth_team_token_secret", **options, stdout=output)
            self.assertTrue(json.loads(output.getvalue())["created"])
            fresh = self.issue()
            self.assertNotEqual(old, fresh)
            retry = io.StringIO()
            call_command("rotate_koth_team_token_secret", **options, stdout=retry)
            self.assertFalse(json.loads(retry.getvalue())["created"])
            response = APIClient().post(
                "/internal/koth/team_tokens/verify",
                {"koth_challenge_id": str(self.challenge.pk), "team_token": fresh},
                format="json",
                HTTP_X_INTERNAL_TOKEN="inbound-secret",
            )
            self.assertEqual(response.status_code, 200)
            self.assertIs(response.data["data"]["valid"], True)
        self.assertEqual(KothTokenKeyRotation.objects.count(), 2)
        self.assertEqual(
            AdminEvent.objects.filter(type="SERVICE_KEY_ROTATED").count(), 2
        )
