import json
import time
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.core.cache import cache
from django.http import HttpResponse
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import Team, User
from apps.common.jwt import hash_token, issue_access_token
from .admin_preview import COOKIE_NAME
from .models import KothChallenge, KothClub, KothPreviewToken, KothTeamToken
from .preview_game import GAME_COOKIE, KEY_PREFIX, _session_key, permits_token_verification
from .tokens import preview_team_id


@override_settings(KOTH_ADMIN_PREVIEW_ORIGINS=["https://game.example"])
class PreviewGameTests(TestCase):
    def setUp(self):
        self.team = Team.objects.create(team_name="preview-game-team")
        self.admin = User.objects.create_user(login_id="preview-game-admin", nickname="admin", role="ADMIN", team=self.team)
        self.challenge = KothChallenge.objects.create(
            club=KothClub.objects.create(name="Preview native QA"), title="Native QA",
            repository_slug="koth-jenga", challenge_url="https://game.example", status="SCHEDULED",
            open_group=1, inbound_internal_token_hash=hash_token("preview-native-internal"))
        self.token = KothTeamToken.objects.create(team=self.team, challenge=self.challenge, token_hash=hash_token("native-qa-token"))
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
            "koth_challenge_id": str(challenge.pk), "team_token": "native-qa-token"},
            format="json", HTTP_X_INTERNAL_TOKEN="preview-native-internal")
        self.assertEqual(native.status_code, 200)
        self.assertIs(native.data["data"]["valid"], True)
        response = HttpResponse(json.dumps({"data": {"team_id": str(self.team.pk)}}), content_type="application/json")
        response.set_cookie("session", "native-session-credential", httponly=True)
        return response

    def login(self):
        with patch("apps.koth.preview_game._native_request", side_effect=self.native_response):
            response = self.client.post(self.proxy, {"team_token": "native-qa-token"}, format="json", **self.headers())
        self.assertEqual(response.status_code, 200)
        return response

    def authorize_session(self):
        return self.client.get(self.authorize, **self.headers("/state"))

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
            "koth_challenge_id": str(self.challenge.pk), "team_token": "native-qa-token"},
            format="json", HTTP_X_INTERNAL_TOKEN="preview-native-internal")
        self.assertIs(verify.data["data"]["valid"], False)

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

    def test_token_revocation_does_not_revive_old_session_when_hash_is_restored(self):
        self.login()
        self.token.token_hash = hash_token("revoked-qa"); self.token.save(update_fields=["token_hash"])
        self.assertEqual(self.authorize_session().status_code, 401)
        self.token.token_hash = hash_token("native-qa-token"); self.token.save(update_fields=["token_hash"])
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
            self.assertEqual(self.client.post(self.proxy, {"team_token": "native-qa-token"}, format="json", **self.headers()).status_code, 401)
            native.assert_not_called()

    def test_private_absolute_and_encoded_paths_are_never_forwarded(self):
        for uri in ["https://169.254.169.254/", "//other.example/", "/internal/koth/scores",
                    "/%252e%252e/internal/", "/%252finternal/koth/scores", "/__operator/preview"]:
            with self.subTest(uri=uri), patch("apps.koth.preview_game._native_request") as native:
                self.assertEqual(self.client.post(self.proxy, {"team_token": "native-qa-token"}, format="json", **self.headers(uri)).status_code, 401)
                native.assert_not_called()

    def test_native_failure_removes_the_temporary_permit(self):
        with patch("apps.koth.preview_game._native_request", side_effect=OSError("offline")):
            self.assertEqual(self.client.post(self.proxy, {"team_token": "native-qa-token"}, format="json", **self.headers()).status_code, 502)
        self.assertFalse(permits_token_verification(self.challenge, self.token))

    def test_busy_native_login_is_retriable_and_does_not_replace_permit(self):
        key = KEY_PREFIX + "permit:" + str(self.challenge.pk) + ":" + self.token.token_hash
        cache.set(key, {"owner": "another-request"}, 12)
        try:
            response = self.client.post(self.proxy, {"team_token": "native-qa-token"}, format="json", **self.headers())
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
            self.assertEqual(self.client.post(self.proxy, {"team_token": "native-qa-token"}, format="json", **headers).status_code, 401)
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
