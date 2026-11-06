import json
import time
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.core.cache import cache
from django.http import HttpResponse
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.common.jwt import hash_token
from .models import KothChallenge, KothClub
from .preview_game import GAME_COOKIE, _callback_key, _session_key
from . import tests_preview_game as fixture_support


@override_settings(KOTH_ADMIN_PREVIEW_ORIGINS=["https://game.example"],
                   RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
class PreviewCallbackTests(TestCase):
    setUp = fixture_support.PreviewGameTests.setUp
    headers = fixture_support.PreviewGameTests.headers

    def verify(self, raw=None, challenge=None, secret="preview-native-internal"):
        return APIClient().post("/internal/koth/team_tokens/verify", {
            "koth_challenge_id": str((challenge or self.challenge).pk),
            "team_token": raw or self.callback}, format="json", HTTP_X_INTERNAL_TOKEN=secret)

    def login(self, native_status=200):
        def native(request, challenge, uri, body):
            self.callback = json.loads(body)["team_token"]
            self.assertNotEqual(self.callback, self.raw_token)
            self.assertTrue(self.callback.startswith("koth_preview_"))
            result = self.verify()
            self.assertEqual(result.status_code, 200)
            self.assertIs(result.data["data"]["valid"], True)
            self.assertEqual(result.data["data"]["team_id"], str(self.team.pk))
            response = HttpResponse(json.dumps({"data": {"team_id": str(self.team.pk)}}),
                status=native_status, content_type="application/json")
            response.set_cookie("session", "private-native-cookie", secure=True, httponly=True, samesite="Lax")
            return response
        with patch("apps.koth.preview_game._native_request", side_effect=native):
            return self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers())

    def test_native_revalidation_survives_login_without_a_lasting_ordinary_token_permit(self):
        self.assertEqual(self.login().status_code, 200)
        self.assertIs(self.verify().data["data"]["valid"], True)
        self.assertIs(self.verify(self.raw_token).data["data"]["valid"], False)
        with patch("apps.koth.preview_game.time.time", return_value=time.time()+20):
            self.assertIs(self.verify().data["data"]["valid"], True)
        self.assertEqual(self.challenge.status, "SCHEDULED")

    def test_expired_preview_revokes_its_gate_and_callback(self):
        self.assertEqual(self.login().status_code, 200)
        gate = _session_key(self.client.cookies[GAME_COOKIE].value)
        key = _callback_key(self.callback)
        record = cache.get(key)
        record["expires_at"] = time.time()-1
        cache.set(key, record, timeout=300)
        self.assertIs(self.verify().data["data"]["valid"], False)
        self.assertIsNone(cache.get(gate))
        self.assertIsNone(cache.get(_callback_key(self.callback)))

    def test_logout_or_missing_gate_invalidates_the_native_callback(self):
        self.assertEqual(self.login().status_code, 200)
        cache.delete(_session_key(self.client.cookies[GAME_COOKIE].value))
        self.assertIs(self.verify().data["data"]["valid"], False)

    def test_admin_demotion_cannot_be_undone_by_restoring_the_role(self):
        self.assertEqual(self.login().status_code, 200)
        self.admin.role = "PARTICIPANT"; self.admin.save(update_fields=["role"])
        self.assertIs(self.verify().data["data"]["valid"], False)
        self.admin.role = "ADMIN"; self.admin.save(update_fields=["role"])
        self.assertIs(self.verify().data["data"]["valid"], False)

    def test_ban_and_team_changes_revoke_the_native_capability(self):
        self.assertEqual(self.login().status_code, 200)
        self.team.is_banned = True; self.team.save()
        self.assertIs(self.verify().data["data"]["valid"], False)
        self.team.is_banned = False; self.team.save()
        self.assertIs(self.verify().data["data"]["valid"], False)

    def test_other_challenge_cannot_use_or_revoke_the_original_capability(self):
        self.assertEqual(self.login().status_code, 200)
        other = KothChallenge.objects.create(club=KothClub.objects.create(name="Other callback"),
            title="Other callback", repository_slug="koth-card-battle-arena", status="SCHEDULED", open_group=2,
            inbound_internal_token_hash=hash_token("other-callback-internal"))
        result = self.verify(challenge=other, secret="other-callback-internal")
        self.assertEqual(result.status_code, 200)
        self.assertIs(result.data["data"]["valid"], False)
        self.assertIs(self.verify().data["data"]["valid"], True)

    def test_native_login_failure_leaves_no_callback_authority(self):
        self.assertEqual(self.login(native_status=401).status_code, 401)
        self.assertIs(self.verify().data["data"]["valid"], False)

    def test_subject_token_replacement_invalidates_the_native_capability(self):
        self.assertEqual(self.login().status_code, 200)
        self.token.token_hash = hash_token("different-subject-token"); self.token.save(update_fields=["token_hash"])
        self.assertIs(self.verify().data["data"]["valid"], False)

    def test_untrusted_game_cannot_verify_a_callback(self):
        self.assertEqual(self.login().status_code, 200)
        result = self.verify(secret="wrong-native-secret")
        self.assertEqual(result.status_code, 401)

    def test_authority_cache_failure_does_not_reach_the_native_login(self):
        with patch("apps.koth.preview_game.cache.set", side_effect=RuntimeError("cache unavailable")), \
                patch("apps.koth.preview_game._native_request") as native:
            result = self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers())
        self.assertEqual(result.status_code, 503)
        native.assert_not_called()
