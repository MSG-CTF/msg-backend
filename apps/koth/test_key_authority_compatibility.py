"""Regression coverage for the already-deployed TC-622 key authority."""

import io
import secrets
import uuid

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.common.key_lifecycle import active_service_key, key_id
from apps.common.exceptions import KothTokenKeyUnavailable
from apps.instances.models import ServiceKeyRotation, ServiceKeyState
from .models import KothTokenKeyRotation, KothTokenKeyState
from .token_keys import activate_key, KeyRotationError


class KeyAuthorityCompatibilityTests(TestCase):
    def setUp(self):
        self.current = secrets.token_urlsafe(48)
        self.retired = secrets.token_urlsafe(48)
        self.configuration = override_settings(KOTH_TEAM_TOKEN_SECRET=self.current)
        self.configuration.enable()
        self.addCleanup(self.configuration.disable)
        for generation, key in enumerate([self.retired, self.current], 1):
            ServiceKeyRotation.objects.create(
                purpose="koth",
                active_key_id=key_id("koth", key),
                generation=generation,
                actor="prior-operator",
                reason="prior activation",
            )
        ServiceKeyState.objects.create(
            purpose="koth",
            active_key_id=key_id("koth", self.current),
            generation=2,
            activated_at=timezone.now(),
        )

    def register(self):
        return activate_key(
            actor="operator",
            reason="adopt TC-622 authority",
            expected_version=0,
            operation_id=uuid.uuid4(),
        )

    def test_initial_registration_adopts_current_legacy_authority_without_rewriting_history(
        self,
    ):
        receipt, created = self.register()
        self.assertTrue(created)
        self.assertEqual(receipt.version, 1)
        self.assertEqual(ServiceKeyState.objects.get(pk="koth").generation, 2)
        self.assertEqual(ServiceKeyRotation.objects.filter(purpose="koth").count(), 2)
        with active_service_key("koth") as version:
            self.assertEqual(version, 1)

    def test_key_retired_before_tc613_cannot_be_registered(self):
        with override_settings(KOTH_TEAM_TOKEN_SECRET=self.retired):
            with self.assertRaises(KeyRotationError):
                self.register()
        self.assertFalse(KothTokenKeyState.objects.exists())
        self.assertFalse(KothTokenKeyRotation.objects.exists())

    def test_rotation_pins_legacy_workers_and_preserves_previous_retirement(self):
        self.register()
        fresh = secrets.token_urlsafe(48)
        with override_settings(KOTH_TEAM_TOKEN_SECRET=fresh):
            activate_key(
                actor="operator",
                reason="forward cutover",
                expected_version=1,
                operation_id=uuid.uuid4(),
            )
        state = ServiceKeyState.objects.get(pk="koth")
        self.assertEqual(state.active_key_id, key_id("koth", fresh))
        self.assertEqual(state.generation, 3)
        with self.assertRaises(KothTokenKeyUnavailable):
            with active_service_key("koth"):
                self.fail("Stale key was authorized")

    def test_legacy_command_cannot_bypass_cutover_metadata(self):
        with self.assertRaises(CommandError):
            call_command(
                "activate_service_key",
                purpose="koth",
                actor="operator",
                reason="no operation id",
                stdout=io.StringIO(),
            )
        self.assertFalse(KothTokenKeyState.objects.exists())
        self.assertEqual(ServiceKeyState.objects.get(pk="koth").generation, 2)
