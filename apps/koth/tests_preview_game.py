import json
import hashlib
import hmac
import secrets
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from cryptography.fernet import Fernet
from itsdangerous import URLSafeTimedSerializer

from django.core.cache import cache
from django.http import HttpResponse
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.common.jwt import hash_token, issue_access_token
from .admin_preview import COOKIE_NAME
from .models import KothChallenge, KothClub, KothPreviewToken, KothTeamToken, KothEnvironmentConfig
from .environment import create_version
from .preview_game import GAME_COOKIE, KEY_PREFIX, _session_key, permits_token_verification
from .tokens import build_team_token, preview_team_id


@override_settings(KOTH_ADMIN_PREVIEW_ORIGINS=["https://game.example"],
                   RUNTIME_SECRET_ENCRYPTION_KEYS=[Fernet.generate_key().decode()])
class PreviewGameTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="preview-game-team")
        self.admin = User.objects.create_user(login_id="preview-game-admin", nickname="admin", role="ADMIN", team=self.team)
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Preview native QA"), title="Native QA",
            repository_slug="koth-jenga", challenge_url="https://game.example", status="SCHEDULED",
            open_group=1, inbound_internal_token_hash=hash_token("preview-native-internal"))
        self.raw_token = build_team_token(self.team.pk, self.challenge.pk)
        self.token = KothTeamToken.objects.create(team=self.team, challenge=self.challenge, token_hash=hash_token(self.raw_token))
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.admin))
        result = self.client.post(f"/api/v1/admin/koth/challenges/{self.challenge.pk}/preview_sessions", {}, format="json")
        self.assertEqual(result.status_code, 200)
        ticket = parse_qs(urlsplit(result.data["data"]["preview_url"]).fragment)["ticket"][0]
        self.client.credentials()
        self.client.cookies[COOKIE_NAME] = ticket
        self.proxy = "/api/v1/koth/preview_sessions/game_proxy"
        self.authorize = "/api/v1/koth/preview_sessions/game_authorize"

    def headers(self, uri="/auth"):
        return {"HTTP_X_KOTH_PREVIEW_CHALLENGE": str(self.challenge.pk),
                "HTTP_X_KOTH_PREVIEW_ORIGIN": "https://game.example", "HTTP_X_KOTH_PREVIEW_URI": uri,
                "HTTP_X_KOTH_PREVIEW_METHOD": "GET", "HTTP_ORIGIN": "https://game.example"}

    def native_response(self, request, challenge, uri, body):
        self.assertTrue(permits_token_verification(self.challenge, self.token))
        native = APIClient().post("/internal/koth/team_tokens/verify", {
            "koth_challenge_id": str(challenge.pk), "team_token": self.raw_token},
            format="json", HTTP_X_INTERNAL_TOKEN="preview-native-internal")
        self.assertEqual(native.status_code, 200)
        self.assertIs(native.data["data"]["valid"], True)
        response = HttpResponse(json.dumps({"data": {"team_id": str(self.team.pk)}}), content_type="application/json")
        response.set_cookie("session", "native-session-credential", httponly=True)
        return response

    def login(self):
        with patch("apps.koth.preview_game._native_request", side_effect=self.native_response):
            response = self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers())
        self.assertEqual(response.status_code, 200)
        return response

    def authorize_session(self):
        return self.client.get(self.authorize, **self.headers("/state"))

    def latest_cookie(self, timestamp=None, secret=None, **changes):
        if not hasattr(self, "jenga_key"):
            self.jenga_key = secrets.token_urlsafe(40)
            self.challenge.source_revision = "e" * 40
            self.challenge.save(update_fields=["source_revision"])
            def entry(value, kind):
                return {"value": value, "kind": kind, "required": True, "locked": True, "operator_override": True}
            settings = {"containers": [{"name": "jenga", "image": "ghcr.io/msg-ctf/challenges/koth-jenga/jenga@sha256:" + "a" * 64,
                "entries": {"SESSION_SECRET": entry(self.jenga_key, "secret"),
                            "KOTH_MATCH_ID": entry("preview-match", "env"),
                            "SESSION_EPOCH": entry("preview-epoch", "env")}}]}
            self.jenga_version = create_version(self.challenge, settings, self.challenge.source_revision,
                                                 "test", uuid.uuid4(), "a" * 64)
            KothEnvironmentConfig.objects.create(challenge=self.challenge, target_id="test", state="READY", applied_version=self.jenga_version)
        payload = {"team_id": str(self.team.pk), "team_name": self.team.team_name, "match_id": "preview-match", "generation": 1,
                   "_permanent": True, "marker": hmac.new(self.jenga_key.encode(), b"preview-match:preview-epoch", hashlib.sha256).hexdigest()}
        payload.update(changes)
        serializer = URLSafeTimedSerializer(secret or self.jenga_key, salt="cookie-session", serializer=json,
            signer_kwargs={"key_derivation": "hmac"})
        with patch("itsdangerous.timed.time.time", return_value=timestamp or int(time.time())):
            return serializer.dumps(payload)

    def login_latest(self, cookie=None):
        value = cookie or self.latest_cookie(timestamp=int(time.time()) - 2)
        def response(*args):
            result = self.native_response(*args)
            result.cookies.pop("session")
            result.set_cookie("jenga_session", value, httponly=True)
            return result
        with patch("apps.koth.preview_game._native_request", side_effect=response):
            result = self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers())
        return result

    def test_native_login_permit_lasts_only_for_the_authorized_call(self):
        self.assertFalse(permits_token_verification(self.challenge, self.token))
        response = self.login()
        self.assertTrue(response.cookies[GAME_COOKIE]["secure"])
        self.assertTrue(response.cookies[GAME_COOKIE]["httponly"])
        self.assertFalse(permits_token_verification(self.challenge, self.token))
        self.assertEqual(self.authorize_session().status_code, 204)
        self.challenge.refresh_from_db()
        self.assertEqual(self.challenge.status, "SCHEDULED")
        verify = APIClient().post("/internal/koth/team_tokens/verify", {
            "koth_challenge_id": str(self.challenge.pk), "team_token": self.raw_token},
            format="json", HTTP_X_INTERNAL_TOKEN="preview-native-internal")
        self.assertIs(verify.data["data"]["valid"], False)

    def test_service_key_rotation_rejects_an_existing_native_session(self):
        self.login()
        self.assertEqual(self.authorize_session().status_code, 204)
        with override_settings(KOTH_TEAM_TOKEN_SECRET=secrets.token_urlsafe(48)):
            self.assertEqual(self.authorize_session().status_code, 401)

    def test_common_cross_problem_and_wrong_team_tokens_never_reach_native_login(self):
        KothTeamToken.objects.create(team=self.team, challenge=None, token_hash=hash_token("legacy-qa"))
        other = KothChallenge.objects.create(club=KothClub.objects.create(name="Other native QA"),
            title="Other", open_group=2, inbound_internal_token_hash=hash_token("other-native-internal"))
        KothTeamToken.objects.create(team=self.team, challenge=other, token_hash=hash_token("other-qa"))
        for raw in ["legacy-qa", "other-qa", "invalid"]:
            with self.subTest(raw=raw), patch("apps.koth.preview_game._native_request") as native:
                response = self.client.post(self.proxy, {"team_token": raw}, format="json", **self.headers())
                self.assertEqual(response.status_code, 401)
                native.assert_not_called()

    def test_ban_revokes_old_native_session_permanently(self):
        self.login()
        self.team.is_banned = True; self.team.save(update_fields=["is_banned"])
        self.assertEqual(self.authorize_session().status_code, 401)
        self.team.is_banned = False; self.team.save(update_fields=["is_banned"])
        self.assertEqual(self.authorize_session().status_code, 401)
        self.login()
        self.assertEqual(self.authorize_session().status_code, 204)

    def test_latest_jenga_cookie_is_bound_and_legacy_cookie_is_cleared(self):
        value = self.latest_cookie()
        def latest_response(*args):
            response = self.native_response(*args)
            response.cookies.pop("session")
            response.set_cookie("jenga_session", value, httponly=True)
            return response
        self.client.cookies["session"] = "stale-legacy-cookie"
        with patch("apps.koth.preview_game._native_request", side_effect=latest_response):
            response = self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.cookies["session"]["max-age"], 0)
        self.assertEqual(self.authorize_session().status_code, 204)

    def test_signed_cookie_refresh_and_out_of_order_responses_keep_the_same_authority(self):
        first = self.latest_cookie(timestamp=int(time.time()) - 2)
        second = self.latest_cookie()
        self.assertNotEqual(first, second)
        self.assertEqual(self.login_latest(first).status_code, 200)
        for value in [first, second, first, second]:
            self.client.cookies["jenga_session"] = value
            self.assertEqual(self.authorize_session().status_code, 204)

    def test_invalid_signature_revokes_the_binding_even_after_valid_cookie_is_restored(self):
        valid = self.latest_cookie()
        self.assertEqual(self.login_latest(valid).status_code, 200)
        self.client.cookies["jenga_session"] = self.latest_cookie(secret=secrets.token_urlsafe(40))
        self.assertEqual(self.authorize_session().status_code, 401)
        self.client.cookies["jenga_session"] = valid
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_validly_signed_wrong_team_match_generation_and_marker_are_rejected(self):
        changes = [{"team_id": str(uuid.uuid4())}, {"match_id": "other-match"}, {"generation": 2},
                   {"generation": True}, {"marker": "0" * 64}, {"_permanent": False}]
        for change in changes:
            with self.subTest(change=change):
                self.assertEqual(self.login_latest().status_code, 200)
                self.client.cookies["jenga_session"] = self.latest_cookie(**change)
                self.assertEqual(self.authorize_session().status_code, 401)

    def test_expired_native_signature_is_rejected_inside_a_fresh_preview(self):
        self.assertEqual(self.login_latest().status_code, 200)
        self.client.cookies["jenga_session"] = self.latest_cookie(timestamp=int(time.time()) - 43201)
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_an_existing_raw_cookie_hash_binding_requires_a_new_login(self):
        value = self.latest_cookie()
        result = self.login_latest(value)
        self.assertEqual(result.status_code, 200)
        key = _session_key(result.cookies[GAME_COOKIE].value)
        stored = cache.get(key);stored["native_hash"] = hash_token(value);cache.set(key, stored, 300)
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_signed_session_without_a_managed_signing_key_cannot_issue_a_binding(self):
        self.assertEqual(self.login_latest("untrusted-cookie-value").status_code, 502)
        self.assertNotIn(GAME_COOKIE, self.client.cookies)

    def test_signed_session_binding_still_checks_administrator_role_after_cookie_refresh(self):
        self.assertEqual(self.login_latest().status_code, 200)
        self.client.cookies["jenga_session"] = self.latest_cookie()
        self.admin.role = "PARTICIPANT";self.admin.save(update_fields=["role"])
        self.assertEqual(self.authorize_session().status_code, 401)
        self.admin.role = "ADMIN";self.admin.save(update_fields=["role"])
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_refreshed_signed_session_cannot_survive_token_revocation(self):
        self.assertEqual(self.login_latest().status_code, 200)
        self.client.cookies["jenga_session"] = self.latest_cookie()
        self.token.token_hash = hash_token("revoked-current-token");self.token.save(update_fields=["token_hash"])
        self.assertEqual(self.authorize_session().status_code, 401)
        self.token.token_hash = hash_token(self.raw_token);self.token.save(update_fields=["token_hash"])
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_mixed_jenga_cookie_names_revoke_the_bound_preview_session(self):
        self.login()
        self.client.cookies["jenga_session"] = "another-team-cookie"
        self.assertEqual(self.authorize_session().status_code, 401)
        del self.client.cookies["jenga_session"]
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_ambiguous_jenga_login_response_does_not_issue_a_preview_capability(self):
        def ambiguous_response(*args):
            response = self.native_response(*args)
            response.set_cookie("jenga_session", "another-team-cookie", httponly=True)
            return response
        with patch("apps.koth.preview_game._native_request", side_effect=ambiguous_response):
            response = self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers())
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(GAME_COOKIE, response.cookies)

    def test_token_revocation_does_not_revive_old_session_when_hash_is_restored(self):
        self.login()
        self.token.token_hash = hash_token("revoked-qa"); self.token.save(update_fields=["token_hash"])
        self.assertEqual(self.authorize_session().status_code, 401)
        self.token.token_hash = hash_token(self.raw_token); self.token.save(update_fields=["token_hash"])
        self.assertEqual(self.authorize_session().status_code, 401)
        self.login()
        self.assertEqual(self.authorize_session().status_code, 204)

    def test_issuer_demotion_revokes_native_session_even_after_role_restored(self):
        self.login()
        self.admin.role = "PARTICIPANT"; self.admin.save(update_fields=["role"])
        self.assertEqual(self.authorize_session().status_code, 401)
        self.admin.role = "ADMIN"; self.admin.save(update_fields=["role"])
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_expired_preview_and_replaced_ticket_reject_old_native_session(self):
        self.login()
        with patch("django.core.signing.time.time", return_value=time.time() + 302):
            self.assertEqual(self.authorize_session().status_code, 401)
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_native_credential_cannot_be_mixed_with_another_gate_session(self):
        self.login()
        self.client.cookies["session"] = "different-native-credential"
        self.assertEqual(self.authorize_session().status_code, 401)
        self.client.cookies["session"] = "native-session-credential"
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_legacy_native_cookie_without_gate_cookie_is_rejected(self):
        self.client.cookies["session"] = "old-native-session"
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_missing_tampered_and_cross_origin_preview_cannot_proxy_login(self):
        self.client.cookies[COOKIE_NAME] = "invalid"
        with patch("apps.koth.preview_game._native_request") as native:
            self.assertEqual(self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers()).status_code, 401)
            native.assert_not_called()

    def test_private_absolute_and_encoded_paths_are_never_forwarded(self):
        for uri in ["https://169.254.169.254/", "//other.example/", "/internal/koth/scores",
                    "/%252e%252e/internal/", "/%252finternal/koth/scores", "/__operator/preview"]:
            with self.subTest(uri=uri), patch("apps.koth.preview_game._native_request") as native:
                self.assertEqual(self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers(uri)).status_code, 401)
                native.assert_not_called()

    def test_native_failure_removes_the_temporary_permit(self):
        with patch("apps.koth.preview_game._native_request", side_effect=OSError("offline")):
            self.assertEqual(self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers()).status_code, 502)
        self.assertFalse(permits_token_verification(self.challenge, self.token))

    def test_busy_native_login_is_retriable_and_does_not_replace_permit(self):
        key = KEY_PREFIX + "permit:" + str(self.challenge.pk) + ":" + self.token.token_hash
        cache.set(key, {"owner": "another-request"}, 12)
        try:
            response = self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **self.headers())
            self.assertEqual(response.status_code, 409)
            self.assertEqual(cache.get(key)["owner"], "another-request")
        finally:
            cache.delete(key)

    def test_redis_failure_is_closed(self):
        self.login()
        with patch("apps.koth.preview_game.cache.get", side_effect=ConnectionError("offline")):
            self.assertEqual(self.authorize_session().status_code, 503)

    def test_old_native_credential_cannot_be_reused_with_new_cookie(self):
        first = self.login().cookies[GAME_COOKIE].value
        self.assertEqual(self.authorize_session().status_code, 204)
        cache.delete(_session_key(first))
        self.assertEqual(self.authorize_session().status_code, 401)
        self.login()
        second = self.client.cookies[GAME_COOKIE].value
        self.assertNotEqual(first, second)
        self.client.cookies[GAME_COOKIE] = first
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_pending_permit_cannot_outlive_the_preview(self):
        key = KEY_PREFIX + "permit:" + str(self.challenge.pk) + ":" + self.token.token_hash
        cache.set(key, {"challenge_id": str(self.challenge.pk), "token_hash": self.token.token_hash,
                        "admin_id": str(self.admin.pk), "expires_at": time.time() - 1}, 12)
        try:
            self.assertFalse(permits_token_verification(self.challenge, self.token))
        finally:
            cache.delete(key)

    def test_cross_origin_login_and_protected_write_are_rejected(self):
        headers = {**self.headers(), "HTTP_ORIGIN": "https://other.example"}
        with patch("apps.koth.preview_game._native_request") as native:
            self.assertEqual(self.client.post(self.proxy, {"team_token": self.raw_token}, format="json", **headers).status_code, 401)
            native.assert_not_called()
        self.login()
        headers = {**self.headers("/place"), "HTTP_ORIGIN": "https://other.example", "HTTP_X_KOTH_PREVIEW_METHOD": "POST"}
        self.assertEqual(self.client.get(self.authorize, **headers).status_code, 401)
        self.assertEqual(self.authorize_session().status_code, 204)

    def test_team_status_change_revokes_even_without_a_request_during_the_ban(self):
        from django.utils import timezone
        self.login()
        Team.objects.filter(pk=self.team.pk).update(is_banned=True, updated_at=timezone.now())
        Team.objects.filter(pk=self.team.pk).update(is_banned=False, updated_at=timezone.now())
        self.assertEqual(self.authorize_session().status_code, 401)

    def test_reissued_token_revokes_old_session_even_when_raw_value_is_the_same(self):
        from django.utils import timezone
        self.login()
        KothTeamToken.objects.filter(pk=self.token.pk).update(issued_at=timezone.now())
        self.assertEqual(self.authorize_session().status_code, 401)

    @override_settings(KOTH_ADMIN_PREVIEW_SECONDS=43200)
    def test_long_native_session_keeps_the_ticket_deadline_and_checks_current_admin(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer " + issue_access_token(self.admin))
        issued = self.client.post(f"/api/v1/admin/koth/challenges/{self.challenge.pk}/preview_sessions", {}, format="json")
        ticket = parse_qs(urlsplit(issued.data["data"]["preview_url"]).fragment)["ticket"][0]
        self.client.credentials()
        self.client.cookies[COOKIE_NAME] = ticket
        from django.core import signing

        issued_at = signing.b62_decode(ticket.rsplit(":", 2)[1])
        with patch("apps.koth.admin_preview.time.time", return_value=issued_at + 600):
            response = self.login()
            self.assertEqual(response.cookies[GAME_COOKIE]["max-age"], 42600)
            self.assertEqual(self.authorize_session().status_code, 204)
            self.admin.role = "PARTICIPANT"
            self.admin.save(update_fields=["role"])
            self.assertEqual(self.authorize_session().status_code, 401)

    @override_settings(KOTH_ADMIN_PREVIEW_SECONDS=43200)
    def test_native_login_does_not_extend_the_signed_ticket(self):
        # setUp issued a five-minute ticket before this method's longer configuration.
        from django.core import signing

        issued_at = signing.b62_decode(self.client.cookies[COOKIE_NAME].value.rsplit(":", 2)[1])
        with patch("apps.koth.admin_preview.time.time", return_value=issued_at + 250):
            response = self.login()
            self.assertEqual(response.cookies[GAME_COOKIE]["max-age"], 50)
        with patch("apps.koth.admin_preview.time.time", return_value=issued_at + 301):
            self.assertEqual(self.authorize_session().status_code, 401)


@override_settings(KOTH_ADMIN_PREVIEW_ORIGINS=["https://game.example"])
class AdminPreviewTokenTests(TestCase):
    """팀이 없는 관리자는 공개 전 게임 테스트에서만 쓰는 운영 테스트 토큰을 받는다."""

    def setUp(self):
        self.admin = User.objects.create_user(login_id="preview-token-admin", nickname="admin", role="ADMIN")
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Preview token QA"), title="Token QA",
            repository_slug="koth-jenga", challenge_url="https://game.example", status="SCHEDULED",
            open_group=1, inbound_internal_token_hash=hash_token("preview-token-internal"))
        self.api = self.bearer(self.admin)
        result = self.api.post(f"/api/v1/admin/koth/challenges/{self.challenge.pk}/preview_sessions", {}, format="json")
        self.assertEqual(result.status_code, 200)
        self.client = APIClient()
        self.client.cookies[COOKIE_NAME] = parse_qs(urlsplit(result.data["data"]["preview_url"]).fragment)["ticket"][0]
        self.proxy = "/api/v1/koth/preview_sessions/game_proxy"
        self.headers = {"HTTP_X_KOTH_PREVIEW_CHALLENGE": str(self.challenge.pk),
                        "HTTP_X_KOTH_PREVIEW_ORIGIN": "https://game.example", "HTTP_X_KOTH_PREVIEW_URI": "/auth",
                        "HTTP_X_KOTH_PREVIEW_METHOD": "GET", "HTTP_ORIGIN": "https://game.example"}

    def bearer(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer " + issue_access_token(user))
        return client

    def issue(self, client=None):
        return (client or self.api).get("/api/v1/koth/team_token", {"koth_challenge_id": str(self.challenge.pk)})

    def verify(self, raw):
        return APIClient().post("/internal/koth/team_tokens/verify", {
            "koth_challenge_id": str(self.challenge.pk), "team_token": raw},
            format="json", HTTP_X_INTERNAL_TOKEN="preview-token-internal").data["data"]

    def login(self, raw, seen=None):
        def native(request, challenge, uri, body):
            if seen is not None:
                seen.append(self.verify(raw))
            response = HttpResponse(json.dumps({"data": {}}), content_type="application/json")
            response.set_cookie("session", "preview-native-credential", httponly=True)
            return response
        with patch("apps.koth.preview_game._native_request", side_effect=native) as called:
            response = self.client.post(self.proxy, {"team_token": raw}, format="json", **self.headers)
        return response, called

    def test_admin_without_team_gets_a_preview_token_only_before_release(self):
        response = self.issue()
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertIs(data["preview"], True)
        self.assertEqual(data["team_name"], "운영 테스트")
        self.assertEqual(data["team_id"], str(preview_team_id(self.admin.pk)))
        self.assertTrue(data["team_token"].startswith("koth_"))
        self.assertIn("no-store", response["Cache-Control"])
        self.assertEqual(self.issue().data["data"]["team_token"], data["team_token"])
        # 실제 팀을 만들지 않아 순위표에 들어갈 팀이 생기지 않는다
        self.assertFalse(Team.objects.exists())
        self.assertEqual(KothPreviewToken.objects.get().token_hash, hash_token(data["team_token"]))

        for status in ["ACTIVE", "CLOSED"]:
            with self.subTest(status=status):
                KothChallenge.objects.filter(pk=self.challenge.pk).update(status=status)
                blocked = self.issue()
                self.assertEqual((blocked.status_code, blocked.data["code"]), (409, "KOTH_PREVIEW_TOKEN_UNAVAILABLE"))
        participant = User.objects.create_user(login_id="preview-token-player", nickname="player")
        self.assertEqual(self.issue(self.bearer(participant)).data["code"], "USER_HAS_NO_TEAM")

    def test_preview_token_passes_only_inside_the_game_test_login(self):
        raw = self.issue().data["data"]["team_token"]
        self.assertIs(self.verify(raw)["valid"], False)
        seen = []
        response, _ = self.login(raw, seen)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen, [{"valid": True, "team_id": str(preview_team_id(self.admin.pk)), "team_name": "운영 테스트",
                                 "koth_division": "EXTERNAL", "koth_challenge_id": str(self.challenge.pk)}])
        gate = {**self.headers, "HTTP_X_KOTH_PREVIEW_URI": "/state"}
        self.assertEqual(self.client.get("/api/v1/koth/preview_sessions/game_authorize", **gate).status_code, 204)
        self.assertIs(self.verify(raw)["valid"], False)

        # 진행 중에는 로그인 허가가 남아 있어도 운영 테스트 토큰을 받아 주지 않는다
        token = KothPreviewToken.objects.get()
        key = KEY_PREFIX + "permit:" + str(self.challenge.pk) + ":" + token.token_hash
        cache.set(key, {"challenge_id": str(self.challenge.pk), "token_hash": token.token_hash,
                        "admin_id": str(self.admin.pk), "expires_at": time.time() + 10}, 12)
        try:
            KothChallenge.objects.filter(pk=self.challenge.pk).update(status="ACTIVE")
            self.assertIs(self.verify(raw)["valid"], False)
        finally:
            cache.delete(key)

    def test_other_admin_preview_token_and_reissue_cannot_open_my_game_session(self):
        other = User.objects.create_user(login_id="preview-token-other", nickname="other", role="ADMIN")
        foreign = self.issue(self.bearer(other)).data["data"]["team_token"]
        response, called = self.login(foreign)
        self.assertEqual(response.status_code, 401)
        called.assert_not_called()

        response, _ = self.login(self.issue().data["data"]["team_token"])
        self.assertEqual(response.status_code, 200)
        gate = {**self.headers, "HTTP_X_KOTH_PREVIEW_URI": "/state"}
        self.assertEqual(self.client.get("/api/v1/koth/preview_sessions/game_authorize", **gate).status_code, 204)
        from django.utils import timezone
        KothPreviewToken.objects.filter(admin=self.admin).update(issued_at=timezone.now())
        self.assertEqual(self.client.get("/api/v1/koth/preview_sessions/game_authorize", **gate).status_code, 401)

    def test_direct_operator_login_is_enabled_only_until_the_explicit_qa_deadline(self):
        from datetime import timedelta
        from django.utils import timezone

        until = timezone.now() + timedelta(hours=2)
        KothChallenge.objects.filter(pk=self.challenge.pk).update(status="ACTIVE")
        with override_settings(KOTH_OPERATOR_QA_UNTIL=until.isoformat()):
            issued = self.issue()
            self.assertEqual(issued.status_code, 200)
            self.assertTrue(issued.data["data"]["qa_open"])
            raw = issued.data["data"]["team_token"]
            self.assertTrue(self.verify(raw)["valid"])
            self.assertFalse(Team.objects.exists())
            with patch("apps.koth.operator_qa.timezone.now", return_value=until):
                self.assertFalse(self.verify(raw)["valid"])
                self.assertEqual(self.issue().status_code, 409)
        self.assertFalse(self.verify(raw)["valid"])

    def test_qa_window_never_accepts_another_problem_token_or_removed_admin(self):
        from datetime import timedelta
        from django.utils import timezone

        raw = self.issue().data["data"]["team_token"]
        other = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Other operator QA"), title="Other QA", open_group=2,
            challenge_url="https://game.example", status="ACTIVE", inbound_internal_token_hash=hash_token("other-qa-internal"))
        with override_settings(KOTH_OPERATOR_QA_UNTIL=(timezone.now()+timedelta(hours=2)).isoformat()):
            KothChallenge.objects.filter(pk=self.challenge.pk).update(status="ACTIVE")
            self.assertTrue(self.verify(raw)["valid"])
            foreign = APIClient().post("/internal/koth/team_tokens/verify", {
                "koth_challenge_id": str(other.pk), "team_token": raw}, format="json", HTTP_X_INTERNAL_TOKEN="other-qa-internal")
            self.assertFalse(foreign.data["data"]["valid"])
            self.admin.role = "PARTICIPANT"
            self.admin.save(update_fields=["role"])
            self.assertFalse(self.verify(raw)["valid"])

    def test_invalid_qa_configuration_closed_status_and_unlisted_origin_remain_blocked(self):
        from datetime import timedelta
        from django.utils import timezone

        raw = self.issue().data["data"]["team_token"]
        KothChallenge.objects.filter(pk=self.challenge.pk).update(status="ACTIVE")
        for value in ("", "invalid", "2026-11-06T10:00:00", (timezone.now()-timedelta(seconds=1)).isoformat()):
            with self.subTest(value=value), override_settings(KOTH_OPERATOR_QA_UNTIL=value):
                self.assertEqual(self.issue().status_code, 409)
                self.assertFalse(self.verify(raw)["valid"])
        with override_settings(KOTH_OPERATOR_QA_UNTIL=(timezone.now()+timedelta(hours=2)).isoformat()):
            with override_settings(KOTH_ADMIN_PREVIEW_ORIGINS=[]):
                self.assertEqual(self.issue().status_code, 409)
                self.assertFalse(self.verify(raw)["valid"])
            KothChallenge.objects.filter(pk=self.challenge.pk).update(status="CLOSED")
            self.assertEqual(self.issue().status_code, 409)
            self.assertFalse(self.verify(raw)["valid"])
