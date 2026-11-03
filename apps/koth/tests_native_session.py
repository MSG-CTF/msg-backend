import hashlib
import json
import time
import uuid
import zlib
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from itsdangerous import TimestampSigner
from itsdangerous.encoding import base64_encode
from rest_framework.test import APIClient

from apps.accounts.models import Team
from .models import KothChallenge, KothClub, KothTeamToken, KothEnvironmentConfig, KothEnvironmentVersion
from .native_session import MAX_AGE, cookie_identity


class NativeJengaSessionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.secret = "jenga-session-test-key-" * 3
        self.team = Team.objects.create(team_name="native-jenga-team")
        self.challenge = KothChallenge.objects.create(club=KothClub.objects.create(name="Native Jenga"),
            title="Jenga", open_group=1, status="ACTIVE", repository_slug="koth-jenga", challenge_url="https://game.example")
        self.token = KothTeamToken.objects.create(team=self.team, challenge=self.challenge, token_hash="b"*64)
        KothTeamToken.objects.filter(pk=self.token.pk).update(issued_at=timezone.now()-timedelta(seconds=5))
        self.version = KothEnvironmentVersion.objects.create(challenge=self.challenge,version=1,source_revision="c"*40,
            encrypted_settings="test-only",request_id=uuid.uuid4(),request_fingerprint="d"*64,created_by="test")
        KothEnvironmentConfig.objects.create(challenge=self.challenge,applied_version=self.version,state="READY",target_id="jenga")
        self.decoder=patch("apps.koth.native_session.decode_settings",return_value={"containers":[{"name":"jenga","entries":{
            "SESSION_SECRET":{"kind":"secret","value":self.secret}}}]})
        self.decoder.start();self.addCleanup(self.decoder.stop)
        self.client=APIClient();self.client.cookies["session"]=self.cookie()
        self.path="/api/v1/koth/native_sessions/jenga_authorize"

    def cookie(self, data=None, seconds=None, compressed=False):
        data=data or {"team_id":str(self.team.pk),"team_name":"native-jenga-team"}
        raw=json.dumps(data,separators=(",", ":")).encode()
        body=(b"."+base64_encode(zlib.compress(raw))) if compressed else base64_encode(raw)
        signer=TimestampSigner(self.secret,salt="cookie-session",key_derivation="hmac",digest_method=hashlib.sha1)
        with patch.object(signer,"get_timestamp",return_value=int(time.time()) if seconds is None else seconds):
            return signer.sign(body).decode()

    def check(self, **headers):
        args={"HTTP_X_KOTH_CHALLENGE":str(self.challenge.pk),"HTTP_X_KOTH_ORIGIN":"https://game.example"}
        args.update(headers);return self.client.get(self.path,**args)

    def test_original_plain_and_compressed_flask_cookies_are_accepted(self):
        for compressed in (False,True):
            self.client.cookies["session"]=self.cookie(compressed=compressed)
            response=self.check();self.assertEqual(response.status_code,204)
            self.assertIn("no-store",response["Cache-Control"])
            self.assertFalse(response.cookies)

    def test_ban_revokes_existing_session_even_after_unban(self):
        self.assertEqual(self.check().status_code,204)
        self.team.is_banned=True;self.team.save(update_fields=["is_banned"])
        self.assertEqual(self.check().status_code,401)
        self.team.is_banned=False;self.team.save(update_fields=["is_banned"])
        self.assertEqual(self.check().status_code,401)
        self.client.cookies["session"]=self.cookie(seconds=int(time.time())+1)
        with patch("itsdangerous.timed.time.time",return_value=time.time()+1):
            self.assertEqual(self.check().status_code,204)

    def test_revoked_or_rotated_token_cannot_keep_an_existing_cookie(self):
        self.assertEqual(self.check().status_code,204)
        self.token.token_hash="e"*64;self.token.save(update_fields=["token_hash"])
        self.assertEqual(self.check().status_code,401)
        self.token.token_hash="b"*64;self.token.save(update_fields=["token_hash"])
        self.assertEqual(self.check().status_code,401)

    def test_legacy_cookie_before_token_issue_and_common_token_are_denied(self):
        self.client.cookies["session"]=self.cookie(seconds=int(time.time())-20)
        self.assertEqual(self.check().status_code,401)
        self.token.delete()
        KothTeamToken.objects.create(team=self.team,challenge=None,token_hash="f"*64)
        self.client.cookies["session"]=self.cookie()
        self.assertEqual(self.check().status_code,401)

    def test_wrong_challenge_origin_signature_and_missing_cookie_are_denied(self):
        self.assertEqual(self.check(HTTP_X_KOTH_CHALLENGE="not-a-uuid").status_code,401)
        self.assertEqual(self.check(HTTP_X_KOTH_ORIGIN="https://other.example").status_code,401)
        self.client.cookies["session"]=self.cookie()[:-1]+"!"
        self.assertEqual(self.check().status_code,401)
        self.client.cookies.clear();self.assertEqual(self.check().status_code,401)

    def test_cross_site_writes_and_unreleased_challenge_are_denied(self):
        self.assertEqual(self.check(HTTP_X_KOTH_REQUEST_METHOD="POST",HTTP_ORIGIN="https://other.example").status_code,401)
        self.assertEqual(self.check(HTTP_X_KOTH_REQUEST_METHOD="POST",HTTP_ORIGIN="https://game.example").status_code,204)
        self.challenge.status="SCHEDULED";self.challenge.save(update_fields=["status"])
        self.assertEqual(self.check().status_code,401)

    def test_signed_expired_future_and_oversized_payloads_are_denied(self):
        for raw in [self.cookie(seconds=int(time.time())-MAX_AGE-2),self.cookie(seconds=int(time.time())+120),
                    self.cookie({"team_id":str(self.team.pk),"team_name":"x"*100000},compressed=True),"x"*4097]:
            self.client.cookies["session"]=raw
            self.assertEqual(self.check().status_code,401)

    def test_cache_or_missing_applied_secret_fails_closed(self):
        with patch("apps.koth.native_session.cache.get",side_effect=ConnectionError()):
            self.assertEqual(self.check().status_code,503)
        KothEnvironmentConfig.objects.filter(challenge=self.challenge).update(applied_version=None)
        self.assertEqual(self.check().status_code,503)

    def test_75_cookie_checks_keep_the_original_team_identity(self):
        cookies=[self.cookie({"team_id":str(self.team.pk),"team_name":"team-"+str(i)},compressed=True) for i in range(75)]
        with ThreadPoolExecutor(max_workers=75) as pool:rows=list(pool.map(lambda raw:cookie_identity(raw,self.secret),cookies))
        self.assertEqual(len(rows),75)
        self.assertTrue(all(tid==str(self.team.pk) for tid,issued in rows))
